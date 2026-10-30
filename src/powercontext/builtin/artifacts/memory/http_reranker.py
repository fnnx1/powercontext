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

"""HTTP cross-encoder reranking over one bounded Memory candidate pool."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score candidates through a caller-supplied HTTP service and select globally."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be a non-empty string")
        self.endpoint = endpoint
        self.client = client
        self.batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Batch all candidates, validate every score, then select globally."""

        _validate_bounds(len(candidates), limit)
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            payload = {
                "query": query,
                "candidates": [
                    {"rank": start + offset + 1, "text": candidate.text}
                    for offset, candidate in enumerate(batch)
                ],
            }
            response = await self.client.post(self.endpoint, json=payload)
            response.raise_for_status()
            requests += 1
            batch_scores = _parse_scores(response, expected_ranks=set(range(start + 1, start + 1 + len(batch))))
            scores.update(batch_scores)

        ranked = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(ranked),
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(response: httpx.Response, *, expected_ranks: set[int]) -> dict[int, float]:
    try:
        payload: Any = json.loads(response.content)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("reranker response must be valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("reranker response must contain exactly a scores field")
    raw_scores = payload["scores"]
    if not isinstance(raw_scores, list):
        raise ValueError("reranker scores must be an array")
    parsed: dict[int, float] = {}
    for item in raw_scores:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            raise ValueError("each reranker score must contain exactly rank and score")
        rank = item["rank"]
        score = item["score"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank not in expected_ranks:
            raise ValueError("reranker score rank is outside its batch")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("reranker score must be numeric")
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("reranker score must be finite")
        if rank in parsed:
            raise ValueError("reranker scores must cover each rank exactly once")
        parsed[rank] = value
    if set(parsed) != expected_ranks:
        raise ValueError("reranker scores must cover every rank in the batch exactly once")
    return parsed


__all__ = ["HTTPMemoryReranker"]
