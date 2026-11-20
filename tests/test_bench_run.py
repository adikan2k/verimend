"""bench/run.py and bench/tasksets.py: the guards, without Docker or a model."""

import json
from pathlib import Path

import pytest

import bench.run as run
from bench.agents import (
    BASELINE_TOOLS,
    CLI_BUDGET_FRACTION,
    AgentSpec,
    baseline_cli_flags,
    cli_budget_usd,
    get_agent,
)
from bench.claims import BASELINE_PROMPT_SUFFIX
from bench.tasksets import (
    SOURCES,
    TaskSetError,
    docker_path,
    list_task_ids,
    require_set_allowed,
    select_tasks,
    stage_tasks,
)

ADE = SOURCES["ade-bench"]

# --- held-out and vendor guards (rule 2) --------------------------------------------------


def test_heldout_is_refused_inside_claude_code() -> None:
    with pytest.raises(TaskSetError, match="Aditya"):
        require_set_allowed("heldout", env={"CLAUDECODE": "1"}, split_exists=True)


def test_heldout_is_allowed_outside_claude_code() -> None:
    assert require_set_allowed("heldout", env={}, split_exists=True) == "heldout"


def test_vendor_set_is_refused_once_the_split_exists() -> None:
    with pytest.raises(TaskSetError, match="split exists"):
        require_set_allowed("vendor", env={}, split_exists=True)
    assert require_set_allowed("vendor", env={}, split_exists=False) == "vendor"


def test_unknown_set_is_refused() -> None:
    with pytest.raises(TaskSetError):
        require_set_allowed("all", env={}, split_exists=False)


def test_run_refuses_heldout_from_claude_code_before_reading_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_listing(*_: object) -> list[str]:
        raise AssertionError("held-out tasks were listed")

    monkeypatch.setattr(run, "list_task_ids", no_listing)
    argv = ["--agent", "none", "--set", "heldout", "--limit", "1", "--max-usd", "1", "--dry-run"]
    assert run.main(argv, env={"CLAUDECODE": "1"}) == 2
    assert "never from inside Claude Code" in capsys.readouterr().err


