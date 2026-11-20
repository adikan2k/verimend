"""Guards as plain functions. verimend/agent/ wires them in as PreToolUse hooks; the tools
call the same predicates again, so a guard holds even if a hook is not wired.

Each guard returns None to allow a call, or the reason it is refused. Every guard has a
test that proves it blocks (CLAUDE.md rule 4).
"""

import json
import re
from typing import Any

import yaml

from verimend.tools.workspace import safe_relative

# What edit_model may change (SPEC section 4): models, macros, seeds and the project files
# that configure them. Never tests, packages, build output or anything outside the project.
EDITABLE_PREFIXES = ("models/", "macros/", "seeds/", "snapshots/", "analyses/")
EDITABLE_FILES = ("dbt_project.yml", "packages.yml")
EDITABLE_SUFFIXES = (".sql", ".yml", ".yaml", ".csv", ".md")

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def edit_refusal(path: str, project_dir: str) -> str | None:
    relative = safe_relative(path, project_dir)
    if relative is None or relative == ".":
        return f"edit_model may only change files inside the project, not {path!r}"
    if relative in EDITABLE_FILES:
        return None
    if not relative.startswith(EDITABLE_PREFIXES):
        return (
            f"edit_model may not change {relative!r}: only files under models/, macros/, "
            "seeds/, snapshots/ and analyses/, plus dbt_project.yml and packages.yml"
        )
    if not relative.endswith(EDITABLE_SUFFIXES):
        return f"edit_model may not change {relative!r}: not a model, macro, seed or YAML file"
    return None


def query_refusal(sql: str) -> str | None:
    """query_data runs one read-only SELECT (or WITH ... SELECT), nothing else."""
    body = _SQL_COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if not body:
        return "query_data needs a SELECT statement"
    if ";" in body:
        return "query_data runs one statement at a time"
    first = body.split(None, 1)[0].lower()
    if first not in {"select", "with", "from"}:
        return f"query_data runs SELECT statements only, not {first.upper()}"
    return None


TEST_KEYS = ("data_tests", "tests")
RESOURCE_KEYS = ("models", "sources", "seeds", "snapshots")
_TEST_MACRO = re.compile(r"{%-?\s*test\s+\w+|{%-?\s*macro\s+(\w+__)?test_\w+", re.I)


def _yaml(text: str | None) -> Any:
    if not text:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def yaml_tests(text: str | None) -> set[tuple[str, str]]:
    """Every test defined in a properties file, as (where it is, what it is).

    Covers tests on models, sources and their tables, seeds and snapshots, and on their
    columns, under both `data_tests` and the older `tests` key.
    """
    found: set[tuple[str, str]] = set()
    document = _yaml(text)
    if not isinstance(document, dict):
        return found

    def collect(owner: str, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        for key in TEST_KEYS:
            for test in entry.get(key) or []:
                found.add((owner, _canonical(test)))
        for column in entry.get("columns") or []:
            if isinstance(column, dict):
                for key in TEST_KEYS:
                    for test in column.get(key) or []:
                        found.add((f"{owner}.{column.get('name')}", _canonical(test)))

    for kind in RESOURCE_KEYS:
        for item in document.get(kind) or []:
            if not isinstance(item, dict):
                continue
            owner = f"{kind}:{item.get('name')}"
            collect(owner, item)
            for table in item.get("tables") or []:
                if isinstance(table, dict):
                    collect(f"{owner}.{table.get('name')}", table)
    return found


def content_refusal(relative: str, before: str | None, after: str) -> str | None:
    """Refuse an edit whose content weakens a test (SPEC section 4: test blocks in YAML
    files are off-limits, like the tests/ folder). Adding a test is fine."""
    if relative.endswith((".yml", ".yaml")):
        removed = yaml_tests(before) - yaml_tests(after)
        if removed:
            owners = sorted({owner for owner, _ in removed})
            return (
                f"edit_model may not remove or change tests ({', '.join(owners[:5])} in "
                f"{relative}). Fix the model, not its tests."
            )
        if relative == "dbt_project.yml":
            old_doc, new_doc = _yaml(before), _yaml(after)
            old_doc = old_doc if isinstance(old_doc, dict) else {}
            new_doc = new_doc if isinstance(new_doc, dict) else {}
            for key in TEST_KEYS:
                if _canonical(old_doc.get(key)) != _canonical(new_doc.get(key)):
                    return f"edit_model may not change the project's {key} settings."
    if relative.endswith(".sql") and (
        _TEST_MACRO.search(before or "") or _TEST_MACRO.search(after)
    ):
        return f"edit_model may not change or add a generic test ({relative} defines one)."
    return None


def after_report_refusal(reported: bool) -> str | None:
    if reported:
        return "the run has ended: report was already called. End your turn now."
    return None


def refusal_for(
    tool: str, arguments: dict[str, Any], *, project_dir: str, reported: bool
) -> str | None:
    """The guard decision for one tool call, by tool name (without the MCP prefix)."""
    reason = after_report_refusal(reported)
    if reason:
        return reason
    if tool == "edit_model":
        return edit_refusal(str(arguments.get("path", "")), project_dir)
    if tool == "query_data":
        return query_refusal(str(arguments.get("sql", "")))
    return None
