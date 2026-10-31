"""MiniMax API client and code-generation step."""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
CODE = ROOT / "sample_app" / "src" / "pricing.py"
MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
API_URL = os.getenv(
    "MINIMAX_API_URL", "https://api.minimaxi.com/v1/text/chatcompletion_v2"
)


def _key() -> str:
    key = os.getenv("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    fallback = Path("/tmp/minimax_api_key")
    if fallback.exists():
        return fallback.read_text().strip()
    raise RuntimeError("Set MINIMAX_API_KEY (do not commit it).")


def _prompt(context: dict[str, Any]) -> str:
    spec = next(d["content"] for d in context["documents"] if d["kind"] == "spec")
    code = next(d["content"] for d in context["documents"] if d["kind"] == "code")
    tests = next(d["content"] for d in context["documents"] if d["kind"] == "test")
    return f"""You are implementing a small, local change in a Python business project.
Use only the context below. Return ONLY the complete new content of pricing.py.
Do not use Markdown fences. Do not change the public function name or signature.

TASKS: {context['task']}

SPEC:
{spec}

CURRENT CODE:
{code}

TESTS:
{tests}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    # Some models prefix a short sentence even when instructed not to.
    start = text.find("def order_total")
    if start > 0:
        text = text[start:]
    return text.strip() + "\n"


def generate(task: str, context: dict[str, Any]) -> dict[str, Any]:
    prompt = _prompt(context)
    key = _key()
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 2000,
    }
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("base_resp", {}).get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax error: {body.get('base_resp')}")
    message = body["choices"][0]["message"]
    content = _strip_fences(message.get("content", ""))
    if "def order_total" not in content:
        raise RuntimeError("MiniMax response does not contain def order_total")
    before = CODE.read_text(encoding="utf-8")
    CODE.write_text(content, encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile="sample_app/src/pricing.py (before)",
            tofile="sample_app/src/pricing.py (after)",
        )
    )
    RUNS.mkdir(exist_ok=True)
    (RUNS / "minimax_call.json").write_text(
        json.dumps(
            {
                "model": body.get("model"),
                "id": body.get("id"),
                "usage": body.get("usage"),
                "base_resp": body.get("base_resp"),
                "prompt_sha256": __import__("hashlib").sha256(prompt.encode()).hexdigest(),
                "response_sha256": __import__("hashlib").sha256(content.encode()).hexdigest(),
                "reasoning_content": message.get("reasoning_content", "")[:500],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (RUNS / "minimax_response.txt").write_text(content, encoding="utf-8")
    (RUNS / "minimax_change.diff").write_text(diff, encoding="utf-8")
    code_check = subprocess.run(
        [sys.executable, "-m", "py_compile", "sample_app/src/pricing.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if code_check.returncode != 0:
        raise RuntimeError(code_check.stderr)
    return {
        "model": body.get("model"),
        "request_id": body.get("id"),
        "usage": body.get("usage"),
        "diff_path": "runs/minimax_change.diff",
    }
