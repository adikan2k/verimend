"""The demo project builds green from its raw data (the fault suite's clean base)."""

import shutil
from pathlib import Path

from verifier.project import Dbt, default_dbt, node_statuses

from bench.faults.demo import DB_NAME, DEMO_PROJECT, RAW_TABLES, build_database, raw_fingerprint


def test_the_raw_tables_load_the_same_way_every_time(tmp_path: Path) -> None:
    first = raw_fingerprint(build_database(tmp_path / "a" / DB_NAME))
    second = raw_fingerprint(build_database(tmp_path / "b" / DB_NAME))
    assert first == second and set(first) == set(RAW_TABLES)


def test_the_clean_demo_project_builds_without_a_failure(tmp_path: Path) -> None:
    project = tmp_path / "jaffle_shop"
    shutil.copytree(
        DEMO_PROJECT, project, ignore=shutil.ignore_patterns("target", "logs", "*.duckdb*")
    )
    build_database(project / DB_NAME)
    code, output = Dbt(default_dbt(), project, "dev").run(project, "build")
    statuses = node_statuses(project)
    assert code == 0, output[-1500:]
    assert len(statuses) == 39 and all(
        s["status"] in ("success", "pass") for s in statuses.values()
    )
