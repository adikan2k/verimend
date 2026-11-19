"""scripts/split_bench.py on fixture task folders. Never touches the real bench/ folder."""

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from scripts.split_bench import SplitError, apply_split, main, plan_source, plan_split

TASKS = [f"t{i:02d}" for i in range(18)]
PINNED = ["t03", "t07", "t11"]


def test_plan_is_deterministic_for_a_seed() -> None:
    a = plan_source("ade-bench", TASKS, PINNED, seed=1119)
    b = plan_source("ade-bench", list(reversed(TASKS)), PINNED, seed=1119)
    assert a == b
    c = plan_source("ade-bench", TASKS, PINNED, seed=7)
    assert c.dev != a.dev  # a different seed gives a different split


def test_plan_covers_every_task_once_with_pinned_in_dev() -> None:
    plan = plan_source("ade-bench", TASKS, PINNED, seed=1119)
    assert set(plan.dev) | set(plan.heldout) == set(TASKS)
    assert not set(plan.dev) & set(plan.heldout)
    assert set(PINNED) <= set(plan.dev)
    assert len(plan.dev) == round(0.4 * 18) == 7


def test_pinned_count_can_exceed_the_fraction() -> None:
    plan = plan_source("s", ["a", "b", "c"], ["a", "b"], seed=1, fraction=0.4)
    assert plan.dev == ["a", "b"] and plan.heldout == ["c"]


def test_sources_split_independently() -> None:
    a = plan_source("ade-bench", TASKS, [], seed=1119)
    b = plan_source("data-eng-bench", TASKS, [], seed=1119)
    assert a.dev != b.dev


def test_bad_inputs_are_refused() -> None:
    with pytest.raises(SplitError):
        plan_source("s", ["a", "a"], [], seed=1)
    with pytest.raises(SplitError):
        plan_source("s", ["a"], [], seed=1, fraction=1.0)


def validation(commit: str, included: list[str], excluded: list[str]) -> dict:
    candidates = [{"task_id": t, "included": True, "excluded_reason": None} for t in included]
    candidates += [
        {"task_id": t, "included": False, "excluded_reason": "reference failed"} for t in excluded
    ]
    return {"sources": {"ade-bench": {"commit": commit, "candidates": candidates}}}


@pytest.fixture
def vendor(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "vendor" / "ade-bench"
    for task in [*TASKS, "broken"]:
        (checkout / "tasks" / task).mkdir(parents=True)
        (checkout / "tasks" / task / "task.yaml").write_text(task, encoding="utf-8")
    (checkout / "LICENSE").write_text("Apache-2.0", encoding="utf-8")
    run = {"cwd": checkout, "check": True, "capture_output": True}
    subprocess.run(["git", "init", "-q"], **run)
    subprocess.run(["git", "add", "."], **run)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], **run
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tmp_path / "vendor", commit


def test_apply_split_copies_tasks_and_never_records_heldout_ids(
    tmp_path: Path, vendor: tuple[Path, str]
) -> None:
    vendor_dir, commit = vendor
    bench = tmp_path / "bench"
    data = validation(commit, TASKS, ["broken"])
    plans = plan_split(data, {"ade-bench": PINNED}, 1119, 0.4)
    record = apply_split(
        plans,
        data,
        bench=bench,
        vendor=vendor_dir,
        seed=1119,
        fraction=0.4,
        today=date(2026, 11, 20),
    )
    dev = sorted(p.name for p in (bench / "dev" / "ade-bench").iterdir() if p.is_dir())
    held = sorted(p.name for p in (bench / "heldout" / "ade-bench").iterdir() if p.is_dir())
    assert dev == record["sources"]["ade-bench"]["dev"]
    assert len(held) == record["sources"]["ade-bench"]["heldout_count"] == 11
    assert "broken" not in dev + held
    for folder in ("dev", "heldout"):
        assert (bench / folder / "ade-bench" / "LICENSE").is_file()
        meta = json.loads((bench / folder / "ade-bench" / "SOURCE.json").read_text("utf-8"))
        assert meta["commit"] == commit
    marker = (bench / "split.json").read_text(encoding="utf-8")
    assert not any(task in marker for task in held)
    assert json.loads(marker)["sources"]["ade-bench"]["excluded"] == {"broken": "reference failed"}


def test_apply_split_runs_once(tmp_path: Path, vendor: tuple[Path, str]) -> None:
    vendor_dir, commit = vendor
    bench = tmp_path / "bench"
    data = validation(commit, TASKS, [])
    plans = plan_split(data, {}, 1119, 0.4)
    kwargs = {"bench": bench, "vendor": vendor_dir, "seed": 1119, "fraction": 0.4,
              "today": date(2026, 11, 20)}  # fmt: skip
    apply_split(plans, data, **kwargs)
    with pytest.raises(SplitError, match="runs once"):
        apply_split(plans, data, **kwargs)


def test_apply_split_refuses_a_checkout_at_another_commit(
    tmp_path: Path, vendor: tuple[Path, str]
) -> None:
    vendor_dir, _ = vendor
    data = validation("0" * 40, TASKS, [])
    with pytest.raises(SplitError, match="commit"):
        apply_split(
            plan_split(data, {}, 1119, 0.4), data, bench=tmp_path / "bench", vendor=vendor_dir,
            seed=1119, fraction=0.4, today=date(2026, 11, 20),
        )  # fmt: skip


def test_main_refuses_to_run_inside_claude_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--seed", "1119", "--validation", str(tmp_path / "v.json"),
            "--bench", str(tmp_path / "bench")]  # fmt: skip
    assert main(argv, env={"CLAUDECODE": "1"}) == 2
    assert "never from inside Claude Code" in capsys.readouterr().err
    assert not (tmp_path / "bench").exists()


def test_main_prints_dev_ids_but_not_heldout_ids(
    tmp_path: Path, vendor: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    vendor_dir, commit = vendor
    (tmp_path / "validation.json").write_text(json.dumps(validation(commit, TASKS, [])), "utf-8")
    (tmp_path / "pinned.json").write_text(json.dumps({"ade-bench": PINNED}), "utf-8")
    argv = ["--seed", "1119", "--validation", str(tmp_path / "validation.json"),
            "--pinned", str(tmp_path / "pinned.json"), "--bench", str(tmp_path / "bench"),
            "--vendor", str(vendor_dir)]  # fmt: skip
    assert main(argv, env={}) == 0
    out = capsys.readouterr().out
    held = [p.name for p in (tmp_path / "bench" / "heldout" / "ade-bench").iterdir() if p.is_dir()]
    assert held and not any(task in out for task in held)
    assert "7 dev, 11 held out" in out
