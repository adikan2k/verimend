"""The pr policy, for any pull request to a dbt project (the inspector Action), on a real
dbt-duckdb project: what it lets through that the fix policy refuses, and what it still
blocks. The fix policy's own tests (test_verifier_run.py) are unchanged."""

import json
import shutil
from pathlib import Path

import pytest
from verifier.cli import main
from verifier.policy import Policy
from verifier.project import default_dbt
from verifier.render import MARKER, render
from verifier.run import snapshot, verify

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_shop"
DBT = default_dbt()
PR = Policy("pr")
PR_ALLOWED = Policy("pr", allow_test_changes=True)


@pytest.fixture(scope="module")
def green(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """tiny_shop with its seeded bug fixed: a project whose main branch builds green."""
    root = tmp_path_factory.mktemp("green")
    project = root / "project"
    shutil.copytree(FIXTURE, project)
    model = project / "models/marts/customer_totals.sql"
    model.write_text(model.read_text().replace("amout", "amount"))
    work = root / "work"
    # No --db: the profile's own path (shop.duckdb, relative to the project) is used.
    taken = snapshot(project, None, work, dbt_exe=DBT)
    assert taken["base_failures"] == []
    return work


@pytest.fixture(scope="module")
def red(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """tiny_shop as it is: customer_totals fails in the base build."""
    root = tmp_path_factory.mktemp("red")
    project = root / "project"
    shutil.copytree(FIXTURE, project)
    work = root / "work"
    snapshot(project, project / "shop.duckdb", work, dbt_exe=DBT)
    return work


def new_file(path: str, body: str) -> str:
    lines = body.strip("\n").splitlines()
    added = "".join(f"+{line}\n" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{added}"


README_AND_CI = new_file("README.md", "# Shop\nHow to run it.") + new_file(
    ".github/workflows/ci.yml", "on: push\njobs: {}"
)
WARN_TEST = """\
--- a/models/schema.yml
+++ b/models/schema.yml
@@ -5,4 +5,6 @@
       - name: order_id
         data_tests:
           - not_null
-          - unique
+          - unique:
+              config:
+                severity: warn
"""
NEW_TEST = new_file(
    "tests/assert_known_customers.sql",
    "select * from {{ ref('stg_orders') }} where customer_id is null",
)
REMOVE_ORDER_COUNT = """\
--- a/models/marts/order_count.sql
+++ /dev/null
@@ -1,3 +0,0 @@
--- Outside customer_totals' lineage: the collateral-damage check compares it.
-{{ config(materialized='table') }}
-select count(*) as orders from {{ ref('raw_orders') }}
"""
REMOVE_BROKEN = """\
--- a/models/marts/customer_totals.sql
+++ /dev/null
@@ -1,6 +0,0 @@
--- Seeded bug for the tool tests: the column is amount, not amout.
-select
-    customer_id,
-    sum(amout) as total_amount
-from {{ ref('stg_orders') }}
-group by customer_id
"""
DROPS_ROWS = new_file(
    "models/marts/customer_revenue.sql",
    """
{{ config(materialized='table') }}
with totals as (
    select customer_id, sum(amount) as revenue from {{ ref('stg_orders') }} group by 1
),
known as (
    select * from (values (10), (11)) as t(customer_id)
)
select k.customer_id, t.revenue from known k left join totals t on t.customer_id = k.customer_id
""",
)

NULL_KEYS = new_file(
    "models/marts/revenue_by_customer.sql",
    """
{{ config(materialized='table') }}
with lines as (
    select * from (values (1, 5.0), (3, 2.5), (99, 7.0)) as t(order_id, amount)
)
select o.customer_id, sum(l.amount) as revenue
from lines l
left join {{ ref('stg_orders') }} o on l.order_id = o.order_id
group by 1
""",
)


def check(work: Path, diff: str, policy: Policy) -> dict:
    return verify(work, diff, dbt_exe=DBT, policy=policy)


def test_files_outside_dbt_are_listed_not_refused(green: Path) -> None:
    result = check(green, README_AND_CI, PR)
    assert result["passed"], result
    (tamper,) = result["checks"]  # nothing in the dbt project changed: no build at all
    assert "2 outside the dbt project, not checked" in tamper["detail"]
    assert ".github/workflows/ci.yml" in tamper["detail"]
    # The fix policy refuses the same diff: CI is off-limits to an agent's fix.
    assert check(green, README_AND_CI, Policy())["failed_check"] == "tamper"


def test_an_empty_pull_request_has_nothing_to_check(green: Path) -> None:
    result = check(green, "", PR)
    assert result["passed"] and result["checks"][0]["name"] == "empty_diff"
    assert check(green, "", Policy())["failed_check"] == "empty_diff"


def test_a_weakened_test_fails_unless_a_maintainer_allows_it(green: Path) -> None:
    blocked = check(green, WARN_TEST, PR)
    assert blocked["failed_check"] == "tamper", blocked
    assert "severity" in blocked["checks"][0]["detail"]
    allowed = check(green, WARN_TEST, PR_ALLOWED)
    assert allowed["passed"], allowed
    assert "allowed by the pull request's label" in allowed["checks"][0]["detail"]
    assert "severity" in allowed["checks"][0]["detail"]


def test_a_new_test_passes(green: Path) -> None:
    result = check(green, NEW_TEST, PR)
    assert result["passed"], result


def test_a_clean_removal_passes_and_is_listed(green: Path) -> None:
    result = check(green, REMOVE_ORDER_COUNT, PR)
    assert result["passed"], result
    masking = next(c for c in result["checks"] if c["name"] == "masking")
    assert "removed, with no failure behind them: model.tiny_shop.order_count" in masking["detail"]
    assert check(green, REMOVE_ORDER_COUNT, Policy())["failed_check"] == "masking"


def test_removing_a_failing_model_still_hides_a_failure(red: Path) -> None:
    result = check(red, REMOVE_BROKEN, PR)
    assert result["failed_check"] == "masking", result
    assert "customer_totals was disabled or removed" in result["checks"][1]["detail"]


def test_row_accounting_applies_to_pull_requests(green: Path) -> None:
    # Rule B fails a pull request as it fails a fix: order 99 has no order, so its amount
    # is grouped under a NULL customer.
    result = check(green, NULL_KEYS, PR)
    assert result["failed_check"] == "row_accounting", result
    assert "NULL customer_id" in result["checks"][-1]["detail"]
    # Rule A is report-only (decision 0008): dropped rows are listed, the pull request passes.
    dropped = check(green, DROPS_ROWS, PR)
    assert dropped["passed"], dropped
    assert "report-only" in dropped["checks"][-1]["detail"]


def test_the_cli_takes_the_policy_and_refuses_a_label_without_it(
    green: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "warn.diff"
    diff.write_text(WARN_TEST)
    base = ["verify", "--work", str(green), "--diff", str(diff), "--dbt", DBT]
    assert main([*base, "--policy", "pr"]) == 1
    assert json.loads(capsys.readouterr().out)["failed_check"] == "tamper"
    assert main([*base, "--policy", "pr", "--allow-test-changes"]) == 0
    capsys.readouterr()
    assert main([*base, "--allow-test-changes"]) == 2  # only under the pr policy
    assert "pr policy" in json.loads(capsys.readouterr().out)["error"]


def test_render_says_the_verdict_in_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = {"passed": False, "failed_check": "tamper", "duration_s": 12.3, "checks": [
        {"name": "tamper", "passed": False, "detail": "Tests were weakened: a | b"},
    ]}  # fmt: skip
    text = render(failed)
    assert text.startswith(MARKER)
    assert "failed at tests and files" in text and "**FAIL**" in text
    assert "a \\| b" in text and "No model was called" in text
    passed = render({"passed": True, "failed_check": None, "checks": [
        {"name": "row_accounting", "passed": True, "detail": "ok"}]})  # fmt: skip
    assert ": passed" in passed and "| Row accounting | pass | ok |" in passed
    broken = render({"error": "dbt deps failed"})
    assert "could not run" in broken and "not a verdict" in broken
    result = tmp_path / "result.json"
    result.write_text("dbt noise\n" + json.dumps(failed) + "\n")
    assert main(["render", "--result", str(result)]) == 0
    assert "failed at tests and files" in capsys.readouterr().out
