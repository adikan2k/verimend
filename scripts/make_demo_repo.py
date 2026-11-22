"""Build the demo repository for the GitHub Action, and its deliberately broken commits.

    uv run python scripts/make_demo_repo.py build --dest ~/.verimend/demo-repo --action-ref main
    uv run python scripts/make_demo_repo.py break logic --repo ~/.verimend/demo-repo
    uv run python scripts/make_demo_repo.py break bad-data --repo ~/.verimend/demo-repo

`build` writes demo/jaffle_shop at the repository root (so VeriMend can read its
history), demo/raw_data as raw_data/, a standalone raw-data loader, the licence and
notice, a README and a workflow that calls the Action when `dbt build` fails, then makes
the baseline commit. Each `break` adds one commit:

    logic     "Update order totals": orders takes the largest payment, not the sum.
              The right outcome is a pull request that restores sum (like fault lg02).
    bad-data  reverts the logic commit if it is there, then "Load the November payments
              export": three payments with negative amounts. The models are right, so the
              right outcome is an escalation (like fault bd01).

`template` builds the public inspector demo instead (phase 6e, decision 0010): the same
project with the inspector on every pull request, an opt-in fixer that needs the copy's
own ANTHROPIC_API_KEY secret, and three branches to open as pull requests:

    uv run python scripts/make_demo_repo.py template --dest ~/.verimend/inspector-demo

    try/weaken-test  not_null on customers.customer_id set to warn: fails at tests.
    try/null-month   monthly revenue whose returned orders' payments land in a NULL month:
                     fails at row accounting (rule B).
    try/good-change  the same model with an inner join and a test: passes.

Nothing here pushes. The commits use your own git identity.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "demo"
SKIP = shutil.ignore_patterns("target", "logs", "*.duckdb", "*.duckdb.wal", ".user.yml")
LOGIC_MESSAGE = "Update order totals"
BAD_DATA_MESSAGE = "Load the November payments export"
NEGATIVE_PAYMENTS = [
    "114,21,credit_card,-1500",
    "115,57,bank_transfer,-2300",
    "116,83,coupon,-900",
]

TEMPLATES = Path(__file__).resolve().parent / "demo_repo"  # load_raw.py, README.md, dbt.yml
INSPECTOR = TEMPLATES / "template"  # the public inspector demo's own files
TRY_BRANCHES = ("try/weaken-test", "try/null-month", "try/good-change")
WEAKENED_BLOCK = (
    "      - name: customer_id\n"
    "        description: This is a unique identifier for a customer\n"
    "        data_tests:\n"
    "          - unique\n"
    "          - not_null\n"
)
GITIGNORE = "target/\nlogs/\n*.duckdb\n*.duckdb.wal\n.user.yml\n__pycache__/\n"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8")  # fmt: skip
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def copy_project(dest: Path) -> None:
    """The jaffle shop at the root, its raw data and loader, the licence and notice."""
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DEMO / "jaffle_shop", dest, ignore=SKIP, dirs_exist_ok=True)
    shutil.copytree(DEMO / "raw_data", dest / "raw_data")
    shutil.copy2(DEMO / "LICENSE", dest / "LICENSE")
    notice = (
        (DEMO / "NOTICE")
        .read_text(encoding="utf-8")
        .replace(
            "demo/jaffle_shop and demo/raw_data are adapted",
            "This repository (the dbt project and raw_data/) is adapted",
        )
    )
    (dest / "scripts").mkdir()
    write(dest / "NOTICE", notice + "- In this repository the project sits at the root, and "
          "scripts/load_raw.py loads raw_data/ into warehouse.duckdb.\n")  # fmt: skip
    shutil.copy2(TEMPLATES / "load_raw.py", dest / "scripts" / "load_raw.py")
    write(dest / ".gitignore", GITIGNORE)


def build(dest: Path, action_ref: str) -> None:
    copy_project(dest)
    workflow = (TEMPLATES / "dbt.yml").read_text(encoding="utf-8")
    write(dest / ".github" / "workflows" / "dbt.yml", workflow.replace("ACTION_REF", action_ref))
    shutil.copy2(TEMPLATES / "README.md", dest / "README.md")
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "Jaffle shop models and CI")
    print(f"Built {dest} at {git(dest, 'rev-parse', '--short', 'HEAD')}")


def template(dest: Path, verify_ref: str, fix_ref: str) -> None:
    copy_project(dest)
    workflows = dest / ".github" / "workflows"
    inspector = (INSPECTOR / "inspector.yml").read_text(encoding="utf-8")
    write(workflows / "inspector.yml", inspector.replace("VERIFY_REF", verify_ref))
    fix = (INSPECTOR / "fix.yml").read_text(encoding="utf-8")
    fix = fix.replace("FIX_REF", fix_ref).replace("DBT_VERSION", version("dbt-core"))
    write(workflows / "fix.yml", fix.replace("DBT_DUCKDB_VERSION", version("dbt-duckdb")))
    shutil.copy2(INSPECTOR / "README.md", dest / "README.md")
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "Jaffle shop models and the inspector")

    git(dest, "checkout", "-q", "-b", TRY_BRANCHES[0])
    schema = dest / "models" / "marts" / "schema.yml"
    text = schema.read_text(encoding="utf-8")
    old = WEAKENED_BLOCK
    if text.replace("\r\n", "\n").count(old) != 1:
        raise SystemExit(f"expected one customers.customer_id test block in {schema}")
    weakened = old.replace("- not_null\n", "- not_null:\n              config:\n"
                           "                severity: warn\n")  # fmt: skip
    write(schema, text.replace("\r\n", "\n").replace(old, weakened))
    git(dest, "commit", "-q", "-am", "Let the customer id check warn instead of fail")

    for branch, message in (
        (TRY_BRANCHES[1], "Monthly revenue, without returned orders"),
        (TRY_BRANCHES[2], "Monthly revenue, without returned orders"),
    ):
        git(dest, "checkout", "-q", "-b", branch, "main")
        source = INSPECTOR / "try" / branch.removeprefix("try/")
        for name in ("monthly_revenue.sql", "monthly_revenue.yml"):
            shutil.copy2(source / name, dest / "models" / "marts" / name)
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", message)
    git(dest, "checkout", "-q", "main")
    print(f"Built {dest} at {git(dest, 'rev-parse', '--short', 'HEAD')}, with "
          + ", ".join(TRY_BRANCHES))  # fmt: skip


def break_logic(repo: Path) -> None:
    path = repo / "models" / "marts" / "orders.sql"
    text = path.read_text(encoding="utf-8")
    old, new = "sum(amount) as total_amount", "max(amount) as total_amount"
    if text.count(old) != 1:
        raise SystemExit(f"expected one {old!r} in {path}")
    write(path, text.replace(old, new))
    git(repo, "commit", "-q", "-am", LOGIC_MESSAGE)


def break_bad_data(repo: Path) -> None:
    subjects = git(repo, "log", "--format=%H %s").splitlines()
    logic = next((line.split(" ", 1)[0] for line in subjects
                  if line.split(" ", 1)[1] == LOGIC_MESSAGE), None)  # fmt: skip
    reverted = any(
        line.split(" ", 1)[1].startswith(f'Revert "{LOGIC_MESSAGE}"') for line in subjects
    )
    if logic and not reverted:
        git(repo, "revert", "--no-edit", logic)
    path = repo / "raw_data" / "payments.csv"
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    write(path, text + "\n" + "\n".join(NEGATIVE_PAYMENTS) + "\n")
    git(repo, "commit", "-q", "-am", BAD_DATA_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the demo repository and its broken commits."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dest", type=Path, required=True)
    b.add_argument("--action-ref", default="main")
    m = sub.add_parser("template", help="the public inspector demo and its try/* branches")
    m.add_argument("--dest", type=Path, required=True)
    m.add_argument("--verify-ref", default="verify-v1")
    m.add_argument("--fix-ref", default="action-v1")
    k = sub.add_parser("break")
    k.add_argument("case", choices=["logic", "bad-data"])
    k.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        build(args.dest.expanduser().resolve(), args.action_ref)
    elif args.command == "template":
        template(args.dest.expanduser().resolve(), args.verify_ref, args.fix_ref)
    elif args.case == "logic":
        break_logic(args.repo.expanduser().resolve())
    else:
        break_bad_data(args.repo.expanduser().resolve())
    if args.command == "break":
        repo = args.repo.expanduser().resolve()
        print(f"{git(repo, 'log', '-1', '--format=%h %s')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
