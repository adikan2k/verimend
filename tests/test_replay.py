"""bench/replay.py on hand-built batches: what it collects, what it refuses, how it counts.
No containers and no dbt: the verifier is a stub."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bench import replay
from bench.run import main as bench_run

DIFF_A = "--- a/models/m.sql\n+++ b/models/m.sql\n@@ -1 +1 @@\n-select 1\n+select 2\n"


def write_batch(results: Path, name: str, source: str, agent: str, runs: list[dict]) -> None:
    folder = results / name
    folder.mkdir(parents=True)
    summary = {"source": source, "agent": agent,
               "runs": [{k: v for k, v in r.items() if k != "attempts"} for r in runs]}  # fmt: skip
    (folder / "summary.json").write_text(json.dumps(summary), "utf-8")
    for run in runs:
        trace = folder / run["task"] / f"{run['trial']}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        events = [{"event": "run_start"}] + [
            {"event": "attempt_end", "attempt": i + 1, **a} for i, a in enumerate(run["attempts"])
        ]
        trace.write_text("".join(json.dumps(e) + "\n" for e in events), "utf-8")


def fixed(task: str, trial: int, grader: bool, *attempts: dict) -> dict[str, Any]:
    outcome = "true_fix" if grader else "false_success"
    return {"task": task, "trial": trial, "claim": "fixed", "grader_passed": grader,
            "outcome": outcome, "attempts": list(attempts)}  # fmt: skip


def attempt(diff: str, failed: str | None = None) -> dict[str, Any]:
    return {"diff": diff, "passed": failed is None, "failed_check": failed}


@pytest.fixture
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(replay, "allowed_tasks", lambda source: {"bd01", "tt02"})
    root = tmp_path / "results"
    write_batch(root, "base-x3", "faults", "baseline", [
        fixed("bd01", 1, False, attempt(DIFF_A)),
        # The same diff with Windows line endings and trailing spaces: one unique diff.
        fixed("bd01", 2, False, attempt(DIFF_A.replace("\n", "  \r\n"))),
        fixed("tt02", 1, True, attempt("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")),
        {"task": "tt02", "trial": 2, "claim": "escalate", "grader_passed": True, "attempts": []},
        fixed("tt02", 3, True),  # claimed fixed but no verifier ran: no diff to replay
    ])  # fmt: skip
    write_batch(root, "vm-x3", "faults", "verimend", [
        # Rejected once, then passed: the final attempt is the one the grader saw.
        fixed("bd01", 1, True, attempt("--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+c\n", "masking"),
              attempt(DIFF_A)),
    ])  # fmt: skip
    return root


def test_collect_takes_final_diffs_of_claimed_fixes_and_dedupes(results: Path) -> None:
    collected = replay.collect("faults", ["base-x3", "vm-x3"], results)
    assert collected["skipped"] == {"base-x3: no recorded diff": 1}
    (only,) = collected["tasks"]["bd01"].values()
    assert [(o["batch"], o["trial"], o["grader_passed"]) for o in only["occurrences"]] == [
        ("base-x3", 1, False), ("base-x3", 2, False), ("vm-x3", 1, True),
    ]  # fmt: skip
    assert len(collected["tasks"]["tt02"]) == 1


def test_collect_refuses_tasks_outside_the_development_set(results: Path) -> None:
    write_batch(results, "other", "faults", "baseline", [fixed("zz99", 1, True, attempt("d"))])
    with pytest.raises(ValueError, match="not in the development set"):
        replay.collect("faults", ["other"], results)
    with pytest.raises(ValueError, match="not data-eng-bench"):
        replay.collect("data-eng-bench", ["base-x3"], results)


def test_the_real_split_allows_only_development_tasks() -> None:
    split = json.loads((replay.REPO_ROOT / "bench" / "split.json").read_text("utf-8"))
    for source in ("ade-bench", "data-eng-bench"):
        assert replay.allowed_tasks(source) == set(split["sources"][source]["dev"])


def checks(*failed: str, five: bool | None = True, detail: str = "ok") -> dict[str, Any]:
    names = ["tamper", "masking", "clean_rebuild", "collateral"]
    found = [{"name": n, "passed": n not in failed, "detail": "d"} for n in names]
    if five is not None:
        found.append({"name": "row_accounting", "passed": five, "detail": detail})
    first = next((c["name"] for c in found if not c["passed"]), None)
    return {"passed": first is None, "failed_check": first, "checks": found}


def test_summarise_counts_reproduction_and_both_gates(results: Path) -> None:
    collected = replay.collect("faults", ["base-x3", "vm-x3"], results)
    (bd01,) = collected["tasks"]["bd01"]
    (tt02,) = collected["tasks"]["tt02"]
    lines = [
        {"task": "bd01", "snapshot": {"ok": True}},
        # Passes the four checks as recorded; row accounting stops it.
        {"task": "bd01", "hash": bd01, "result": checks(five=False, detail="lost 3 rows")},
        # Recorded as passing, replayed as a collateral failure: not reproduced.
        {"task": "tt02", "hash": tt02,
         "result": checks("collateral", five=True, detail="1 model analysed; not analysed: x")},
    ]  # fmt: skip
    for entries in collected["tasks"].values():  # as if all were recorded with today's checks
        for entry in entries.values():
            for occ in entry["occurrences"]:
                occ["recorded_with_four_checks"] = True
    summary = replay.summarise(collected, lines)
    reproduced = summary["reproduced"]
    assert (reproduced["same"], reproduced["different"], reproduced["share_different"]) == (
        3, 1, 0.25,
    )  # fmt: skip
    assert [o["task"] for o in reproduced["not_reproduced"]] == ["tt02"]
    assert summary["gate_four_checks"] == {"right_let_through": 1, "right_blocked": 1,
                                           "wrong_let_through": 2, "wrong_stopped": 0}  # fmt: skip
    gate = summary["gate_with_row_accounting"]
    assert gate == {"right_let_through": 0, "right_blocked": 2,
                    "wrong_let_through": 0, "wrong_stopped": 2}  # fmt: skip
    newly = summary["newly_stopped_by_row_accounting"]
    assert [(n["batch"], n["grader_passed"]) for n in newly] == [
        ("base-x3", False), ("base-x3", False), ("vm-x3", True),
    ]  # fmt: skip
    assert summary["diffs_with_something_not_analysed"] == 1 and summary["not_replayed"] == 0


class StubVerifier:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    async def prepare(self) -> dict[str, Any]:
        self.calls.append("prepare")
        return {"ok": True}

    async def check_diff(self, diff: str, name: str) -> dict[str, Any]:
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError("dbt fell over")
        return {**checks(), "diff": diff}


def test_replay_task_snapshots_once_and_records_every_diff(tmp_path: Path) -> None:
    stub = StubVerifier(fail_on="b" * 16)
    out = tmp_path / "replay.jsonl"
    diffs = [{"hash": "a" * 64, "diff": "d1"}, {"hash": "b" * 64, "diff": "d2"}]
    asyncio.run(replay.replay_task(stub, "bd01", diffs, out))
    assert stub.calls == ["prepare", "a" * 16, "b" * 16]
    lines = replay.read_lines(out)
    assert "snapshot" in lines[0]
    assert lines[1]["result"]["passed"] and "diff" not in lines[1]["result"]
    assert lines[2]["result"] is None and "dbt fell over" in lines[2]["error"]


def test_bench_run_takes_replay_diffs_only_with_the_replay_agent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = ["--set", "dev", "--source", "ade-bench", "--limit", "1", "--max-usd", "0.01"]
    assert bench_run(["--agent", "replay", *base, "--dry-run"]) == 2
    assert "--replay-diffs goes with --agent replay" in capsys.readouterr().err
    assert bench_run(["--agent", "none", *base, "--replay-diffs", "x.json", "--dry-run"]) == 2
    assert bench_run(["--agent", "replay", *base, "--replay-diffs", "x.json", "--dry-run"]) == 0


def test_only_batches_recorded_with_the_four_checks_count_for_reproduction() -> None:
    assert replay.verifier_tree("3a6a697") == replay.FOUR_CHECKS_TREE  # faults-baseline-x3
    assert replay.verifier_tree("cbd6178") != replay.FOUR_CHECKS_TREE  # phase 3's verifier
    assert replay.verifier_tree(None) is None
