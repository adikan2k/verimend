"""Hand a `verimend fix` result back to GitHub (SPEC section 10).

Fixed and verified: a pull request from `verimend/fix-<sha>` into the branch that failed,
its body in five sections (root cause, evidence, diff summary, verifier output, cost).
Escalated, not fixed or stopped by a cap: the diagnosis as a comment, on the pull request
that failed or else on the failing commit.

It never pushes anything but a `verimend/fix-*` branch, never pushes to the default
branch or the branch that failed, never merges, and never runs for a pull request from a
fork or for a `verimend/fix-*` branch of its own. GitHub's REST API is called through
urllib, so there is no new dependency; tests pass their own `http`.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verifier.patch import apply_diff, parse_diff

from verimend.runrecord import AgentRun

BRANCH_PREFIX = "verimend/fix-"
API_URL = "https://api.github.com"
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")

Http = Callable[[str, str, dict[str, Any] | None, str], dict[str, Any]]


class GitHubError(RuntimeError):
    """The result could not be handed back to GitHub, or must not be."""


@dataclass(frozen=True)
class Context:
    repository: str  # owner/name
    sha: str  # the commit that failed
    ref_name: str  # the branch that failed: the pushed branch, or the pull request's head
    event: str  # push, pull_request, workflow_dispatch, or local
    token: str
    pr_number: int | None = None
    fork: bool = False
    in_actions: bool = False
    api_url: str = API_URL

    @property
    def short_sha(self) -> str:
        return self.sha[:7]


# ---- where we are ---------------------------------------------------------------------


def run_git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")  # fmt: skip
    if done.returncode != 0:
        raise GitHubError(f"git {' '.join(args)} failed: {done.stderr.strip()[-400:]}")
    return done.stdout.strip()


def _repository_from_remote(url: str) -> str:
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not match:
        raise GitHubError(f"the origin remote is not a GitHub repository: {url}")
    return match.group(1)


def context_from_env(env: Mapping[str, str], project_dir: Path, token: str | None) -> Context:
    """The failing commit and branch, from GitHub Actions' variables or the local checkout."""
    if not token:
        raise GitHubError("no GitHub token: set GITHUB_TOKEN (the Action passes its own)")
    if env.get("GITHUB_ACTIONS") == "true":
        event_name = env.get("GITHUB_EVENT_NAME", "")
        event: dict[str, Any] = {}
        if env.get("GITHUB_EVENT_PATH"):
            event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        common = {"repository": env["GITHUB_REPOSITORY"], "event": event_name, "token": token,
                  "in_actions": True, "api_url": env.get("GITHUB_API_URL", API_URL)}  # fmt: skip
        if event_name in ("pull_request", "pull_request_target"):
            pull = event["pull_request"]
            head_repo = (pull["head"].get("repo") or {}).get("full_name")
            base_repo = (pull["base"].get("repo") or {}).get("full_name")
            return Context(sha=pull["head"]["sha"], ref_name=pull["head"]["ref"],
                           pr_number=int(pull["number"]), fork=head_repo != base_repo,
                           **common)  # fmt: skip
        return Context(sha=env["GITHUB_SHA"], ref_name=env["GITHUB_REF_NAME"], **common)
    remote = run_git(project_dir, "remote", "get-url", "origin")
    return Context(
        repository=_repository_from_remote(remote),
        sha=run_git(project_dir, "rev-parse", "HEAD"),
        ref_name=run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD"),
        event="local",
        token=token,
    )


def refusal(ctx: Context) -> str | None:
    """Why VeriMend must not act here at all, or None."""
    if ctx.fork:
        return "this is a pull request from a fork; VeriMend never runs with secrets for one"
    if ctx.ref_name.startswith(BRANCH_PREFIX):
        return f"{ctx.ref_name} is one of VeriMend's own fix branches"
    if ctx.ref_name == "HEAD":
        return "the checkout is not on a branch, so there is nothing to open a pull request into"
    return None


