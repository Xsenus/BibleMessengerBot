"""Операции нативного деплоя: проверка архива панели, схемы, бэкап и API ботов."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


def unpack_panel(archive: str, directory: str) -> None:
    """Распаковать ограниченный набор обычных файлов в пустой каталог."""
    target = Path(directory)
    paths: set[PurePosixPath] = set()
    total = 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        for count, entry in enumerate(bundle, 1):
            path = PurePosixPath(entry.name)
            total += entry.size
            if (count > 4096 or total > 96 * 1024 * 1024 or path.is_absolute()
                    or ".." in path.parts or path in paths or not (entry.isfile() or entry.isdir())):
                raise ValueError("Invalid panel archive")
            paths.add(path)
            output = target.joinpath(*path.parts)
            if entry.isdir():
                output.mkdir(parents=True, exist_ok=True)
                output.chmod(0o755)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(entry) as source, output.open("xb") as stream:
                    while block := source.read(128 * 1024):
                        stream.write(block)
                output.chmod(0o644)
    if not (target / "index.html").is_file() or not (target / "assets").is_dir():
        raise ValueError("Panel build is incomplete")
    for parent, directories, _ in os.walk(target):
        for name in directories:
            (Path(parent) / name).chmod(0o755)
    target.chmod(0o755)


def database_environment() -> dict[str, str]:
    """Передать параметры существующей PostgreSQL через окружение libpq."""
    url = urlsplit(os.environ["DATABASE_URL"])
    if (url.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not url.username or not url.path.lstrip("/") or url.query or url.fragment):
        raise ValueError("Expected the existing local PostgreSQL connection")
    return {**os.environ, "PGHOST": url.hostname, "PGPORT": str(url.port or 5432),
            "PGUSER": unquote(url.username), "PGPASSWORD": unquote(url.password or ""),
            "PGDATABASE": unquote(url.path.lstrip("/")), "PGCONNECT_TIMEOUT": "8"}


def check_schema() -> None:
    """Напечатать хеш списка миграций без параметров подключения."""
    query = "SELECT name,sha256 FROM schema_migrations ORDER BY name"
    rows = subprocess.check_output(["psql", "-XAtq", "-v", "ON_ERROR_STOP=1", "-c", query],
                                   env=database_environment())
    print(hashlib.sha256(rows).hexdigest())


def backup_database() -> None:
    """Записать потоковый однопоточный дамп; файл создаёт родитель под root."""
    subprocess.run(["nice", "-n", "10", "pg_dump", "-Fc", "-Z", "1", "--no-owner"],
                   env=database_environment(), check=True)


async def check_apis() -> None:
    """Проверить токены через API без отправки сообщений."""
    from aiogram import Bot

    from app.maxbot.client import MaxClient
    from app.maxbot.config import MaxSettings

    async def telegram() -> None:
        async with Bot(os.environ["BOT_TOKEN"]) as bot:
            if not (await bot.get_me()).is_bot:
                raise ValueError("Telegram identity check failed")
        print("Telegram getMe OK", flush=True)

    async def max_api() -> None:
        client = MaxClient(MaxSettings.from_env(require_token=True))
        try:
            if not (await client.get_me()).get("user_id"):
                raise ValueError("MAX identity check failed")
        finally:
            await client.close()
        print("MAX GET /me OK", flush=True)

    results = await asyncio.gather(asyncio.wait_for(telegram(), 13), asyncio.wait_for(max_api(), 13),
                                   return_exceptions=True)
    for name, result in zip(("Telegram", "MAX"), results, strict=True):
        if isinstance(result, BaseException):
            print(f"{name} verification failed ({type(result).__name__})", file=sys.stderr)
    if any(isinstance(result, BaseException) for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        command, *args = sys.argv[1:]
        if command == "panel":
            unpack_panel(*args)
        elif command == "fingerprint":
            check_schema()
        elif command == "backup":
            backup_database()
        elif command == "apis":
            asyncio.run(check_apis())
        else:
            raise ValueError("Unknown deployment operation")
    except Exception as error:
        print(f"Deployment operation failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
