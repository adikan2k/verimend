"""The real-world case contract, and building a case into a folder offline.

A case is a folder bench/realworld/<id>/ (decision 0007):

    case.json          the PR it comes from (repository, number, parent and fix commits,
                       merge date), its licence and attribution, the scenario vars, the
                       nodes that fail at the parent, and a neutral commit message
    reference/fix.diff the human fix, limited to the case's files (`files` in case.json)

Fivetran's packages test themselves with an `integration_tests` project that installs the
package as `local: ../` and loads seed CSVs. That layout needs the network (`dbt deps`)
and a project folder that reaches outside itself. A case is built differently, so any
tool that takes one project folder can run it with no network:

    project/   the package at the parent commit as the root dbt project, a git repository
               with one neutral commit. Its dbt_project.yml gains the integration
               project's profile, vars, dispatch and flags; its packages.yml lists the
               vendored packages under vendor/ as `local:` packages; the integration
               project's singular tests sit in tests/integration_tests/.
    db/        warehouse.duckdb: the seeds, loaded by `dbt seed` as the integration project
               does, read as sources. The agent can read the data but not edit it.
    pristine/  the same database, kept for grading
    broken/    the project's files as built, kept for grading (tests_unchanged)
    key/       the package at the fix commit, built the same way: the answer key's source
    profiles/  profiles.yml for the case's profile, pointed at db/

The harness (seeds, seed config, vars, singular tests) always comes from the fix commit:
a fix PR often adds the seed rows that show its bug. So a case is "the package as shipped
at the parent, run on data that triggers the bug".

The network is needed once per case, by `prepare`, which fills a cache under
~/.verimend/realworld/cache/<id>/: both trees, the vendored packages and the seeded
database. Everything after that is offline and the same on every build.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bench.faults.dbt import Build, run_dbt
from bench.faults.lib import remove_tree

CASES_DIR = Path(__file__).resolve().parent
DB_NAME = "warehouse.duckdb"  # DuckDB names the catalog after the file: keep it
TARGET = "dev"
KINDS = ("bug_fix", "duckdb_port")
# Fixed identity and date, so a case's commit hashes the same on every machine.
GIT_AUTHOR = ("Analytics Engineering", "analytics@warehouse.example")
COMMIT_DATE = "2026-11-02T09:00:00Z"
GITIGNORE = "target/\nlogs/\ndbt_packages/\n*.duckdb\n*.duckdb.wal\n.user.yml\n"
# Left out of a case's project: release tooling, generated docs, the integration project
# (folded into the root), build output.
DROP = {".github", ".buildkite", ".quickstart", ".changes", "docs", "integration_tests",
        "target", "logs", "dbt_packages", "package-lock.yml"}  # fmt: skip
VENDOR = "vendor"
SKIP_FILES = shutil.ignore_patterns("target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal",
                                    ".user.yml")  # fmt: skip


class CaseError(Exception):
    """A case that does not follow the contract, or a build step that failed."""


@dataclass(frozen=True)
class Case:
    case_id: str
    kind: str
    repo: str  # owner/name
    pr: int
    parent_sha: str
    fix_sha: str
    merged_at: str
    license: str
    attribution: str
    changelog: tuple[str, ...]
    files: tuple[str, ...]
    failing: tuple[str, ...]
    commit_message: str
    description: str
    vars: dict[str, Any] = field(default_factory=dict)
    seed_schema: str | None = None

    @property
    def pr_url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.pr}"

    @classmethod
    def load(cls, case_dir: Path) -> Case:
        path = case_dir / "case.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CaseError(f"{case_dir.name}: cannot read case.json: {exc}") from exc
        missing = [k for k in ("kind", "repo", "pr", "parent_sha", "fix_sha", "merged_at",
                               "license", "attribution", "files", "failing", "commit_message",
                               "description") if k not in data]  # fmt: skip
        if missing:
            raise CaseError(f"{case_dir.name}/case.json: missing {', '.join(missing)}")
        case = cls(
            case_id=case_dir.name,
            kind=data["kind"],
            repo=data["repo"],
            pr=int(data["pr"]),
            parent_sha=data["parent_sha"],
            fix_sha=data["fix_sha"],
            merged_at=data["merged_at"],
            license=data["license"],
            attribution=data["attribution"],
            changelog=tuple(data.get("changelog") or ()),
            files=tuple(sorted(data["files"])),
            failing=tuple(sorted(data["failing"])),
            commit_message=data["commit_message"],
            description=data["description"],
            vars=dict(data.get("vars") or {}),
            seed_schema=data.get("seed_schema"),
        )
        problems = []
        if case.kind not in KINDS:
            problems.append(f"kind must be one of {KINDS}")
        if case.license != "Apache-2.0":
            problems.append("only Apache-2.0 packages are used")
        if not all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (case.parent_sha, case.fix_sha)):
            problems.append("parent_sha and fix_sha must be full commit hashes")
        if not case.files:
            problems.append("a case needs the files its reference changes")
        if any(f.startswith(("integration_tests/", "docs/")) for f in case.files):
            problems.append("case files are package code, never the harness or docs")
        if not case.failing:
            problems.append("a case must make something fail")
        if _leaks(case.commit_message, case):
            problems.append("the commit message must not point at the fix")
        if problems:
            raise CaseError(f"{case_dir.name}/case.json: " + "; ".join(problems))
        return case


def case_ids(cases_dir: Path = CASES_DIR) -> list[str]:
    return sorted(p.parent.name for p in cases_dir.glob("*/case.json"))


def _leaks(text: str, case: Case) -> bool:
    """Whether `text` points at the fix: its PR, its URL or its commit."""
    pattern = rf"(pull/{case.pr}\b|PR #{case.pr}\b|#{case.pr}\b|{case.fix_sha[:7]})"
    return re.search(pattern, text) is not None


# ---- git and dbt ----------------------------------------------------------------------


def git(cwd: Path, *args: str, date: str | None = None, binary: bool = False) -> Any:
    """git with a fixed identity and, for commits, a fixed date. Signing and line-ending
    rewrites are off: these are throwaway repositories."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": GIT_AUTHOR[0],
           "GIT_AUTHOR_EMAIL": GIT_AUTHOR[1], "GIT_COMMITTER_NAME": GIT_AUTHOR[0],
           "GIT_COMMITTER_EMAIL": GIT_AUTHOR[1]}  # fmt: skip
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    command = ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
               "-c", "init.defaultBranch=main", *args]  # fmt: skip
    done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, check=False)
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip()[-400:]
        raise CaseError(f"git {' '.join(args)} failed: {err}")
    return done.stdout if binary else done.stdout.decode("utf-8", "replace")


