"""Fan-out broker for WebSocket subscribers.

REST handlers run in FastAPI's thread pool, the WebSocket lives on the event
loop, so publishing hops threads via `loop.call_soon_threadsafe`.

Each subscriber gets its own bounded queue. A device that stops draining (an
ESP32 mid-refresh, a dropped Wi-Fi link) has its oldest message dropped instead
of stalling the publisher.
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict

QUEUE_MAXSIZE = 16


class Broker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------ subscribing
    def subscribe(self, topic: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers[topic].discard(queue)
            if not self._subscribers[topic]:
                self._subscribers.pop(topic, None)

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._subscribers.get(topic, ()))

    # ------------------------------------------------------------- publishing
    def publish(self, topic: str, message: dict) -> None:
        """Safe to call from any thread."""
        with self._lock:
            queues = list(self._subscribers.get(topic, ()))
            queues += list(self._subscribers.get("*", ()))
        if not queues:
            return
        loop = self._loop
        for queue in queues:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._offer, queue, message)
            else:
                self._offer(queue, message)

    @staticmethod
    def _offer(queue: asyncio.Queue, message: dict) -> None:
        if queue.full():
            try:
                queue.get_nowait()  # drop the stalest update
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


def table_topic(table_number: int) -> str:
    return f"table:{table_number}"


broker = Broker()
