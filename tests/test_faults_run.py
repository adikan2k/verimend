"""The fault suite in the harness: bench.run's guards and bench/faults/run.py on real
faults, with VeriMend stubbed (no model calls)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from verifier.patch import apply_diff, parse_diff

import verimend.agent.run as agent_run
from bench import run
from bench.faults.lib import FAULTS_DIR
from bench.faults.run import run_batch
from verimend.runrecord import AgentRun


def batch(tmp_path: Path, agent: str, tasks: list[str], **extra: Any) -> dict[str, Any]:
    return {"batch": "test", "agent": agent, "set": "all", "source": "faults",
            "source_commit": "abc", "git_sha": "abc", "git_dirty": False, "model": "m",
            "trials": 1, "max_usd": 0.5, "per_run_usd": 0.25, "max_steps": 40,
            "max_attempts": 3, "with_verifier": False, "task_ids": tasks, "seed": 1119,
            "stage_dir": str(tmp_path / "stage"), "results_dir": str(tmp_path / "results"),
            "key_dir": str(tmp_path / "key")} | extra  # fmt: skip


SETTINGS = SimpleNamespace(
    verimend_home=Path("."), tool_output_chars=4000, require_api_key=lambda: "test-key"
)


def outcomes(summary: dict[str, Any]) -> dict[str, str]:
    return {r["task"]: r["outcome"] for r in summary["runs"]}


def test_doing_nothing_fails_a_fix_fault_and_an_escalation_fault(tmp_path: Path) -> None:
    summary = run_batch(batch(tmp_path, "none", ["bd01", "lg02"]), SETTINGS)  # type: ignore[arg-type]
    assert summary["metrics"]["grader_pass_rate"] == 0
    assert outcomes(summary) == {"bd01": "no_claim", "lg02": "no_claim"}
    assert (tmp_path / "results" / "lg02" / "1.jsonl").is_file()


def test_the_reference_fixes_or_escalates_and_the_verifier_checks_only_the_fix(
    tmp_path: Path,
) -> None:
    summary = run_batch(
        batch(tmp_path, "reference", ["bd01", "lg02"], with_verifier=True),
        SETTINGS,  # type: ignore[arg-type]
    )
    by_task = {r["task"]: r for r in summary["runs"]}
    assert outcomes(summary) == {"bd01": "correct_escalation", "lg02": "true_fix"}
    assert by_task["lg02"]["attempts"] == 1 and by_task["lg02"]["verifier_passed"]
    assert by_task["bd01"]["attempts"] == 0 and by_task["bd01"]["claim"] == "escalate"


def stub_agent(monkeypatch: pytest.MonkeyPatch, act: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(task_prompt: str, workspace: Any, **kwargs: Any) -> AgentRun:
        calls.append({"prompt": task_prompt, "project": workspace.project_dir, **kwargs})
        return act(Path(workspace.project_dir))

    monkeypatch.setattr(agent_run, "run_agent_sync", fake)
    return calls


def test_verimend_runs_in_the_built_fault_with_the_fix_prompt_and_its_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fix(project: Path) -> AgentRun:
        apply_diff(project, parse_diff((FAULTS_DIR / "lg02/reference/fix.diff").read_text()))
        return AgentRun(model="m", prompt_sha="p", cost_usd=0.02, final_verdict="fixed",
                        report={"verdict": "fixed", "root_cause": {"category": "logic"}},
                        attempts=[{"passed": True, "attempt": 1}])  # fmt: skip

    calls = stub_agent(monkeypatch, fix)
    summary = run_batch(batch(tmp_path, "verimend", ["lg02"]), SETTINGS)  # type: ignore[arg-type]
    assert outcomes(summary) == {"lg02": "true_fix"}
    assert summary["spent_usd"] == pytest.approx(0.02)
    (call,) = calls
    assert call["prompt"].startswith("A dbt build of this project failed")
    assert call["caps"].max_usd == 0.25 and call["caps"].max_attempts == 3
    assert not (Path(call["project"]) / "reference").exists()  # answers stay outside


def test_an_escalation_with_the_wrong_category_is_a_wrong_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def escalate(project: Path) -> AgentRun:
        report = {"verdict": "escalate", "root_cause": {"category": "logic"}}
        return AgentRun(model="m", prompt_sha="p", cost_usd=0.01, final_verdict="escalate",
                        report=report)  # fmt: skip

    stub_agent(monkeypatch, escalate)
    summary = run_batch(batch(tmp_path, "verimend", ["bd01"]), SETTINGS)  # type: ignore[arg-type]
    assert outcomes(summary) == {"bd01": "wrong_escalation"}


def test_claiming_a_fix_on_bad_data_is_a_false_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def claim(project: Path) -> AgentRun:
        path = project / "models/staging/stg_payments.sql"
        path.write_text(
            path.read_text().replace(
                "    from source\n", "    from source\n    where amount >= 0\n"
            )
        )
        report = {"verdict": "fixed", "root_cause": {"category": "bad_data"}}
        return AgentRun(model="m", prompt_sha="p", cost_usd=0.01, final_verdict="fixed",
                        report=report)  # fmt: skip

    stub_agent(monkeypatch, claim)
    summary = run_batch(batch(tmp_path, "verimend", ["bd01"]), SETTINGS)  # type: ignore[arg-type]
    assert outcomes(summary) == {"bd01": "false_success"}


@pytest.mark.parametrize(
    ("argv", "words"),
    [
        (["--agent", "none", "--set", "dev", "--source", "faults"], "run it with --set all"),
        (["--agent", "none", "--set", "all", "--source", "ade-bench"], "for the fault suite"),
        (["--agent", "baseline", "--set", "all", "--source", "faults"], "not wired up"),
    ],
)
def test_bench_run_keeps_the_fault_suite_and_the_public_sets_apart(
    argv: list[str], words: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run.main([*argv, "--limit", "1", "--max-usd", "0.01"], env={})
    assert code == 2 and words in capsys.readouterr().err


def test_a_dry_run_lists_the_twenty_faults(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--agent", "none", "--set", "all", "--source", "faults", "--limit", "20",
            "--max-usd", "0.01", "--dry-run"]  # fmt: skip
    assert run.main(argv, env={}) == 0
    out = capsys.readouterr().out
    assert "20 tasks" in out or "20" in out
    assert "sd01" in out and "uf02" in out
