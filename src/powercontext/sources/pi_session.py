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

"""Pi v3 session history parsing and incremental Source connector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    Source,
    SourceMaterialization,
    TextEvidence,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

PI_EVENT_SOURCE_NAME = "pi-event"


class PiSessionInput(BaseModel):
    """One UTF-8 JSONL Pi session archive and an optional selected leaf."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible Pi event with its complete original record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """A captured Pi event whose identity is stable across branch switches."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def _identity(session_id: str, event_id: str) -> str:
    source_id = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        event_hash = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        source_id = f"pi:{session_hash[:48]}:{event_hash[:48]}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("pi event source id must not exceed the source id limit")
    return source_id


def _text_blocks(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for block in value:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def _message_body(message: dict[str, Any], role: str) -> str:
    if role == "toolResult":
        tool_name = message.get("toolName")
        call_id = message.get("toolCallId")
        text = "\n".join(_text_blocks(message.get("content")))
        return f"[tool_result {tool_name} {call_id}] {text}"
    if role == "bashExecution":
        command = message.get("command", "")
        output = message.get("output", "")
        return f"$ {command}\n{output}"
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "toolCall":
            arguments = block.get("arguments", {})
            compact = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            parts.append(f"[tool_call {block.get('name')} {block.get('id')}] {compact}")
        # thinking and image blocks never become text evidence
    return "\n".join(parts)


def _visible_event(record: dict[str, Any], session_id: str) -> PiEventInput | None:
    event_type = record.get("type")
    if event_type == "message" and isinstance(record.get("message"), dict):
        message = record["message"]
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(message, role)
        if not body:
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=str(record["id"]),
            parent_id=record.get("parentId"),
            event_type="message",
            role=role,
            content=f"{role}: {body}",
            record=record,
        )
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=str(record["id"]),
            parent_id=record.get("parentId"),
            event_type=str(event_type),
            role=str(event_type),
            content=f"{event_type}: {summary}",
            record=record,
        )
    if event_type == "custom_message":
        body = "\n".join(_text_blocks(record.get("content")))
        if not body and isinstance(record.get("content"), str):
            body = record["content"]
        if not body:
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=str(record["id"]),
            parent_id=record.get("parentId"),
            event_type="custom_message",
            role="custom",
            content=f"custom: {body}",
            record=record,
        )
    return None


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return visible events on the selected leaf's full ancestor chain."""

    if not isinstance(value.content, (bytes, bytearray)) or not value.content:
        raise ValueError("Pi session content must be non-empty bytes")
    try:
        text = bytes(value.content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session must be valid UTF-8") from error
    records: list[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError("Pi session contains invalid JSON") from error
    if not records or not isinstance(records[0], dict):
        raise ValueError("Pi session header is missing")
    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("Pi session header must declare type=session and version=3")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id or not session_id.strip():
        raise ValueError("Pi session header id must be non-empty")

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in records[1:]:
        if not isinstance(raw, dict):
            raise ValueError("Pi session records must be objects")
        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id or not event_id.strip():
            raise ValueError("Pi session event id must be non-empty")
        if event_id in nodes:
            raise ValueError("Pi session event ids must be unique")
        parent_id = raw.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError("Pi session parentId must be a non-empty string or null")
            if parent_id not in nodes:
                raise ValueError("Pi session parent must refer to an earlier event")
        nodes[event_id] = raw
        order.append(event_id)
    if not order:
        raise ValueError("Pi session has no events")
    leaf = value.leaf_id if value.leaf_id is not None else order[-1]
    if leaf not in nodes:
        raise ValueError("Pi session leaf does not exist")

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = leaf
    while current is not None:
        if current in seen:
            raise ValueError("Pi session contains a parent cycle")
        seen.add(current)
        record = nodes[current]
        chain.append(record)
        parent = record.get("parentId")
        current = None if parent is None else str(parent)
    chain.reverse()

    events: list[PiEventInput] = []
    for record in chain:
        event = _visible_event(record, session_id)
        if event is not None:
            _identity(event.session_id, event.event_id)
            events.append(event)
    return tuple(events)


class PiEventAdapter:
    """Resolve and read Pi event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = _identity(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one visible Pi event as text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version="1",
    projections=(PiEventTextEvidenceProjection(),),
)


def _record_digest(record: dict[str, Any]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PiSessionConnector:
    """Incrementally import visible events from one selected Pi branch."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        try:
            content = self._path.read_bytes()
        except OSError as error:
            raise ValueError("Pi session file could not be read") from error
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))
        if not events:
            raise ValueError("Pi session has no visible events")
        session_id = events[0].session_id

        checkpoint = session.checkpoint
        imported: dict[str, str] = {}
        if checkpoint is not None:
            if not isinstance(checkpoint, dict) or set(checkpoint) != {"session_id", "events"}:
                raise ValueError("Pi connector checkpoint is invalid")
            if checkpoint.get("session_id") != session_id:
                raise ValueError("Pi connector checkpoint belongs to another session")
            raw_events = checkpoint.get("events")
            if not isinstance(raw_events, dict):
                raise ValueError("Pi connector checkpoint events are invalid")
            for event_id, digest in raw_events.items():
                if not isinstance(event_id, str) or not isinstance(digest, str):
                    raise ValueError("Pi connector checkpoint event entry is invalid")
                imported[event_id] = digest

        for event in events:
            digest = _record_digest(event.record)
            previous = imported.get(event.event_id)
            if previous is not None:
                if previous != digest:
                    raise ValueError(f"Pi event {event.event_id!r} changed after import")
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status.value != "accepted":
                raise ValueError(f"Pi event {event.event_id!r} was rejected")
            imported[event.event_id] = digest
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"session_id": session_id, "events": imported},
        )


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
