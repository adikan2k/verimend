"""verimend fix | verify (SPEC section 10).

`verify` checks any diff, no agent involved. `fix` copies the project and its database
into a sandbox under ~/.verimend, runs the agent there with the verifier loop, and writes
the result next to the sandbox. The project you point it at is never changed. With
`--open-pr`, a verified fix becomes a pull request from `verimend/fix-<sha>` and any
other outcome a diagnosis comment (verimend/github.py).

Exit codes for fix: 0 fixed and verified, 2 escalated, 3 not fixed, 4 stopped by a cap,
1 error. For verify: 0 passed, 1 a check failed, 2 the verifier could not run.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from verifier.patch import make_diff
from verifier.project import (
    VerifierError,
    copy_database,
    copy_project,
    find_profiles_dir,
    read_profile,
    write_profiles,
)

from verimend import github
from verimend.config import ConfigError, load_settings

EXIT_FIXED, EXIT_ERROR, EXIT_ESCALATED, EXIT_NOT_FIXED, EXIT_CAP = 0, 1, 2, 3, 4

DEFAULT_TASK = (
    "A dbt build of this project failed or produced wrong results. Find the cause and fix it."
)


def run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{time.monotonic_ns() % 10**6}"


def resolve_db(project_dir: Path, profiles_dir: Path, target: str | None, db: Path | None) -> Path:
    """The DuckDB file the project builds into: --db, or the profile's own path."""
    if db is not None:
        return db.resolve()
    path = str(read_profile(project_dir, profiles_dir, target).output.get("path", ""))
    if not path or "{{" in path or path == ":memory:":
        raise VerifierError("cannot tell the project's DuckDB file from its profile (pass --db)")
    candidate = Path(path)
    return (candidate if candidate.is_absolute() else project_dir / candidate).resolve()


