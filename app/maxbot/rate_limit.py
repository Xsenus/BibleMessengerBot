"""Ограничитель скорости MAX: отдельные резервы, не зависящие от общего бюджета Telegram (общая реализация — app.platforms)."""
from app.platforms.builtin import MAX
from app.platforms.rate_limit import RateLimiter

wait_send_slot = RateLimiter(MAX).wait
