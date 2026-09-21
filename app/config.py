"""Environment-backed application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    owner_claim_code: str
    database_url: str
    admin_api_key: str
    admin_bind: str
    admin_port: int
    bible_profile: str
    max_editions_per_language: int
    import_on_start: bool
    allow_restricted_licenses: bool
    allow_unknown_licenses: bool
    import_batch_size: int
    download_timeout_seconds: int
    source_cache_dir: Path
    default_timezone: str
    default_send_time: str
    worker_poll_seconds: int
    telegram_global_rate_per_second: float
    telegram_chat_rate_per_second: float
    max_message_length: int
    log_level: str
    public_base_url: str

    @classmethod
    def from_env(cls, *, require_bot_token: bool = True) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if require_bot_token and not bot_token:
            raise RuntimeError("BOT_TOKEN is required")

        profile = os.getenv("BIBLE_PROFILE", "extended").strip().lower()
        if profile not in {"core", "extended", "all-open", "none"}:
            raise ValueError("BIBLE_PROFILE must be core, extended, all-open, or none")

        send_time = os.getenv("DEFAULT_SEND_TIME", "09:00").strip()
        hour, separator, minute = send_time.partition(":")
        if separator != ":" or not hour.isdigit() or not minute.isdigit():
            raise ValueError("DEFAULT_SEND_TIME must be HH:MM")
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError("DEFAULT_SEND_TIME is outside 00:00..23:59")

        return cls(
            bot_token=bot_token,
            owner_claim_code=os.getenv("OWNER_CLAIM_CODE", "").strip(),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://biblebot:biblebot@localhost:5432/biblebot",
            ).strip(),
            admin_api_key=os.getenv("ADMIN_API_KEY", "").strip(),
            admin_bind=os.getenv("ADMIN_BIND", "0.0.0.0").strip(),
            admin_port=_int("ADMIN_PORT", 8080),
            bible_profile=profile,
            max_editions_per_language=_int("MAX_EDITIONS_PER_LANGUAGE", 2),
            import_on_start=_bool("IMPORT_ON_START", True),
            allow_restricted_licenses=_bool("ALLOW_RESTRICTED_LICENSES", False),
            allow_unknown_licenses=_bool("ALLOW_UNKNOWN_LICENSES", False),
            import_batch_size=_int("IMPORT_BATCH_SIZE", 2000, 100),
            download_timeout_seconds=_int("DOWNLOAD_TIMEOUT_SECONDS", 180, 10),
            source_cache_dir=Path(os.getenv("SOURCE_CACHE_DIR", "/app/cache")),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Amsterdam").strip(),
            default_send_time=send_time,
            worker_poll_seconds=_int("WORKER_POLL_SECONDS", 15),
            telegram_global_rate_per_second=_float(
                "TELEGRAM_GLOBAL_RATE_PER_SECOND", 20.0, 0.1
            ),
            telegram_chat_rate_per_second=_float(
                "TELEGRAM_CHAT_RATE_PER_SECOND", 1.0, 0.1
            ),
            max_message_length=_int("MAX_MESSAGE_LENGTH", 3900, 500),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        )
