"""CLAUDE.md rule 4: every guard has a test that proves it blocks what it should."""

import pytest

from verimend.hooks.guards import (
    after_report_refusal,
    content_refusal,
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
        ".gitlab-ci.yml",
        "verifier/src/verifier/checks.py",  # the verifier and the benchmark, even inside
        "bench/grade.py",
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


# ---- content: tests in YAML files and generic test macros (SPEC section 4) ----------

SCHEMA = """\
version: 2
models:
  - name: orders
    data_tests:
      - dbt_utils.expression_is_true:
          expression: "amount >= 0"
    columns:
      - name: order_id
        data_tests:
          - not_null
          - unique
      - name: status
        tests:
          - accepted_values:
              values: ["placed", "shipped"]
sources:
  - name: raw
    tables:
      - name: payments
        columns:
          - name: id
            data_tests: [not_null]
"""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("          - unique\n", ""),  # remove a column test
        (
            "          - unique\n",
            "          - unique:\n              config:\n                severity: warn\n",
        ),
        ('values: ["placed", "shipped"]', 'values: ["placed", "shipped", "lost"]'),  # loosen
        ('expression: "amount >= 0"', 'expression: "amount >= -100"'),  # model-level test
        ("data_tests: [not_null]", "data_tests: []"),  # source table column
        ("    data_tests:\n      - dbt_utils", "    tests_disabled:\n      - dbt_utils"),
        ("- name: order_id", "- name: order_key"),  # moving a test to another column
    ],
)
def test_content_guard_blocks_weakened_yaml_tests(old: str, new: str) -> None:
    assert old in SCHEMA
    after = SCHEMA.replace(old, new)
    assert content_refusal("models/schema.yml", SCHEMA, after) is not None


def test_content_guard_allows_yaml_edits_that_keep_every_test() -> None:
    added = SCHEMA.replace("          - unique\n", "          - unique\n          - positive\n")
    described = SCHEMA.replace("  - name: orders\n", "  - name: orders\n    description: Orders.\n")
    for after in (added, described, SCHEMA + "\n# a comment\n"):
        assert content_refusal("models/schema.yml", SCHEMA, after) is None
    assert content_refusal("models/new.yml", None, SCHEMA) is None  # a new file
    assert content_refusal("models/schema.yml", SCHEMA, "not: [valid") is not None


PROJECT = "name: p\nprofile: p\ndata_tests:\n  +severity: error\nmodels:\n  +materialized: view\n"


def test_content_guard_blocks_project_wide_test_settings() -> None:
    warn = PROJECT.replace("+severity: error", "+severity: warn")
    assert content_refusal("dbt_project.yml", PROJECT, warn) is not None
    added = PROJECT.replace("models:\n", "tests:\n  +enabled: false\nmodels:\n")
    assert content_refusal("dbt_project.yml", PROJECT, added) is not None
    table = PROJECT.replace("+materialized: view", "+materialized: table")
    assert content_refusal("dbt_project.yml", PROJECT, table) is None


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "{% test positive(model, column_name) %}\nselect 1\n{% endtest %}",
            "{% test positive(model, column_name) %}\nselect 1 where false\n{% endtest %}",
        ),
        (
            None,
            "{% macro default__test_not_null(model, column_name) %}"
            "select 1 where false{% endmacro %}",
        ),
        (None, "{%- macro test_unique(model) -%}select 1{%- endmacro -%}"),
    ],
)
def test_content_guard_blocks_generic_test_macros(before: str | None, after: str) -> None:
    assert content_refusal("macros/tests.sql", before, after) is not None


def test_content_guard_allows_ordinary_macros_and_models() -> None:
    macro = "{% macro cents(col) %}{{ col }} / 100{% endmacro %}"
    assert content_refusal("macros/cents.sql", None, macro) is None
    assert content_refusal("models/latest_tests.sql", None, "select * from test_results") is None
