"""The verifier loop (SPEC section 4, attempts): report sends a fixed claim to the
verifier, feeds a rejection back, and ends the run as not fixed after the last attempt."""

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from verimend.agent.run import finish, run_agent
from verimend.hooks.caps import RunCaps
from verimend.runrecord import AgentRun
from verimend.tools.base import ToolContext
from verimend.tools.registry import get_tool
from verimend.tools.verify import (
    VerifierUnavailable,
    WorkspaceVerifier,
    feedback_text,
    parse_output,
    verifier_sources,
)
from verimend.tools.workspace import LocalWorkspace

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_shop"

REPORT = {
    "verdict": "fixed",
    "root_cause": {"category": "logic", "model": "customer_totals", "summary": "Typo."},
    "evidence": [{"kind": "log", "ref": "read_failure", "finding": "amout not found"}],
    "changes": [{"path": "models/marts/customer_totals.sql", "summary": "amout -> amount"}],
    "confidence": "high",
}

REJECTED = {
    "passed": False,
    "failed_check": "masking",
    "checks": [
        {"name": "tamper", "passed": True, "detail": "ok"},
        {"name": "masking", "passed": False, "detail": "models/a.sql adds limit 0"},
    ],
    "duration_s": 1.0,
    "diff": "--- a/x\n+++ b/x\n",
}
PASSED = {"passed": True, "failed_check": None, "checks": [], "duration_s": 1.0, "diff": "d"}


class StubVerifier:
    def __init__(self, *results: dict[str, Any] | Exception) -> None:
        self.results = list(results)
        self.checks = 0
        self.prepared = 0

    async def prepare(self) -> dict[str, Any]:
        self.prepared += 1
        first = self.results[0] if self.results else None
        if isinstance(first, Exception):
            self.results.pop(0)
            raise first
        return {"ok": True}

    async def check(self) -> dict[str, Any]:
        self.checks += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def context(verifier: StubVerifier | None, max_attempts: int = 3, feedback: bool = True):
    ws = LocalWorkspace(Path("."), Path("x.duckdb"))
    return ToolContext(ws, verifier=verifier, max_attempts=max_attempts, feedback=feedback)


def report(ctx: ToolContext, **changes: Any):
    return asyncio.run(get_tool("report").handler(ctx, {**REPORT, **changes}))


def test_a_rejection_is_fed_back_and_a_second_attempt_can_pass():
    stub = StubVerifier(REJECTED, PASSED)
    ctx = context(stub)
    first = report(ctx)
    assert not first.is_error and not ctx.accepted
    assert "attempt 1 of 3" in first.text
    assert "Failed check: masking" in first.text and "adds limit 0" in first.text
    second = report(ctx)
    assert ctx.accepted and ctx.final_verdict == "fixed"
    assert "verifier passed" in second.text
    assert [a["attempt"] for a in ctx.attempts] == [1, 2]
    assert ctx.attempts[0]["failed_check"] == "masking" and ctx.attempts[0]["diff"]


def test_the_last_rejection_ends_the_run_as_not_fixed():
    stub = StubVerifier(REJECTED, REJECTED, REJECTED)
    ctx = context(stub)
    report(ctx)
    report(ctx)
    last = report(ctx)
    assert ctx.accepted and ctx.final_verdict == "not_fixed"
    assert "No attempts are left" in last.text
    assert stub.checks == 3
    refused = report(ctx)
    assert refused.is_error and stub.checks == 3  # nothing after the run is over


@pytest.mark.parametrize("verdict", ["escalate", "not_fixed"])
def test_escalate_and_not_fixed_skip_the_verifier(verdict: str):
    stub = StubVerifier()
    ctx = context(stub)
    report(ctx, verdict=verdict)
    assert ctx.accepted and ctx.final_verdict == verdict and stub.checks == 0


