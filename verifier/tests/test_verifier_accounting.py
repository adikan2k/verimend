"""Check 5, row accounting, on small in-memory databases: each rule blocks the case it is
for, leaves the right shape alone, and anything it cannot read passes as unknown."""

import time
from typing import Any

import duckdb
import pytest
from verifier.accounting import ModelResult, analyse

from verifier import checks

# Product 3 has no row in products: its order lines are orphans. Line 5 has no order.
SETUP = """
create table products (product_id int, name varchar, active bool);
insert into products values (1, 'a', true), (2, 'b', true), (4, 'd', false);
create table variants (variant_id int, product_id int);
insert into variants values (10, 1), (20, 2), (40, 4);
create table orders (order_id int, month varchar);
insert into orders values (100, '2024-01'), (101, '2024-02');
create table lines (line_id int, order_id int, product_id int, variant_id int, amount double);
insert into lines values
  (1, 100, 1, 10, 10.0), (2, 100, 2, 20, 20.0), (3, 101, 3, null, 7.5),
  (4, 101, 1, 10, 5.0), (5, 999, 2, 20, 2.0);
"""

WRONG_PRODUCT_METRICS = """
with sales as (
    select product_id, sum(amount) as revenue, count(*) as line_count
    from lines group by product_id
)
select p.product_id, coalesce(s.revenue, 0) as revenue
from products p
left join sales s on p.product_id = s.product_id
"""


@pytest.fixture
def con() -> Any:
    connection = duckdb.connect()
    connection.execute(SETUP)
    yield connection
    connection.close()


def run(con: Any, sql: str, base_sql: str | None = None, output: bool = True) -> ModelResult:
    if output:
        con.execute(f"create or replace table out as {sql}")
    return analyse(con, "model.shop.m", sql, base_sql, '"out"' if output else None,
                   time.monotonic() + 60)  # fmt: skip


def test_rule_a_catches_facts_a_spine_drops(con: Any) -> None:
    result = run(con, WRONG_PRODUCT_METRICS)
    (finding,) = result.findings
    assert (finding.rule, finding.rows, finding.measures) == ("lost_facts", 1, {"revenue": 7.5})
    assert "left join sales" in finding.join and finding.scope == "final"
    check = checks.row_accounting([result])
    assert not check.passed and "7.50" in check.detail and "dropped" in check.detail


def test_rule_a_passes_when_every_fact_reaches_the_spine(con: Any) -> None:
    # The reference shape: facts reach products through their variant, and all do.
    sql = """
    with sales as (
        select v.product_id, sum(l.amount) as revenue
        from lines l inner join variants v on l.variant_id = v.variant_id
        group by v.product_id
    )
    select p.product_id, coalesce(s.revenue, 0) as revenue
    from products p left join sales s on p.product_id = s.product_id
    """
    result = run(con, sql)
    assert result.findings == [] and result.joins == 1 and result.unknown == []
    assert checks.row_accounting([result]).passed


@pytest.mark.parametrize(
    "spine",
    [
        "(select * from products where active)",
        "(select * from products limit 2)",
        "(select distinct * from products)",
        "(select p.* from products p inner join variants v on v.product_id = p.product_id)",
    ],
)
def test_rule_a_leaves_a_filtered_spine_alone(con: Any, spine: str) -> None:
    sql = f"""
    with spine as {spine},
    sales as (select product_id, sum(amount) as revenue from lines group by product_id)
    select p.product_id, s.revenue from spine p left join sales s on p.product_id = s.product_id
    """
    assert run(con, sql).findings == []


def test_rule_a_ignores_counts_and_inner_joins(con: Any) -> None:
    counts = WRONG_PRODUCT_METRICS.replace("sum(amount) as revenue", "count(*) as revenue")
    assert run(con, counts).findings == []
    inner = WRONG_PRODUCT_METRICS.replace("left join sales", "inner join sales")
    assert run(con, inner).findings == []


def test_rule_a_reads_using_and_a_subquery_on_the_right(con: Any) -> None:
    using = """
    with sales as (select product_id, sum(amount) as revenue from lines group by 1)
    select product_id, revenue from products left join sales using (product_id)
    """
    assert [f.rule for f in run(con, using).findings] == ["lost_facts"]
    subquery = """
    select p.product_id, s.revenue from products p
    left join (select product_id, sum(amount) as revenue from lines group by 1) s
      on s.product_id = p.product_id
    """
    assert [f.rule for f in run(con, subquery).findings] == ["lost_facts"]


def test_rule_a_exempts_a_join_the_base_already_had(con: Any) -> None:
    # The fix changed something else; the same join, under other aliases, was in the base.
    base = WRONG_PRODUCT_METRICS.replace("p.product_id", "pr.product_id").replace(
        "products p", "products pr"
    )
    fixed = WRONG_PRODUCT_METRICS.replace("coalesce(s.revenue, 0)", "coalesce(s.revenue, 0.0)")
    result = run(con, fixed, base_sql=base)
    assert result.findings == [] and len(result.exempt) == 1
    assert "was in the base" in checks.row_accounting([result]).detail


