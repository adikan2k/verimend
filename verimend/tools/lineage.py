"""walk_lineage: parents or children of a node, read from dbt's manifest.json."""

import json
from collections import deque
from typing import Any, Literal

from verimend.tools.base import ToolContext, ToolResult

MANIFEST = "target/manifest.json"
PARSE_TIMEOUT_S = 300
MAX_DEPTH = 5
MAX_NODES = 60

Direction = Literal["upstream", "downstream"]


def find_node(manifest: dict[str, Any], name: str) -> str | None:
    """The unique ID for a model, seed, snapshot or source named `name`.

    Accepts a unique ID, a bare name (`stg_orders`), or `source_name.table` for sources.
    Project nodes win over package nodes of the same name.
    """
    name = name.strip()
    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})
    if name in nodes or name in sources:
        return name
    project = manifest.get("metadata", {}).get("project_name")
    matches = [
        uid
        for uid, node in nodes.items()
        if node.get("resource_type") in {"model", "seed", "snapshot"} and node.get("name") == name
    ]
    if matches:
        matches.sort(key=lambda uid: (nodes[uid].get("package_name") != project, uid))
        return matches[0]
    for uid, source in sources.items():
        qualified = f"{source.get('source_name')}.{source.get('name')}"
        if name in (qualified, source.get("name")):
            return uid
    return None


def describe(manifest: dict[str, Any], uid: str) -> str:
    node = manifest.get("nodes", {}).get(uid) or manifest.get("sources", {}).get(uid) or {}
    kind = node.get("resource_type", "?")
    path = node.get("original_file_path", "?")
    if kind == "source":
        return f"{uid} (source, {path})"
    materialized = (node.get("config") or {}).get("materialized", "?")
    return f"{uid} ({kind}, {materialized}, {path})"


def walk(manifest: dict[str, Any], start: str, direction: Direction, depth: int) -> str:
    """Breadth-first walk to `depth` levels. Tests on a node are listed, not walked."""
    edges = manifest.get("parent_map" if direction == "upstream" else "child_map", {})
    depth = max(1, min(depth, MAX_DEPTH))
    lines = [f"{direction} of {describe(manifest, start)}, {depth} level(s):"]
    seen = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    listed = 0
    tests: list[str] = []
    while queue:
        uid, level = queue.popleft()
        if level >= depth:
            continue
        for other in sorted(edges.get(uid, [])):
            if other in seen:
                continue
            seen.add(other)
            if other.startswith("test."):
                if level == 0:
                    tests.append(other.split(".")[-2] if other.count(".") >= 3 else other)
                continue
            listed += 1
            if listed <= MAX_NODES:
                lines.append(f"{'  ' * (level + 1)}{describe(manifest, other)}")
            queue.append((other, level + 1))
    if listed == 0:
        lines.append("  (none)")
    if listed > MAX_NODES:
        lines.append(f"  ... {listed - MAX_NODES} more not shown")
    if tests:
        lines.append(f"tests on this node: {', '.join(sorted(set(tests)))}")
    return "\n".join(lines)


async def load_manifest(ctx: ToolContext) -> tuple[dict[str, Any] | None, str]:
    if ctx.manifest is not None:
        return ctx.manifest, ""
    ws = ctx.workspace
    done = await ws.exec([*ws.dbt, "parse"], timeout=PARSE_TIMEOUT_S)
    raw = await ws.read_text(MANIFEST)
    if raw is None:
        tail = "\n".join(done.output.strip().splitlines()[-20:])
        return None, f"dbt parse did not write a manifest (exit code {done.code}):\n{tail}"
    try:
        ctx.manifest = json.loads(raw)
    except json.JSONDecodeError:
        return None, "manifest.json is not valid JSON"
    return ctx.manifest, ""


async def walk_lineage(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manifest, problem = await load_manifest(ctx)
    if manifest is None:
        return ctx.result(problem, True)
    name = str(args.get("model", ""))
    uid = find_node(manifest, name)
    if uid is None:
        return ctx.result(f"No model, seed, snapshot or source named {name!r}.", True)
    direction = args.get("direction", "upstream")
    if direction not in ("upstream", "downstream"):
        return ctx.result("direction must be 'upstream' or 'downstream'.", True)
    depth = int(args.get("depth", 2) or 2)
    return ctx.result(walk(manifest, uid, direction, depth))
