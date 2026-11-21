"""The fault contract, and building a fault into a folder the same way every time.

A fault is a folder bench/faults/<id>/ (SPEC section 7):

    inject.py      inject(ctx: FaultContext) changes the project files or the raw tables
    expected.json  kind, outcome, accepted root-cause categories, the nodes that fail,
                   and the commit message for a fault that changes code
    checks/*.sql   optional extra hidden checks: each must return no rows
    reference/     fix.diff (against the faulted project) or escalation.json

`materialize` builds one fault into a folder:

    <dest>/project/   the demo project as a git repository: a baseline commit, then the
                      fault's own commit when it changes code (a data fault leaves none)
    <dest>/db/        warehouse.duckdb with the raw tables, the fault applied
    <dest>/pristine/  the same database, kept for grading
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bench.faults.demo import DB_NAME, DEMO_PROJECT, build_database, raw_fingerprint

FAULTS_DIR = Path(__file__).resolve().parent
SUITE_SEED = 1119
KINDS = {"schema_drift": 4, "logic": 4, "bad_data": 4, "config": 3, "tamper_trap": 3,
         "unfixable": 2}  # fmt: skip
# The agent's root-cause categories (verimend/tools/report.py).
CATEGORIES = ("schema_drift", "logic", "bad_data", "config", "missing_source", "unknown")
SKIP = shutil.ignore_patterns("target", "logs", "*.duckdb", "*.duckdb.wal", ".user.yml")
GITIGNORE = "target/\nlogs/\n*.duckdb\n*.duckdb.wal\n.user.yml\n"

# Fixed identity and dates, so a fault's commits hash the same on every machine.
GIT_AUTHOR = ("Shop Analytics", "analytics@jaffle.example")
BASELINE_DATE = "2026-11-02T09:00:00Z"
FAULT_DATE = "2026-11-16T14:30:00Z"


class FaultError(Exception):
    """A fault that does not follow the contract."""


@dataclass(frozen=True)
class Expected:
    fault_id: str
    kind: str
    outcome: Literal["fix", "escalate"]
    categories: tuple[str, ...]
    failing: tuple[str, ...]
    description: str
    commit_message: str | None = None

    @classmethod
    def load(cls, fault_dir: Path) -> Expected:
        data = json.loads((fault_dir / "expected.json").read_text(encoding="utf-8"))
        expected = cls(
            fault_id=fault_dir.name,
            kind=data["kind"],
            outcome=data["outcome"],
            categories=tuple(data["categories"]),
            failing=tuple(sorted(data["failing"])),
            description=data["description"],
            commit_message=data.get("commit_message"),
        )
        problems = []
        if expected.kind not in KINDS:
            problems.append(f"unknown kind {expected.kind!r}")
        if expected.outcome not in ("fix", "escalate"):
            problems.append(f"unknown outcome {expected.outcome!r}")
        if not expected.categories or set(expected.categories) - set(CATEGORIES):
            problems.append(f"categories must be some of {CATEGORIES}")
        if not expected.failing:
            problems.append("a fault must make something fail")
        if problems:
            raise FaultError(f"{fault_dir.name}/expected.json: " + "; ".join(problems))
        return expected


def fault_ids(faults_dir: Path = FAULTS_DIR) -> list[str]:
    return sorted(p.parent.name for p in faults_dir.glob("*/expected.json"))


def load_inject(fault_dir: Path) -> Callable[[FaultContext], object]:
    spec = importlib.util.spec_from_file_location(
        f"fault_{fault_dir.name}", fault_dir / "inject.py"
    )
    if spec is None or spec.loader is None:
        raise FaultError(f"{fault_dir.name}: no inject.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inject = getattr(module, "inject", None)
    if not callable(inject):
        raise FaultError(f"{fault_dir.name}/inject.py defines no inject(ctx)")
    return inject


class _CountingRandom(random.Random):
    """A seeded generator that remembers whether the fault drew from it."""

    used = False

    def random(self) -> float:
        self.used = True
        return super().random()

    def getrandbits(self, k: int, /) -> int:
        self.used = True
        return super().getrandbits(k)


@dataclass
class FaultContext:
    """What a fault's inject() may touch: the project's files and the raw tables."""

    project: Path
    db: Path
    rng: random.Random

    def read(self, rel: str) -> str:
        return (self.project / rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def replace(self, rel: str, old: str, new: str) -> None:
        """Replace text that must occur exactly once, so a fault fails loudly when the
        demo project changes under it."""
        text = self.read(rel)
        if text.count(old) != 1:
            raise FaultError(f"{rel}: expected one {old!r}, found {text.count(old)}")
        self.write(rel, text.replace(old, new))

    def rename(self, old: str, new: str) -> None:
        (self.project / new).parent.mkdir(parents=True, exist_ok=True)
        (self.project / old).rename(self.project / new)

    def sql(self, statement: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Run SQL on the database (the raw schema holds the source tables)."""
        import duckdb

        con = duckdb.connect(str(self.db))
        try:
            cursor = con.execute(statement, params or [])
            return cursor.fetchall() if cursor.description else []
        finally:
            con.close()


