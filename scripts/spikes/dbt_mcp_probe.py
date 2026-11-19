"""Spike 2b: try every dbt CLI tool the dbt MCP server exposes, against local DuckDB.

    uv run python scripts/spikes/dbt_mcp_probe.py <dbt project dir>

Starts `uvx dbt-mcp` over stdio with only the local CLI toolset, lists its tools, calls
each once and writes a works / fails table to bench/results/spikes-2026-11-19/. No model
calls. Throwaway evidence for docs/spikes.md.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "bench" / "results" / "spikes-2026-11-19" / "dbt_mcp_probe.json"

# One reasonable call per tool, against jaffle_shop_duckdb.
ARGUMENTS: dict[str, dict[str, Any]] = {
    "parse": {},
    "list": {"node_selection": "stg_orders+"},
    "compile": {"node_selection": "orders"},
    "run": {"node_selection": "stg_orders"},
    "test": {"node_selection": "stg_orders"},
    "build": {"node_selection": "stg_orders"},
    "show": {"sql_query": "select count(*) as n from {{ ref('orders') }}", "limit": 5},
    "docs": {},
    "clone": {"node_selection": "stg_orders", "state_path": "target"},
    "get_lineage_dev": {"unique_id": "model.jaffle_shop.orders", "depth": 2},
    "get_node_details_dev": {"node_id": "orders"},
}

FAILURE_MARKERS = ("traceback", "encountered an error", "error executing tool", "error in model")


def looks_failed(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in FAILURE_MARKERS)


def text_of(result: Any) -> str:
    parts = [getattr(block, "text", "") for block in result.content]
    return "\n".join(part for part in parts if part)


async def probe(project_dir: Path) -> list[dict[str, Any]]:
    uvx = shutil.which("uvx")
    dbt = REPO / ".venv" / ("Scripts/dbt.exe" if os.name == "nt" else "bin/dbt")
    if uvx is None:
        raise SystemExit("uvx not found on PATH")
    env = {
        **os.environ,
        "DBT_PROJECT_DIR": project_dir.as_posix(),
        "DBT_PROFILES_DIR": project_dir.as_posix(),
        "DBT_PATH": dbt.as_posix(),
        "DISABLE_SEMANTIC_LAYER": "true",
        "DISABLE_DISCOVERY": "true",
        "DISABLE_ADMIN_API": "true",
        "DBT_CLI_TIMEOUT": "120",
        # jaffle_shop_duckdb asks for dbt >= 1.11; we pin 1.10 to match ADE-bench.
        "DBT_VERSION_CHECK": "false",
    }
    server = StdioServerParameters(command=uvx, args=["dbt-mcp"], env=env)
    rows: list[dict[str, Any]] = []
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        print(f"server exposes {len(tools)} tools: {', '.join(sorted(t.name for t in tools))}")
        for spec in sorted(tools, key=lambda t: t.name):
            args = ARGUMENTS.get(spec.name)
            row: dict[str, Any] = {
                "tool": spec.name,
                "params": sorted((spec.input_schema or {}).get("properties", {})),
            }
            if args is None:
                row.update(status="not called", note="not a local dbt CLI tool in this probe")
                rows.append(row)
                continue
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(session.call_tool(spec.name, args), timeout=180)
                output = text_of(result)
                failed = bool(result.is_error) or looks_failed(output)
                row.update(status="fails" if failed else "works", output=output[:1500])
            except Exception as error:  # record and keep probing the rest
                row.update(status="fails", output=f"{type(error).__name__}: {error}"[:1500])
            row.update(args=args, seconds=round(time.monotonic() - started, 1))
            print(f"{row['status']:10} {spec.name:22} {row['seconds']:>6}s")
            rows.append(row)
    return rows


def main() -> None:
    project_dir = Path(sys.argv[1]).resolve()
    rows = asyncio.run(probe(project_dir))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(rows, indent=2).replace(Path.home().as_posix(), "~")
    OUT.write_text(text.replace(str(Path.home()).replace("\\", "\\\\"), "~") + "\n", "utf-8")
    print(f"written: {OUT.relative_to(REPO).as_posix()}")


if __name__ == "__main__":
    main()
