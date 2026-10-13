"""Журналирование контейнеров: текстовый или JSON-формат, секреты вырезаются из сообщений и трассировок."""
from __future__ import annotations
import json
import logging
import os
import re
import sys


def redact(value: str) -> str:
    """Заменить в строке все секреты на [REDACTED]: токены ботов, ключи API и администратора, пароли БД, ключи пула картинок и строки вида `postgres://user:пароль@`."""
    for key in ('BOT_TOKEN','MAX_BOT_TOKEN','MAX_WEBHOOK_SECRET','YOOKASSA_SECRET_KEY','OWNER_CLAIM_CODE','ADMIN_API_KEY','POSTGRES_PASSWORD','DATABASE_URL','OPENAI_API_KEY','GEMINI_API_KEY','BFL_API_KEY','IDEOGRAM_API_KEY','STABILITY_API_KEY','S3_ACCESS_KEY','S3_SECRET_KEY','BACKUP_ENCRYPTION_KEY'):
        secret = os.getenv(key,'')
        if len(secret)>=8:
            value = value.replace(secret,'[REDACTED]')
    value = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}', '[BOT_TOKEN]',value)
    from app.services.openai_credentials import from_env as image_keys
    try:
        for secret in image_keys():
            if len(secret) >= 8:
                value = value.replace(secret, '[REDACTED]')
    except ValueError:
        pass
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[REDACTED]', value)
    return re.sub(r'(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+@',r'\1[REDACTED]@',value)


class RedactingFormatter(logging.Formatter):
    """Текстовый форматтер журнала: скрывает секреты уже после форматирования исключения, чтобы текст ошибки драйвера не обошёл защиту."""
    def format(self, record: logging.LogRecord) -> str:
        """Отформатировать запись обычным способом и затем скрыть в результате секреты."""
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """Форматтер журнала в JSON (`LOG_FORMAT=json`): один объект на строку для сборщиков логов; секреты скрываются так же."""
    def format(self, record: logging.LogRecord) -> str:
        """Собрать JSON-объект записи: время, уровень, имя логгера, сообщение и (при наличии) текст исключения, всё со скрытыми секретами."""
        payload = {'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'), 'level': record.levelname,
                   'logger': record.name, 'message': redact(record.getMessage())}
        if record.exc_info:
            payload['exception'] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, stream=None) -> None:
    """Настроить журналирование процесса один раз: вывод в stdout (или заданный поток), уровень из LOG_LEVEL, формат из LOG_FORMAT. Библиотеки httpx/httpcore приглушены, чтобы URL с токеном не попадал в журнал."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter() if os.getenv('LOG_FORMAT','').lower()=='json'
                         else RedactingFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO').upper(),handlers=[handler],force=True)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
