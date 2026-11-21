"""Apply a fault's reference: a fix.diff against the faulted project, or an escalation.

The reference agent of the fault suite, and what `python -m bench.faults check` grades.
Diffs go through the verifier's applier (the same one `verimend verify` uses); grading
never does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from verifier.patch import apply_diff, parse_diff

from bench.faults.lib import Expected, FaultError, Materialized


def apply_reference(built: Materialized, fault_dir: Path) -> dict[str, Any]:
    """Change `built.project` as the reference does and return the reference's report."""
    reference = fault_dir / "reference"
    fix, escalation = reference / "fix.diff", reference / "escalation.json"
    expected: Expected = built.expected
    if expected.outcome == "fix":
        if not fix.is_file():
            raise FaultError(f"{fault_dir.name}: a fix fault needs reference/fix.diff")
        apply_diff(built.project, parse_diff(fix.read_text(encoding="utf-8")))
        return {
            "verdict": "fixed",
            "root_cause": {"category": expected.categories[0], "summary": "reference fix"},
        }
    if not escalation.is_file():
        raise FaultError(f"{fault_dir.name}: an escalate fault needs reference/escalation.json")
    return json.loads(escalation.read_text(encoding="utf-8"))
