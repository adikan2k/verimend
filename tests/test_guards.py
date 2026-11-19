"""CLAUDE.md rule 4: every guard has a test that proves it blocks what it should."""

import pytest

from verimend.hooks.guards import (
    after_report_refusal,
    edit_refusal,
    query_refusal,
    refusal_for,
)

ROOT = "/app"


@pytest.mark.parametrize(
    "path",
    [
        "tests/assert_positive_amount.sql",  # singular tests
        "tests/generic/my_test.sql",
        "dbt_packages/pkg/models/stg.sql",  # installed packages: override instead
        "target/run_results.json",  # build output
        "profiles.yml",  # the connection
        "../verifier/checks.py",  # outside the project
        "/etc/passwd",
        "/app/../bench/grade.py",
        ".github/workflows/ci.yml",  # CI
        "models/../tests/x.sql",
        "models/helper.py",  # not a model, macro, seed or YAML file
        "",
    ],
)
def test_edit_guard_blocks(path: str) -> None:
    assert edit_refusal(path, ROOT) is not None


@pytest.mark.parametrize(
    "path",
    [
        "models/staging/stg_orders.sql",
        "/app/models/marts/orders.sql",  # absolute inside the project
        "models/schema.yml",
        "macros/cents.sql",
        "seeds/countries.csv",
        "snapshots/orders_snapshot.sql",
        "dbt_project.yml",
        "packages.yml",
    ],
)
def test_edit_guard_allows_project_code(path: str) -> None:
    assert edit_refusal(path, ROOT) is None


@pytest.mark.parametrize(
    "sql",
    [
        "delete from orders",
        "drop table orders",
        "update orders set amount = 0",
        "insert into orders values (1)",
        "create table x as select 1",
        "copy orders to 'out.csv'",
        "attach 'other.duckdb'",
        "select 1; drop table orders",
        "select 1; select 2",
        "-- just a comment",
        "",
        "/* hidden */ delete from orders",
        "pragma database_list",
    ],
)
def test_query_guard_blocks(sql: str) -> None:
    assert query_refusal(sql) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "select * from orders",
        "SELECT count(*) FROM orders;",
        "with t as (select 1 as a) select a from t",
        "-- check totals\nselect sum(amount) from orders",
        "from orders limit 5",
    ],
)
def test_query_guard_allows_a_single_select(sql: str) -> None:
    assert query_refusal(sql) is None


def test_after_report_every_call_is_blocked() -> None:
    assert after_report_refusal(False) is None
    assert after_report_refusal(True) is not None
    for tool in ("read_failure", "read_file", "query_data", "edit_model", "report"):
        args = {"path": "models/a.sql", "sql": "select 1", "content": "x"}
        assert refusal_for(tool, args, project_dir=ROOT, reported=True) is not None


def test_refusal_for_routes_by_tool() -> None:
    assert refusal_for("edit_model", {"path": "tests/x.sql"}, project_dir=ROOT, reported=False)
    assert refusal_for("query_data", {"sql": "drop table x"}, project_dir=ROOT, reported=False)
    assert not refusal_for("read_file", {"path": "tests/x.sql"}, project_dir=ROOT, reported=False)
    assert not refusal_for("run_dbt", {"selector": "a+"}, project_dir=ROOT, reported=False)
