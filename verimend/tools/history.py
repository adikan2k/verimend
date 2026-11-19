"""inspect_history: the last commits that touched a file, diffs trimmed."""

from typing import Any

from verimend.tools.base import ToolContext, ToolResult
from verimend.tools.workspace import safe_relative

MAX_COMMITS = 5
DIFF_LINES = 80


def trim_log(log: str, diff_lines: int = DIFF_LINES) -> str:
    """Keep each commit's header and at most `diff_lines` lines of its diff."""
    commits: list[list[str]] = []
    for line in log.splitlines():
        if line.startswith("commit ") or not commits:
            commits.append([])
        commits[-1].append(line)
    out: list[str] = []
    for lines in commits:
        start = next((i for i, line in enumerate(lines) if line.startswith("diff --git")), None)
        if start is None:
            out.extend(lines)
            continue
        out.extend(lines[:start])
        diff = lines[start:]
        out.extend(diff[:diff_lines])
        if len(diff) > diff_lines:
            out.append(f"[diff trimmed: showed {diff_lines} of {len(diff)} lines]")
    return "\n".join(out)


async def inspect_history(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ws = ctx.workspace
    relative = safe_relative(str(args.get("path", "")), ws.project_dir)
    if relative is None:
        return ctx.result("Path must stay inside the project.", True)
    done = await ws.exec(
        ["git", "log", f"-n{MAX_COMMITS}", "-p", "--follow", "--no-color", "--", relative],
        timeout=30,
    )
    if done.code == 127:
        return ctx.result("git is not available here, so there is no history to inspect.")
    if done.code != 0:
        if "not a git repository" in done.output:
            return ctx.result("This project is not a git repository: no history to inspect.")
        return ctx.result(f"git log failed: {done.output.strip()[-300:]}", True)
    if not done.output.strip():
        return ctx.result(f"No commits touch {relative}.")
    return ctx.result(trim_log(done.output))
