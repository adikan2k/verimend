# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Spike 3: a three-task pilot that measures tokens and cost per run on Haiku 5.5.

Runs inside the Linux runner (scripts/spikes/pilot_run.ps1), never directly on Windows:

    python /verimend/scripts/spikes/pilot.py --limit 3 --max-usd 0.75

ADE-bench's own harness stages each task and grades it with the task's own dbt tests. This
file only swaps in a throwaway mini-agent: five draft tools on the Claude Agent SDK that
work on the task container through `docker exec`. The code is evidence for docs/spikes.md
and is discarded after phase 1; it is the stated exception to "only verimend/agent/
imports the Agent SDK".
"""

import argparse
import asyncio
import difflib
import io
import json
import os
import re
import shlex
import tarfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ade_bench.agents.agent_factory import AgentFactory
from ade_bench.agents.agent_name import AgentName
from ade_bench.agents.base_agent import AgentResult, BaseAgent
from ade_bench.harness import Harness
from ade_bench.harness_models import FailureMode
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from verimend.config import REPO_ROOT, load_settings
from verimend.hooks.caps import BatchBudget, RunCaps, estimate_batch_usd, require_batch_caps
from verimend.pricing import Usage, is_long_prompt

BATCH = "pilot-2026-11-19"
TASKS = ["analytics_engineering002", "asana001", "f1007"]  # pinned to the development set
ESTIMATED_USD_PER_RUN = 0.10  # SPEC section 9 estimate, which this pilot replaces
OUT_DIR = REPO_ROOT / "bench" / "results" / BATCH
APP = PurePosixPath("/app")
SERVER = "pilot"
OUTPUT_CAP = 4_000
EDITABLE_PREFIXES = ("models/", "macros/", "seeds/", "snapshots/", "analyses/")
EDITABLE_FILES = ("dbt_project.yml", "packages.yml")
SELECTOR = re.compile(r"^[A-Za-z0-9_+@:.,*/ -]{1,200}$")

SYSTEM_PROMPT = """You repair dbt projects. The project is at /app and runs on DuckDB.

Work in this order: find out what is failing, look at the code and the data that explain
it, make the smallest change that fixes the cause, then rebuild the affected models to
confirm. Never edit, delete or weaken a test to make a run pass.

Paths you pass to tools are relative to the project root, for example
models/staging/stg_orders.sql. Tool output is capped and says when it was trimmed.

