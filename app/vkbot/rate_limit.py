"""Ограничитель скорости ВКонтакте (лимит сообщества — 20 запросов в секунду; берётся запас): общая реализация — app.platforms."""
from app.platforms.builtin import VK
from app.platforms.rate_limit import RateLimiter

wait_send_slot = RateLimiter(VK).wait