def fix_branch(ctx: Context) -> str:
    return f"{BRANCH_PREFIX}{ctx.short_sha}"


def require_fix_branch(branch: str, ctx: Context, default_branch: str) -> None:
    """The only branch VeriMend may push: its own, never the default or the failing one."""
    if not branch.startswith(BRANCH_PREFIX) or branch in (default_branch, ctx.ref_name):
        raise GitHubError(f"refusing to push {branch!r}: only {BRANCH_PREFIX}* branches")


# ---- the GitHub API -------------------------------------------------------------------


def default_http(method: str, url: str, body: dict[str, Any] | None, token: str) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "verimend",
    })  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        raise GitHubError(f"GitHub {method} {url} failed: {error.code} {detail}") from None
    except urllib.error.URLError as error:
        raise GitHubError(f"GitHub {method} {url} failed: {error.reason}") from None


def open_pull_request(
    ctx: Context,
    project_dir: Path,
    diff_text: str,
    title: str,
    body: str,
    worktree: Path,
    http: Http | None = None,
) -> str:
    """Commit the verified diff on `verimend/fix-<sha>` in a separate worktree (the
    checkout itself is untouched), push only that branch, and open the pull request."""
    http = http or default_http
    if not diff_text.strip():
        raise GitHubError("the verified fix changes nothing; there is nothing to propose")
    repo_api = f"{ctx.api_url}/repos/{ctx.repository}"
    default_branch = str(http("GET", repo_api, None, ctx.token).get("default_branch") or "")
    branch = fix_branch(ctx)
    require_fix_branch(branch, ctx, default_branch)

    root = Path(run_git(project_dir, "rev-parse", "--show-toplevel")).resolve()
    sub = project_dir.resolve().relative_to(root)
    run_git(root, "worktree", "add", "--force", "-B", branch, str(worktree), ctx.sha)
    try:
        apply_diff(worktree / sub, parse_diff(diff_text))
        run_git(worktree, "add", "-A")
        # In Actions, commit as the Actions bot; locally, as whoever runs it.
        author = (
            ["-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}"] if ctx.in_actions else []
        )
        run_git(worktree, *author, "commit", "-q", "-m", title, "-m",
                f"Proposed by VeriMend for {ctx.short_sha}; the verifier passed it.")  # fmt: skip
        run_git(worktree, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    finally:
        run_git(root, "worktree", "remove", "--force", str(worktree))
    pull = http("POST", f"{repo_api}/pulls",
                {"title": title, "head": branch, "base": ctx.ref_name, "body": body},
                ctx.token)  # fmt: skip
    return str(pull.get("html_url"))


def post_diagnosis(ctx: Context, body: str, http: Http | None = None) -> str:
    """A comment on the pull request that failed, or else on the failing commit."""
    http = http or default_http
    repo_api = f"{ctx.api_url}/repos/{ctx.repository}"
    if ctx.pr_number is not None:
        url = f"{repo_api}/issues/{ctx.pr_number}/comments"
    else:
        url = f"{repo_api}/commits/{ctx.sha}/comments"
    return str(http("POST", url, {"body": body}, ctx.token).get("html_url"))


# ---- what the pull request and the comment say ---------------------------------------


def _root_cause(report: dict[str, Any]) -> list[str]:
    cause = report.get("root_cause") or {}
    category = str(cause.get("category") or "unknown").replace("_", " ")
    lines = ["## Root cause", ""]
    lines.append(f"**{category.capitalize()}** in `{cause.get('model') or 'unknown'}`. "
                 f"{cause.get('summary') or 'No summary was given.'}")  # fmt: skip
    return lines


def _evidence(report: dict[str, Any]) -> list[str]:
    items = report.get("evidence") or []
    lines = ["## Evidence", ""]
    lines += [f"- **{e.get('kind')}** `{e.get('ref')}`: {e.get('finding')}" for e in items]
    return lines if items else [*lines, "None was recorded."]


def _cost(run: AgentRun) -> list[str]:
    dollars = f"${run.cost_usd:.4f}" if run.cost_usd is not None else "unknown"
    attempts = len(run.attempts)
    return [
        "## Cost",
        "",
        f"{dollars} on {run.model}, {run.num_turns or 0} steps, {run.seconds or 0:.0f} s, "
        f"{attempts} verifier run{'s' if attempts != 1 else ''}.",
    ]


def diff_stats(diff_text: str) -> list[tuple[str, int, int]]:
    """(path, lines added, lines removed) per file of a unified diff."""
    return [(p.path, len(p.added_lines()),
             sum(1 for h in p.hunks for line in h.lines if line[:1] == "-"))
            for p in parse_diff(diff_text)]  # fmt: skip


def pr_title(run: AgentRun) -> str:
    cause = (run.report or {}).get("root_cause") or {}
    category = str(cause.get("category") or "unknown").replace("_", " ")
    return f"Fix {cause.get('model') or 'the dbt build'} ({category})"


def pr_body(run: AgentRun, diff_text: str, ctx: Context) -> str:
    report = run.report or {}
    changes = {c.get("path"): c.get("summary") for c in report.get("changes") or []}
    stats = diff_stats(diff_text)
    added, removed = sum(s[1] for s in stats), sum(s[2] for s in stats)
    last = run.attempts[-1] if run.attempts else {}
    lines = [
        f"VeriMend found why the dbt build failed at `{ctx.short_sha}`, and the independent "
        "verifier passed its fix. Nothing was merged: a person must review this pull request.",
        "",
        *_root_cause(report),
        "",
        *_evidence(report),
        "",
        "## Diff summary",
        "",
        f"{added} line{'s' if added != 1 else ''} added and {removed} removed in "
        f"{len(stats)} file{'s' if len(stats) != 1 else ''}:",
        "",
    ]
    for path, plus, minus in stats:
        why = f": {changes[path]}" if changes.get(path) else ""
        lines.append(f"- `{path}` (+{plus} −{minus}){why}")
    lines += ["", "<details><summary>The diff</summary>", "", "```diff", diff_text.rstrip(),
              "```", "", "</details>", "", "## Verifier output", ""]  # fmt: skip
    lines.append(f"The verifier passed the fix on attempt {len(run.attempts)}.")
    lines += ["", "| Check | Result | Detail |", "| --- | --- | --- |"]
    for check in last.get("checks", []):
        detail = str(check.get("detail", "")).replace("|", "\\|").replace("\n", " ")[:300]
        lines.append(f"| {check.get('name')} | {'passed' if check.get('passed') else 'failed'} "
                     f"| {detail} |")  # fmt: skip
    lines += ["", *_cost(run), ""]
    return "\n".join(lines)


def diagnosis_body(run: AgentRun, ctx: Context) -> str:
    report = run.report or {}
    verdict = run.final_verdict
    if verdict == "escalate":
        headline = "**Escalated.** The code looks right; a person needs to look at this."
    elif verdict is None and run.stopped_by_cap:
        headline = "**Not fixed.** The run reached its cost or step cap before it finished."
    else:
        headline = "**Not fixed.** VeriMend did not find a fix the verifier would pass."
    lines = [
        f"VeriMend looked at the failed dbt build at `{ctx.short_sha}` and did not open a "
        "pull request.",
        "",
        headline,
        "",
    ]
    if report:
        lines += [*_root_cause(report), "", *_evidence(report), ""]
    rejected = [a for a in run.attempts if not a.get("passed")]
    if rejected:
        lines += ["## What the verifier rejected", ""]
        for attempt in rejected:
            failing = next((c for c in attempt.get("checks", []) if not c.get("passed")), {})
            detail = str(failing.get("detail", "")).replace("\n", " ")[:300]
            lines.append(f"- Attempt {attempt.get('attempt')}: {attempt.get('failed_check')}. "
                         f"{detail}")  # fmt: skip
        lines.append("")
    lines += [*_cost(run), ""]
    return "\n".join(lines)
