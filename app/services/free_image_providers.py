"""Дополнительные генераторы картинок, настраиваемые из панели: бесплатные уровни и любые совместимые API.

Адаптеры: `cloudflare` (Workers AI), `huggingface` (Inference Providers), `pollinations` (без ключа), `horde` (AI Horde,
общий бесплатный пул), `openai_compatible` (любой сервис с `POST {base}/images/generations`, например Together или SiliconFlow).
Параметры сервисов меняются, поэтому адрес, модель и размеры задаются в настройках, а не в коде. Ключи в исключения и
журнал не попадают; адреса проверяются (HTTPS, не локальные сети), перенаправления не выполняются.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.services.chat_sources import safe_image_url
from app.services.image_providers import GenerationError, check_status, normalize_download

ADAPTERS = ('cloudflare', 'huggingface', 'pollinations', 'horde', 'openai_compatible')
MAX_DOWNLOAD = 20 * 1024 * 1024
ANON_HORDE_KEY = '0000000000'
DEFAULT_URLS = {
    'cloudflare': 'https://api.cloudflare.com/client/v4',
    'huggingface': 'https://router.huggingface.co/hf-inference/models',
    'pollinations': 'https://image.pollinations.ai',
    'horde': 'https://aihorde.net/api/v2',
}
NEEDS_KEY = {'cloudflare': True, 'huggingface': True, 'pollinations': False, 'horde': False, 'openai_compatible': True}

# Шаблоны для быстрого добавления. Условия бесплатного тарифа меняются — перед использованием сверьтесь с сайтом сервиса.
TEMPLATES = [
    dict(id='cloudflare-flux', howto=['Зарегистрируйтесь на dash.cloudflare.com (банковская карта не нужна).', 'Скопируйте Account ID: он виден на главной странице аккаунта справа и в адресе кабинета (32 символа из цифр и букв a–f).', 'Откройте My Profile → API Tokens → Create Token и выберите шаблон «Workers AI» (либо права Workers AI: Read и Edit). Скопируйте токен — он показывается один раз.', 'Вставьте Account ID и токен в форму ниже и нажмите «Добавить», затем «Проверить».'], label='Cloudflare Workers AI · FLUX.1 schnell', adapter='cloudflare', model='@cf/black-forest-labs/flux-1-schnell',
         needs_key=True, needs_account=True, steps=4, width=1024, height=768, signup='https://dash.cloudflare.com/profile/api-tokens',
         free='Бесплатная суточная квота аккаунта (без карты). Нужны Account ID и API-токен с правом Workers AI.'),
    dict(id='cloudflare-sdxl', howto=['Шаги те же, что для Cloudflare FLUX: нужны Account ID и токен с правом Workers AI.', 'Эта модель отдаёт картинку напрямую, поле «Шагов» можно оставить 4.', 'Добавьте генератор и нажмите «Проверить».'], label='Cloudflare Workers AI · SDXL Lightning', adapter='cloudflare', model='@cf/bytedance/stable-diffusion-xl-lightning',
         needs_key=True, needs_account=True, steps=4, width=1024, height=768, signup='https://dash.cloudflare.com/profile/api-tokens',
         free='Та же бесплатная квота Workers AI. Отдаёт картинку напрямую.'),
    dict(id='huggingface-flux', howto=['Зарегистрируйтесь на huggingface.co и откройте Settings → Access Tokens.', 'Создайте токен (New token) с правом вызова Inference Providers (подойдёт fine-grained с доступом «Make calls to Inference Providers» или токен Read).', 'Вставьте токен, добавьте генератор и нажмите «Проверить». Ежемесячный бесплатный кредит ограничен: когда он кончится, сервис ответит ошибкой квоты и будет пропущен на сутки.'], label='Hugging Face · FLUX.1 schnell', adapter='huggingface', model='black-forest-labs/FLUX.1-schnell',
         needs_key=True, needs_account=False, width=1216, height=832, signup='https://huggingface.co/settings/tokens',
         free='Ежемесячный бесплатный кредит аккаунта; токен с правом Inference.'),
    dict(id='pollinations', howto=['Ключ не нужен: просто нажмите «Добавить».', 'Анонимный доступ ограничен примерно одним запросом в 15 секунд и может добавлять водяной знак — поставьте небольшой дневной лимит и более поздний приоритет, чем у других.', 'Нажмите «Проверить».'], label='Pollinations · FLUX (без ключа)', adapter='pollinations', model='flux',
         needs_key=False, needs_account=False, width=1216, height=832, signup='https://pollinations.ai',
         free='Работает без ключа, анонимно примерно 1 запрос в 15 секунд. Возможен водяной знак.'),
    dict(id='horde', howto=['Ключ не нужен: сервис принимает анонимный ключ 0000000000. Для более быстрой очереди можно зарегистрироваться на aihorde.net и вставить свой бесплатный ключ.', 'Картинка создаётся на чужих видеокартах, ожидание от десятков секунд до нескольких минут — ставьте этот генератор последним по приоритету.', 'Нажмите «Добавить», затем «Проверить» (проверка может занять до 3 минут).'], label='AI Horde · общий бесплатный пул', adapter='horde', model='',
         needs_key=False, needs_account=False, width=768, height=512, signup='https://aihorde.net/register',
         free='Анонимно (ключ 0000000000) или с бесплатным ключом. Очередь волонтёрских GPU: от десятков секунд до минут.'),
    dict(id='together', howto=['Зарегистрируйтесь на api.together.ai и создайте ключ в Settings → API Keys.', 'Бесплатная модель FLUX.1-schnell-Free была временной акцией и может быть недоступна. Если «Проверить» вернёт ошибку модели, замените её на другую доступную модель этого сервиса.', 'Вставьте ключ, добавьте генератор, нажмите «Проверить».'], label='Together AI · FLUX.1 schnell Free', adapter='openai_compatible', base_url='https://api.together.xyz/v1',
         model='black-forest-labs/FLUX.1-schnell-Free', needs_key=True, needs_account=False, width=1024, height=768,
         signup='https://api.together.ai/settings/api-keys', free='Бесплатная модель была временной акцией: проверьте наличие.'),
    dict(id='siliconflow', howto=['Зарегистрируйтесь на siliconflow.com и создайте ключ API.', 'Проверьте в их кабинете, какие модели бесплатны для вашего аккаунта, и при необходимости измените поле «Модель».', 'Вставьте ключ, добавьте генератор, нажмите «Проверить».'], label='SiliconFlow · FLUX.1 schnell', adapter='openai_compatible', base_url='https://api.siliconflow.com/v1',
         model='black-forest-labs/FLUX.1-schnell', needs_key=True, needs_account=False, width=1024, height=768,
         signup='https://cloud.siliconflow.com/account/ak', free='Есть бесплатные модели/кредиты для новых аккаунтов: проверьте актуальность.'),
    dict(id='custom', howto=['Узнайте в документации сервиса адрес API (обычно оканчивается на /v1), название модели и способ получения ключа.', 'Сервис должен поддерживать запрос POST {адрес}/images/generations и ответ с полем data[0].b64_json или data[0].url.', 'Заполните адрес, модель и ключ, добавьте генератор и нажмите «Проверить». Если сервис не понимает response_format, добавьте в поле extra {"no_response_format": true} через REST.'], label='Другой сервис (OpenAI-совместимый API)', adapter='openai_compatible', base_url='', model='',
         needs_key=True, needs_account=False, width=1024, height=768, signup='', free='Любой сервис с POST {адрес}/images/generations.'),
]


def safe_base(url: str) -> str:
    """Проверить адрес сервиса: HTTPS, не локальная сеть, без логина; возвращает адрес без завершающего слэша."""
    if not url or not safe_image_url(url):
        raise ValueError('Unsafe service address')
    return url.rstrip('/')


def validate_spec(spec: dict) -> None:
    """Проверить настройки провайдера перед сохранением (ValueError с кратким кодом)."""
    adapter = spec.get('adapter')
    if adapter not in ADAPTERS:
        raise ValueError('adapter')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,29}', str(spec.get('slug', ''))):
        raise ValueError('slug')
    base = spec.get('base_url') or ''
    if base:
        safe_base(base)
    elif adapter == 'openai_compatible':
        raise ValueError('base_url')
    if adapter in {'cloudflare', 'huggingface', 'openai_compatible'} and not spec.get('model'):
        raise ValueError('model')
    if adapter == 'cloudflare' and not re.fullmatch(r'[0-9a-f]{32}', str(spec.get('account_id', ''))):
        raise ValueError('account_id')


def _error_for(response: httpx.Response) -> None:
    """Общий разбор HTTP-ответа бесплатных сервисов: 429 — повтор позже, 401/403 — ключ, 5xx — временный сбой."""
    if response.status_code == 503:
        raise GenerationError('server_busy', retry=True, retry_after=60)
    check_status(response)


def _image_from(content: bytes, content_type: str) -> bytes:
    """Картинка из ответа: бинарная или JSON с base64 (поля `result.image`, `image`, `data[0].b64_json`, `images[0]`)."""
    if content_type.startswith('image/') or content[:4] in {b'\x89PNG', b'RIFF'} or content[:3] == b'\xff\xd8\xff':
        return content
    try:
        body = json.loads(content)
    except ValueError:
        raise GenerationError('response_invalid') from None
    candidates: list[Any] = []
    if isinstance(body, dict):
        result = body.get('result')
        candidates += [result.get('image') if isinstance(result, dict) else None, body.get('image')]
        data = body.get('data')
        if isinstance(data, list) and data and isinstance(data[0], dict):
            candidates.append(data[0].get('b64_json'))
        images = body.get('images')
        if isinstance(images, list) and images:
            candidates.append(images[0] if isinstance(images[0], str) else None)
    for value in candidates:
        if isinstance(value, str) and value and not value.startswith('http'):
            try:
                return base64.b64decode(value.split(',', 1)[-1], validate=False)
            except ValueError:
                continue
    raise GenerationError('no_image')


async def _fetch_url(client: httpx.AsyncClient, url: str) -> bytes:
    """Скачать результат по ссылке сервиса: HTTPS, не локальная сеть, без ключа и перенаправлений, до 20 МБ."""
    if not safe_image_url(url):
        raise GenerationError('response_invalid')
    async with client.stream('GET', url, follow_redirects=False) as response:
        if response.status_code != 200:
            raise GenerationError('download_failed', retry=True)
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise GenerationError('response_invalid')
            chunks.append(chunk)
    return b''.join(chunks)


async def _cloudflare(spec: dict, prompt: str, client: httpx.AsyncClient) -> bytes:
    """Workers AI: `POST /accounts/{id}/ai/run/{model}` с Bearer-токеном; промпт не длиннее 2048 символов."""
    base = safe_base(spec.get('base_url') or DEFAULT_URLS['cloudflare'])
    body: dict[str, Any] = {'prompt': prompt[:2048]}
    if spec.get('steps'):
        body['steps'] = spec['steps']
    if (spec.get('extra') or {}).get('send_size'):
        body.update(width=spec['width'], height=spec['height'])
    response = await client.post(f"{base}/accounts/{spec['account_id']}/ai/run/{spec['model']}",
                                 headers={'Authorization': 'Bearer ' + spec['api_key']}, json=body)
    _error_for(response)
    return _image_from(response.content, response.headers.get('content-type', ''))


async def _huggingface(spec: dict, prompt: str, client: httpx.AsyncClient) -> bytes:
    """Hugging Face Inference: `POST {base}/{model}` с `{"inputs": prompt}`; ответ — картинка."""
    base = safe_base(spec.get('base_url') or DEFAULT_URLS['huggingface'])
    body: dict[str, Any] = {'inputs': prompt[:4000], 'parameters': {'width': spec['width'], 'height': spec['height']}}
    if spec.get('steps'):
        body['parameters']['num_inference_steps'] = spec['steps']
    response = await client.post(f"{base}/{spec['model']}", headers={'Authorization': 'Bearer ' + spec['api_key'], 'Accept': 'image/*'}, json=body)
    _error_for(response)
    return _image_from(response.content, response.headers.get('content-type', ''))


async def _pollinations(spec: dict, prompt: str, client: httpx.AsyncClient) -> bytes:
    """Pollinations: `GET {base}/prompt/{prompt}?width&height&model`; ключ необязателен (Bearer повышает лимиты)."""
    base = safe_base(spec.get('base_url') or DEFAULT_URLS['pollinations'])
    headers = {'Authorization': 'Bearer ' + spec['api_key']} if spec.get('api_key') else {}
    params = {'width': spec['width'], 'height': spec['height'], 'model': spec.get('model') or 'flux', 'nologo': 'true'}
    response = await client.get(f"{base}/prompt/{quote(prompt[:1500], safe='')}", params=params, headers=headers)
    _error_for(response)
    return _image_from(response.content, response.headers.get('content-type', ''))


async def _horde(spec: dict, prompt: str, client: httpx.AsyncClient) -> bytes:
    """AI Horde: поставить задачу, опрашивать готовность до таймаута (по умолчанию 200 с), забрать результат; при таймауте задача отменяется."""
    base = safe_base(spec.get('base_url') or DEFAULT_URLS['horde'])
    headers = {'apikey': spec.get('api_key') or ANON_HORDE_KEY, 'Client-Agent': 'BibleMessengerBot:1.0:admin'}
    params: dict[str, Any] = {'width': spec['width'], 'height': spec['height'], 'n': 1, 'steps': spec.get('steps') or 25}
    body: dict[str, Any] = {'prompt': prompt[:1000], 'params': params, 'nsfw': False, 'censor_nsfw': True, 'r2': True}
    if spec.get('model'):
        body['models'] = [spec['model']]
    response = await client.post(f'{base}/generate/async', headers=headers, json=body)
    _error_for(response)
    try:
        task = str(response.json()['id'])
    except (ValueError, KeyError, TypeError):
        raise GenerationError('response_invalid') from None
    if not re.fullmatch(r'[0-9a-fA-F-]{8,64}', task):
        raise GenerationError('response_invalid')
    deadline = time.monotonic() + float((spec.get('extra') or {}).get('timeout', 200))
    while time.monotonic() < deadline:
        await asyncio.sleep(4)
        check = await client.get(f'{base}/generate/check/{task}', headers=headers)
        if check.status_code != 200:
            continue
        state = check.json()
        if state.get('faulted'):
            raise GenerationError('remote_failed', retry=True)
        if state.get('done'):
            status = await client.get(f'{base}/generate/status/{task}', headers=headers)
            _error_for(status)
            generations = status.json().get('generations') or []
            if not generations or not generations[0].get('img'):
                raise GenerationError('no_image')
            if generations[0].get('censored'):
                raise GenerationError('moderation')
            image = str(generations[0]['img'])
            return await _fetch_url(client, image) if image.startswith('http') else base64.b64decode(image)
    try:
        await client.delete(f'{base}/generate/status/{task}', headers=headers)
    except httpx.HTTPError:
        pass
    raise GenerationError('remote_timeout', retry=True, retry_after=120)


async def _openai_compatible(spec: dict, prompt: str, client: httpx.AsyncClient) -> bytes:
    """`POST {base}/images/generations`: ответ `data[0].b64_json` либо ссылка `data[0].url` / `images[0].url`."""
    base = safe_base(spec['base_url'])
    body = {'model': spec['model'], 'prompt': prompt[:4000], 'n': 1, 'size': f"{spec['width']}x{spec['height']}"}
    if not (spec.get('extra') or {}).get('no_response_format'):
        body['response_format'] = 'b64_json'
    response = await client.post(f'{base}/images/generations', headers={'Authorization': 'Bearer ' + spec['api_key']}, json=body)
    _error_for(response)
    try:
        payload = response.json()
        items = payload.get('data') or payload.get('images') or []
        item = items[0] if items else {}
    except (ValueError, AttributeError, IndexError):
        raise GenerationError('response_invalid') from None
    if isinstance(item, dict) and item.get('url'):
        return await _fetch_url(client, str(item['url']))
    return _image_from(response.content, 'application/json')


_RUNNERS = {'cloudflare': _cloudflare, 'huggingface': _huggingface, 'pollinations': _pollinations,
            'horde': _horde, 'openai_compatible': _openai_compatible}


async def generate(spec: dict, prompt: str, *, client: httpx.AsyncClient | None = None) -> tuple[bytes, dict, str | None]:
    """Получить картинку у настроенного сервиса. Возвращает (картинка, сведения, идентификатор запроса).

    Любые сбои превращаются в `GenerationError`; платной отправки здесь нет, поэтому «неопределённых» исходов нет."""
    if NEEDS_KEY.get(spec['adapter']) and not spec.get('api_key'):
        raise GenerationError('missing_key')
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(240, connect=15), follow_redirects=False)
    try:
        try:
            data = await _RUNNERS[spec['adapter']](spec, prompt, client)
            data = normalize_download(data)
        except httpx.TransportError as error:
            raise GenerationError('transport_error', retry=True, retry_after=60) from error
        except (ValueError, KeyError, TypeError) as error:
            raise GenerationError('response_invalid') from error
        return data, {'adapter': spec['adapter'], 'model': spec.get('model', '')}, None
    finally:
        if owned:
            await client.aclose()


def host_of(spec: dict) -> str:
    """Имя хоста сервиса (для отображения в панели, без ключей и путей)."""
    return urlsplit(spec.get('base_url') or DEFAULT_URLS.get(spec['adapter'], '')).hostname or ''
