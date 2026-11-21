"""verimend verify on hand-written diffs (phase 4's done-when), and verimend fix with the
agent stubbed: the sandbox, the outputs, the exit codes, the untouched project."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from verimend import cli
from verimend.runrecord import AgentRun

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_shop"

FIX = """\
--- a/models/marts/customer_totals.sql
+++ b/models/marts/customer_totals.sql
@@ -2,5 +2,5 @@
 select
     customer_id,
-    sum(amout) as total_amount
+    sum(amount) as total_amount
 from {{ ref('stg_orders') }}
 group by customer_id
"""

LOOSEN = """\
--- a/models/schema.yml
+++ b/models/schema.yml
@@ -5,4 +5,3 @@
       - name: order_id
         data_tests:
           - not_null
-          - unique
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VERIMEND_HOME", str(tmp_path / "home"))
    copy = tmp_path / "tiny_shop"
    shutil.copytree(FIXTURE, copy)
    return copy


def test_verify_passes_a_real_fix_and_rejects_a_loosened_test(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good, bad = tmp_path / "fix.diff", tmp_path / "loosen.diff"
    good.write_text(FIX)
    bad.write_text(LOOSEN)
    assert cli.main(["verify", "--project-dir", str(project), "--diff", str(good)]) == 0
    assert "Verifier: passed" in capsys.readouterr().out
    assert cli.main(["verify", "--project-dir", str(project), "--diff", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  tamper" in out and "unique_stg_orders_order_id" in out
    assert "amout" in (project / "models/marts/customer_totals.sql").read_text()


def test_verify_needs_a_database_it_can_find(project: Path, tmp_path: Path) -> None:
    (project / "profiles.yml").write_text(
        "tiny_shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: \"{{ env_var('DUCKDB_PATH') }}\"\n"
    )
    diff = tmp_path / "fix.diff"
    diff.write_text(FIX)
    assert cli.main(["verify", "--project-dir", str(project), "--diff", str(diff)]) == 2


def fake_run(**fields: Any) -> AgentRun:
    return AgentRun(model="m", prompt_sha="x", **fields)


@pytest.mark.parametrize(
    ("run", "code"),
    [
        (fake_run(final_verdict="fixed"), 0),
        (fake_run(final_verdict="escalate"), 2),
        (fake_run(final_verdict="not_fixed"), 3),
        (fake_run(final_verdict=None), 3),
        (fake_run(final_verdict=None, subtype="error_max_budget_usd"), 4),
        (fake_run(final_verdict=None, verifier_error="snapshot failed"), 1),
        (fake_run(final_verdict=None, error="boom"), 1),
    ],
)
def test_fix_exit_codes(run: AgentRun, code: int) -> None:
    assert cli.exit_code(run) == code


def test_fix_works_in_a_sandbox_and_leaves_the_project_alone(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    async def stub_agent(task: str, workspace: Any, **kwargs: Any) -> AgentRun:
        seen.update(kwargs, task=task, root=workspace.root)
        model = workspace.root / "models/marts/customer_totals.sql"
        model.write_text(model.read_text().replace("amout", "amount"))
        return fake_run(final_verdict="fixed", cost_usd=0.01, num_turns=5,
                        attempts=[{"passed": True, "checks": [], "duration_s": 1}])  # fmt: skip

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr("verimend.agent.run.run_agent", stub_agent)
    code = cli.main(["fix", "--project-dir", str(project), "--max-usd", "0.05"])
    assert code == 0
    sandbox: Path = seen["root"]
    assert sandbox != project and (sandbox / "dbt_project.yml").is_file()
    assert seen["caps"].max_usd == 0.05 and seen["caps"].max_attempts == 3
    assert seen["verifier"].work_dir.endswith("/verify")
    out_dir = sandbox.parent / "out"
    assert "+    sum(amount) as total_amount" in (out_dir / "fix.diff").read_text()
    assert '"final_verdict": "fixed"' in (out_dir / "run.json").read_text()
    assert "amout" in (project / "models/marts/customer_totals.sql").read_text()
    profiles = (sandbox.parent / "profiles" / "profiles.yml").read_text()
    assert (sandbox.parent / "db").as_posix() in profiles  # the copy, not the real database
    assert "Verdict: fixed" in capsys.readouterr().out


def test_fix_refuses_a_zero_cap(project: Path) -> None:
    assert cli.main(["fix", "--project-dir", str(project), "--max-usd", "0"]) == cli.EXIT_ERROR
