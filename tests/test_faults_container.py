"""The plain-agent baseline on the fault suite, with the container stubbed: what gets
mounted, where the key goes, how its reply is read, and the verifier watching it."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bench.claims import parse_category
from bench.faults import container
from bench.faults.run import baseline_report, run_batch

SETTINGS = SimpleNamespace(
    verimend_home=Path("."), tool_output_chars=4000, require_api_key=lambda: "test-key"
)


def stream(text: str, cost: float = 0.02) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": text,
                       "total_cost_usd": cost, "num_turns": 7})  # fmt: skip


def batch(tmp_path: Path, tasks: list[str]) -> dict[str, Any]:
    return {"batch": "test", "agent": "baseline", "set": "all", "source": "faults",
            "source_commit": "abc", "git_sha": "abc", "git_dirty": False, "model": "m",
            "trials": 1, "max_usd": 0.5, "per_run_usd": 0.25, "max_steps": 40,
            "max_attempts": 1, "with_verifier": True, "task_ids": tasks, "seed": 1119,
            "stage_dir": str(tmp_path / "stage"), "results_dir": str(tmp_path / "results"),
            "key_dir": str(tmp_path / "key")}  # fmt: skip


def stub_container(monkeypatch: pytest.MonkeyPatch, act: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(project: Path, db_dir: Path, profiles: Path, command: str, api_key: str | None,
             timeout: float = 0) -> tuple[str, float]:  # fmt: skip
        calls.append({"project": project, "db_dir": db_dir, "profiles": profiles,
                      "command": command, "api_key": api_key})  # fmt: skip
        return act(project), 12.5

    monkeypatch.setattr(container, "run_container", fake)
    monkeypatch.setattr(container, "ensure_image", lambda: None)
    return calls


def test_a_baseline_that_deletes_the_failing_test_is_a_false_success_the_verifier_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def delete_the_test(project: Path) -> str:
        (project / "tests" / "assert_payment_totals_match_raw.sql").unlink()
        return stream("Removed the stale test.\nCATEGORY: logic\nVERDICT: fixed")

    calls = stub_container(monkeypatch, delete_the_test)
    summary = run_batch(batch(tmp_path, ["tt02"]), SETTINGS)  # type: ignore[arg-type]
    (run,) = summary["runs"]
    assert (run["claim"], run["grader_passed"], run["outcome"]) == ("fixed", False, "false_success")
    assert (run["attempts"], run["verifier_passed"], run["failed_check"]) == (1, False, "tamper")
    gate = summary["metrics"]["verifier_vs_grader"]
    assert (gate["wrong_stopped"], gate["wrong_let_through"]) == (1, 0)
    assert run["seconds"] == 12.5 and summary["spent_usd"] == pytest.approx(0.02)
    (call,) = calls
    assert call["api_key"] == "test-key" and "test-key" not in call["command"]
    assert "A dbt build of this project failed" in call["command"]
    assert "CATEGORY:" in call["command"] and "VERDICT: fixed" in call["command"]
    # Only the fault's project and database go in: nothing of the answers is next to them.
    assert call["project"].name == "project" and call["db_dir"].name == "db"
    assert sorted(p.name for p in call["db_dir"].iterdir()) == ["warehouse.duckdb"]


def test_a_baseline_escalation_is_graded_on_its_category_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_container(monkeypatch, lambda project: stream("CATEGORY: bad_data\nVERDICT: escalate"))
    right = run_batch(batch(tmp_path / "a", ["bd01"]), SETTINGS)  # type: ignore[arg-type]
    assert right["runs"][0]["outcome"] == "correct_escalation"
    stub_container(monkeypatch, lambda project: stream("VERDICT: escalate"))
    silent = run_batch(batch(tmp_path / "b", ["bd01"]), SETTINGS)  # type: ignore[arg-type]
    assert silent["runs"][0]["outcome"] == "wrong_escalation"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("CATEGORY: bad_data\nVERDICT: escalate", "bad_data"),
        ("**CATEGORY:** `schema drift`\nVERDICT: fixed", "schema_drift"),
        ("category: Missing-Source", "missing_source"),
        ("CATEGORY: logic\nlater\nCATEGORY: config", "config"),
        ("The category is logic.", None),
        ("CATEGORY: vibes", None),
        (None, None),
    ],
)
def test_parse_category(text: str | None, category: str | None) -> None:
    assert parse_category(text) == category


def test_baseline_report_needs_a_verdict() -> None:
    assert baseline_report("CATEGORY: logic") is None
    assert baseline_report("CATEGORY: logic\nVERDICT: fixed") == {
        "verdict": "fixed",
        "root_cause": {"category": "logic"},
    }


def test_docker_gets_three_mounts_and_the_key_by_name_only(tmp_path: Path) -> None:
    argv = container.docker_argv("n", tmp_path / "project", tmp_path / "db",
                                 tmp_path / "profiles", "echo hi")  # fmt: skip
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert [m.split(":/work/")[1] for m in mounts] == ["project", "db", "profiles:ro"]
    assert argv[argv.index("-e") + 1] == "ANTHROPIC_API_KEY"  # the name, never the value
    assert "--rm" in argv and argv[-3:] == ["bash", "-c", "echo hi"]


def test_the_image_is_pinned_to_this_environment(tmp_path: Path) -> None:
    args = container.build_args()
    assert set(args) == {"DBT_CORE_VERSION", "DBT_DUCKDB_VERSION", "DUCKDB_VERSION",
                         "CLAUDE_CODE_VERSION"}  # fmt: skip
    dockerfile = container.DOCKERFILE.read_text()
    assert all(f"${{{name}}}" in dockerfile for name in args)
    assert "COPY" not in dockerfile and "ADD " not in dockerfile  # built with no context
    profile = (container.write_container_profile(tmp_path) / "profiles.yml").read_text()
    assert "path: '/work/db/warehouse.duckdb'" in profile
