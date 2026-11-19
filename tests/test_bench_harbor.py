"""data-eng-bench via Harbor: reading its trial results, and the run.py wiring."""

from pathlib import Path

import pytest

import bench.run as run
from bench.grade import GradeError, grades_from_harbor_trials, index_harbor_trials
from bench.harbor.inner import harbor_config, records_from_trials


def trial(task: str, started: str, reward: float | None, exception: str | None = None) -> dict:
    return {
        "task_id": {"path": f"tasks/{task}"},
        "started_at": started,
        "agent_execution": {"started_at": started, "finished_at": started.replace(":00Z", ":30Z")},
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
        "exception_info": {"exception_type": exception} if exception else None,
        "reward_valid": True,
    }


TRIALS = [
    trial("b", "2026-11-19T10:05:00Z", 1.0),
    trial("a", "2026-11-19T10:02:00Z", 0.5),
    trial("a", "2026-11-19T10:01:00Z", 1.0),
    trial("c", "2026-11-19T10:03:00Z", None, "RuntimeError"),
]


def test_trials_are_numbered_in_start_order_per_task() -> None:
    assert [(t, n) for t, n, _ in index_harbor_trials(TRIALS)] == [
        ("a", 1), ("a", 2), ("b", 1), ("c", 1),
    ]  # fmt: skip


def test_only_a_full_reward_passes() -> None:
    grades = {(g.task_id, g.trial): g for g in grades_from_harbor_trials(TRIALS)}
    assert grades[("a", 1)].passed is True
    assert grades[("a", 2)].passed is False  # partial score is not a fix
    assert grades[("b", 1)].passed is True
    assert grades[("c", 1)].passed is False
    assert grades[("c", 1)].failure_mode == "RuntimeError"
    assert grades[("a", 1)].grader == "data-eng-bench"


def test_a_trial_without_a_task_path_is_an_error() -> None:
    with pytest.raises(GradeError):
        grades_from_harbor_trials([{"started_at": "x"}])


def test_records_carry_agent_time() -> None:
    records = records_from_trials(TRIALS)
    assert [(r.task, r.trial, r.seconds) for r in records][:2] == [("a", 1, 30.0), ("a", 2, 30.0)]


def test_harbor_config_uses_harbors_own_agents_on_duckdb() -> None:
    config = harbor_config({"batch": "b", "trials": 3, "agent": "reference"})
    assert config["agents"] == [{"name": "oracle"}]
    assert config["n_attempts"] == 3
    assert "DB_TYPE=duckdb" in config["environment"]["env"]
    assert harbor_config({"batch": "b", "trials": 1, "agent": "none"})["agents"] == [
        {"name": "nop"}
    ]


def test_runner_command_for_harbor_installs_the_pinned_harbor(tmp_path: Path) -> None:
    home = Path("C:/Users/x/.verimend")
    command = run.runner_command(
        home=home, repo=Path("C:/repo"), checkout=home / "vendor" / "data-eng-bench",
        batch_file=home / "runs" / "b" / "batch.json", env={}, runner="harbor",
    )  # fmt: skip
    script = command[-1]
    assert f"'harbor=={run.HARBOR_VERSION}'" in script
    assert "-e /verimend" not in script  # dbt-core and Harbor cannot share a venv
    assert "python -m bench.harbor.inner" in script
    assert f"{run.VENV_VOLUME}-harbor:/opt/venv" in command


HARBOR_VENV = {"harbor", "claude_agent_sdk", "pydantic", "pydantic_settings"}


def harbor_side_imports(*roots: str) -> dict[str, set[str]]:
    """Third-party packages imported anywhere reachable from `roots`, by importing module.

    bench/ and verimend/ come from PYTHONPATH in the Harbor venv; everything else must be
    installed there, and dbt-core cannot be (its pathspec pin conflicts with Harbor's).
    """
    import ast
    import sys

    repo = Path(__file__).resolve().parents[1]
    todo, seen = list(roots), set()
    found: dict[str, set[str]] = {}
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        path = repo / (module.replace(".", "/") + ".py")
        if not path.exists():
            path = repo / module.replace(".", "/") / "__init__.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                top = name.split(".")[0]
                if top in {"bench", "verimend"}:
                    todo.append(name)
                elif top not in sys.stdlib_module_names:
                    found.setdefault(top, set()).add(module)
    return found


def test_harbor_side_needs_only_what_the_harbor_venv_has() -> None:
    imports = harbor_side_imports("bench.harbor.inner")
    assert set(imports) <= HARBOR_VENV, imports
    assert "dbt" not in imports and "duckdb" not in imports


def test_harbor_import_check_sees_third_party_imports() -> None:
    imports = harbor_side_imports("bench.ade.inner")  # the ADE side imports ade_bench
    assert "ade_bench" in imports


def test_run_refuses_an_agent_not_wired_for_the_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--agent", "baseline", "--set", "dev", "--source", "data-eng-bench",
            "--limit", "1", "--max-usd", "0.1", "--dry-run"]  # fmt: skip
    assert run.main(argv, env={}) == 2
    assert "not wired up for data-eng-bench" in capsys.readouterr().err
