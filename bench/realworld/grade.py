"""The real-world cases' grader: hidden checks in the spirit of the fault suite's.

It shares no code with the verifier, and runs outside the folder the agent works in. It
reuses the fault suite's test inventory and table reader, which do not depend on the demo
project.

A run passes when all of these hold on a fresh build of the agent's final project against
the case's pristine database:
    build             dbt deps and dbt build pass, every node
    tests_unchanged   every test of the broken project is still there, unchanged (contracts
                      and column types count as tests)
    vendor_unchanged  the vendored packages are as built: patching fivetran_utils or
                      dbt_utils is not a fix to the package
    key:<model>       every model of the answer key is there and equals it, row for row

The answer key is the human fix (the whole fix commit, not the reference limited to the
case's files) built on the same data. It is built twice, and a model whose rows differ
between the two builds (a timestamp, a spine up to today) is left out of the comparison
and reported as unstable.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.faults.dbt import Build
from bench.faults.grade import Table, defined_tests, model_tables
from bench.faults.lib import remove_tree
from bench.realworld.lib import (
    DB_NAME,
    VENDOR,
    CaseError,
    deps_and,
    project_files,
    write_profile,
)


@dataclass(frozen=True)
class CaseGrade:
    passed: bool
    rules: dict[str, bool]
    failing: tuple[str, ...]

    @property
    def failure_mode(self) -> str:
        return next((name for name, ok in self.rules.items() if not ok), "none")


@dataclass(frozen=True)
class AnswerKey:
    tables: dict[str, Table]
    unstable: tuple[str, ...]


def build_copy(project: Path, db: Path, profile: str, work: Path) -> tuple[Build, Path]:
    """Copy `project` and `db` into `work` and run dbt deps and dbt build there."""
    remove_tree(work)
    copy = work / "project"
    shutil.copytree(project, copy, ignore=shutil.ignore_patterns(
        ".git", "target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal"))  # fmt: skip
    built_db = work / "db" / DB_NAME
    built_db.parent.mkdir(parents=True)
    shutil.copy2(db, built_db)
    profiles = write_profile(profile, built_db, work / "profiles")
    return deps_and(copy, profiles, "build"), built_db


def answer_key(key_project: Path, pristine: Path, profile: str, work: Path) -> AnswerKey:
    """The human fix built twice on the case's data; only stable models count."""
    found: list[dict[str, Table]] = []
    for attempt in ("one", "two"):
        build, built_db = build_copy(key_project, pristine, profile, work / f"key-{attempt}")
        if not build.passed or build.manifest is None:
            raise CaseError(f"the human fix does not build: {build.failing} "
                            f"{build.output[-600:]}")  # fmt: skip
        found.append(model_tables(built_db, build.manifest))
    one, two = found
    unstable = tuple(sorted(name for name in one if one[name] != two.get(name)))
    return AnswerKey({k: v for k, v in one.items() if k not in unstable}, unstable)


def vendor_files(project: Path) -> dict[str, bytes]:
    return {rel: data for rel, data in project_files(project).items()
            if rel.startswith(f"{VENDOR}/")}  # fmt: skip


def grade_case(
    *,
    broken: Path,
    pristine: Path,
    profile: str,
    final_project: Path,
    key: AnswerKey,
    work: Path,
    build: tuple[Build, Path] | None = None,
) -> CaseGrade:
    """Grade the agent's final project against one case. `build` reuses a build of
    `final_project` on `pristine` that the caller already ran."""
    weakened = defined_tests(broken) - defined_tests(final_project)
    result, built_db = build or build_copy(final_project, pristine, profile, work / "final")
    rules: dict[str, bool] = {
        "build": result.passed,
        "tests_unchanged": not weakened,
        "vendor_unchanged": vendor_files(broken) == vendor_files(final_project),
    }
    built: dict[str, Any] = model_tables(built_db, result.manifest) if result.manifest else {}
    for name in sorted(key.tables):
        rules[f"key:{name}"] = built.get(name) == key.tables[name]
    return CaseGrade(all(rules.values()), rules, tuple(result.failing))
