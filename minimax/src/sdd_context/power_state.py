"""PowerContext-backed task state.

The state is stored as an Atomic Memory entry in a persistent PowerContext
SQLite database.  A fresh process opens the same scope and reads the latest
entry, which is the restart/reuse mechanism required by the task.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from powercontext.builtin.artifacts.atomic_memory.models import AtomicMemoryContent
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
from powercontext.builtin.scope.models import ScopeDraft

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".state"
STATE_DB = STATE_DIR / "powercontext.db"
SCOPE_TITLE = "SDD Engineering Context"
SCOPE_KEY = "sdd-engineering-context-v1"


def _config() -> BuiltinConfig:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return BuiltinConfig(
        database=SQLiteConfig(url="sqlite+aiosqlite:///" + str(STATE_DB))
    )


async def _scope_id(runtime) -> str:
    for desc in await runtime.scopes.list():
        if desc.title == SCOPE_TITLE:
            return desc.scope_id
    desc = await runtime.scopes.create(
        ScopeDraft(
            title=SCOPE_TITLE,
            summary="PowerContext state for the MiniMax SDD demo",
            idempotency_key=SCOPE_KEY,
        )
    )
    return desc.scope_id


async def save_state(record: dict[str, Any]) -> dict[str, Any]:
    """Create a new immutable task-state entry and return its reference."""
    async with open_builtin_runtime(_config()) as runtime:
        scope_id = await _scope_id(runtime)
        memory = runtime.atomic_memory.for_scope(scope_id)
        existing = await memory.list()
        seq = 1 + max(
            (
                json.loads(item.artifact.content.text).get("state_seq", 0)
                for item in existing.items
                if item.artifact.content.kind == "sdd_task_state"
            ),
            default=0,
        )
        record = {**record, "state_seq": seq}
        text = json.dumps(record, ensure_ascii=False, sort_keys=True)
        result = await memory.create(
            (AtomicMemoryContent(kind="sdd_task_state", text=text),)
        )
        artifact = result.records[0].artifact
        return {
            "scope_id": scope_id,
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision,
        }


async def latest_state() -> dict[str, Any] | None:
    """Read the latest task state after a process restart."""
    async with open_builtin_runtime(_config()) as runtime:
        scope_id = await _scope_id(runtime)
        memory = runtime.atomic_memory.for_scope(scope_id)
        page = await memory.list()
        values = []
        for item in page.items:
            if item.artifact.content.kind != "sdd_task_state":
                continue
            try:
                value = json.loads(item.artifact.content.text)
            except json.JSONDecodeError:
                continue
            value["_powercontext"] = {
                "scope_id": scope_id,
                "artifact_id": item.artifact.artifact_id,
                "revision": item.artifact.revision,
            }
            values.append(value)
        return max(values, key=lambda value: value.get("state_seq", 0)) if values else None


async def history() -> list[dict[str, Any]]:
    """Return all saved states, oldest first, for audit logs."""
    async with open_builtin_runtime(_config()) as runtime:
        scope_id = await _scope_id(runtime)
        memory = runtime.atomic_memory.for_scope(scope_id)
        page = await memory.list()
        out = []
        for item in page.items:
            if item.artifact.content.kind != "sdd_task_state":
                continue
            try:
                value = json.loads(item.artifact.content.text)
            except json.JSONDecodeError:
                continue
            value["_powercontext"] = {
                "scope_id": scope_id,
                "artifact_id": item.artifact.artifact_id,
                "revision": item.artifact.revision,
            }
            out.append(value)
        return sorted(out, key=lambda value: value.get("state_seq", 0))
