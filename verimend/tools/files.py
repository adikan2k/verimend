"""read_file and edit_model: look at a project file, change one."""

import difflib
from typing import Any

from verimend.hooks.guards import edit_refusal
from verimend.tools.base import ToolContext, ToolResult
from verimend.tools.workspace import safe_relative

LIST_DEPTH = 3
MAX_LISTED = 200


async def read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ws = ctx.workspace
    relative = safe_relative(str(args.get("path", ".")), ws.project_dir)
    if relative is None:
        return ctx.result("Path must stay inside the project.", True)
    listing = await ws.list_files(relative, LIST_DEPTH)
    if listing is not None:
        shown = listing[:MAX_LISTED]
        more = f"\n... {len(listing) - MAX_LISTED} more" if len(listing) > MAX_LISTED else ""
        return ctx.result(f"{relative}/ ({len(listing)} files)\n" + "\n".join(shown) + more)
    text = await ws.read_text(relative)
    if text is None:
        return ctx.result(f"Not found: {relative}", True)
    lines = text.splitlines()
    start = max(int(args.get("start_line") or 1), 1)
    end = min(int(args.get("end_line") or len(lines)), len(lines))
    width = len(str(end))
    body = "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(start, end + 1))
    header = f"{relative}, lines {start}-{end} of {len(lines)}"
    return ctx.result(f"{header}\n{body}")


async def edit_model(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ws = ctx.workspace
    path = str(args.get("path", ""))
    refusal = edit_refusal(path, ws.project_dir)
    if refusal:
        return ctx.result(refusal, True)
    relative = safe_relative(path, ws.project_dir)
    assert relative is not None  # edit_refusal has checked it
    before = await ws.read_text(relative)
    content = args.get("content")
    old, new = args.get("old_text"), args.get("new_text")
    if content is not None and old is None:
        after = str(content)
    elif old is not None and new is not None and content is None:
        if before is None:
            return ctx.result(f"{relative} does not exist; pass the whole content instead.", True)
        found = before.count(str(old))
        if found != 1:
            return ctx.result(
                f"old_text must appear exactly once in {relative}; it appears {found} times.",
                True,
            )
        after = before.replace(str(old), str(new), 1)
    else:
        return ctx.result("Pass either content, or old_text and new_text.", True)
    before = before or ""
    if after == before:
        return ctx.result("No change: the new content equals the old content.")
    await ws.write_text(relative, after)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )
    ctx.diffs.append({"path": relative, "diff": diff})
    ctx.manifest = None
    return ctx.result(diff)
