"""Умный выбор маршрута (напрямую или через прокси) для каждого внешнего сервиса.

Для сервиса (telegram, max, vk, youtube, rutube, images, …) настраивается упорядоченный список маршрутов и стратегия:

* `failover` — по порядку списка; сломанные маршруты временно отодвигаются в конец;
* `auto` — выбирается самый быстрый и надёжный по скользящей оценке задержки и доле ошибок;
* `round_robin` — по кругу среди работающих.

У каждого маршрута есть «автомат» (circuit breaker): после нескольких подряд неудач маршрут отключается на растущее время
(30 с … 15 мин) и затем получает пробный запрос. Если рабочих маршрутов нет, пробуется тот, у которого раньше всех
истекает пауза, — сервис не остаётся без попыток. Состояние здоровья хранится в памяти процесса и периодически
сохраняется в БД для панели.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

STRATEGIES = ('failover', 'auto', 'round_robin')
OPEN_AFTER = 3            # столько неудач подряд отключают маршрут
BASE_PAUSE = 30.0         # секунд; удваивается с каждой следующей неудачей, максимум MAX_PAUSE
MAX_PAUSE = 900.0
DEFAULT_LATENCY_MS = 400.0
ALPHA = 0.3               # вес нового измерения в скользящей оценке задержки


@dataclass(frozen=True)
class Route:
    """Один путь к сервису: `direct` или прокси. URL прокси может содержать логин и пароль и в журнал не выводится."""
    key: str                         # 'direct' или 'proxy:<id>'
    label: str
    proxy_url: str | None = field(default=None, repr=False)


DIRECT = Route('direct', 'Напрямую')


@dataclass
class Policy:
    """Настройки сервиса: стратегия, число попыток на запрос и упорядоченные маршруты."""
    strategy: str = 'failover'
    attempts: int = 3
    routes: list[Route] = field(default_factory=lambda: [DIRECT])


@dataclass
class Health:
    """Здоровье маршрута для сервиса."""
    successes: int = 0
    failures: int = 0
    consecutive: int = 0
    latency_ms: float | None = None
    open_until: float = 0.0
    last_error: str = ''
    last_ok_at: float | None = None


class Router:
    """Хранит политики и здоровье маршрутов; не выполняет сетевых запросов."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.policies: dict[str, Policy] = {}
        self.health: dict[tuple[str, str], Health] = {}
        self._rotation: dict[str, int] = {}

    def set_policies(self, policies: dict[str, Policy]) -> None:
        """Заменить настройки (здоровье маршрутов сохраняется)."""
        self.policies = dict(policies)

    def policy(self, service: str) -> Policy:
        """Политика сервиса; без настроек — только прямой доступ."""
        return self.policies.get(service) or self.policies.get('*') or Policy()

    def state(self, service: str, route: Route) -> Health:
        """Состояние маршрута (создаётся при первом обращении)."""
        return self.health.setdefault((service, route.key), Health())

    def is_open(self, service: str, route: Route) -> bool:
        """Отключён ли маршрут «автоматом» в данный момент."""
        return self.state(service, route).open_until > self.clock()

    def score(self, service: str, route: Route) -> float:
        """Оценка маршрута для стратегии `auto` (меньше — лучше): задержка с поправкой на недавние ошибки и долю неудач."""
        state = self.state(service, route)
        latency = state.latency_ms if state.latency_ms is not None else DEFAULT_LATENCY_MS
        total = state.successes + state.failures
        failure_rate = state.failures / total if total else 0.0
        return latency * (1 + state.consecutive * 0.5) * (1 + failure_rate * 2)

    def plan(self, service: str) -> list[Route]:
        """Упорядоченный список маршрутов для одного запроса (не длиннее `attempts`)."""
        policy = self.policy(service)
        routes = policy.routes or [DIRECT]
        usable = [r for r in routes if not self.is_open(service, r)]
        blocked = sorted((r for r in routes if self.is_open(service, r)), key=lambda r: self.state(service, r).open_until)
        if policy.strategy == 'auto':
            usable.sort(key=lambda r: self.score(service, r))
        elif policy.strategy == 'round_robin' and usable:
            start = self._rotation.get(service, 0) % len(usable)
            self._rotation[service] = start + 1
            usable = usable[start:] + usable[:start]
        return (usable + blocked)[:max(1, policy.attempts)]

    def record_success(self, service: str, route: Route, latency_ms: float) -> None:
        """Отметить успех: сбросить серию неудач, обновить оценку задержки."""
        state = self.state(service, route)
        state.successes += 1
        state.consecutive = 0
        state.open_until = 0.0
        state.last_ok_at = time.time()
        state.latency_ms = latency_ms if state.latency_ms is None else state.latency_ms * (1 - ALPHA) + latency_ms * ALPHA

    def record_failure(self, service: str, route: Route, error: str) -> None:
        """Отметить неудачу; после серии неудач маршрут отключается на растущее время."""
        state = self.state(service, route)
        state.failures += 1
        state.consecutive += 1
        state.last_error = error[:100]
        if state.consecutive >= OPEN_AFTER:
            pause = min(BASE_PAUSE * 2 ** (state.consecutive - OPEN_AFTER), MAX_PAUSE)
            state.open_until = self.clock() + pause

    def reset(self, service: str | None = None) -> None:
        """Сбросить здоровье (после изменения настроек из панели)."""
        for key in [k for k in self.health if service is None or k[0] == service]:
            del self.health[key]

    def snapshot(self) -> list[dict]:
        """Состояние всех маршрутов для сохранения в БД и показа в панели."""
        now = self.clock()
        return [dict(service=service, route=route, successes=s.successes, failures=s.failures, consecutive=s.consecutive,
                     latency_ms=round(s.latency_ms, 1) if s.latency_ms is not None else None,
                     open_seconds=max(0, round(s.open_until - now)), last_error=s.last_error)
                for (service, route), s in sorted(self.health.items())]


ROUTER = Router()
