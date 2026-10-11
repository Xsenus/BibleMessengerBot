"""Клиент платёжного API ЮKassa: фиксированный хост, авторизация магазина и явные постоянные ключи идемпотентности."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class PaymentError(Exception):
    """Обезличенная ошибка платежа с кодом; в неё никогда не попадают данные авторизации, клиента или тело ответа провайдера."""

    def __init__(self, code: str):
        """Запомнить код ошибки и использовать его же как текст."""
        self.code = code
        super().__init__(code)


def https_url(value: str) -> bool:
    """Проверить, что строка — безопасный HTTPS-адрес (порт 443, без логина и пароля, до 2048 символов)."""
    if not isinstance(value,str):
        return False
    try:
        url = urlsplit(value)
        return bool(url.scheme == 'https' and url.hostname and not url.username and
                    not url.password and url.port in (None, 443) and len(value) <= 2048)
    except ValueError:
        return False


def identifier(value: str) -> str:
    """Привести значение к каноническому UUID-строке; иначе PaymentError('invalid_identifier')."""
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise PaymentError('invalid_identifier') from None


def minor_amount(value: dict) -> int:
    """Перевести сумму из ответа провайдера (`{'value': '100.00', 'currency': 'RUB'}`) в копейки; допустимы только положительные рубли с целым числом копеек до 10000 ₽."""
    try:
        if not isinstance(value,dict) or not isinstance(value.get('value'),str):
            raise ValueError
        number = Decimal(value['value'])
        if value['currency'] != 'RUB' or not number.is_finite() or number <= 0:
            raise ValueError
        cents = number * 100
        if cents != cents.to_integral_value() or cents > 1000000:
            raise ValueError
        return int(cents)
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise PaymentError('invalid_amount') from None


@dataclass(frozen=True, slots=True)
class MerchantSettings:
    """Настройки магазина (идентификатор, секретный ключ, адрес возврата, контакт поддержки, флаг включения). Секретный ключ скрыт из repr."""
    shop_id: str = ''
    secret_key: str = field(default='', repr=False)
    return_url: str = ''
    support_contact: str = ''
    active: bool = True

    @property
    def enabled(self):
        """Истина, если платежи включены и заданы все обязательные значения."""
        return bool(self.active and self.shop_id and self.secret_key and self.return_url and self.support_contact)

    @classmethod
    def from_env(cls):
        """Прочитать настройки из переменных окружения YOOKASSA_* и проверить их формат (числовой идентификатор магазина, HTTPS-адрес возврата, длина контакта)."""
        result = cls(*(os.getenv(key, '').strip() for key in
                       ('YOOKASSA_SHOP_ID', 'YOOKASSA_SECRET_KEY', 'YOOKASSA_RETURN_URL', 'PAYMENT_SUPPORT_CONTACT')),
                     active=os.getenv('YOOKASSA_ENABLED','true').strip().lower() in {'true','1','yes'})
        if result.shop_id and (not result.shop_id.isascii() or not result.shop_id.isdigit()):
            raise ValueError('YOOKASSA_SHOP_ID must be numeric')
        if result.return_url and not https_url(result.return_url):
            raise ValueError('YOOKASSA_RETURN_URL must be HTTPS on port 443')
        if len(result.support_contact) > 300 or '\x00' in result.support_contact:
            raise ValueError('Invalid PAYMENT_SUPPORT_CONTACT')
        return result


def merchant_available():
    """Безопасная проверка «платежи доступны»: при неверной конфигурации возвращает False, чтобы не ломать чтение и навигацию."""
    try:
        return MerchantSettings.from_env().enabled
    except ValueError:
        return False


class YooKassa:
    """Асинхронный клиент API ЮKassa (https://api.yookassa.ru/v3/) без перенаправлений."""
    def __init__(self, settings: MerchantSettings, *, transport=None):
        """Создать HTTP-клиент с авторизацией магазина и тайм-аутами; отказывается работать, если магазин не настроен."""
        if not settings.enabled:
            raise PaymentError('merchant_not_configured')
        self.settings = settings
        self.client = httpx.AsyncClient(base_url='https://api.yookassa.ru/v3/',
                                        auth=(settings.shop_id, settings.secret_key),
                                        timeout=httpx.Timeout(25, connect=10),
                                        follow_redirects=False, transport=transport)

    async def close(self):
        """Закрыть HTTP-клиент."""
        await self.client.aclose()

    async def request(self, method, path, *, body=None, key=None):
        """Выполнить запрос к API: относительный путь, для POST обязателен ключ идемпотентности. Сетевые сбои и ответы 5xx/429 превращаются в `provider_unavailable`, прочие отказы — в `provider_rejected`, некорректный JSON — в `invalid_response`."""
        if path.startswith(('/', 'http')) or '..' in path:
            raise PaymentError('invalid_path')
        headers = {}
        if method == 'POST':
            headers['Idempotence-Key'] = identifier(key)
        try:
            response = await self.client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError:
            raise PaymentError('provider_unavailable') from None
        if response.status_code not in (200, 201):
            raise PaymentError('provider_unavailable' if response.status_code >= 500 or response.status_code == 429
                               else 'provider_rejected')
        try:
            result = response.json()
        except ValueError:
            raise PaymentError('invalid_response') from None
        if not isinstance(result, dict):
            raise PaymentError('invalid_response')
        return result

    async def payment(self, payment_id):
        """Получить платёж по идентификатору (авторизованный GET)."""
        return await self.request('GET', 'payments/' + identifier(payment_id))

    async def refund(self, refund_id):
        """Получить возврат по идентификатору (авторизованный GET)."""
        return await self.request('GET', 'refunds/' + identifier(refund_id))

    async def create(self, order):
        """Создать платёж по заказу: сумма, способ (карта/СБП), перенаправление на оплату, описание и метаданные (приложение, номер заказа); повтор с тем же ключом не создаёт второй платёж."""
        amount = {'value': f"{order['amount_minor'] // 100}.{order['amount_minor'] % 100:02d}", 'currency': 'RUB'}
        return await self.request('POST', 'payments', key=str(order['idempotency_key']), body={
            'amount': amount, 'capture': True, 'save_payment_method': False,
            'payment_method_data': {'type': order['method']},
            'confirmation': {'type': 'redirect', 'return_url': self.settings.return_url},
            'description': 'Добровольная разовая поддержка «Библия каждый день»',
            'metadata': {'application': 'bible-messenger-' + order.get('platform','max'), 'order_id': str(order['id'])},
        })

    async def create_refund(self, order, refund):
        """Создать полный возврат по платежу заказа; ключ идемпотентности хранится вместе с возвратом."""
        return await self.request('POST', 'refunds', key=str(refund['idempotency_key']), body={
            'payment_id': identifier(order['provider_payment_id']),
            'amount': {'value': f"{order['amount_minor'] // 100}.{order['amount_minor'] % 100:02d}", 'currency': 'RUB'},
            'description': 'Возврат добровольной поддержки',
        })
