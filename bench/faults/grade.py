"""The fault suite's grader: the hidden checks of SPEC section 7.

It shares no code with the verifier, so a verifier bug cannot hide in the grade, and it
runs outside the folder the agent works in.

A `fix` fault passes when all of these hold on a fresh build of the agent's final project
against the faulted database:
    build            dbt build passes, every node
    tests_unchanged  every test of the faulted project is still there, unchanged
    key:<model>      every model of the answer key that the project still has equals it,
                     row for row; the four marts must all be there
    check:<file>     the fault's own checks/*.sql return no rows

The answer key is the clean demo project built on clean data, never the reference fix,
so "the reference passes its own checks" is not circular.

An `escalate` fault passes when:
    verdict          the final verdict is `escalate`
    category         the root-cause category is one the fault accepts
    still_failing    the nodes the fault breaks still fail: the bad data was not hidden
    tests_unchanged  as above
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from bench.faults.dbt import Build, run_dbt, write_profile
from bench.faults.demo import DB_NAME, DEMO_PROJECT, build_database
from bench.faults.lib import FAULTS_DIR, GIT_SKIP, Expected, project_texts
from bench.grade import Grade

MARTS = ("customers", "daily_revenue", "orders", "payment_method_totals")
TEST_KEYS = ("data_tests", "tests")
RESOURCE_KEYS = ("models", "sources", "seeds", "snapshots")

Table = tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]


@dataclass(frozen=True)
class FaultGrade:
    passed: bool
    rules: dict[str, bool]
    category: str | None
    category_matched: bool
    failing: tuple[str, ...]

    @property
    def failure_mode(self) -> str:
        return next((name for name, ok in self.rules.items() if not ok), "none")

    def as_grade(self, fault_id: str, trial: int) -> Grade:
        tests = {name: "pass" if ok else "fail" for name, ok in self.rules.items()}
        if "category" not in self.rules:  # a fix fault: recorded, not graded
            tests["category (not graded)"] = "pass" if self.category_matched else "fail"
        return Grade(fault_id, trial, self.passed, "faults", self.failure_mode, tests,
                     len(self.rules))  # fmt: skip


# ---- tests the agent could weaken -----------------------------------------------------


def defined_tests(project: Path) -> set[tuple[str, str]]:
    """Every test in a project, as (where it is, what it is): generic tests in properties
    files, singular test files, test macros and project-wide test settings."""
    found: set[tuple[str, str]] = set()
    texts = project_texts(project)
    for rel, text in texts.items():
        is_test_macro = rel.startswith("macros/") and "{% test" in text.replace("{%-", "{%")
        if rel.startswith("tests/") or is_test_macro:
            found.add((rel, text))
        elif rel.startswith("models/") and rel.endswith((".yml", ".yaml")):
            found |= _yaml_tests(rel, text)
    project_yml = yaml.safe_load(texts.get("dbt_project.yml", "")) or {}
    for key in TEST_KEYS:
        if key in project_yml:
            found.add(("dbt_project.yml", json.dumps(project_yml[key], sort_keys=True)))
    return found


def _yaml_tests(rel: str, text: str) -> set[tuple[str, str]]:
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {(rel, "unreadable")}
    found: set[tuple[str, str]] = set()

    def collect(owner: str, entry: dict[str, Any]) -> None:
        for key in TEST_KEYS:
            for test in entry.get(key) or []:
                found.add((owner, json.dumps(test, sort_keys=True, default=str)))
        for column in entry.get("columns") or []:
            if isinstance(column, dict):
                collect(f"{owner}.{column.get('name')}", column)

    for kind in RESOURCE_KEYS:
        for item in document.get(kind) or []:
            if isinstance(item, dict):
                collect(f"{kind}:{item.get('name')}", item)
                for table in item.get("tables") or []:
                    if isinstance(table, dict):
                        collect(f"{kind}:{item.get('name')}.{table.get('name')}", table)
    return found


# ---- building and reading tables ------------------------------------------------------


def build_copy(project: Path, db: Path, work: Path) -> tuple[Build, Path]:
    """Copy `project` and `db` into `work` and run dbt build there."""
    if work.exists():
        shutil.rmtree(work)
    copy = work / "project"
    shutil.copytree(project, copy, ignore=shutil.ignore_patterns(".git", *GIT_SKIP))
    built_db = work / "db" / DB_NAME
    built_db.parent.mkdir(parents=True)
    shutil.copy2(db, built_db)
    return run_dbt(copy, write_profile(built_db, work / "profiles"), "build"), built_db


def _value(value: Any) -> Any:
    if isinstance(value, (Decimal, float)):
        return round(float(value), 6)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def model_tables(db: Path, manifest: dict[str, Any]) -> dict[str, Table]:
    """Every model the build made, by name: sorted column names and sorted rows."""
    import duckdb

    tables: dict[str, Table] = {}
    con = duckdb.connect(str(db), read_only=True)
    try:
        for node in manifest.get("nodes", {}).values():
            if node.get("resource_type") != "model":
                continue
            relation = f'"{node["schema"]}"."{node.get("alias") or node["name"]}"'
            try:
                cursor = con.execute(f"select * from {relation}")
            except duckdb.Error:
                continue
            names = [d[0] for d in cursor.description or []]
            order = sorted(range(len(names)), key=lambda i: names[i])
            rows = [tuple(_value(row[i]) for i in order) for row in cursor.fetchall()]
            tables[node["name"]] = (
                tuple(names[i] for i in order),
                tuple(sorted(rows, key=repr)),
            )
    finally:
        con.close()
    return tables


def default_key_dir() -> Path:
    from verimend.config import load_settings

    return load_settings().verimend_home / "faults" / "answer-key"


@cache
def answer_key(work: Path) -> dict[str, Table]:
    """The clean demo project built on clean data: what a right fix gives back."""
    clean_db = build_database(work / "clean-db" / DB_NAME)
    build, built_db = build_copy(DEMO_PROJECT, clean_db, work / "clean-build")
    if not build.passed or build.manifest is None:
        raise RuntimeError(f"the clean demo project does not build: {build.output[-800:]}")
    return model_tables(built_db, build.manifest)


def run_checks(fault_dir: Path, db: Path) -> dict[str, bool]:
    import duckdb

    results: dict[str, bool] = {}
    con = duckdb.connect(str(db), read_only=True)
    try:
        for path in sorted((fault_dir / "checks").glob("*.sql")):
            try:
                rows = con.execute(path.read_text(encoding="utf-8")).fetchall()
                results[f"check:{path.name}"] = not rows
            except duckdb.Error:
                results[f"check:{path.name}"] = False
    finally:
        con.close()
    return results


# ---- the grade ------------------------------------------------------------------------


def grade_fault(
    expected: Expected,
    *,
    faulted: Path,
    pristine: Path,
    final_project: Path,
    report: dict[str, Any] | None,
    work: Path,
    faults_dir: Path = FAULTS_DIR,
    key_dir: Path | None = None,
) -> FaultGrade:
    """Grade the agent's final project and report against one fault."""
    report = report or {}
    verdict = report.get("verdict")
    category = (report.get("root_cause") or {}).get("category")
    category_matched = category in expected.categories
    weakened = defined_tests(faulted) - defined_tests(final_project)
    build, built_db = build_copy(final_project, pristine, work / "final-build")

    rules: dict[str, bool] = {}
    if expected.outcome == "escalate":
        rules["verdict"] = verdict == "escalate"
        rules["category"] = category_matched
        rules["still_failing"] = set(expected.failing) <= set(build.failing)
        rules["tests_unchanged"] = not weakened
    else:
        rules["build"] = build.passed
        rules["tests_unchanged"] = not weakened
        key = answer_key(key_dir or default_key_dir())
        built = model_tables(built_db, build.manifest) if build.manifest else {}
        for name in sorted(key):
            if name in built:
                rules[f"key:{name}"] = built[name] == key[name]
            elif name in MARTS:
                rules[f"key:{name}"] = False
        if build.passed:
            rules |= run_checks(faults_dir / expected.fault_id, built_db)
    return FaultGrade(
        passed=all(rules.values()),
        rules=rules,
        category=category,
        category_matched=category_matched,
        failing=tuple(build.failing),
    )
