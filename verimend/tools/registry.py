"""The eight tools (SPEC section 4): names, descriptions, input schemas and handlers.

Descriptions and schemas are part of every prompt, so they stay byte-identical between
runs (prompt caching) and any change to them is a change to what the numbers mean.
"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from verimend.tools.base import ToolContext, ToolResult
from verimend.tools.data import query_data
from verimend.tools.dbt_tools import read_failure, run_dbt
from verimend.tools.files import edit_model, read_file
from verimend.tools.history import inspect_history
from verimend.tools.lineage import walk_lineage
from verimend.tools.report import VERDICT_SCHEMA, report

Handler = Callable[[ToolContext, dict[str, Any]], Coroutine[Any, Any, ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}  # fmt: skip


_PATH = {"type": "string", "description": "Path relative to the project root."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "read_failure",
        "Build the whole project once with `dbt build` and summarise what fails, per node, "
        "with error text. Says so plainly when nothing fails.",
        _schema({}, []),
        read_failure,
    ),
    ToolSpec(
        "walk_lineage",
        "Parents (upstream) or children (downstream) of a model, seed, snapshot or source "
        "from dbt's manifest, with materialisation and file path, and the tests on it.",
        _schema(
            {
                "model": {"type": "string", "description": "Model name, or source.table."},
                "direction": {"type": "string", "enum": ["upstream", "downstream"]},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            ["model", "direction"],
        ),
        walk_lineage,
    ),
    ToolSpec(
        "query_data",
        "Run one read-only SELECT against the project's DuckDB database. Returns up to 50 "
        "rows and the total row count. Models are tables or views named after the model.",
        _schema({"sql": {"type": "string"}}, ["sql"]),
        query_data,
    ),
    ToolSpec(
        "read_file",
        "Read a project file with line numbers, optionally a line range, or list a folder. "
        "Installed packages under dbt_packages/ can be read too.",
        _schema(
            {
                "path": _PATH,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        read_file,
    ),
    ToolSpec(
        "inspect_history",
        "The last 5 git commits that touched a file, with diffs trimmed to 80 lines.",
        _schema({"path": _PATH}, ["path"]),
        inspect_history,
    ),
    ToolSpec(
        "edit_model",
        "Change a model, macro, seed, snapshot or analysis file, or dbt_project.yml / "
        "packages.yml. Pass either the whole new `content`, or `old_text` (which must "
        "appear exactly once) and `new_text`. Returns the diff. Tests cannot be edited.",
        _schema(
            {
                "path": _PATH,
                "content": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            ["path"],
        ),
        edit_model,
    ),
    ToolSpec(
        "run_dbt",
        "Run `dbt build --select <selector>` and report pass or fail per node, for example "
        "'stg_orders+' for a model and everything downstream of it.",
        _schema({"selector": {"type": "string"}}, ["selector"]),
        run_dbt,
    ),
    ToolSpec(
        "report",
        "Your final answer, once: the verdict, root cause, evidence and changes. Calling it "
        "ends the run.",
        VERDICT_SCHEMA,
        report,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOLS)


def get_tool(name: str) -> ToolSpec:
    for spec in TOOLS:
        if spec.name == name:
            return spec
    raise KeyError(name)
