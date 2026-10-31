"""Build the task context from real seekdb query results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .db import query_materials

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"


def build_context(task: str) -> dict[str, Any]:
    rows = query_materials(task)
    documents = [
        {
            "kind": row["kind"],
            "path": row["path"],
            "content": row["content"],
            "citation": f"seekdb://sdd_documents/{row['path']}",
        }
        for row in rows
    ]
    # The task contract requires all three material classes.  The SQL result is
    # the selection, but fail loudly rather than silently inventing a citation.
    kinds = {doc["kind"] for doc in documents}
    missing = {"spec", "code", "test"} - kinds
    if missing:
        raise RuntimeError(f"seekdb context is missing required kinds: {sorted(missing)}")
    context = {
        "task": task,
        "documents": documents,
        "required": {
            "spec": next(d["path"] for d in documents if d["kind"] == "spec"),
            "code": next(d["path"] for d in documents if d["kind"] == "code"),
            "test": next(d["path"] for d in documents if d["kind"] == "test"),
        },
        "selection": "SELECT kind, path, content FROM sdd_documents WHERE MATCH(content) AGAINST(task)",
        "context_hash": hashlib.sha256(
            json.dumps([d["path"] for d in documents], sort_keys=True).encode()
        ).hexdigest()[:16],
    }
    RUNS.mkdir(exist_ok=True)
    (RUNS / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2))
    return context
