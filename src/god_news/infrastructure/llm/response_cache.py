from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

CacheSource = Literal["hit", "shared", "miss"]


@dataclass(frozen=True, slots=True)
class CachedResponse:
    payload: str
    source: CacheSource


class LLMResponseCache:
    """Bounded process-local response cache with duplicate-request coalescing.

    Persistent provider caches remain responsible for prefix reuse across process
    restarts. This cache intentionally retains only validated model output in
    memory, so source documents are not duplicated on disk.
    """

    def __init__(self, max_entries: int) -> None:
        if max_entries < 0:
            raise ValueError("max_entries cannot be negative")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[str]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        key: str,
        factory: Callable[[], Awaitable[str]],
    ) -> CachedResponse:
        async with self._lock:
            payload = self._entries.get(key)
            if payload is not None:
                self._entries.move_to_end(key)
                return CachedResponse(payload=payload, source="hit")

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._produce(key, factory))
                self._inflight[key] = task
                source: CacheSource = "miss"
            else:
                source = "shared"

        return CachedResponse(payload=await asyncio.shield(task), source=source)

    async def _produce(self, key: str, factory: Callable[[], Awaitable[str]]) -> str:
        try:
            payload = await factory()
            if self._max_entries > 0:
                async with self._lock:
                    self._entries[key] = payload
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
            return payload
        finally:
            async with self._lock:
                if self._inflight.get(key) is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def aclose(self) -> None:
        async with self._lock:
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            self._entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