When you are done, reply with one short paragraph that starts with exactly one of
FIXED, NOT_FIXED or ESCALATE, then names the root cause and the files you changed."""


def cap(text: str, limit: int = OUTPUT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[trimmed: showed {limit} of {len(text)} characters]"


def reply(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": cap(text)}]}
    if is_error:
        result["is_error"] = True
    return result


def safe_relative(path: str) -> PurePosixPath | None:
    """A project-relative path, or None if it escapes the project."""
    candidate = PurePosixPath(path.strip().lstrip("./") or ".")
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate


def is_editable(path: str) -> bool:
    relative = safe_relative(path)
    if relative is None:
        return False
    text = relative.as_posix()
    return text in EDITABLE_FILES or text.startswith(EDITABLE_PREFIXES)


def is_single_select(sql: str) -> bool:
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        return False
    first = body.split(None, 1)[0].lower() if body else ""
    return first in {"select", "with"}


@dataclass
class Trace:
    task: str
    events: list[dict[str, Any]] = field(default_factory=list)

    def add(self, event: str, **data: Any) -> None:
        self.events.append({"event": event, "t": round(time.time(), 3), **data})


class Container:
    """The task container, reached through the Docker API the harness already holds."""

    def __init__(self, container: Any) -> None:
        self._container = container

    def run(self, command: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        result = self._container.exec_run(command, workdir=str(APP), environment=env)
        return result.exit_code, result.output.decode("utf-8", errors="replace")

    def read(self, relative: PurePosixPath) -> str | None:
        code, output = self.run(["cat", str(APP / relative)])
        return output if code == 0 else None

    def write(self, relative: PurePosixPath, content: str) -> None:
        data = content.encode("utf-8")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo(name=relative.name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        parent = APP / relative.parent
        self.run(["mkdir", "-p", str(parent)])
        self._container.put_archive(str(parent), stream.getvalue())


def summarise_dbt(container: Container, exit_code: int, output: str) -> str:
    """Pass or fail per node from run_results.json, or the log tail if dbt did not get there."""
    code, raw = container.run(["cat", str(APP / "target" / "run_results.json")])
    lines = [f"dbt exit code: {exit_code}"]
    try:
        results = json.loads(raw)["results"] if code == 0 else []
    except (json.JSONDecodeError, KeyError):
        results = []
    counts: dict[str, int] = {}
    problems: list[str] = []
    for result in results:
        status = str(result.get("status"))
        counts[status] = counts.get(status, 0) + 1
        if status in {"error", "fail", "warn", "runtime error"}:
            message = " ".join(str(result.get("message") or "").split())[:400]
            problems.append(f"- {status.upper()} {result.get('unique_id')}: {message}")
    if counts:
        lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.extend(problems[:25])
    if exit_code != 0 and not problems:
        tail = [line for line in output.splitlines() if line.strip()][-40:]
        lines.append("log tail:")
        lines.extend(tail)
    return "\n".join(lines)


QUERY_SCRIPT = """
import duckdb, glob, json, os
db = sorted(glob.glob('/app/*.duckdb'))[0]
con = duckdb.connect(db, read_only=True)
q = os.environ['Q'].strip().rstrip(';')
cur = con.execute(q)
cols = [d[0] for d in cur.description]
rows = cur.fetchmany(51)
total = con.execute('select count(*) from (' + q + ') as t').fetchone()[0]
print(json.dumps({'db': os.path.basename(db), 'row_count': total, 'columns': cols,
                  'rows': [[str(v) for v in r] for r in rows[:50]]}))