def run_verifier(*args: str) -> tuple[int, dict[str, Any]]:
    """`python -m verifier ...` as its own process, like the agent loop runs it."""
    done = subprocess.run(
        [sys.executable, "-m", "verifier", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = [line for line in done.stdout.strip().splitlines() if line.startswith("{")]
    if not lines:
        return 2, {"error": (done.stderr or done.stdout)[-800:]}
    return done.returncode, json.loads(lines[-1])


def print_checks(result: dict[str, Any]) -> None:
    for check in result.get("checks", []):
        mark = "pass" if check["passed"] else "FAIL"
        print(f"  {mark}  {check['name']:<14} {check['detail']}")
    if result.get("passed"):
        print(f"Verifier: passed ({result.get('duration_s')} s)")
    else:
        print(f"Verifier: failed at {result.get('failed_check')} ({result.get('duration_s')} s)")


def cmd_verify(args: argparse.Namespace) -> int:
    project = args.project_dir.resolve()
    try:
        profiles = find_profiles_dir(project, args.profiles_dir)
        db = resolve_db(project, profiles, args.target, args.db)
    except VerifierError as error:
        print(f"verimend verify: {error}", file=sys.stderr)
        return 2
    work = load_settings().verimend_home / "verify" / run_id()
    snap_args = ["--project-dir", str(project), "--db", str(db), "--work", str(work),
                 "--profiles-dir", str(profiles)]  # fmt: skip
    if args.target:
        snap_args += ["--target", args.target]
    print(f"Snapshot of {project} in {work} ...")
    code, snap = run_verifier("snapshot", *snap_args)
    if code != 0:
        print(f"verimend verify: snapshot failed: {snap.get('error')}", file=sys.stderr)
        return 2
    code, result = run_verifier("verify", "--work", str(work), "--diff", str(args.diff.resolve()))
    if code == 2:
        print(f"verimend verify: {result.get('error')}", file=sys.stderr)
        return 2
    result.pop("diff", None)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print_checks(result)
    return 0 if result.get("passed") else 1


def make_sandbox(
    project: Path, profiles: Path, target: str | None, db: Path, root: Path
) -> tuple[Path, Path, Path]:
    """Copy the project and its database; point a profile of our own at the copy."""
    sandbox_project = root / "project"
    copy_project(project, sandbox_project)
    # A project at the root of a git repository keeps its history in the sandbox, so
    # inspect_history can find the commit that broke the build. The verifier ignores .git.
    if (project / ".git").is_dir():
        shutil.copytree(project / ".git", sandbox_project / ".git")
    sandbox_db = copy_database(db, root / "db")
    profile = read_profile(project, profiles, target)
    sandbox_profiles = write_profiles(profile, sandbox_db, root / "profiles")
    return sandbox_project, sandbox_db, sandbox_profiles


def exit_code(run: Any) -> int:
    if run.error or run.verifier_error:
        return EXIT_ERROR
    if run.final_verdict == "fixed":
        return EXIT_FIXED
    if run.final_verdict == "escalate":
        return EXIT_ESCALATED
    if run.final_verdict is None and run.stopped_by_cap:
        return EXIT_CAP
    return EXIT_NOT_FIXED


def cmd_fix(args: argparse.Namespace) -> int:
    from verimend.agent.run import run_agent  # the SDK loads only for fix
    from verimend.hooks.caps import RunCaps
    from verimend.tools.verify import WorkspaceVerifier
    from verimend.tools.workspace import LocalWorkspace

    settings = load_settings()
    try:
        api_key = settings.require_api_key()
        project = args.project_dir.resolve()
        profiles = find_profiles_dir(project, args.profiles_dir)
        db = resolve_db(project, profiles, args.target, args.db)
    except (ConfigError, VerifierError) as error:
        print(f"verimend fix: {error}", file=sys.stderr)
        return EXIT_ERROR
    ctx = None
    if args.open_pr:  # check where we are before spending anything
        try:
            token = settings.github_token.get_secret_value() if settings.github_token else None
            ctx = github.context_from_env(os.environ, project, token)
        except github.GitHubError as error:
            print(f"verimend fix: {error}", file=sys.stderr)
            return EXIT_ERROR
        reason = github.refusal(ctx)
        if reason:
            print(f"verimend fix: not running: {reason}.", file=sys.stderr)
            return EXIT_ERROR
    root = settings.verimend_home / "sandboxes" / run_id()
    sandbox, sandbox_db, sandbox_profiles = make_sandbox(project, profiles, args.target, db, root)
    workspace = LocalWorkspace(sandbox, sandbox_db, {"DBT_PROFILES_DIR": str(sandbox_profiles)})
    verifier = WorkspaceVerifier(workspace, (root / "verify").as_posix())
    caps = RunCaps(max_usd=args.max_usd, max_steps=settings.max_steps,
                   max_attempts=args.max_attempts)  # fmt: skip
    model = args.model or settings.model_default
    print(f"Sandbox: {root}\nModel {model}, cap ${args.max_usd:.2f}, {args.max_attempts} attempts")
    run = asyncio.run(
        run_agent(
            args.task or DEFAULT_TASK,
            workspace,
            model=model,
            api_key=api_key,
            caps=caps,
            cwd=settings.verimend_home / "agent-cwd",
            output_chars=settings.tool_output_chars,
            verifier=verifier,
        )
    )
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(run.to_dict(), indent=1), "utf-8")
    diff = make_diff(project, sandbox)
    (out / "fix.diff").write_text(diff, "utf-8")
    report = run.report or {}
    cause = report.get("root_cause") or {}
    print(f"Verdict: {run.final_verdict or 'none'} after {len(run.attempts)} verifier attempt(s)")
    if cause:
        print(f"Root cause ({cause.get('category')}, {cause.get('model')}): {cause.get('summary')}")
    if run.attempts:
        print_checks(run.attempts[-1])
    cost = f"${run.cost_usd:.4f}" if run.cost_usd is not None else "unknown"
    print(f"Cost {cost}, {run.seconds} s, {run.num_turns} steps")
    if run.error or run.verifier_error:
        print(f"Error: {run.error or run.verifier_error}", file=sys.stderr)
    print(f"Wrote {out / 'run.json'} and {out / 'fix.diff'}; {project} is unchanged.")
    code = exit_code(run)
    if ctx is not None and code != EXIT_ERROR:
        try:
            url = hand_back(ctx, run, project, diff, root)
        except github.GitHubError as error:
            print(f"verimend fix: {error}", file=sys.stderr)
            return EXIT_ERROR
        print(url)
    return code


def hand_back(ctx: github.Context, run: Any, project: Path, diff: str, root: Path) -> str:
    """A pull request for a verified fix; a diagnosis comment for anything else."""
    out = root / "out"
    if run.final_verdict == "fixed":
        body = github.pr_body(run, diff, ctx)
        (out / "pull_request.md").write_text(body, "utf-8")
        url = github.open_pull_request(ctx, project, diff, github.pr_title(run), body,
                                       root / "pr-worktree")  # fmt: skip
        print(f"Opened a pull request from {github.fix_branch(ctx)} into {ctx.ref_name}:")
    else:
        body = github.diagnosis_body(run, ctx)
        (out / "diagnosis.md").write_text(body, "utf-8")
        url = github.post_diagnosis(ctx, body)
        where = f"pull request #{ctx.pr_number}" if ctx.pr_number else f"commit {ctx.short_sha}"
        print(f"Posted the diagnosis on {where}:")
    return url


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(prog="verimend", description=__doc__, formatter_class=raw)
    sub = parser.add_subparsers(dest="command", required=True)
    settings = load_settings()

    fix = sub.add_parser("fix", help="diagnose and fix a failed dbt project in a sandbox")
    verify = sub.add_parser("verify", help="check a diff with the verifier, no agent involved")
    for p in (fix, verify):
        p.add_argument("--project-dir", type=Path, default=Path("."))
        p.add_argument("--profiles-dir", type=Path)
        p.add_argument("--target")
        p.add_argument("--db", type=Path, help="the DuckDB file, if the profile does not say")
    fix.add_argument("--max-usd", type=float, default=settings.max_usd_per_run)
    fix.add_argument("--max-attempts", type=int, default=settings.max_attempts)
    fix.add_argument("--model", help="defaults to the configured model")
    fix.add_argument("--task", help="what is wrong, if the failure alone does not say")
    fix.add_argument(
        "--open-pr",
        action="store_true",
        help="open a pull request for a verified fix, or post the diagnosis as a comment",
    )
    verify.add_argument("--diff", type=Path, required=True)
    verify.add_argument("--json", action="store_true", help="print the verifier's JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "verify":
        return cmd_verify(args)
    if args.max_usd <= 0 or args.max_attempts < 1:
        print("verimend fix: --max-usd must be positive and --max-attempts at least 1",
              file=sys.stderr)  # fmt: skip
        return EXIT_ERROR
    return cmd_fix(args)


if __name__ == "__main__":
    raise SystemExit(main())