def test_without_feedback_the_agent_hears_only_that_it_was_rejected():
    ctx = context(StubVerifier(REJECTED, PASSED), feedback=False)
    text = report(ctx).text
    assert "attempt 1 of 3" in text and "masking" not in text and "limit 0" not in text


def test_a_verifier_that_cannot_run_ends_the_run_without_a_verdict():
    ctx = context(StubVerifier(VerifierUnavailable("dbt missing")))
    report(ctx)
    assert ctx.accepted and ctx.final_verdict is None
    assert ctx.verifier_error and "dbt missing" in ctx.verifier_error


def test_no_verifier_means_the_report_is_final():
    ctx = context(None)
    report(ctx)
    assert ctx.accepted and ctx.final_verdict == "fixed" and not ctx.attempts


def test_an_unaccepted_run_with_rejections_ends_as_not_fixed():
    ctx = context(StubVerifier(REJECTED))
    report(ctx)  # rejected, then the run hits a cap before reporting again
    run = AgentRun(model="m", prompt_sha="x")
    finish(run, ctx)
    assert run.final_verdict == "not_fixed" and run.verifier_rejections == 1
    untouched = AgentRun(model="m", prompt_sha="x")
    finish(untouched, context(None))
    assert untouched.final_verdict is None


def test_a_failed_snapshot_stops_the_run_before_any_model_call(tmp_path: Path):
    stub = StubVerifier(VerifierUnavailable("no profiles.yml"))
    run = asyncio.run(
        run_agent(
            "task",
            LocalWorkspace(tmp_path, tmp_path / "x.duckdb"),
            model="claude-haiku-5-5",
            api_key="not-used",
            caps=RunCaps(max_usd=0.01, max_steps=1, max_attempts=3),
            cwd=tmp_path / "cwd",
            verifier=stub,
        )
    )
    assert run.verifier_error and "no profiles.yml" in run.verifier_error
    assert run.cost_usd is None and run.num_turns is None and not (tmp_path / "cwd").exists()


def test_parse_output_reads_the_last_json_line():
    assert parse_output(1, "dbt noise\n" + '{"passed": false}')["passed"] is False
    with pytest.raises(VerifierUnavailable, match="no profiles"):
        parse_output(2, '{"error": "VerifierError: no profiles.yml found"}')
    with pytest.raises(VerifierUnavailable, match="no result"):
        parse_output(1, "Traceback ...")


def test_feedback_names_the_failing_check_only():
    text = feedback_text(REJECTED, 2, 3, detail=True)
    assert "attempt 2 of 3" in text and "masking" in text and "tamper" not in text


def test_verifier_sources_are_the_whole_package():
    names = set(verifier_sources())
    assert {"__init__.py", "__main__.py", "cli.py", "checks.py", "patch.py", "run.py"} <= names


@pytest.mark.parametrize("uploaded", [False, True])
def test_workspace_verifier_on_a_real_project(tmp_path: Path, uploaded: bool):
    """Snapshot, fix the model the way the agent would, check: passes. Uploaded mode runs
    the copy put into the workspace, as in a container."""
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    ws = LocalWorkspace(project, project / "shop.duckdb")
    source = (tmp_path / "verifier-src").as_posix() if uploaded else None
    verifier = WorkspaceVerifier(ws, (tmp_path / "work").as_posix(), source_dir=source)

    async def go() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        snap = await verifier.prepare()
        before = await verifier.check()
        model = "models/marts/customer_totals.sql"
        text = await ws.read_text(model)
        assert text is not None
        await ws.write_text(model, text.replace("amout", "amount"))
        return snap, before, await verifier.check()

    snap, before, after = asyncio.run(go())
    assert snap["base_failures"] == ["model.tiny_shop.customer_totals"]
    assert before["failed_check"] == "empty_diff"
    assert after["passed"], after
    assert "+    sum(amount) as total_amount" in after["diff"]
    if uploaded:
        assert (tmp_path / "verifier-src" / "verifier" / "checks.py").is_file()
