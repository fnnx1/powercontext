"""Contract tests for the SDD context module (requires the demo seekdb)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdd_context.context_builder import build_context
from sdd_context.db import connect, init_db

ROOT = Path(__file__).resolve().parents[1]


def _db_available() -> bool:
    try:
        conn = connect()
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="seekdb is not running")


def test_context_contains_real_required_sources():
    init_db(reset=True)
    context = build_context("volume discount quantity tax pricing")
    docs = {doc["kind"]: doc for doc in context["documents"]}
    assert docs["spec"]["path"] == "sample_app/specs/pricing.md"
    assert docs["code"]["path"] == "sample_app/src/pricing.py"
    assert docs["test"]["path"] == "sample_app/tests/test_pricing.py"
    assert "quantity >= 10" in docs["spec"]["content"]
    assert all(Path(ROOT / d["path"]).exists() for d in context["documents"])


def test_updated_spec_is_reimported_and_used():
    spec = ROOT / "sample_app" / "specs" / "pricing.md"
    original = spec.read_text(encoding="utf-8")
    marker = "\n## Marker from the spec-update test\n- The discount applies before tax.\n"
    try:
        spec.write_text(original + marker, encoding="utf-8")
        init_db()
        context = build_context("volume discount tax")
        spec_doc = next(d for d in context["documents"] if d["kind"] == "spec")
        assert "Marker from the spec-update test" in spec_doc["content"]
    finally:
        spec.write_text(original, encoding="utf-8")
        init_db()
