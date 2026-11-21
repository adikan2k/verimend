"""verimend/github.py and `verimend fix --open-pr`: the guards block what they should, a
verified fix becomes a pull request from verimend/fix-<sha>, anything else a comment.
A local bare repository stands in for GitHub's git, and a fake function for its API."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from verimend import cli, github
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
REPORT = {
    "verdict": "fixed",
    "root_cause": {"category": "logic", "model": "customer_totals", "summary": "It reads amout."},
    "evidence": [{"kind": "log", "ref": "dbt build", "finding": "Binder error on amout."}],
    "changes": [{"path": "models/marts/customer_totals.sql", "summary": "Read amount."}],
    "confidence": "high",
}
CHECKS = [
    {"name": "tamper", "passed": True, "detail": "1 changed file(s), all allowed"},
    {"name": "collateral", "passed": True, "detail": "1 model(s) outside the lineage unchanged"},
]


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd,
                          capture_output=True, text=True, check=True)  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout of tiny_shop on main, pushed to a local bare 'origin'."""
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    checkout = tmp_path / "shop"
    shutil.copytree(FIXTURE, checkout, ignore=shutil.ignore_patterns("*.duckdb", "target"))
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Test")
    git(checkout, "config", "user.email", "test@example.com")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "Shop models")
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "-q", "origin", "main")
    return checkout, remote


class FakeGitHub:
    def __init__(self, default_branch: str = "main") -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.default_branch = default_branch

    def __call__(self, method: str, url: str, body: dict | None, token: str) -> dict[str, Any]:
        assert token == "t"
        self.calls.append((method, url, body))
        if method == "GET":
            return {"default_branch": self.default_branch}
        return {"html_url": f"https://github.example/{len(self.calls)}"}


def ctx(checkout: Path, **fields: Any) -> github.Context:
    base: dict[str, Any] = {"repository": "o/shop", "sha": git(checkout, "rev-parse", "HEAD"),
            "ref_name": "main", "event": "push", "token": "t"}  # fmt: skip
    return github.Context(**(base | fields))


def run(**fields: Any) -> AgentRun:
    base: dict[str, Any] = {
        "model": "claude-haiku-5-5", "prompt_sha": "p", "report": REPORT, "cost_usd": 0.0123,
        "num_turns": 9, "seconds": 41.0, "final_verdict": "fixed",
        "attempts": [{"attempt": 1, "passed": True, "checks": CHECKS}],
    }  # fmt: skip
    return AgentRun(**(base | fields))


# ---- the pull request -----------------------------------------------------------------


def test_a_verified_fix_is_pushed_only_to_its_own_branch(repo, tmp_path: Path) -> None:
    checkout, remote = repo
    context = ctx(checkout)
    api = FakeGitHub()
    url = github.open_pull_request(context, checkout, FIX, "Fix customer_totals (logic)",
                                   "body", tmp_path / "wt", http=api)  # fmt: skip
    branch = f"verimend/fix-{context.short_sha}"
    heads = git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert sorted(heads.split()) == sorted(["main", branch])
    assert git(remote, "rev-parse", "main") == context.sha  # main is untouched
    fixed = git(remote, "show", f"{branch}:models/marts/customer_totals.sql")
    assert "sum(amount)" in fixed
    assert "amout" in (checkout / "models/marts/customer_totals.sql").read_text()  # checkout too
    assert not (tmp_path / "wt").exists()
    method, endpoint, body = api.calls[-1]
    assert (method, endpoint) == ("POST", "https://api.github.com/repos/o/shop/pulls")
    assert body == {"title": "Fix customer_totals (logic)", "head": branch, "base": "main",
                    "body": "body"}  # fmt: skip
    assert url == "https://github.example/2"


@pytest.mark.parametrize("branch", ["main", "develop", "feature/x", "verimend-fix-abc"])
def test_no_branch_but_its_own_can_be_pushed(repo, branch: str) -> None:
    checkout, _ = repo
    with pytest.raises(github.GitHubError, match="refusing to push"):
        github.require_fix_branch(branch, ctx(checkout, ref_name="develop"), "main")
    github.require_fix_branch("verimend/fix-abc1234", ctx(checkout), "main")  # allowed


