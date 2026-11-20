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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataclasses import replace

    from bench.tasksets import get_source

    narrow = replace(get_source("data-eng-bench"), agents=("none",))
    monkeypatch.setattr(run, "get_source", lambda name: narrow)
    argv = ["--agent", "baseline", "--set", "dev", "--source", "data-eng-bench",
            "--limit", "1", "--max-usd", "0.1", "--dry-run"]  # fmt: skip
    assert run.main(argv, env={}) == 2
    assert "not wired up for data-eng-bench" in capsys.readouterr().err


# --- our agents on Harbor ----------------------------------------------------------------


def test_harbor_config_loads_our_agents_by_import_path() -> None:
    batch = {"batch": "b", "trials": 1, "agent": "verimend", "agent_timeout_sec": 1200}
    (entry,) = harbor_config(batch, "/stage/batch.json")["agents"]
    assert entry["import_path"] == "bench.harbor.agents:VerimendHarborAgent"
    assert entry["kwargs"] == {"batch_file": "/stage/batch.json"}
    assert entry["override_timeout_sec"] == 1200.0
    (base,) = harbor_config({**batch, "agent": "baseline"}, "x")["agents"]
    assert base["import_path"] == "bench.harbor.agents:BaselineHarborAgent"


def test_harbor_side_agents_need_only_the_harbor_venv() -> None:
    imports = harbor_side_imports("bench.harbor.inner", "bench.harbor.agents")
    assert set(imports) <= HARBOR_VENV, imports


def test_ledger_carries_spend_between_trials_and_stops_the_batch(tmp_path: Path) -> None:
    from bench.harbor.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.json", max_usd=0.30)
    assert ledger.next_run_cap(0.25) == 0.25
    ledger.record(0.20)
    again = Ledger(tmp_path / "ledger.json", max_usd=0.30)  # the next trial, a new object
    assert again.spent_usd() == pytest.approx(0.20)
    assert again.next_run_cap(0.25) == pytest.approx(0.10)
    again.record(0.10)
    assert again.next_run_cap(0.25) is None


def test_baseline_command_matches_the_ade_baseline() -> None:
    from bench.agents import BASELINE_TOOLS, baseline_cli_flags
    from bench.claims import BASELINE_PROMPT_SUFFIX
    from bench.harbor.records import baseline_command

    command = baseline_command("Fix the 'model'.", "claude-haiku-5-5", 0.25, 40)
    assert command.startswith("claude --output-format stream-json --verbose -p ")
    assert BASELINE_PROMPT_SUFFIX in command
    assert f"--model claude-haiku-5-5 --allowedTools {' '.join(BASELINE_TOOLS)}" in command
    assert baseline_cli_flags(0.25, 40) in command
    assert command.endswith("2>&1 | tee /tmp/agent_output.log")


def test_verimend_prompt_tells_it_the_backend_and_project() -> None:
    from bench.harbor.records import PROJECT_DIR, verimend_prompt

    prompt = verimend_prompt("Fix the report.")
    assert prompt.endswith("Fix the report.")
    assert "duckdb" in prompt and PROJECT_DIR in prompt and "no shell" in prompt


def test_records_from_trials_attach_our_record(tmp_path: Path) -> None:
    from bench.harbor.inner import TRIAL_DIR
    from bench.harbor.records import write_record
    from verimend.runrecord import AgentRun

    trial_dir = tmp_path / "job" / "a__1"
    run_record = AgentRun(model="m", prompt_sha="s", report={"verdict": "fixed"}, cost_usd=0.01)
    write_record(trial_dir / "agent", run_cap_usd=0.25, seconds=12.5,
                 agent_run=run_record.to_dict())  # fmt: skip
    with_dir = {**trial("a", "2026-11-19T10:01:00Z", 1.0), TRIAL_DIR: str(trial_dir)}
    (record,) = records_from_trials([with_dir])
    assert record.agent_run == run_record
    assert record.run_cap_usd == 0.25 and record.seconds == 12.5
    skipped = tmp_path / "job" / "b__1"
    write_record(skipped / "agent", stopped="batch_cap", run_cap_usd=None)
    (stopped,) = records_from_trials([{**trial("b", "2026-11-19T10:02:00Z", 0.0),
                                       TRIAL_DIR: str(skipped)}])  # fmt: skip
    assert stopped.stopped == "batch_cap" and stopped.agent_run is None


def test_harbor_workspace_runs_commands_in_the_project() -> None:
    import asyncio
    from types import SimpleNamespace

    from bench.harbor.workspace import HarborWorkspace

    class FakeEnvironment:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def exec(self, command: str, **kwargs: object) -> SimpleNamespace:
            self.calls.append((command, kwargs))
            out = "/data/retail.duckdb\n" if command == "printenv DUCKDB_PATH" else "ok"
            return SimpleNamespace(return_code=0, stdout=out, stderr="")

    env = FakeEnvironment()
    ws = asyncio.run(HarborWorkspace.open(env))
    assert ws.db_path == "/data/retail.duckdb"
    assert ws.project_dir == "/app/dbt_models_duckdb"
    result = asyncio.run(ws.exec(["dbt", "build", "--select", "a b"], timeout=60))
    command, kwargs = env.calls[-1]
    assert command == "timeout 60 dbt build --select 'a b'"
    assert kwargs["cwd"] == "/app/dbt_models_duckdb"
    assert result.code == 0 and result.output == "ok"
