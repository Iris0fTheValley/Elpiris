from __future__ import annotations

import asyncio

import pytest

from god_news.infrastructure.llm.response_cache import LLMResponseCache


@pytest.mark.asyncio
async def test_response_cache_reuses_entries_and_evicts_least_recently_used() -> None:
    cache = LLMResponseCache(max_entries=1)
    calls: list[str] = []

    async def produce(value: str) -> str:
        calls.append(value)
        return value

    assert (await cache.get_or_create("a", lambda: produce("first"))).source == "miss"
    assert (await cache.get_or_create("a", lambda: produce("unused"))).source == "hit"
    await cache.get_or_create("b", lambda: produce("second"))
    assert (await cache.get_or_create("a", lambda: produce("third"))).payload == "third"
    assert calls == ["first", "second", "third"]
    await cache.aclose()


@pytest.mark.asyncio
async def test_response_cache_coalesces_concurrent_identical_requests() -> None:
    cache = LLMResponseCache(max_entries=8)
    release = asyncio.Event()
    calls = 0

    async def produce() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "validated-json"

    first = asyncio.create_task(cache.get_or_create("same", produce))
    second = asyncio.create_task(cache.get_or_create("same", produce))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert calls == 1
    assert {result.source for result in results} == {"miss", "shared"}
    assert {result.payload for result in results} == {"validated-json"}
    await cache.aclose()