def test_a_fix_branch_named_like_the_default_branch_is_refused(repo) -> None:
    checkout, _ = repo
    with pytest.raises(github.GitHubError):
        github.require_fix_branch("verimend/fix-x", ctx(checkout), "verimend/fix-x")


def test_an_empty_diff_pushes_nothing(repo, tmp_path: Path) -> None:
    checkout, remote = repo
    api = FakeGitHub()
    with pytest.raises(github.GitHubError, match="changes nothing"):
        github.open_pull_request(ctx(checkout), checkout, "", "t", "b", tmp_path / "wt", http=api)
    assert git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "main"
    assert api.calls == []


# ---- where VeriMend refuses to act ----------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "words"),
    [
        ({"fork": True, "event": "pull_request", "pr_number": 3}, "from a fork"),
        ({"ref_name": "verimend/fix-abc1234"}, "own fix branches"),
        ({"ref_name": "HEAD"}, "not on a branch"),
    ],
)
def test_refusals(repo, fields: dict, words: str) -> None:
    reason = github.refusal(ctx(repo[0], **fields))
    assert reason is not None and words in reason
    assert github.refusal(ctx(repo[0])) is None


def actions_env(tmp_path: Path, event_name: str, event: dict) -> dict[str, str]:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/shop", "GITHUB_SHA": "f" * 40,
            "GITHUB_REF_NAME": "main", "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(path)}  # fmt: skip


def pull_event(head_repo: str) -> dict:
    head = {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": head_repo}}
    return {"pull_request": {"number": 7, "head": head, "base": {"repo": {"full_name": "o/shop"}}}}


def test_context_in_actions_for_a_push_and_for_pull_requests(tmp_path: Path) -> None:
    push = github.context_from_env(actions_env(tmp_path, "push", {}), tmp_path, "t")
    assert (push.sha, push.ref_name, push.pr_number, push.fork) == ("f" * 40, "main", None, False)
    same = github.context_from_env(actions_env(tmp_path, "pull_request", pull_event("o/shop")),
                                   tmp_path, "t")  # fmt: skip
    assert (same.sha, same.ref_name, same.pr_number, same.fork) == ("a" * 40, "feature", 7, False)
    fork = github.context_from_env(actions_env(tmp_path, "pull_request", pull_event("x/shop")),
                                   tmp_path, "t")  # fmt: skip
    assert fork.fork and github.refusal(fork)


def test_context_locally_reads_the_origin_remote(repo) -> None:
    checkout, _ = repo
    git(checkout, "remote", "set-url", "origin", "git@github.com:someone/shop.git")
    local = github.context_from_env({}, checkout, "t")
    assert (local.repository, local.ref_name, local.event) == ("someone/shop", "main", "local")


def test_no_token_no_github(tmp_path: Path) -> None:
    with pytest.raises(github.GitHubError, match="no GitHub token"):
        github.context_from_env({}, tmp_path, None)


# ---- the diagnosis --------------------------------------------------------------------


def test_the_diagnosis_goes_on_the_pull_request_or_else_the_commit(repo) -> None:
    checkout, _ = repo
    api = FakeGitHub()
    github.post_diagnosis(ctx(checkout, pr_number=7, event="pull_request"), "b", http=api)
    on_commit = ctx(checkout)
    github.post_diagnosis(on_commit, "b", http=api)
    assert api.calls[0][1].endswith("/repos/o/shop/issues/7/comments")
    assert api.calls[1][1].endswith(f"/repos/o/shop/commits/{on_commit.sha}/comments")


def test_the_pull_request_body_has_the_five_sections(repo) -> None:
    body = github.pr_body(run(), FIX, ctx(repo[0]))
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## Root cause", "## Evidence", "## Diff summary", "## Verifier output",
                        "## Cost"]  # fmt: skip
    assert "Nothing was merged" in body and "**Logic** in `customer_totals`" in body
    assert "`models/marts/customer_totals.sql` (+1 −1): Read amount." in body
    assert "| tamper | passed |" in body and "$0.0123 on claude-haiku-5-5" in body
    assert github.pr_title(run()) == "Fix customer_totals (logic)"


