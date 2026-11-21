"""scripts/make_demo_repo.py: the demo builds green, and each broken commit fails the way
its case needs (a reconciliation test for the logic bug, the negative-payments test for
the bad data)."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bench.faults.dbt import run_dbt
from scripts import make_demo_repo as demo


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