def write_profile(profile: str, db: Path, out_dir: Path, schema: str = "main") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"{profile}:\n  target: {TARGET}\n  outputs:\n    {TARGET}:\n"
        f"      type: duckdb\n      path: '{db.resolve().as_posix()}'\n"
        f"      schema: {schema}\n      threads: 4\n"
    )
    (out_dir / "profiles.yml").write_text(text, "utf-8")
    return out_dir


def deps_and(project: Path, profiles_dir: Path, *args: str) -> Build:
    """`dbt deps` (every package is local, so no network), then `dbt <args>`."""
    deps = run_dbt(project, profiles_dir, "deps")
    if deps.code != 0:
        return Build(deps.code, deps.output, {}, None)
    return run_dbt(project, profiles_dir, *args)


# ---- the integration project folded into the package ----------------------------------


def _blocks(text: str) -> list[tuple[str | None, str]]:
    """A YAML file's top-level blocks as (key, text), comments kept with the block above."""
    blocks: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines(keepends=True):
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if match:
            blocks.append((match.group(1), []))
        blocks[-1][1].append(line)
    return [(key, "".join(lines)) for key, lines in blocks if lines]


def merge_vars(package: dict[str, Any], harness: dict[str, Any],
               scenario: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    """The package's vars, then the integration project's, then the scenario's.

    A top-level dict is a package scope and merges key by key. Scenario vars stand for
    `--vars` on the command line, which beats every scope, so they are set in each scope
    that names them too."""
    merged: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in package.items()}
    for key, value in harness.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    for key, value in scenario.items():
        merged[key] = value
        for scope in merged.values():
            if isinstance(scope, dict) and key in scope:
                scope[key] = value
    return merged


