"""scripts/validate_tasks.py: which tasks the pre-split check keeps."""

from pathlib import Path

import pytest

from scripts.validate_tasks import ade_candidates, build_validation, decide


@pytest.mark.parametrize(
    ("reference", "none", "kept", "reason"),
    [
        (True, False, True, None),
        (False, False, False, "reference solution fails"),
        (True, True, False, "passes without any change"),
        (None, False, False, "missing result"),
        (True, None, False, "missing result"),
    ],
)
def test_decide(reference: bool | None, none: bool | None, kept: bool, reason: str | None) -> None:
    assert decide(reference, none) == (kept, reason)


def test_ade_candidates_are_ready_debugging_tasks_plus_pinned(tmp_path: Path) -> None:
    tasks = {
        "fix_ready": "status: ready\ntags: [dbt, debugging]\n",
        "fix_draft": "status: draft\ntags: [debugging]\n",
        "build_ready": "status: ready\ntags: [model-creation]\n",
        "pinned_build": "status: ready\ntags: [analysis]\n",
    }
    for name, text in tasks.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "task.yaml").write_text(text, encoding="utf-8")
    assert ade_candidates(tmp_path, ["pinned_build"]) == ["fix_ready", "pinned_build"]


def test_build_validation_matches_the_split_input() -> None:
    v = build_validation("ade-bench", "abc", ["a", "b"], {"a": True, "b": False}, {"a": False})
    (a, b) = v["sources"]["ade-bench"]["candidates"]
    assert a == {"task_id": "a", "reference": True, "none": False, "included": True,
                 "excluded_reason": None}  # fmt: skip
    assert b["included"] is False and b["excluded_reason"] == "reference solution fails"
