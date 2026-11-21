"""The part of bench/replay.py that runs next to a task's container (in the Harbor and
ADE-bench runners) or on this machine: snapshot once, verify each recorded diff. Standard
library only, because the Harbor runner's venv has nothing else it needs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

FIVE = "row_accounting"


def load_replay_diffs(batch: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    return json.loads(Path(batch["replay_diffs"]).read_text(encoding="utf-8"))


def _append(out: Path, line: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


async def replay_task(verifier: Any, task: str, diffs: list[dict[str, str]], out: Path) -> None:
    """Snapshot once, then verify each diff against that snapshot. `verifier` is a
    verimend.tools.verify.WorkspaceVerifier for the task's untouched project."""
    started = time.monotonic()
    try:
        snapshot = await verifier.prepare()
    except Exception as error:
        _append(out, {"task": task, "snapshot_error": f"{type(error).__name__}: {error}"[:800]})
        return
    _append(out, {"task": task, "snapshot": snapshot,
                  "seconds": round(time.monotonic() - started, 1)})  # fmt: skip
    for entry in diffs:
        began = time.monotonic()
        line: dict[str, Any] = {"task": task, "hash": entry["hash"], "result": None, "error": None}
        try:
            result = await verifier.check_diff(entry["diff"], entry["hash"][:16])
            result.pop("diff", None)
            line["result"] = result
        except Exception as error:
            line["error"] = f"{type(error).__name__}: {error}"[:800]
        line["seconds"] = round(time.monotonic() - began, 1)
        _append(out, line)
        print(f"{task} {entry['hash'][:10]}: {_verdict_text(line)}", flush=True)


def _verdict_text(line: dict[str, Any]) -> str:
    result = line.get("result")
    if result is None:
        return f"error {line.get('error')}"
    five = next((c for c in result["checks"] if c["name"] == FIVE), None)
    accounting = "-" if five is None else ("pass" if five["passed"] else "FAIL")
    return f"verifier={result['failed_check'] or 'pass'} row_accounting={accounting}"
