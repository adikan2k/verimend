"""Guards as plain functions. verimend/agent/ wires them in as PreToolUse hooks; the tools
call the same predicates again, so a guard holds even if a hook is not wired.

Each guard returns None to allow a call, or the reason it is refused. Every guard has a
test that proves it blocks (CLAUDE.md rule 4).
"""

import re
from typing import Any

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
