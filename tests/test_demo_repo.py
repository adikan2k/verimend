"""scripts/make_demo_repo.py: the demo builds green, and each broken commit fails the way
its case needs (a reconciliation test for the logic bug, the negative-payments test for
the bad data)."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from verifier.policy import Policy
from verifier.project import default_dbt
from verifier.run import snapshot, verify

from bench.faults.dbt import run_dbt
from scripts import make_demo_repo as demo

DBT = default_dbt()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    dest = tmp_path / "demo"
    demo.build(dest, "phase-6/github-action")
    return dest


def build(repo: Path) -> list[str]:
    subprocess.run([sys.executable, "scripts/load_raw.py"], cwd=repo, check=True,
                   capture_output=True)  # fmt: skip
    return run_dbt(repo, repo, "build").failing


def test_the_demo_builds_green_and_calls_the_action_only_on_failure(repo: Path) -> None:
    assert build(repo) == []
    workflow = yaml.safe_load((repo / ".github/workflows/dbt.yml").read_text())
    steps = workflow["jobs"]["build"]["steps"]
    verimend = steps[-1]
    assert verimend["if"] == "failure()"
    assert verimend["uses"] == "adikan2k/verimend/action@phase-6/github-action"
    assert verimend["with"]["anthropic-api-key"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert steps[0]["with"]["fetch-depth"] == 0
    assert (repo / "dbt_project.yml").is_file() and (repo / "raw_data" / "payments.csv").is_file()


def test_the_logic_bug_fails_the_order_reconciliation(repo: Path) -> None:
    demo.break_logic(repo)
    assert build(repo) == ["test.jaffle_shop.assert_order_amount_matches_payments"]


def test_the_bad_data_reverts_the_bug_and_fails_only_on_negative_payments(repo: Path) -> None:
    demo.break_logic(repo)
    demo.break_bad_data(repo)
    log = demo.git(repo, "log", "--format=%s").splitlines()
    assert log[:2] == [demo.BAD_DATA_MESSAGE, f'Revert "{demo.LOGIC_MESSAGE}"']
    assert build(repo) == ["test.jaffle_shop.assert_no_negative_payments"]


# ---- The public inspector demo (phase 6e) ----------------------------------------------

PR = Policy("pr")


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    mp = pytest.MonkeyPatch()
    for who in ("AUTHOR", "COMMITTER"):
        mp.setenv(f"GIT_{who}_NAME", "Test")
        mp.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    dest = tmp_path_factory.mktemp("template") / "demo"
    demo.template(dest, "verify-v1", "action-v1")
    mp.undo()
    return dest


def workflow(repo: Path, name: str) -> dict:
    loaded = yaml.safe_load((repo / ".github" / "workflows" / name).read_text())
    loaded["on"] = loaded.pop(True, loaded.get("on"))  # YAML reads a bare `on` as True
    return loaded


def test_the_template_inspects_every_pull_request_with_no_key(template: Path) -> None:
    inspector = workflow(template, "inspector.yml")
    assert set(inspector["on"]) == {"pull_request"}
    assert "labeled" in inspector["on"]["pull_request"]["types"]
    (step,) = [s for s in inspector["jobs"]["inspect"]["steps"] if "verify" in s.get("uses", "")]
    assert step["uses"] == "adikan2k/verimend/action/verify@verify-v1"
    assert step["with"]["setup-command"] == "python scripts/load_raw.py"
    text = (template / ".github/workflows/inspector.yml").read_text().lower()
    assert "secrets." not in text and "anthropic" not in text


def test_the_templates_fixer_needs_the_copys_own_key_and_never_runs_on_a_pull_request(
    template: Path,
) -> None:
    fix = workflow(template, "fix.yml")
    assert "pull_request" not in fix["on"] and "pull_request_target" not in fix["on"]
    job = fix["jobs"]["build"]
    assert job["env"]["HAS_KEY"] == "${{ secrets.ANTHROPIC_API_KEY != '' }}"
    (step,) = [s for s in job["steps"] if "verimend/action" in s.get("uses", "")]
    assert step["uses"] == "adikan2k/verimend/action@action-v1"
    assert step["if"] == "failure() && env.HAS_KEY == 'true'"
    for workflow_step in (
        job["steps"] + workflow(template, "inspector.yml")["jobs"]["inspect"]["steps"]
    ):
        assert "${{" not in workflow_step.get("run", "")


def test_the_templates_three_pull_requests_get_the_stamps_its_readme_promises(
    template: Path, tmp_path: Path
) -> None:
    """What the inspector Action runs, on each try/* branch: snapshot the base, then
    verify the branch's diff under the pr policy."""
    assert demo.git(template, "branch", "--format=%(refname:short)").splitlines() == sorted(
        ["main", *demo.TRY_BRANCHES]
    )
    subprocess.run([sys.executable, "scripts/load_raw.py"], cwd=template, check=True,
                   capture_output=True)  # fmt: skip
    work = tmp_path / "work"
    snapshot(template, None, work, profiles_dir=template, dbt_exe=DBT)
    results = {
        branch: verify(
            work, demo.git(template, "diff", "main", branch) + "\n", dbt_exe=DBT, policy=PR
        )  # fmt: skip
        for branch in demo.TRY_BRANCHES
    }
    weaken, null_month, good = (results[b] for b in demo.TRY_BRANCHES)
    assert weaken["failed_check"] == "tamper", weaken
    assert "not_null_customers_customer_id" in weaken["checks"][0]["detail"]
    assert null_month["failed_check"] == "row_accounting", null_month
    assert "5 row(s)" in null_month["checks"][-1]["detail"]
    assert "NULL order_month" in null_month["checks"][-1]["detail"]
    assert good["passed"], good
    assert [c["name"] for c in good["checks"]][-1] == "row_accounting"