def fold_project(package_text: str, harness: dict[str, Any], scenario: dict[str, Any]) -> str:
    """The package's dbt_project.yml as the root project of a case. Only the profile, vars,
    dispatch and flags change; the rest of the file keeps its text."""
    package = yaml.safe_load(package_text) or {}
    profile = harness.get("profile")
    if not profile:
        raise CaseError("the integration project names no profile")
    merged = merge_vars(package.get("vars") or {}, harness.get("vars") or {}, scenario)
    dumped = {
        "profile": f"profile: '{profile}'\n",
        "vars": yaml.safe_dump({"vars": merged}, sort_keys=False, width=1000),
    }
    out: list[str] = []
    seen: set[str] = set()
    for key, text in _blocks(package_text):
        if key in dumped:
            out.append(dumped[key] if key not in seen else "")
            seen.add(key)
        else:
            out.append(text)
    text = "".join(out)
    if not text.endswith("\n"):
        text += "\n"
    for key in ("profile", "vars"):
        if key not in seen:
            text += dumped[key]
    for key in ("dispatch", "flags"):
        if key in harness and key not in package:
            text += yaml.safe_dump({key: harness[key]}, sort_keys=False, width=1000)
    return text


def strip_changelog(text: str, case: Case) -> str:
    """CHANGELOG.md without any release section that points at the fix, so neither the
    files nor `git log` give the answer away."""
    sections = re.split(r"(?m)^(?=# )", text)
    return "".join(s for s in sections if not _leaks(s, case))


def vendor_packages(project: Path, vendored: Path) -> list[str]:
    """Copy the vendored packages into project/vendor/ and list them as local packages.
    Their own package files are removed, so `dbt deps` never needs the network."""
    names = sorted(p.name for p in vendored.iterdir() if p.is_dir()) if vendored.is_dir() else []
    for name in names:
        dest = project / VENDOR / name
        shutil.copytree(vendored / name, dest, ignore=SKIP_FILES)
        for stale in ("packages.yml", "dependencies.yml", "package-lock.yml"):
            (dest / stale).unlink(missing_ok=True)
    lines = ["packages:\n"] + [f"  - local: {VENDOR}/{name}\n" for name in names]
    (project / "packages.yml").write_text("".join(lines) if names else "packages: []\n", "utf-8")
    (project / "dependencies.yml").unlink(missing_ok=True)
    return names


def _is_text(path: Path) -> bool:
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def transplant(tree: Path, harness: Path, vendored: Path, case: Case, dest: Path) -> None:
    """Make `dest` a self-contained case project from a package `tree` and the fix's
    integration project `harness` (see the module docstring)."""
    remove_tree(dest)
    dest.mkdir(parents=True)
    for item in sorted(tree.iterdir()):
        if item.name in DROP or item.name == ".git":
            continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name, ignore=SKIP_FILES)
        else:
            shutil.copy2(item, dest / item.name)
    harness_project = yaml.safe_load((harness / "dbt_project.yml").read_text("utf-8")) or {}
    project_file = dest / "dbt_project.yml"
    folded = fold_project(project_file.read_text("utf-8"), harness_project, case.vars)
    project_file.write_text(folded, "utf-8", newline="\n")
    for sub, into in (("tests", "tests"), ("macros", "macros")):
        if (harness / sub).is_dir():
            shutil.copytree(harness / sub, dest / into / "integration_tests", ignore=SKIP_FILES)
    vendor_packages(dest, vendored)
    for path in sorted(dest.rglob("*")):  # README images: graders read every file as text
        if path.is_file() and not _is_text(path):
            path.unlink()
    changelog = dest / "CHANGELOG.md"
    if changelog.is_file():
        changelog.write_text(strip_changelog(changelog.read_text("utf-8"), case), "utf-8",
                             newline="\n")  # fmt: skip
    (dest / ".gitignore").write_text(GITIGNORE, "utf-8", newline="\n")