"""


def build_tools(container: Container, trace: Trace) -> list[Any]:
    async def timed(name: str, args: dict[str, Any], work: Any) -> dict[str, Any]:
        started = time.monotonic()
        result: dict[str, Any] = await asyncio.to_thread(work)
        text = result["content"][0]["text"]
        trace.add(
            "tool_call",
            name=name,
            arguments=args,
            duration_s=round(time.monotonic() - started, 2),
            output_chars=len(text),
            is_error=bool(result.get("is_error")),
        )
        return result

    @tool("read_failure", "Run `dbt build` on the whole project and summarise what fails.", {})
    async def read_failure(args: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            code, output = container.run(["dbt", "build"])
            return reply(summarise_dbt(container, code, output))

        return await timed("read_failure", args, work)

    @tool(
        "read_path",
        "Read a file in the project, or list a folder. Path is relative to the project root.",
        {"path": str},
    )
    async def read_path(args: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            relative = safe_relative(str(args.get("path", ".")))
            if relative is None:
                return reply("Path must stay inside the project.", is_error=True)
            target = str(APP / relative)
            code, output = container.run(
                [
                    "bash",
                    "-c",
                    f"if [ -d {shlex.quote(target)} ]; then "
                    f"find {shlex.quote(target)} -maxdepth 2 -not -path '*/target*' "
                    f"-not -path '*/dbt_packages*' -not -path '*/logs*' | sort; "
                    f"else cat {shlex.quote(target)}; fi",
                ]
            )
            return reply(output if code == 0 else f"Not found: {relative}", is_error=code != 0)

        return await timed("read_path", args, work)

    @tool(
        "query_data",
        "Run one read-only SELECT against the DuckDB database. Returns up to 50 rows "
        "and the total row count. dbt models are tables or views named after the model.",
        {"sql": str},
    )
    async def query_data(args: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            code, output = container.run(
                ["timeout", "20", "python3", "-c", QUERY_SCRIPT], env={"Q": str(args["sql"])}
            )
            if code == 124:
                return reply("Query stopped after 20 seconds.", is_error=True)
            if code != 0:
                return reply(output.strip().splitlines()[-1] if output else "query failed", True)
            return reply(output)

        return await timed("query_data", args, work)

    @tool(
        "edit_file",
        "Replace the whole content of a model, macro, seed, snapshot or analysis file, or "
        "dbt_project.yml / packages.yml. Returns the diff.",
        {"path": str, "content": str},
    )
    async def edit_file(args: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            relative = safe_relative(str(args["path"]))
            assert relative is not None  # the hook has already refused anything else
            before = container.read(relative) or ""
            content = str(args["content"])
            container.write(relative, content)
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
            trace.add("diff", path=relative.as_posix(), diff=diff)
            return reply(diff or "No change: the new content equals the old content.")

        return await timed("edit_file", args, work)

    @tool(
        "run_dbt",
        "Run `dbt build --select <selector>` and report pass or fail per node, "
        "for example 'stg_orders+'.",
        {"selector": str},
    )
    async def run_dbt(args: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            selector = str(args["selector"])
            if not SELECTOR.match(selector):
                return reply("Selector has characters dbt selectors do not use.", True)
            code, output = container.run(["dbt", "build", "--select", *selector.split()])
            return reply(summarise_dbt(container, code, output))

        return await timed("run_dbt", args, work)

    return [read_failure, read_path, query_data, edit_file, run_dbt]


def build_hooks(trace: Trace) -> dict[str, list[HookMatcher]]:
    def deny(tool_name: str, reason: str) -> dict[str, Any]:
        trace.add("hook_block", tool=tool_name, reason=reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def guard_edit(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        path = str(input_data["tool_input"].get("path", ""))
        if is_editable(path):
            return {}
        return deny(input_data["tool_name"], f"edit_file may not touch {path!r}")

    async def guard_query(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        if is_single_select(str(input_data["tool_input"].get("sql", ""))):
            return {}
        return deny(input_data["tool_name"], "query_data accepts one SELECT statement only")

    return {
        "PreToolUse": [
            HookMatcher(matcher=f"mcp__{SERVER}__edit_file", hooks=[guard_edit]),
            HookMatcher(matcher=f"mcp__{SERVER}__query_data", hooks=[guard_query]),
        ]
    }


def usage_from(raw: dict[str, Any]) -> Usage:
    split = raw.get("cache_creation") or {}
    one_hour = split.get("ephemeral_1h_input_tokens", 0) or 0
    created = raw.get("cache_creation_input_tokens", 0) or 0
    return Usage(
        input_tokens=raw.get("input_tokens", 0) or 0,
        output_tokens=raw.get("output_tokens", 0) or 0,
        cache_read_tokens=raw.get("cache_read_input_tokens", 0) or 0,
        cache_write_5m_tokens=split.get("ephemeral_5m_input_tokens", created - one_hour) or 0,
        cache_write_1h_tokens=one_hour,
    )


async def run_agent(
    task_prompt: str, container: Container, trace: Trace, model: str, api_key: str, caps: RunCaps
) -> dict[str, Any]:
    server = create_sdk_mcp_server(
        name=SERVER, version="0.0.1", tools=build_tools(container, trace)
    )
    names = [
        f"mcp__{SERVER}__{n}"
        for n in ("read_failure", "read_path", "query_data", "edit_file", "run_dbt")
    ]
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        mcp_servers={SERVER: server},
        allowed_tools=names,
        permission_mode="dontAsk",
        setting_sources=[],
        hooks=build_hooks(trace),  # type: ignore[arg-type]
        max_turns=caps.max_steps,
        max_budget_usd=caps.max_usd,
        env={"ANTHROPIC_API_KEY": api_key},
        cwd="/tmp",
    )
    prompts: dict[str, int] = {}
    final_text = ""
    result: dict[str, Any] = {}
    async for message in query(prompt=task_prompt, options=options):
        if isinstance(message, AssistantMessage):
            if message.message_id and message.usage:
                usage = usage_from(message.usage)
                # Prompt size is complete when the message starts; output tokens are not.
                prompts[message.message_id] = usage.prompt_tokens
            texts = [block.text for block in message.content if isinstance(block, TextBlock)]
            if texts:
                final_text = "\n".join(texts)
        elif isinstance(message, ResultMessage):
            result = {
                "subtype": message.subtype,
                "is_error": message.is_error,
                "num_turns": message.num_turns,
                "duration_ms": message.duration_ms,
                "total_cost_usd": message.total_cost_usd,
                "usage": message.usage,
                "model_usage": message.model_usage,
                "stop_reason": message.stop_reason,
            }
    for message_id, prompt_tokens in prompts.items():
        trace.add("model_usage", message_id=message_id, prompt_tokens=prompt_tokens)
    long_prompts = sum(1 for p in prompts.values() if is_long_prompt(model, Usage(input_tokens=p)))
    trace.add("report", text=final_text)
    return {
        "result": result,
        "largest_prompt_tokens": max(prompts.values(), default=0),
        "requests": len(prompts),
        "long_prompt_requests": long_prompts,
    }


class PilotAgent(BaseAgent):
    NAME = AgentName.NONE  # the harness only uses this as a label; grading is unchanged

    def __init__(self, factory: "PilotFactory", **_: Any) -> None:
        super().__init__()
        self._factory = factory

    def perform_task(
        self,
        task_prompt: str,
        session: Any,
        logging_dir: Path | None = None,
        task_name: str | None = None,
    ) -> AgentResult:
        factory = self._factory
        task = task_name or "unknown"
        trace = Trace(task=task)
        factory.traces[task] = trace
        run_cap = factory.budget.next_run_cap(factory.settings.max_usd_per_run)
        caps = RunCaps(
            max_usd=run_cap or 0.0,
            max_steps=factory.settings.max_steps,
            max_attempts=1,
        )
        trace.add(
            "run_start",
            model=factory.model,
            git_sha=os.environ.get("VERIMEND_GIT_SHA", "unknown"),
            task=task,
            caps={"max_usd": caps.max_usd, "max_steps": caps.max_steps, "max_attempts": 1},
        )
        if run_cap is None:
            trace.add("run_end", stopped="batch dollar cap reached before this run")
            return AgentResult(input_tokens=0, output_tokens=0, failure_mode=FailureMode.NONE)

        started = time.monotonic()
        outcome = asyncio.run(
            run_agent(
                task_prompt,
                Container(session.container),
                trace,
                factory.model,
                factory.api_key,
                caps,
            )
        )
        result = outcome["result"]
        cost = float(result.get("total_cost_usd") or 0.0)
        factory.budget.record(cost)
        usage = result.get("usage") or {}
        trace.add(
            "run_end",
            subtype=result.get("subtype"),
            stop_reason=result.get("stop_reason"),
            dollars=cost,
            seconds=round(time.monotonic() - started, 1),
            steps=result.get("num_turns"),
            tool_calls=sum(1 for e in trace.events if e["event"] == "tool_call"),
            hook_blocks=sum(1 for e in trace.events if e["event"] == "hook_block"),
            tokens={
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_write": usage.get("cache_creation_input_tokens", 0),
            },
            requests=outcome["requests"],
            largest_prompt_tokens=outcome["largest_prompt_tokens"],
            long_prompt_requests=outcome["long_prompt_requests"],
            model_usage=result.get("model_usage"),
        )
        return AgentResult(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            num_turns=int(result.get("num_turns") or 0),
            cost_usd=cost,
            model_name=factory.model,
            failure_mode=FailureMode.NONE,
        )


class PilotFactory(AgentFactory):
    def __init__(self, budget: BatchBudget) -> None:
        self.settings = load_settings()
        self.api_key = self.settings.require_api_key()
        self.model = self.settings.model_default
        self.budget = budget
        self.traces: dict[str, Trace] = {}

    @property
    def agent_name(self) -> AgentName:
        return AgentName.NONE

    def get_agent(self, **kwargs: Any) -> BaseAgent:
        return PilotAgent(self)


def write_traces(factory: PilotFactory, results: Any, run_id: str) -> dict[str, Any]:
    by_task = {r.task_id: r for r in results.results}
    runs: list[dict[str, Any]] = []
    for task, trace in factory.traces.items():
        trial = by_task.get(task)
        statuses = {
            k: str(v.value if hasattr(v, "value") else v)
            for k, v in ((trial.parser_results or {}) if trial else {}).items()
        }
        trace.add(
            "grade",
            grader="ade-bench",
            ade_run_id=run_id,
            passed=bool(trial and trial.is_resolved),
            failure_mode=str(trial.failure_mode.value) if trial else "missing",
            tests=statuses,
        )
        # Move run_end to the last line, after the grade, as SPEC section 8 orders them.
        end = [e for e in trace.events if e["event"] == "run_end"]
        trace.events = [e for e in trace.events if e["event"] != "run_end"] + end
        folder = OUT_DIR / task
        folder.mkdir(parents=True, exist_ok=True)
        lines = (json.dumps(event, default=str) for event in trace.events)
        (folder / "1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        last = end[-1] if end else {}
        runs.append(
            {
                "task": task,
                "trace": f"{task}/1.jsonl",
                "passed": bool(trial and trial.is_resolved),
                "claimed": (
                    next((e["text"] for e in trace.events if e["event"] == "report"), "") or ""
                ).split(None, 1)[0:1],
                "dollars": last.get("dollars"),
                "seconds": last.get("seconds"),
                "steps": last.get("steps"),
                "tool_calls": last.get("tool_calls"),
                "hook_blocks": last.get("hook_blocks"),
                "tokens": last.get("tokens"),
                "largest_prompt_tokens": last.get("largest_prompt_tokens"),
                "stopped": last.get("subtype") or last.get("stopped"),
            }
        )
    dollars = [r["dollars"] for r in runs if r["dollars"] is not None]
    summary = {
        "batch": BATCH,
        "model": factory.model,
        "ade_run_id": run_id,
        "git_sha": os.environ.get("VERIMEND_GIT_SHA", "unknown"),
        "runs": runs,
        "total_dollars": sum(dollars),
        "mean_dollars_per_run": sum(dollars) / len(dollars) if dollars else None,
        "passed": sum(1 for r in runs if r["passed"]),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Spike 3: three-task pilot.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-usd", type=float)
    args = parser.parse_args()
    limit, max_usd = require_batch_caps(args.limit, args.max_usd)
    tasks = TASKS[:limit]

    budget = BatchBudget(max_usd=max_usd)
    factory = PilotFactory(budget)
    estimate = estimate_batch_usd(len(tasks), ESTIMATED_USD_PER_RUN)
    print(
        f"Pilot: {len(tasks)} runs on {factory.model}. Estimated ${estimate:.2f}; "
        f"batch cap ${max_usd:.2f}; per-run cap ${factory.settings.max_usd_per_run:.2f}, "
        f"{factory.settings.max_steps} steps.",
        flush=True,
    )

    run_id = f"{BATCH}__{datetime.now(UTC):%H-%M-%S}"
    harness = Harness(
        dataset_path=Path("tasks"),
        output_path=Path("experiments"),
        run_id=run_id,
        agent_factory=factory,
        task_ids=[f"{t}.base" for t in tasks],
        n_concurrent_trials=1,
        db_type="duckdb",
        project_type="dbt",
    )
    results = harness.run()
    summary = write_traces(factory, results, run_id)
    print(json.dumps(summary, indent=2))
    print(f"Spent ${budget.spent_usd:.4f} of ${max_usd:.2f}.")


if __name__ == "__main__":
    main()
