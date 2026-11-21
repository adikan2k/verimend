"""List candidate real breakages: bug-fix PRs in Fivetran's dbt packages since DuckDB.

    uv run python -m bench.realworld.mine [--repos dbt_hubspot,dbt_zendesk] [--out DIR]

Fivetran's dbt packages (github.com/fivetran/dbt_*, Apache-2.0) added DuckDB as a
supported destination around August 2026. Each release section of a package's CHANGELOG
names the PRs it shipped. For every package whose CHANGELOG records DuckDB support, this
lists the releases after it, their PRs (merge date, merge commit, parent) and the
changelog lines that read like bug fixes. Picking the cases is done by hand from this
list (decision 0007).

Public GitHub only, through `gh`; nothing about the user is sent. No model calls.
Writes bench/results/realworld-mine-<date>/candidates.json.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from verimend.config import REPO_ROOT

OWNER = "fivetran"
SECTION = re.compile(r"(?m)^# (?=\S+ v\d)")
VERSION = re.compile(r"^(\S+) v(\S+)")
PR_LINK = re.compile(r"\[PR #(\d+)\]\(https://github\.com/fivetran/([\w.-]+)/pull/\d+\)")
DUCKDB_SUPPORT = re.compile(r"duckdb[^\n]*support|support[^\n]*duckdb", re.I)
FIX_WORDS = re.compile(r"\b(fix\w*|bug|error|fail\w*|resolv\w*|broke\w*|mismatch|overflow)\b", re.I)
# Fixes that cannot fail on DuckDB, by the changelog's own words.
OTHER_WAREHOUSE = re.compile(r"\b(bigquery|snowflake|redshift|databricks|postgres|sql ?server)"
                             r"[- ]?(specific|only)?\b", re.I)  # fmt: skip


@dataclass
class Release:
    version: str
    prs: list[int]
    fix_lines: list[str]
    headings: list[str]


@dataclass
class Candidate:
    repo: str
    kind: str
    versions: list[str]
    pr: int
    title: str = ""
    merged_at: str | None = None
    fix_sha: str | None = None
    parent_sha: str | None = None
    files: list[str] = field(default_factory=list)
    code_files: list[str] = field(default_factory=list)
    fix_lines: list[str] = field(default_factory=list)
    other_warehouse_only: bool = False


def releases(changelog: str) -> list[Release]:
    """A CHANGELOG's release sections, newest first."""
    found = []
    for section in SECTION.split(changelog):
        match = VERSION.match(section)
        if not match:
            continue
        lines = section.splitlines()
        prs = sorted({int(n) for n, _ in PR_LINK.findall(section)})
        fix_lines = [
            line.strip()
            for line in lines[1:]
            if line.lstrip().startswith(("-", "*", "|")) and FIX_WORDS.search(line)
        ]
        headings = [line.lstrip("# ").strip() for line in lines[1:] if line.startswith("##")]
        found.append(Release(match.group(2), prs, fix_lines, headings))
    return found


def since_duckdb(changelog: str) -> tuple[str | None, list[Release]]:
    """The release that added DuckDB support, and the releases after it (newest first)."""
    found = releases(changelog)
    # Newest first, so the last release that mentions DuckDB support is where it was added.
    for index in range(len(found) - 1, -1, -1):
        if DUCKDB_SUPPORT.search(_section_text(changelog, found[index].version)):
            return found[index].version, found[:index]
    return None, []


def _section_text(changelog: str, version: str) -> str:
    for section in SECTION.split(changelog):
        match = VERSION.match(section)
        if match and match.group(2) == version:
            return section
    return ""


def is_code_file(path: str) -> bool:
    """SQL and YAML that dbt reads, outside docs and the integration tests' seeds."""
    if path.startswith(
        ("docs/", ".github/", ".buildkite/", ".changes/", "integration_tests/seeds/")
    ):
        return False
    return path.endswith((".sql", ".yml", ".yaml")) and path not in (
        "dbt_project.yml",
        "packages.yml",  # a case lists vendored packages instead (bench/realworld/lib.py)
        "integration_tests/dbt_project.yml",
        "integration_tests/ci/test_scenarios.yml",
        ".quickstart/quickstart.yml",
    )


def gh(*args: str) -> Any:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def list_repos() -> list[str]:
    jq = '.[] | select(.name|startswith("dbt_")) | select(.isArchived|not) | .name'
    out = gh("repo", "list", OWNER, "--limit", "500", "--json", "name,isArchived", "--jq", jq)
    return sorted(out.split())


