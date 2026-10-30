# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-call Embedding request coalescing with a bounded LRU vector cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage


@dataclass(frozen=True, slots=True)
class _TextResult:
    vector: tuple[float, ...]
    usage: InferenceUsage | None


class CoalescingEmbeddingModel:
    """Share in-flight text embeddings and cache successful vectors."""

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if not isinstance(max_batch_size, int) or isinstance(max_batch_size, bool) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if not isinstance(coalesce_ms, (int, float)) or isinstance(coalesce_ms, bool) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be non-negative")
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._pending: dict[str, asyncio.Future[_TextResult]] = {}
        self._queue: deque[str] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    async def embed(self, texts: Any) -> EmbeddingResult:
        values = tuple(str(text) for text in texts)
        if not values:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        unique = tuple(dict.fromkeys(values))
        waiting: list[asyncio.Future[_TextResult]] = []
        for text in unique:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                future: asyncio.Future[_TextResult] = asyncio.get_running_loop().create_future()
                future.set_result(_TextResult(cached, None))
                waiting.append(future)
                continue
            future = self._pending.get(text)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                future.add_done_callback(_consume_exception)
                self._pending[text] = future
                self._queue.append(text)
                self._ensure_worker()
                self._wake.set()
            waiting.append(future)

        results: list[_TextResult] = []
        for future in waiting:
            results.append(await asyncio.shield(future))

        by_text = dict(zip(unique, results, strict=True))
        usages: dict[int, InferenceUsage] = {}
        for result in results:
            if result.usage is not None:
                usages[id(result.usage)] = result.usage
        requests = sum(usage.requests for usage in usages.values())
        input_tokens = _sum_optional(usage.input_tokens for usage in usages.values())
        output_tokens = _sum_optional(usage.output_tokens for usage in usages.values())
        return EmbeddingResult(
            vectors=tuple(by_text[text].vector for text in values),
            usage=InferenceUsage(
                requests=requests,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="powercontext-embedding-coalescer")

    async def _run(self) -> None:
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            if self._coalesce_seconds > 0:
                await asyncio.sleep(self._coalesce_seconds)
            batch: list[str] = []
            while self._queue and len(batch) < self._max_batch_size:
                text = self._queue.popleft()
                if text in self._pending:
                    batch.append(text)
            if not batch:
                continue
            try:
                response = await self._model.embed(tuple(batch))
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                for text in batch:
                    future = self._pending.pop(text, None)
                    if future is not None and not future.done():
                        future.set_exception(error)
                continue
            vectors = tuple(response.vectors)
            if len(vectors) != len(batch):
                error = ValueError("embedding vector count does not match the batch")
                for text in batch:
                    future = self._pending.pop(text, None)
                    if future is not None and not future.done():
                        future.set_exception(error)
                continue
            usage = response.usage
            for text, vector in zip(batch, vectors, strict=True):
                value = tuple(float(item) for item in vector)
                if self._cache_size > 0:
                    self._cache[text] = value
                    self._cache.move_to_end(text)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                future = self._pending.pop(text, None)
                if future is not None and not future.done():
                    future.set_result(_TextResult(value, usage))


def _sum_optional(values: Any) -> int | None:
    items = tuple(values)
    if not items or all(item is None for item in items):
        return None
    return sum(int(item or 0) for item in items)


def _consume_exception(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


__all__ = ["CoalescingEmbeddingModel"]
