"""The eight tools, without a model: pure helpers, then a real dbt-duckdb build of the
tiny fixture project through LocalWorkspace (a copy in tmp_path, never the fixture)."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from verimend.tools.base import ToolContext, ToolResult, cap
from verimend.tools.data import format_rows
from verimend.tools.dbt_tools import NOTHING_FAILED, summarise_run_results
from verimend.tools.history import trim_log
from verimend.tools.lineage import find_node, walk
from verimend.tools.registry import TOOL_NAMES, TOOLS, get_tool
from verimend.tools.report import VERDICT_SCHEMA
from verimend.tools.workspace import LocalWorkspace, safe_relative

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_shop"

GOOD_REPORT = {
    "verdict": "fixed",
    "root_cause": {"category": "logic", "model": "customer_totals", "summary": "Typo."},
    "evidence": [{"kind": "log", "ref": "dbt build", "finding": "amout not found"}],
    "changes": [{"path": "models/marts/customer_totals.sql", "summary": "amout -> amount"}],
    "confidence": "high",
}


def call(ctx: ToolContext, name: str, **args: Any) -> ToolResult:
    return asyncio.run(get_tool(name).handler(ctx, args))


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    project = tmp_path / "tiny_shop"
    shutil.copytree(FIXTURE, project)
    return ToolContext(LocalWorkspace(project, project / "shop.duckdb"))


# Pure helpers


def test_safe_relative_keeps_paths_inside_the_project() -> None:
    root = "/app/proj"
    assert safe_relative("models/a.sql", root) == "models/a.sql"
    assert safe_relative("./models/a.sql", root) == "models/a.sql"
    assert safe_relative("/app/proj/models/a.sql", root) == "models/a.sql"
    assert safe_relative("models\\a.sql", root) == "models/a.sql"
    assert safe_relative(".hidden.yml", root) == ".hidden.yml"
    assert safe_relative("", root) == "."
    for bad in ("../x.sql", "models/../../x", "/etc/passwd", "/app/other/a.sql", "C:/x.sql"):
        assert safe_relative(bad, root) is None, bad


def test_cap_says_when_it_trims() -> None:
    assert cap("abc", 5) == "abc"
    assert cap("abcdefgh", 5) == "abcde\n[trimmed: showed 5 of 8 characters]"


def test_summarise_run_results_lists_problems_or_the_log_tail() -> None:
    raw = json.dumps({"results": [
        {"status": "success", "unique_id": "model.p.a"},
        {"status": "error", "unique_id": "model.p.b", "message": "Binder Error:\n  no amout"},
    ]})  # fmt: skip
    text, problems = summarise_run_results(1, raw, "")
    assert problems == 1
    assert "counts: error=1, success=1" in text
    assert "- ERROR model.p.b: Binder Error: no amout" in text
    text, problems = summarise_run_results(2, None, "line 1\nCompilation Error in x\n")
    assert problems == 1 and "log tail:" in text and "Compilation Error in x" in text
    assert summarise_run_results(0, json.dumps({"results": []}), "")[1] == 0


def test_trim_log_cuts_each_diff() -> None:
    diff = "\n".join(f"+line {i}" for i in range(100))
    log = f"commit aaa\nAuthor: a\n\n    msg\n\ndiff --git a/x b/x\n{diff}\ncommit bbb\n    two"
    trimmed = trim_log(log, diff_lines=10)
    assert "commit aaa" in trimmed and "commit bbb" in trimmed
    assert "[diff trimmed: showed 10 of 101 lines]" in trimmed
    assert "+line 50" not in trimmed


MANIFEST = {
    "metadata": {"project_name": "shop"},
    "nodes": {
        "seed.shop.raw": {"resource_type": "seed", "name": "raw", "package_name": "shop",
                          "original_file_path": "seeds/raw.csv", "config": {}},
        "model.shop.stg": {"resource_type": "model", "name": "stg", "package_name": "shop",
                           "original_file_path": "models/stg.sql",
                           "config": {"materialized": "view"}},
        "model.pkg.stg": {"resource_type": "model", "name": "stg", "package_name": "pkg",
                          "original_file_path": "models/stg.sql", "config": {}},
        "model.shop.mart": {"resource_type": "model", "name": "mart", "package_name": "shop",
                            "original_file_path": "models/mart.sql",
                            "config": {"materialized": "table"}},
    },
    "sources": {"source.shop.app.orders": {"resource_type": "source", "source_name": "app",
                                           "name": "orders", "original_file_path": "m/s.yml"}},
    "parent_map": {"model.shop.mart": ["model.shop.stg"], "model.shop.stg": ["seed.shop.raw"]},
    "child_map": {"seed.shop.raw": ["model.shop.stg"],
                  "model.shop.stg": ["model.shop.mart", "test.shop.not_null_stg_id.abc"]},
}  # fmt: skip


def test_find_node_prefers_the_project_over_packages() -> None:
    assert find_node(MANIFEST, "stg") == "model.shop.stg"
    assert find_node(MANIFEST, "app.orders") == "source.shop.app.orders"
    assert find_node(MANIFEST, "model.pkg.stg") == "model.pkg.stg"
    assert find_node(MANIFEST, "nope") is None


def test_walk_follows_edges_to_the_depth_and_lists_tests() -> None:
    up = walk(MANIFEST, "model.shop.mart", "upstream", 2)
    assert "model.shop.stg (model, view, models/stg.sql)" in up and "seed.shop.raw" in up
    assert "seed.shop.raw" not in walk(MANIFEST, "model.shop.mart", "upstream", 1)
    down = walk(MANIFEST, "model.shop.stg", "downstream", 3)
    assert "model.shop.mart (model, table" in down
    assert "tests on this node: not_null_stg_id" in down


def test_format_rows_shows_count_and_nulls() -> None:
    text = format_rows({"columns": ["a", "b"], "rows": [["1", None]], "row_count": 7})
    assert text.splitlines() == ["row_count: 7 (showing 1)", "a | b", "1 | NULL"]


def test_registry_has_the_eight_tools_with_plain_schemas() -> None:
    assert TOOL_NAMES == ("read_failure", "walk_lineage", "query_data", "read_file",
                          "inspect_history", "edit_model", "run_dbt", "report")  # fmt: skip
    for spec in TOOLS:
        assert spec.input_schema["type"] == "object"
        assert "properties" in spec.input_schema
    assert "$ref" not in json.dumps(VERDICT_SCHEMA) and "$defs" not in VERDICT_SCHEMA
    assert set(VERDICT_SCHEMA["required"]) == set(GOOD_REPORT)


# Tools on the fixture project (no dbt needed)


def test_report_validates_and_records_once(ctx: ToolContext) -> None:
    bad = call(ctx, "report", **{**GOOD_REPORT, "verdict": "probably"})
    assert bad.is_error and "verdict" in bad.text and ctx.report is None
    extra = call(ctx, "report", **{**GOOD_REPORT, "mood": "great"})
    assert extra.is_error and ctx.report is None
    assert not call(ctx, "report", **GOOD_REPORT).is_error
    assert ctx.report is not None and ctx.report["verdict"] == "fixed"
    assert call(ctx, "report", **GOOD_REPORT).is_error


def test_read_file_numbers_lines_and_lists_folders(ctx: ToolContext) -> None:
    text = call(ctx, "read_file", path="models/marts/customer_totals.sql", start_line=3).text
    assert text.splitlines()[0] == "models/marts/customer_totals.sql, lines 3-6 of 6"
    assert "4      sum(amout) as total_amount" in text
    listing = call(ctx, "read_file", path="models").text
    assert "models/staging/stg_orders.sql" in listing
    assert call(ctx, "read_file", path="../secrets.txt").is_error
    assert call(ctx, "read_file", path="models/nope.sql").is_error


def test_edit_model_replaces_text_once_and_refuses_tests(ctx: ToolContext) -> None:
    path = "models/marts/customer_totals.sql"
    twice = call(ctx, "edit_model", path=path, old_text="customer_id", new_text="cid")
    assert twice.is_error and "appears 2 times" in twice.text
    done = call(ctx, "edit_model", path=path, old_text="sum(amout)", new_text="sum(amount)")
    assert not done.is_error and "+    sum(amount) as total_amount" in done.text
    assert ctx.diffs and ctx.diffs[0]["path"] == path
    blocked = call(ctx, "edit_model", path="tests/assert_positive_amount.sql", content="select 1")
    assert blocked.is_error
    assert (
        (Path(ctx.workspace.project_dir) / "tests/assert_positive_amount.sql")
        .read_text(encoding="utf-8")
        .startswith("select * from")
    )


def test_inspect_history_without_git(ctx: ToolContext) -> None:
    result = call(ctx, "inspect_history", path="models/staging/stg_orders.sql")
    assert not result.is_error
    assert "no history" in result.text or "git is not available" in result.text


# A real dbt build: read the failure, fix it, rebuild, query


def test_find_fix_and_rebuild_with_dbt(ctx: ToolContext) -> None:
    failure = call(ctx, "read_failure")
    assert not failure.is_error
    assert "ERROR model.tiny_shop.customer_totals" in failure.text, failure.text
    assert NOTHING_FAILED not in failure.text
    assert call(ctx, "read_failure").text.startswith("Same as the first call")

    lineage = call(ctx, "walk_lineage", model="customer_totals", direction="upstream", depth=2)
    assert "model.tiny_shop.stg_orders" in lineage.text
    assert "seed.tiny_shop.raw_orders" in lineage.text

    rows = call(ctx, "query_data", sql="select order_id, amount from stg_orders order by 1")
    assert not rows.is_error and rows.text.startswith("row_count: 4"), rows.text
    refused = call(ctx, "query_data", sql="delete from stg_orders")
    assert refused.is_error
    written = call(ctx, "query_data", sql="select 1; drop table stg_orders")
    assert written.is_error

    call(ctx, "edit_model", path="models/marts/customer_totals.sql",
         old_text="sum(amout)", new_text="sum(amount)")  # fmt: skip
    rebuilt = call(ctx, "run_dbt", selector="customer_totals")
    assert "counts: success=1" in rebuilt.text, rebuilt.text
    totals = call(ctx, "query_data", sql="select total_amount from customer_totals "
                                         "where customer_id = 10")  # fmt: skip
    assert "40.5" in totals.text


def test_run_dbt_passes_full_refresh_only_when_asked(ctx: ToolContext) -> None:
    from verimend.tools.workspace import ExecResult

    seen: list[list[str]] = []

    async def fake_exec(argv: list[str], **_: object) -> ExecResult:
        seen.append(argv)
        return ExecResult(0, "")

    ctx.workspace.exec = fake_exec  # type: ignore[method-assign]
    call(ctx, "run_dbt", selector="mom_agg+")
    call(ctx, "run_dbt", selector="mom_agg+", full_refresh=True)
    assert seen[0][-2:] == ["--select", "mom_agg+"]
    assert seen[1][-3:] == ["--select", "mom_agg+", "--full-refresh"]