def fetch_changelog(repo: str) -> str | None:
    try:
        return gh("api", f"repos/{OWNER}/{repo}/contents/CHANGELOG.md",
                  "-H", "Accept: application/vnd.github.raw")  # fmt: skip
    except RuntimeError:
        return None


def fill_pr(candidate: Candidate) -> None:
    pr = json.loads(gh("api", f"repos/{OWNER}/{candidate.repo}/pulls/{candidate.pr}"))
    candidate.title = pr.get("title") or ""
    candidate.merged_at = pr.get("merged_at")
    candidate.fix_sha = pr.get("merge_commit_sha") if candidate.merged_at else None
    if candidate.fix_sha:
        commit = json.loads(
            gh("api", f"repos/{OWNER}/{candidate.repo}/commits/{candidate.fix_sha}")
        )
        parents = commit.get("parents") or []
        candidate.parent_sha = parents[0]["sha"] if parents else None
    files = gh("api", "--paginate", f"repos/{OWNER}/{candidate.repo}/pulls/{candidate.pr}/files",
               "--jq", ".[].filename")  # fmt: skip
    candidate.files = files.split()
    candidate.code_files = [f for f in candidate.files if is_code_file(f)]


def mine(repos: list[str]) -> dict[str, Any]:
    """Two kinds of candidate, each PR once:
    bug_fix      a PR of a release after DuckDB support whose notes read like a bug fix
    duckdb_port  the PR that added DuckDB support: its parent is code that did not run on
                 DuckDB until a human adapted it
    """
    packages: dict[str, Any] = {}
    found: dict[tuple[str, int], Candidate] = {}
    for repo in repos:
        changelog = fetch_changelog(repo)
        if changelog is None:
            packages[repo] = {"duckdb_since": None, "note": "no CHANGELOG.md"}
            continue
        added, later = since_duckdb(changelog)
        packages[repo] = {"duckdb_since": added, "releases_since": [r.version for r in later]}
        port = [r for r in releases(changelog) if r.version == added]
        picks = [(r, "bug_fix") for r in later if r.fix_lines]
        picks += [(r, "duckdb_port") for r in port]
        for release, kind in picks:
            lines = release.fix_lines
            if kind == "duckdb_port":
                section = _section_text(changelog, release.version)
                lines = [line.strip() for line in section.splitlines()
                         if DUCKDB_SUPPORT.search(line)]  # fmt: skip
            for pr in release.prs:
                if (repo, pr) in found:  # a pre-release and its release list the same PR
                    found[(repo, pr)].versions.append(release.version)
                    continue
                candidate = Candidate(repo, kind, [release.version], pr, fix_lines=lines)
                candidate.other_warehouse_only = kind == "bug_fix" and all(
                    OTHER_WAREHOUSE.search(line) for line in lines
                )
                try:
                    fill_pr(candidate)
                except RuntimeError as exc:  # e.g. a link to another repository's PR
                    candidate.title = f"not found: {exc}"[:200]
                found[(repo, pr)] = candidate
    return {"packages": packages, "candidates": [asdict(c) for c in found.values()]}


def main(argv: list[str] | None = None) -> int:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="python -m bench.realworld.mine", description=__doc__, formatter_class=raw
    )
    parser.add_argument("--repos", help="comma-separated repositories (default: every dbt_*)")
    parser.add_argument("--out", type=Path, help="default bench/results/realworld-mine-<date>")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # changelogs hold non-ASCII; Windows prints cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    repos = args.repos.split(",") if args.repos else list_repos()
    found = mine(repos)
    with_duckdb = [r for r, p in found["packages"].items() if p.get("duckdb_since")]
    print(f"{len(repos)} packages, {len(with_duckdb)} record DuckDB support")
    for c in found["candidates"]:
        files = f"{len(c['code_files'])} code files"
        flag = "other-warehouse" if c["other_warehouse_only"] else files
        merged = (c["merged_at"] or "-")[:10]
        print(f"{c['kind']:11} {c['repo']} v{c['versions'][0]} PR #{c['pr']} merged {merged}"
              f" ({flag})")  # fmt: skip
        for line in c["fix_lines"]:
            print(f"    {line[:160]}")
    out = args.out or REPO_ROOT / "bench" / "results" / f"realworld-mine-{date.today():%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates.json").write_text(json.dumps(found, indent=1) + "\n", "utf-8")
    print(f"{len(found['candidates'])} candidate PRs. Wrote {out / 'candidates.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