WRONG_CATEGORY = """
select o.month, sum(l.amount) as revenue
from lines l
left join orders o on l.order_id = o.order_id
group by {group}
"""


@pytest.mark.parametrize("group", ["1", "o.month", "month"])
def test_rule_b_catches_dangling_keys_grouped_under_null(con: Any, group: str) -> None:
    result = run(con, WRONG_CATEGORY.format(group=group))
    (finding,) = result.findings
    assert (finding.rule, finding.rows, finding.key) == ("null_keys", 1, "month")
    assert "NULL month" in checks.row_accounting([result]).detail


def test_rule_b_passes_with_an_explicit_filter(con: Any) -> None:
    sql = WRONG_CATEGORY.format(group="1").replace(
        "group by", "where o.order_id is not null group by"
    )
    assert run(con, sql).findings == []


def test_rule_b_follows_joins_through_the_nullable_side(con: Any) -> None:
    # The key comes from products, joined through a variant that line 3 does not have.
    sql = """
    select p.name, sum(l.amount) as revenue
    from lines l
    left join variants v on l.variant_id = v.variant_id
    left join products p on v.product_id = p.product_id
    group by 1
    """
    # Line 3's variant_id is NULL, so it is not a dangling key: nothing to report.
    assert run(con, sql).findings == []
    con.execute("insert into lines values (6, 100, 1, 77, 1.0)")  # variant 77 does not exist
    (finding,) = run(con, sql).findings
    assert (finding.rule, finding.key) == ("null_keys", "name")


def test_rule_b_needs_a_null_in_the_built_model(con: Any) -> None:
    # Dangling rows exist, but the model keeps a non-NULL key for them: not reported.
    sql = WRONG_CATEGORY.format(group="1").replace(
        "o.month,", "coalesce(o.month, 'none') as month,"
    )
    assert run(con, sql).findings == []


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("select from where", "does not parse"),
        (
            "with s as (select product_id, sum(amount) as r from lines group by 1) "
            "select * from products p left join s on s.product_id > p.product_id",
            "not column equalities",
        ),
        (
            "with s as (select product_id, sum(amount) as r from lines group by 1) "
            "select * from products p left join s on product_id = s.product_id",
            "not qualified",
        ),
    ],
)
def test_what_it_cannot_read_is_unknown_and_passes(con: Any, sql: str, reason: str) -> None:
    result = run(con, sql, output=False)
    assert result.findings == [] and reason in " ".join(result.unknown)
    check = checks.row_accounting([result])
    assert check.passed and "not analysed" in check.detail


def test_a_slow_count_is_unknown(con: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from verifier import accounting

    monkeypatch.setattr(accounting, "MAX_QUERIES_PER_MODEL", 0)
    result = run(con, WRONG_PRODUCT_METRICS)
    assert result.findings == [] and "queries for one model" in " ".join(result.unknown)


def test_the_check_with_nothing_to_analyse() -> None:
    check = checks.row_accounting([])
    assert check.passed and check.name == "row_accounting"
    skipped = ModelResult("model.shop.inc", skipped="incremental")
    assert "model.shop.inc (incremental)" in checks.row_accounting([skipped]).detail


# ---- v2.1 (decision 0008): defects found by the replay -------------------------------------


def test_a_cte_named_after_the_table_it_reads_is_followed_not_looped(con: Any) -> None:
    # `with products as (select * from main.products)`: the CTE reads the table. v2.0 took
    # main.products for the CTE itself, looped, and passed it as unknown.
    sql = """
    with products as (select * from main.products),
    sales as (select product_id, sum(amount) as revenue from lines group by 1)
    select p.product_id, s.revenue from products p left join sales s on s.product_id = p.product_id
    """
    result = run(con, sql)
    assert result.unknown == [] and [f.rule for f in result.findings] == ["lost_facts"]


def test_an_unreadable_built_model_is_unknown_not_a_pass(con: Any) -> None:
    sql = WRONG_CATEGORY.format(group="1")
    con.execute(f"create or replace table out as {sql}")
    result = analyse(con, "model.shop.m", sql, None, '"no_such_table"', time.monotonic() + 60)
    assert result.findings == []
    assert any("could not be read" in u for u in result.unknown)
    assert "not analysed" in checks.row_accounting([result]).detail


def test_rule_b_says_when_rows_found_no_partner_but_no_key_is_null(con: Any) -> None:
    sql = WRONG_CATEGORY.format(group="1").replace(
        "o.month,", "coalesce(o.month, 'none') as month,"
    )
    result = run(con, sql)
    assert result.findings == []
    detail = checks.row_accounting([result]).detail
    assert "1 row(s) found no partner" in detail and "month is never NULL" in detail