def test_the_diagnosis_says_what_happened(repo) -> None:
    escalated = {**REPORT, "verdict": "escalate",
                 "root_cause": {"category": "bad_data", "model": "stg_payments",
                                "summary": "Three payments are negative."}}  # fmt: skip
    body = github.diagnosis_body(run(final_verdict="escalate", report=escalated, attempts=[]),
                                 ctx(repo[0]))  # fmt: skip
    assert "**Escalated.**" in body and "**Bad data** in `stg_payments`" in body
    masking = {"name": "masking", "passed": False, "detail": "adds limit 0"}
    rejected = [{"attempt": 1, "passed": False, "failed_check": "masking", "checks": [masking]}]
    body = github.diagnosis_body(run(final_verdict="not_fixed", attempts=rejected), ctx(repo[0]))
    assert "**Not fixed.**" in body and "Attempt 1: masking. adds limit 0" in body


# ---- verimend fix --open-pr -----------------------------------------------------------


@pytest.fixture
def cli_env(repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, FakeGitHub]:
    checkout, remote = repo
    monkeypatch.setenv("VERIMEND_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    for name in ("GITHUB_ACTIONS", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(name, raising=False)
    git(checkout, "remote", "set-url", "--push", "origin", str(remote))
    git(checkout, "remote", "set-url", "origin", "https://github.com/o/shop.git")
    git(checkout, "remote", "set-url", "--push", "origin", str(remote))
    api = FakeGitHub()
    monkeypatch.setattr(github, "default_http", api)
    return checkout, remote, api


def test_fix_open_pr_opens_a_pull_request_and_the_sandbox_has_history(
    cli_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout, remote, api = cli_env
    seen: dict[str, Any] = {}

    async def stub_agent(task: str, workspace: Any, **kwargs: Any) -> AgentRun:
        root = workspace.root
        seen["log"] = git(root, "log", "--format=%s")  # what inspect_history would see
        model = root / "models/marts/customer_totals.sql"
        model.write_text(model.read_text().replace("amout", "amount"))
        return run()

    monkeypatch.setattr("verimend.agent.run.run_agent", stub_agent)
    assert cli.main(["fix", "--project-dir", str(checkout), "--max-usd", "0.05", "--open-pr"]) == 0
    assert seen["log"] == "Shop models"
    sha7 = git(checkout, "rev-parse", "--short=7", "HEAD")
    assert f"verimend/fix-{sha7}" in git(remote, "for-each-ref", "--format=%(refname:short)")
    assert [c[0] for c in api.calls] == ["GET", "POST"]
    assert "Opened a pull request" in capsys.readouterr().out


def test_fix_open_pr_posts_the_diagnosis_for_an_escalation(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, remote, api = cli_env

    async def stub_agent(task: str, workspace: Any, **kwargs: Any) -> AgentRun:
        return run(final_verdict="escalate", attempts=[])

    monkeypatch.setattr("verimend.agent.run.run_agent", stub_agent)
    code = cli.main(["fix", "--project-dir", str(checkout), "--max-usd", "0.05", "--open-pr"])
    assert code == cli.EXIT_ESCALATED
    assert len(api.calls) == 1 and api.calls[0][1].endswith("/comments")
    assert git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "main"


def test_fix_open_pr_refuses_a_fork_before_spending_anything(
    cli_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _, api = cli_env
    for name, value in actions_env(tmp_path, "pull_request", pull_event("x/shop")).items():
        monkeypatch.setenv(name, value)
    called = []

    async def stub_agent(*args: Any, **kwargs: Any) -> AgentRun:
        called.append(True)
        return run()

    monkeypatch.setattr("verimend.agent.run.run_agent", stub_agent)
    code = cli.main(["fix", "--project-dir", str(checkout), "--max-usd", "0.05", "--open-pr"])
    assert code == cli.EXIT_ERROR and not called and not api.calls