# ---- the cache filled from the network -------------------------------------------------


def export_tree(repo_dir: Path, sha: str, dest: Path) -> None:
    """The files of `sha`, without a checkout (git archive)."""
    remove_tree(dest)
    archive = git(repo_dir, "archive", "--format=zip", sha, binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        zipped.extractall(dest)


def find_seed_schema(harness: Path, harness_project: dict[str, Any]) -> str | None:
    """The schema the integration project's sources read: the value of the var its CI
    names in ci/test_scenarios.yml."""
    scenarios_file = harness / "ci" / "test_scenarios.yml"
    if not scenarios_file.is_file():
        return None
    scenarios = yaml.safe_load(scenarios_file.read_text("utf-8")) or {}
    name = scenarios.get("schema_variable_name")
    variables = harness_project.get("vars") or {}
    if name in variables:
        return str(variables[name])
    for scope in variables.values():
        if isinstance(scope, dict) and name in scope:
            return str(scope[name])
    return None


@dataclass(frozen=True)
class Prepared:
    parent: Path
    fix: Path
    vendor: Path
    db: Path
    info: dict[str, Any]

    @classmethod
    def at(cls, cache: Path) -> Prepared:
        info = json.loads((cache / "prepared.json").read_text("utf-8"))
        return cls(cache / "parent", cache / "fix", cache / "vendor", cache / DB_NAME, info)


def prepare(case: Case, cache: Path, repos: Path) -> Prepared:
    """Fill the case's cache, once: both trees, the vendored packages, the seeded
    database. The only step that needs the network (git clone, dbt deps)."""
    if (cache / "prepared.json").is_file():
        return Prepared.at(cache)
    remove_tree(cache)
    cache.mkdir(parents=True)
    repo_dir = repos / case.repo.split("/")[1]
    if not repo_dir.is_dir():
        repos.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{case.repo}.git"
        git(repos, "clone", "-q", "--filter=blob:none", url, str(repo_dir))
    for sha in (case.parent_sha, case.fix_sha):
        export_tree(repo_dir, sha, cache / ("parent" if sha == case.parent_sha else "fix"))

    scratch = cache / "seed-scratch"
    shutil.copytree(cache / "fix", scratch)
    harness = scratch / "integration_tests"
    harness_project = yaml.safe_load((harness / "dbt_project.yml").read_text("utf-8")) or {}
    schema = case.seed_schema or find_seed_schema(harness, harness_project)
    if not schema:
        raise CaseError(f"{case.case_id}: cannot tell the seed schema; set seed_schema")
    profiles = write_profile(str(harness_project.get("profile")), cache / DB_NAME,
                             scratch / "profiles", schema)  # fmt: skip
    deps = run_dbt(harness, profiles, "deps")
    if deps.code != 0:
        raise CaseError(f"{case.case_id}: dbt deps failed: {deps.output[-600:]}")
    seed = run_dbt(harness, profiles, "seed", "--full-refresh", "--vars", json.dumps(case.vars))
    if seed.code != 0:
        raise CaseError(f"{case.case_id}: dbt seed failed: {seed.output[-600:]}")
    package_name = (yaml.safe_load((scratch / "dbt_project.yml").read_text("utf-8")) or {})["name"]
    for installed in sorted((harness / "dbt_packages").iterdir()):
        meta = yaml.safe_load((installed / "dbt_project.yml").read_text("utf-8")) or {}
        if meta.get("name") == package_name:
            continue  # the package itself, installed from ../
        shutil.copytree(installed, cache / "vendor" / installed.name, ignore=SKIP_FILES)
    # What dbt deps resolved (a package's own dbt_project.yml version is often stale).
    lock_file = harness / "package-lock.yml"
    lock = yaml.safe_load(lock_file.read_text("utf-8")) if lock_file.is_file() else {}
    versions = {str(p.get("package") or p.get("local")): str(p.get("version", "local"))
                for p in (lock or {}).get("packages") or []}  # fmt: skip
    remove_tree(scratch)
    info = {"seed_schema": schema, "packages": versions, "profile": harness_project["profile"],
            "package": package_name}  # fmt: skip
    (cache / "prepared.json").write_text(json.dumps(info, indent=1) + "\n", "utf-8")
    return Prepared.at(cache)


# ---- building a case -------------------------------------------------------------------


@dataclass
class Built:
    case: Case
    root: Path
    project: Path
    db: Path
    pristine: Path
    broken: Path
    key: Path
    profiles: Path
    fingerprint: str
    commits: list[str]


def materialize(case: Case, prepared: Prepared, dest: Path) -> Built:
    """Build one case into `dest` from its cache, replacing whatever was there. Offline."""
    remove_tree(dest)
    harness = prepared.fix / "integration_tests"
    project = dest / "project"
    transplant(prepared.parent, harness, prepared.vendor, case, project)
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", case.commit_message, date=COMMIT_DATE)
    key = dest / "key"
    transplant(prepared.fix, harness, prepared.vendor, case, key)

    db = dest / "db" / DB_NAME
    db.parent.mkdir(parents=True)
    shutil.copy2(prepared.db, db)
    pristine = dest / "pristine" / DB_NAME
    pristine.parent.mkdir(parents=True)
    shutil.copy2(prepared.db, pristine)
    broken = dest / "broken"
    shutil.copytree(project, broken, ignore=shutil.ignore_patterns(".git", "target", "logs",
                                                                   "dbt_packages"))  # fmt: skip
    profiles = write_profile(str(prepared.info["profile"]), db, dest / "profiles")
    commits = git(project, "log", "--format=%H %s").splitlines()
    return Built(case, dest, project, db, pristine, broken, key, profiles,
                 fingerprint(project, pristine, commits), commits)  # fmt: skip


def project_files(project: Path) -> dict[str, bytes]:
    """Every file of a project, without git, build output, installed packages or data."""
    found: dict[str, bytes] = {}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if not path.is_file() or rel.parts[0] in (".git", "target", "logs", "dbt_packages"):
            continue
        if path.name.endswith((".duckdb", ".duckdb.wal")) or path.name == ".user.yml":
            continue
        found[rel.as_posix()] = path.read_bytes().replace(b"\r\n", b"\n")
    return found


def table_fingerprint(db: Path) -> dict[str, str]:
    """Per table of every schema: a hash of its columns, their types and its sorted rows."""
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "order by table_schema, table_name"
        ).fetchall()
        found: dict[str, str] = {}
        for schema, table in tables:
            columns = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = ? and table_name = ? order by ordinal_position",
                [schema, table],
            ).fetchall()
            query = f'select * from "{schema}"."{table}"'
            rows = sorted(repr(r) for r in con.execute(query).fetchall())
            digest = hashlib.sha256(repr(columns).encode())
            for row in rows:
                digest.update(row.encode())
            found[f"{schema}.{table}"] = digest.hexdigest()
        return found
    finally:
        con.close()


def fingerprint(project: Path, db: Path, commits: list[str]) -> str:
    """One hash of the project's files, the data and the commit history."""
    digest = hashlib.sha256()
    for rel, data in project_files(project).items():
        digest.update(rel.encode() + b"\n" + data + b"\n")
    for table, value in table_fingerprint(db).items():
        digest.update(f"{table} {value}\n".encode())
    for commit in commits:
        digest.update(f"{commit}\n".encode())
    return digest.hexdigest()


def leaks_in(project: Path, case: Case) -> list[str]:
    """Files, and the git log, that point at the fix. Must be empty."""
    found = [rel for rel, data in project_files(project).items()
             if _leaks(data.decode("utf-8", "replace"), case)]  # fmt: skip
    if (project / ".git").is_dir() and _leaks(git(project, "log", "--format=%H %an %s %b"), case):
        found.append("git log")
    return found