# --- caps (rule 1) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "caps",
    [[], ["--limit", "3"], ["--max-usd", "1"], ["--limit", "0", "--max-usd", "1"]],
)
def test_run_refuses_a_batch_without_usable_caps(
    caps: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--agent", "none", "--set", "dev", "--dry-run", *caps]
    assert run.main(argv, env={}) == 2
    assert "--limit" in capsys.readouterr().err


def test_run_refuses_an_unavailable_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unbuilt = AgentSpec("later", "x", True, 0.1, "report", False, "built in phase 9")
    monkeypatch.setattr(run, "get_agent", lambda name: unbuilt)
    argv = ["--agent", "later", "--set", "dev", "--limit", "1", "--max-usd", "1", "--dry-run"]
    assert run.main(argv, env={}) == 2
    assert "built in phase 9" in capsys.readouterr().err


def test_dry_run_prints_the_plan_and_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "tasks"
    for task in ("b", "a", "c"):
        (root / task).mkdir(parents=True)
        (root / task / "task.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(run, "tasks_root", lambda *_: root)
    monkeypatch.setattr(run.SPLIT_MARKER.__class__, "exists", lambda self: False)
    argv = ["--agent", "none", "--set", "vendor", "--limit", "2", "--max-usd", "0.01", "--dry-run"]
    assert run.main(argv, env={}) == 0
    out = capsys.readouterr().out
    assert "2 tasks x 1 trials = 2 runs" in out
    assert "Estimated $0.00" in out
    assert "Tasks: a, b" in out


# --- tasks and staging -------------------------------------------------------------------


def make_tasks(root: Path, names: list[str]) -> None:
    for name in names:
        (root / name / "tests").mkdir(parents=True)
        (root / name / "task.yaml").write_text(name, encoding="utf-8")
    (root / "not_a_task").mkdir()


def test_list_select_and_stage(tmp_path: Path) -> None:
    root = tmp_path / "set"
    make_tasks(root, ["t2", "t1", "t3"])
    ids = list_task_ids(root, ADE)
    assert ids == ["t1", "t2", "t3"]
    assert select_tasks(ids, None, 2) == ["t1", "t2"]
    assert select_tasks(ids, ["t3", "t1"], 5) == ["t1", "t3"]
    with pytest.raises(TaskSetError, match="t9"):
        select_tasks(ids, ["t9"], 5)
    staged = stage_tasks(root, ["t1"], tmp_path / "stage")
    assert (staged / "t1" / "task.yaml").read_text(encoding="utf-8") == "t1"
    assert not (staged / "t2").exists()
    with pytest.raises(TaskSetError, match="already exists"):
        stage_tasks(root, ["t1"], tmp_path / "stage")


def test_docker_path() -> None:
    assert (
        docker_path(Path("C:\\Users\\x\\.verimend\\runs"))
        == "/run/desktop/mnt/host/c/Users/x/.verimend/runs"
    )
    assert docker_path(Path("D:/data")) == "/run/desktop/mnt/host/d/data"


# --- baseline wiring ---------------------------------------------------------------------


def test_baseline_plugin_set_is_ade_benchs_baseline_plus_the_verdict_line() -> None:
    config = json.loads(run.plugin_sets_text())
    (only,) = config["sets"]
    assert only["default"] is True
    assert only["allowed_tools"] == list(BASELINE_TOOLS)
    assert only["skills"] == [] and only["mcp_servers"] == {}
    assert only["prompt_suffix"] == BASELINE_PROMPT_SUFFIX


def test_baseline_flags_restrict_offered_tools_and_cap_the_run() -> None:
    flags = baseline_cli_flags(0.25, 40).split()
    assert flags[flags.index("--tools") + 1] == "Bash,Edit,Write,Read,Glob,Grep"
    assert flags[flags.index("--max-turns") + 1] == "40"
    assert float(flags[flags.index("--max-budget-usd") + 1]) == cli_budget_usd(0.25)
    i = flags.index("--disallowedTools")
    assert flags[i + 1 : i + 3] == ["WebSearch", "WebFetch"]


def test_cli_gets_less_than_the_real_run_cap() -> None:
    assert cli_budget_usd(0.25) == pytest.approx(0.25 * CLI_BUDGET_FRACTION)
    assert cli_budget_usd(0.25) < 0.25


def test_runner_command_mounts_home_at_the_docker_desktop_path(tmp_path: Path) -> None:
    home = Path("C:/Users/x/.verimend")
    command = run.runner_command(
        home=home,
        repo=Path("C:/repo"),
        checkout=home / "vendor" / "ade-bench",
        batch_file=home / "runs" / "b" / "batch.json",
        env={"DEFAULT_AGENT_TIMEOUT_SEC": "1200"},
    )
    assert f"{home}:/run/desktop/mnt/host/c/Users/x/.verimend" in command
    assert "DEFAULT_AGENT_TIMEOUT_SEC=1200" in command
    script = command[-1]
    assert (
        "python -m bench.ade.inner '/run/desktop/mnt/host/c/Users/x/.verimend/runs/b/batch.json'"
        in script
    )


def test_agent_registry() -> None:
    assert get_agent("none").model_calls is False
    assert get_agent("reference").fixed_claim() == "fixed"
    assert get_agent("baseline").fixed_claim() is None
    with pytest.raises(KeyError):
        get_agent("gpt")


@pytest.mark.parametrize("agent", ["verimend", "baseline"])
def test_with_verifier_is_only_for_the_free_agents(
    agent: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--agent", agent, "--set", "dev", "--limit", "1", "--max-usd", "1",
            "--with-verifier", "--dry-run"]  # fmt: skip
    assert run.main(argv, env={}) == 2
    assert "--with-verifier is for none and reference" in capsys.readouterr().err
