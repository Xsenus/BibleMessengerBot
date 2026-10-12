"""Описание платформы мессенджера и реестр платформ."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlatformSpec:
    """Постоянные свойства платформы. Объект неизменяем и не содержит секретов."""
    name: str                          # короткий ключ в БД: 'max', 'vk' (Telegram — платформа по умолчанию, не «мостовая»)
    title: str                         # название для людей
    service: str                       # имя службы (сигнал жизни, healthcheck, профиль Compose)
    inbox_table: str                   # таблица входящих событий
    rate_table: str                    # таблица ограничителя скорости
    global_gap: float                  # минимальный интервал между любыми двумя сообщениями, секунд
    chat_gap: float = 1.05             # минимальный интервал между сообщениями в один чат
    text_limit: int = 3600             # безопасная длина части текста
    split: Callable[[str, int], list] | None = None   # как резать HTML на части: (текст, лимит) → части для очереди
    chunk_kind: str = 'text'           # вид части текста в очереди доставки
    bridged: bool = True               # работает через мост-имитацию бота (все, кроме Telegram)
    extra: dict = field(default_factory=dict)


REGISTRY: dict[str, PlatformSpec] = {}


def register(spec: PlatformSpec) -> PlatformSpec:
    """Добавить платформу в реестр (повторная регистрация того же имени — ошибка)."""
    if spec.name in REGISTRY:
        raise ValueError(f'Platform {spec.name} is already registered')
    REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> PlatformSpec:
    """Описание платформы по имени."""
    return REGISTRY[name]


def bridged() -> frozenset[str]:
    """Имена платформ, работающих через мост (то есть все, кроме Telegram)."""
    return frozenset(name for name, spec in REGISTRY.items() if spec.bridged)
