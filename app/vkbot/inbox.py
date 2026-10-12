"""Потребители входящих событий VK: в одном чате строго по очереди, без слепого повтора после сбоя (общая реализация — app.platforms)."""
from app.platforms.builtin import VK
from app.platforms.inbox import CONCURRENCY, InboxQueue

queue = InboxQueue(VK)
due_events = queue.due_events
recover = queue.recover
process = queue.process
consumer_loop = queue.consumer_loop

__all__ = ['CONCURRENCY', 'consumer_loop', 'due_events', 'process', 'recover']