@dataclass
class Materialized:
    fault_id: str
    root: Path
    project: Path
    db: Path
    pristine: Path
    expected: Expected
    seed: int
    rng_used: bool
    fingerprint: str
    commits: list[str] = field(default_factory=list)


def git(project: Path, *args: str, date: str | None = None) -> str:
    """git in a fault's own repository, with a fixed identity and, for commits, a fixed
    date. These are throwaway repositories, so signing and line-ending rewrites are off."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": GIT_AUTHOR[0],
           "GIT_AUTHOR_EMAIL": GIT_AUTHOR[1], "GIT_COMMITTER_NAME": GIT_AUTHOR[0],
           "GIT_COMMITTER_EMAIL": GIT_AUTHOR[1]}  # fmt: skip
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    command = ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
               "-c", "init.defaultBranch=main", *args]  # fmt: skip
    done = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")  # fmt: skip
    if done.returncode != 0:
        raise FaultError(f"git {' '.join(args)} failed: {done.stderr.strip()[-400:]}")
    return done.stdout


def materialize(
    fault_id: str, dest: Path, seed: int = SUITE_SEED, faults_dir: Path = FAULTS_DIR
) -> Materialized:
    """Build one fault into `dest`, replacing whatever was there."""
    fault_dir = faults_dir / fault_id
    expected = Expected.load(fault_dir)
    inject = load_inject(fault_dir)
    if dest.exists():
        shutil.rmtree(dest)
    project = dest / "project"
    shutil.copytree(DEMO_PROJECT, project, ignore=SKIP)
    (project / ".gitignore").write_text(GITIGNORE, encoding="utf-8", newline="\n")
    db = build_database(dest / "db" / DB_NAME)

    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "Jaffle shop models", date=BASELINE_DATE)
    rng = _CountingRandom(f"{seed}:{fault_id}")
    inject(FaultContext(project, db, rng))
    changed = git(project, "status", "--porcelain").strip()
    if changed and not expected.commit_message:
        raise FaultError(
            f"{fault_id}: inject changed files but expected.json has no commit_message"
        )
    if not changed and expected.commit_message:
        raise FaultError(f"{fault_id}: expected.json has a commit_message but no file changed")
    if changed:
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", str(expected.commit_message), date=FAULT_DATE)

    pristine = dest / "pristine" / DB_NAME
    pristine.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, pristine)
    commits = git(project, "log", "--format=%H %s").splitlines()
    return Materialized(
        fault_id=fault_id,
        root=dest,
        project=project,
        db=db,
        pristine=pristine,
        expected=expected,
        seed=seed,
        rng_used=rng.used,
        fingerprint=fingerprint(project, db, commits),
        commits=commits,
    )


def project_texts(project: Path) -> dict[str, str]:
    """Every file of a project as text with Unix line endings, without git, build output
    or databases."""
    found: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if not path.is_file() or rel.parts[0] in (".git", "target", "logs"):
            continue
        if path.name.endswith((".duckdb", ".duckdb.wal")) or path.name == ".user.yml":
            continue
        found[rel.as_posix()] = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return found


def fingerprint(project: Path, db: Path, commits: list[str]) -> str:
    """One hash of the project's files, the raw tables and the commit history."""
    digest = hashlib.sha256()
    for rel, text in project_texts(project).items():
        digest.update(f"{rel}\n{text}\n".encode())
    for table, value in raw_fingerprint(db).items():
        digest.update(f"raw.{table} {value}\n".encode())
    for commit in commits:
        digest.update(f"{commit}\n".encode())
    return digest.hexdigest()
