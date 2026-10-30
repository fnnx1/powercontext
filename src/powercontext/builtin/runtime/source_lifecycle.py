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

"""Persistent source-dependency lifecycle for Memory and Topic Memory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.sources import SourceRef

_SOURCE = "source"
_MEMORY = "memory"
_TOPIC = "topic"
_POLICIES = {"all", "any"}


class SourceLifecycleError(ValueError):
    """Report an invalid lifecycle graph, event, or resolution."""


class SourceLifecycleEvent(BaseModel):
    """One strictly validated availability change for a Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int = Field(gt=0, strict=True)
    sequence: int = Field(gt=0, strict=True)
    available: bool = Field(strict=True)
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_trimmed_text(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("lifecycle text must be non-empty")  # noqa: TRY003
        if value != value.strip():
            raise ValueError("lifecycle text must be trimmed")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class _Node:
    node_id: str
    kind: str
    payload: dict[str, Any]
    parents: tuple[str, ...]
    policy: str
    revision: int
    sequence: int
    available: bool
    last_event: dict[str, Any] | None


def _reference(node: _Node) -> SourceRef | MemoryCitation | ArtifactRef:
    try:
        if node.kind == _SOURCE:
            return SourceRef.model_validate(node.payload)
        if node.kind == _MEMORY:
            return MemoryCitation.model_validate(node.payload)
        return ArtifactRef.model_validate(node.payload)
    except Exception as error:
        raise SourceLifecycleError("persisted lifecycle reference is invalid") from error


def _decode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _node_from_row(row: Any) -> _Node:
    parents = _decode(row["parents"])
    if not isinstance(parents, list) or any(not isinstance(item, str) for item in parents):
        raise SourceLifecycleError("persisted lifecycle parents are invalid")
    payload = _decode(row["payload"])
    if not isinstance(payload, dict):
        raise SourceLifecycleError("persisted lifecycle payload is invalid")
    last_event = _decode(row["last_event"])
    if last_event is not None and not isinstance(last_event, dict):
        raise SourceLifecycleError("persisted lifecycle event is invalid")
    return _Node(
        node_id=str(row["node_id"]),
        kind=str(row["kind"]),
        payload=payload,
        parents=tuple(parents),
        policy=str(row["policy"]),
        revision=int(row["revision"]),
        sequence=int(row["sequence"]),
        available=bool(row["available"]),
        last_event=last_event,
    )


@dataclass(frozen=True, slots=True)
class _Graph:
    nodes: dict[str, _Node]
    available: dict[str, bool]


class ScopedSourceLifecycle:
    """Maintain one Scope's dependency graph and native read eligibility."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    @property
    def _database(self) -> Any:
        database = getattr(self._runtime._provider, "database", None)
        if database is None:
            raise SourceLifecycleError("lifecycle requires a relational Runtime")
        return database

    async def _load_graph(self) -> _Graph:
        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id)
                )
            ).mappings()
            nodes = {str(row["node_id"]): _node_from_row(row) for row in rows}
        return _Graph(nodes=nodes, available=_compute_available(nodes))

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        _require_node_id(node_id)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SourceLifecycleError("source revision must be a positive integer")
        try:
            record = await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type, source_ref.source_id
            )
        except Exception as error:
            raise SourceLifecycleError("source reference is not readable") from error
        if (record.source_type, record.source_id) != (source_ref.source_type, source_ref.source_id):
            raise SourceLifecycleError("source reference is not readable")
        await self._register(
            node_id=node_id,
            kind=_SOURCE,
            payload=source_ref.model_dump(mode="json"),
            parents=(),
            policy="all",
            revision=revision,
            available=True,
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...] | list[str],
        policy: str,
    ) -> None:
        _require_node_id(node_id)
        _require_policy(policy)
        parent_ids = _require_parents(parents)
        try:
            await self._runtime.memory.for_scope(self.scope_id).get(
                _get_memory_request(citation)
            )
        except Exception as error:
            raise SourceLifecycleError("memory citation is not readable") from error
        await self._register(
            node_id=node_id,
            kind=_MEMORY,
            payload=citation.model_dump(mode="json"),
            parents=parent_ids,
            policy=policy,
            revision=0,
            available=None,
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: tuple[str, ...] | list[str],
        policy: str,
    ) -> None:
        _require_node_id(node_id)
        _require_policy(policy)
        parent_ids = _require_parents(parents)
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
            )
        except Exception as error:
            raise SourceLifecycleError("topic reference is not readable") from error
        await self._register(
            node_id=node_id,
            kind=_TOPIC,
            payload=artifact_ref.model_dump(mode="json"),
            parents=parent_ids,
            policy=policy,
            revision=0,
            available=None,
        )

    async def _register(
        self,
        *,
        node_id: str,
        kind: str,
        payload: dict[str, Any],
        parents: tuple[str, ...],
        policy: str,
        revision: int,
        available: bool | None,
    ) -> None:
        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id)
                )
            ).mappings()
            nodes = {str(row["node_id"]): _node_from_row(row) for row in rows}
            existing = nodes.get(node_id)
            if existing is not None:
                if (
                    existing.kind != kind
                    or existing.payload != payload
                    or existing.parents != parents
                    or existing.policy != policy
                    or (kind == _SOURCE and existing.revision != revision)
                ):
                    raise SourceLifecycleError("node id is already bound to another registration")
                return
            if kind != _SOURCE:
                for parent in parents:
                    if parent not in nodes:
                        raise SourceLifecycleError("parent node is not registered")
                if node_id in parents:
                    raise SourceLifecycleError("a node cannot depend on itself")
                trial = {
                    **nodes,
                    node_id: _Node(
                        node_id=node_id,
                        kind=kind,
                        payload=payload,
                        parents=parents,
                        policy=policy,
                        revision=0,
                        sequence=0,
                        available=False,
                        last_event=None,
                    ),
                }
                computed = _compute_available(trial)
                available = computed[node_id]
            if available is None:
                available = True
            connection_row = {
                "scope_id": self.scope_id,
                "node_id": node_id,
                "kind": kind,
                "payload": _encode(payload),
                "parents": _encode(list(parents)),
                "policy": policy,
                "revision": revision,
                "sequence": 0,
                "available": bool(available),
                "last_event": None,
            }
            await connection.execute(SOURCE_LIFECYCLE_TABLE.insert().values(**connection_row))
            target_state = bool(available)
        if kind == _MEMORY:
            await self._set_native_memory_state(node_id, target_state, "source lifecycle registration")

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        graph = await self._load_graph()
        source = graph.nodes.get(event.node_id)
        if source is None or source.kind != _SOURCE:
            raise SourceLifecycleError("event requires a registered source node")
        if source.revision != event.revision:
            raise SourceLifecycleError("event revision does not match the registered source")
        event_payload = event.model_dump(mode="json")
        if event.sequence < source.sequence:
            raise SourceLifecycleError("event sequence is older than the committed sequence")
        if event.sequence == source.sequence:
            if source.last_event != event_payload:
                raise SourceLifecycleError("event sequence was reused with different content")
            return sorted(graph.available.keys() and [key for key, active in graph.available.items() if active])

        updated_source = _Node(
            node_id=source.node_id,
            kind=source.kind,
            payload=source.payload,
            parents=source.parents,
            policy=source.policy,
            revision=source.revision,
            sequence=event.sequence,
            available=event.available,
            last_event=event_payload,
        )
        updated_nodes = {**graph.nodes, event.node_id: updated_source}
        next_available = _compute_available(updated_nodes)
        changed_memory = [
            node
            for node in updated_nodes.values()
            if node.kind == _MEMORY and next_available[node.node_id] != graph.available.get(node.node_id, False)
        ]
        for node in changed_memory:
            await self._set_native_memory_state(
                node.node_id,
                next_available[node.node_id],
                event.reason,
            )
        async with self._database.transaction() as connection:
            for node in updated_nodes.values():
                current = graph.available.get(node.node_id, False)
                target = next_available[node.node_id]
                if node.node_id == event.node_id:
                    await connection.execute(
                        update(SOURCE_LIFECYCLE_TABLE)
                        .where(
                            SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id,
                            SOURCE_LIFECYCLE_TABLE.c.node_id == node.node_id,
                        )
                        .values(
                            available=target,
                            sequence=event.sequence,
                            last_event=_encode(event_payload),
                        )
                    )
                elif current != target:
                    await connection.execute(
                        update(SOURCE_LIFECYCLE_TABLE)
                        .where(
                            SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id,
                            SOURCE_LIFECYCLE_TABLE.c.node_id == node.node_id,
                        )
                        .values(available=target)
                    )
        return sorted(node_id for node_id, active in next_available.items() if active)

    async def resolve(self, node_id: str, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown lifecycle purpose")
        graph = await self._load_graph()
        node = graph.nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown lifecycle node")
        if purpose == "generation" and not graph.available[node_id]:
            raise SourceLifecycleError("node is not available for generation")
        return _reference(node)

    async def _set_native_memory_state(self, node_id: str, active: bool, reason: str) -> None:
        graph = await self._load_graph()
        node = graph.nodes.get(node_id)
        if node is None or node.kind != _MEMORY:
            raise SourceLifecycleError("unknown memory node")
        citation = MemoryCitation.model_validate(node.payload)
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                service = context.artifacts.memory
                current = await service.head(context.artifacts.memory_artifact_id)
                if current is None:
                    raise SourceLifecycleError("memory artifact is missing")
                state = next(
                    (item.state for item in current.content.manifest.entries if item.entry_id == citation.entry_id),
                    None,
                )
                if state is None:
                    raise SourceLifecycleError("memory entry is missing")
                if (state == "active") == active:
                    return
                entries = await service.entries(current)
                entry = next((item for item in entries if item.entry_id == citation.entry_id), None)
                if entry is None:
                    raise SourceLifecycleError("memory entry is missing")
                if active:
                    await service.reactivate(current, entries=(entry,), reason=reason)
                else:
                    await service.forget(current, entries=(entry,), reason=reason)


class SourceLifecycleApplication:
    """Select the Scope-bound lifecycle service."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, scope_id)


def _get_memory_request(citation: MemoryCitation):
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest

    return GetMemoryEntryRequest(citation=citation)


def _require_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id or node_id != node_id.strip():
        raise SourceLifecycleError("node id must be a non-empty trimmed string")


def _require_policy(policy: str) -> None:
    if policy not in _POLICIES:
        raise SourceLifecycleError("policy must be all or any")


def _require_parents(parents: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    values = tuple(parents)
    if not values:
        raise SourceLifecycleError("a derived node requires at least one parent")
    if any(not isinstance(parent, str) or not parent or parent != parent.strip() for parent in values):
        raise SourceLifecycleError("parent node ids must be non-empty trimmed strings")
    if len(set(values)) != len(values):
        raise SourceLifecycleError("parent node ids must be unique")
    return values


def _compute_available(nodes: dict[str, _Node]) -> dict[str, bool]:
    resolved: dict[str, bool] = {}
    visiting: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in resolved:
            return resolved[node_id]
        if node_id in visiting:
            raise SourceLifecycleError("lifecycle graph contains a cycle")
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("lifecycle graph references a missing parent")
        if node.kind == _SOURCE:
            value = node.available
        else:
            visiting.add(node_id)
            try:
                support = [visit(parent) for parent in node.parents]
            finally:
                visiting.discard(node_id)
            value = all(support) if node.policy == "all" else any(support)
        resolved[node_id] = value
        return value

    for node_id in nodes:
        visit(node_id)
    return resolved


__all__ = [
    "ScopedSourceLifecycle",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]
