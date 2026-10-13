"""Потребители входящих событий MAX: в одном чате строго по очереди, без слепого повтора после сбоя (общая реализация — app.platforms)."""
from app.platforms.builtin import MAX
from app.platforms.inbox import CONCURRENCY, InboxQueue

queue = InboxQueue(MAX)
due_events = queue.due_events
recover = queue.recover
process = queue.process
consumer_loop = queue.consumer_loop

__all__ = ['CONCURRENCY', 'consumer_loop', 'due_events', 'process', 'recover']
