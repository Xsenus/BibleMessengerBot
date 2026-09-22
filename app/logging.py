"""Container logging with credential redaction applied to messages and tracebacks."""
from __future__ import annotations
import logging
import os
import re
import sys


def redact(value: str) -> str:
    """Never print tokens, claim codes, admin keys or database passwords."""
    for key in ('BOT_TOKEN','OWNER_CLAIM_CODE','ADMIN_API_KEY','POSTGRES_PASSWORD','DATABASE_URL'):
        secret = os.getenv(key,'')
        if len(secret)>=8:
            value = value.replace(secret,'[REDACTED]')
    value = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}', '[BOT_TOKEN]',value)
    return re.sub(r'(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+@',r'\1[REDACTED]@',value)


class RedactingFormatter(logging.Formatter):
    """Redact after exception formatting so driver exception text cannot bypass it."""
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(*, stream=None) -> None:
    """Configure once per process; HTTP clients do not log token-bearing URLs."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO').upper(),handlers=[handler],force=True)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
