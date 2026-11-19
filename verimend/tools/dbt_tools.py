"""read_failure and run_dbt: build the project and say what fails, per node."""

import json
import re
from typing import Any

from verimend.tools.base import ToolContext, ToolResult

RUN_RESULTS = "target/run_results.json"
BUILD_TIMEOUT_S = 900
PROBLEM_STATUSES = {"error", "fail", "warn", "runtime error"}
MAX_PROBLEMS = 25
MESSAGE_CHARS = 400
LOG_TAIL_LINES = 40
SELECTOR = re.compile(r"^[A-Za-z0-9_+@:.,*/ -]{1,200}$")

NOTHING_FAILED = (
    "Nothing failed: every node built and every test passed. The problem may be wrong "
    "data rather than a failed run; work from the task description and check the data "
    "with query_data."
)


def summarise_run_results(exit_code: int, raw_results: str | None, log: str) -> tuple[str, int]:
    """dbt's outcome as short text, and how many nodes had a problem.

    Uses run_results.json when dbt got far enough to write it, otherwise the log tail
    (a compilation or parse error stops dbt before any node runs).
    """
    try:
        results: list[dict[str, Any]] = json.loads(raw_results)["results"] if raw_results else []
    except (json.JSONDecodeError, KeyError, TypeError):
        results = []
    counts: dict[str, int] = {}
    problems: list[str] = []
    for result in results:
        status = str(result.get("status"))
        counts[status] = counts.get(status, 0) + 1
        if status in PROBLEM_STATUSES:
            message = " ".join(str(result.get("message") or "").split())[:MESSAGE_CHARS]
            problems.append(f"- {status.upper()} {result.get('unique_id')}: {message}")
    lines = [f"dbt exit code: {exit_code}"]
    if counts:
        lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.extend(problems[:MAX_PROBLEMS])
    if len(problems) > MAX_PROBLEMS:
        lines.append(f"... and {len(problems) - MAX_PROBLEMS} more problems")
    if exit_code != 0 and not problems:
        tail = [line for line in log.splitlines() if line.strip()][-LOG_TAIL_LINES:]
        lines.append("log tail:")
        lines.extend(tail)
    return "\n".join(lines), len(problems) + (1 if exit_code != 0 and not problems else 0)


async def _build(ctx: ToolContext, extra: list[str]) -> tuple[str, int]:
    ws = ctx.workspace
    await ws.remove(RUN_RESULTS)  # never report a previous build's results as this one's
    done = await ws.exec([*ws.dbt, "build", *extra], timeout=BUILD_TIMEOUT_S)
    if done.timed_out:
        return f"dbt build stopped after {BUILD_TIMEOUT_S} seconds.", 1
    raw = await ws.read_text(RUN_RESULTS)
    return summarise_run_results(done.code, raw, done.output)


async def read_failure(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.failure_summary is not None:
        return ctx.result(
            "Same as the first call (read_failure builds once; use run_dbt to rebuild):\n"
            + ctx.failure_summary
        )
    summary, problems = await _build(ctx, [])
    if problems == 0:
        summary += "\n" + NOTHING_FAILED
    ctx.failure_summary = summary
    return ctx.result(summary)


async def run_dbt(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    selector = str(args.get("selector", "")).strip()
    if not SELECTOR.match(selector):
        return ctx.result("Selector is empty or has characters dbt selectors do not use.", True)
    summary, _ = await _build(ctx, ["--select", *selector.split()])
    ctx.manifest = None  # a build re-parses the project
    return ctx.result(summary)
