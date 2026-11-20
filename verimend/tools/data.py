"""query_data: one read-only SELECT against the project's DuckDB file."""

import json
from typing import Any

from verimend.hooks.guards import query_refusal
from verimend.tools.base import ToolContext, ToolResult

QUERY_TIMEOUT_S = 20
MAX_ROWS = 50
CELL_CHARS = 80

# Runs inside the workspace, with that workspace's duckdb. Read-only connection, and no
# access to files or the network beyond the database itself.
QUERY_SCRIPT = """
import duckdb, json, os
con = duckdb.connect(os.environ['VERIMEND_DB'], read_only=True,
                     config={'enable_external_access': False})
q = os.environ['VERIMEND_SQL'].strip().rstrip(';')
try:
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(LIMIT + 1)
    total = con.execute('select count(*) from (' + q + ') as verimend_q').fetchone()[0]
    print(json.dumps({'columns': cols, 'rows': [[None if v is None else str(v) for v in r]
                      for r in rows[:LIMIT]], 'row_count': total}))
except Exception as error:
    print(json.dumps({'error': type(error).__name__ + ': ' + str(error)}))
""".replace("LIMIT", str(MAX_ROWS))


def format_rows(payload: dict[str, Any]) -> str:
    columns = [str(c) for c in payload.get("columns", [])]
    rows = payload.get("rows", [])
    total = payload.get("row_count", len(rows))

    def cell(value: Any) -> str:
        text = "NULL" if value is None else str(value).replace("\n", " ")
        return text if len(text) <= CELL_CHARS else text[: CELL_CHARS - 1] + "…"

    lines = [f"row_count: {total}" + (f" (showing {len(rows)})" if total > len(rows) else "")]
    lines.append(" | ".join(columns))
    lines.extend(" | ".join(cell(v) for v in row) for row in rows)
    return "\n".join(lines)


async def query_data(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sql = str(args.get("sql", ""))
    refusal = query_refusal(sql)
    if refusal:
        return ctx.result(refusal, True)
    ws = ctx.workspace
    done = await ws.exec(
        [*ws.python, "-c", QUERY_SCRIPT],
        env={"VERIMEND_DB": ws.db_path, "VERIMEND_SQL": sql},
        timeout=QUERY_TIMEOUT_S,
    )
    if done.timed_out:
        return ctx.result(f"Query stopped after {QUERY_TIMEOUT_S} seconds.", True)
    last = done.output.strip().splitlines()[-1] if done.output.strip() else ""
    try:
        payload = json.loads(last)
    except json.JSONDecodeError:
        return ctx.result(f"query failed (exit code {done.code}): {last[-500:]}", True)
    if "error" in payload:
        return ctx.result(str(payload["error"]), True)
    return ctx.result(format_rows(payload))
