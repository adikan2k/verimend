"""bench/realworld/run.py, the runner for the real-world cases, with no model calls: one
kind a batch, the container's profile, packages that resolve inside a container, and the
free agents graded on a real cached case (skipped when the case is not cached)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from bench.faults.container import write_container_profile
from bench.realworld import run as rw
from bench.realworld.lib import CASES_DIR, Case
from verimend.config import load_settings

CASE = "fivetran_log-203"  # the quickest case to build (55 s in the check)


def cached() -> bool:
    return (
        load_settings().verimend_home / "realworld" / "cache" / CASE / "prepared.json"
    ).is_file()


needs_cache = pytest.mark.skipif(not cached(), reason=f"{CASE} is not cached in ~/.verimend")


def test_a_batch_holds_one_kind_of_case() -> None:
    assert rw.group_of(["zendesk-290", "shopify-167"]) == "bug fixes"
    assert rw.group_of(["linkedin-65"]) == "DuckDB ports"
    with pytest.raises(rw.MixedKinds, match="never both"):
        rw.group_of(["zendesk-290", "linkedin-65"])


def test_every_case_has_a_kind_the_runner_labels() -> None:
    kinds = {Case.load(p.parent).kind for p in CASES_DIR.glob("*/case.json")}
    assert kinds <= set(rw.GROUPS)


def test_the_container_profile_carries_the_cases_profile_and_schema(tmp_path: Path) -> None:
    out = write_container_profile(tmp_path, "integration_tests", "main")
    profile = yaml.safe_load((out / "profiles.yml").read_text())["integration_tests"]
    assert profile["outputs"]["dev"]["path"] == "/work/project/warehouse.duckdb"
    assert profile["outputs"]["dev"]["schema"] == "main"
    faults = yaml.safe_load((write_container_profile(tmp_path / "f") / "profiles.yml").read_text())
    assert (
        list(faults) == ["jaffle_shop"] and "schema" not in faults["jaffle_shop"]["outputs"]["dev"]
    )


def batch(tmp_path: Path, agent: str, with_verifier: bool) -> dict[str, Any]:
    return {"stage_dir": str(tmp_path / "stage"), "agent": agent,
            "with_verifier": with_verifier}  # fmt: skip


@needs_cache
def test_the_baselines_case_has_its_database_and_packages_inside_the_project(
    tmp_path: Path,
) -> None:
    built = rw.build_case(tmp_path, Case.load(CASES_DIR / CASE), "baseline")
    assert built.db == built.project / "warehouse.duckdb" and built.db.is_file()
    assert str(built.db.resolve().as_posix()) in (built.profiles / "profiles.yml").read_text()
    installed = list((built.project / "dbt_packages").iterdir())
    assert installed and not any(p.is_symlink() for p in installed)
    assert "dbt_packages/" in (built.project / ".gitignore").read_text()


@needs_cache
def test_doing_nothing_fails_and_the_reference_passes_under_a_watching_verifier(
    tmp_path: Path,
) -> None:
    settings = load_settings()
    keys: dict[str, Any] = {}
    _, nothing = rw.run_trial(batch(tmp_path / "none", "none", False), settings, CASE, 1, 0.0,
                              keys)  # fmt: skip
    assert not nothing.passed and nothing.grader == "realworld"
    assert CASE in keys  # the answer key is built once and reused
    record, reference = rw.run_trial(batch(tmp_path / "ref", "reference", True), settings, CASE,
                                     1, 0.0, keys)  # fmt: skip
    assert reference.passed, reference.tests
    assert record.report == {"verdict": "fixed"}
    assert record.verifier is not None and record.verifier["error"] is None
    (attempt,) = record.verifier["attempts"]
    assert "passed" in attempt  # its verdict is measured in step A, not assumed here


def manifest(**models: tuple[str, list[str]]) -> dict[str, Any]:
    """models: name -> (sql, children by name)."""
    nodes = {f"model.p.{name}": {"resource_type": "model", "name": name, "raw_code": sql}
             for name, (sql, _) in models.items()}  # fmt: skip
    children = {f"model.p.{name}": [f"model.p.{c}" for c in kids]
                for name, (_, kids) in models.items()}  # fmt: skip
    return {"nodes": nodes, "child_map": children}


def test_a_model_that_reads_the_clock_is_never_compared() -> None:
    from bench.realworld.grade import clock_models

    found = manifest(
        status=("datediff(valid_from, coalesce(valid_to, {{ dbt.current_timestamp() }}))", []),
        today=("select now() as at", []),
        plain=("select current_dates_seen from t", []),
    )
    assert clock_models(found) == {"status", "today"}


def test_the_grading_time_build_drops_what_moved_and_its_downstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bench.faults.dbt import Build
    from bench.realworld import grade

    graph = manifest(rollup=("select 1", ["emails"]), emails=("select 1", []),
                     orders=("select 1", []))  # fmt: skip
    key = grade.AnswerKey({"rollup": (("a",), (("x, y",),)), "emails": (("a",), ()),
                           "orders": (("a",), ((1,),))}, (), graph)  # fmt: skip
    now = {"rollup": (("a",), (("y, x",),)), "emails": (("a",), ()), "orders": (("a",), ((1,),))}
    monkeypatch.setattr(grade, "build_copy",
                        lambda *a: (Build(0, "", {}, graph), tmp_path / "db"))  # fmt: skip
    monkeypatch.setattr(grade, "model_tables", lambda db, m: now)
    checked = grade.recheck(key, tmp_path, tmp_path, "p", tmp_path / "w")
    assert set(checked.tables) == {"orders"}
    assert set(checked.unstable) == {"rollup", "emails"}
