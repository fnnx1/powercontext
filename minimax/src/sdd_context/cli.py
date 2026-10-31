"""Command-line entry point for the SDD context demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import db
from .context_builder import build_context
from .minimax_client import generate
from .power_state import history, latest_state, save_state

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
DEFAULT_TASK = (
    "Implement the volume discount for pricing: lines with quantity >= 10 "
    "get 10% off before tax, while small quantities and existing tax behaviour stay unchanged."
)


def _run_tests(label: str) -> dict:
    RUNS.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "sample_app/tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    (RUNS / f"test-{label}.log").write_text(
        f"$ python -m pytest -q sample_app/tests\n{output}\nexit_code={proc.returncode}\n"
    )
    return {"label": label, "exit_code": proc.returncode, "tail": output[-1200:]}


def cmd_init(args) -> None:
    imported = db.init_db(reset=args.reset)
    print(json.dumps({"database": db.DB_CONFIG["database"], "imported": imported}, indent=2))


def cmd_context(args) -> None:
    context = build_context(args.task)
    ref = asyncio.run(
        save_state(
            {
                "task": args.task,
                "status": "context_ready",
                "context_hash": context["context_hash"],
                "required": context["required"],
                "documents": [d["path"] for d in context["documents"]],
            }
        )
    )
    print(json.dumps({"context": context, "powercontext": ref}, ensure_ascii=False, indent=2))


def cmd_generate(args) -> None:
    RUNS.mkdir(exist_ok=True)
    baseline = ROOT / "sample_app" / "src" / "pricing.py"
    (RUNS / "before.py").write_text(baseline.read_text(encoding="utf-8"), encoding="utf-8")
    context = build_context(args.task)
    asyncio.run(
        save_state(
            {
                "task": args.task,
                "status": "context_ready",
                "context_hash": context["context_hash"],
                "required": context["required"],
            }
        )
    )
    result = generate(args.task, context)
    tests = _run_tests("after")
    ref = asyncio.run(
        save_state(
            {
                "task": args.task,
                "status": "code_changed",
                "context_hash": context["context_hash"],
                "required": context["required"],
                "minimax": result,
                "tests": tests,
            }
        )
    )
    print(json.dumps({"minimax": result, "tests": tests, "powercontext": ref}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if tests["exit_code"] == 0 else 1)


def cmd_test(args) -> None:
    result = _run_tests(args.label)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["exit_code"] == 0 else 1)


def cmd_state(args) -> None:
    print(json.dumps(asyncio.run(latest_state()), ensure_ascii=False, indent=2))


def cmd_history(args) -> None:
    print(json.dumps(asyncio.run(history()), ensure_ascii=False, indent=2))


def cmd_reset(args) -> None:
    # Restore the pre-change snapshot.  The submitted repo keeps the final
    # modified code (so `pytest` passes on a clean checkout); this command
    # recreates the failing baseline for the before/after demonstration.
    baseline = RUNS / "before.py"
    if baseline.exists():
        shutil.copyfile(baseline, ROOT / "sample_app" / "src" / "pricing.py")
        print("reset sample_app/src/pricing.py from runs/before.py")
        return
    subprocess.run(
        ["git", "checkout", "--", "minimax/sample_app/src/pricing.py"],
        cwd=ROOT.parent,
        check=True,
    )
    print("reset sample_app/src/pricing.py from git")


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniMax SDD context demo")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the seekdb schema and import materials")
    p.add_argument("--reset", action="store_true")
    p.set_defaults(func=cmd_init)

    for name, func, helptext in (
        ("context", cmd_context, "build and persist the task context"),
        ("generate", cmd_generate, "call MiniMax, apply the code change and test"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--task", default=DEFAULT_TASK)
        p.set_defaults(func=func)

    p = sub.add_parser("test", help="run the sample app tests")
    p.add_argument("--label", default="manual")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("state", help="read the latest PowerContext state in a new process")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("history", help="list all saved PowerContext states")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("reset", help="restore the baseline pricing.py from git")
    p.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
