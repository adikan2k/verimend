"""The agent loop: the eight tools on an in-process MCP server, the guards as PreToolUse
hooks, one `query()` call. The only module that imports the Agent SDK (CLAUDE.md rule 5).

Every SDK call here is one docs/spikes.md records as working (spike 1 and the pilot).
"""

import asyncio
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultError,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from verimend import __version__
from verimend.agent.prompt import SYSTEM_PROMPT, prompt_sha
from verimend.hooks.caps import RunCaps, sdk_budget_usd
from verimend.hooks.guards import content_refusal, refusal_for
from verimend.pricing import usage_from_api
from verimend.runrecord import AgentRun
from verimend.tools.base import DEFAULT_OUTPUT_CHARS, ToolContext, ToolResult
from verimend.tools.edits import planned_content
from verimend.tools.registry import TOOL_NAMES, TOOLS, ToolSpec
from verimend.tools.workspace import Workspace, safe_relative

SERVER = "verimend"
ARGUMENT_CHARS = 300  # how much of each tool argument the trace keeps
ERROR_CHARS = 500


def mcp_name(tool_name: str) -> str:
    return f"mcp__{SERVER}__{tool_name}"


def short_name(name: str) -> str:
    return name.removeprefix(f"mcp__{SERVER}__")


def trim_arguments(args: dict[str, Any]) -> dict[str, Any]:
    def trim(value: Any) -> Any:
        if isinstance(value, str) and len(value) > ARGUMENT_CHARS:
            return value[:ARGUMENT_CHARS] + f"… [{len(value)} chars]"
        return value

    return {key: trim(value) for key, value in args.items()}


def wrap_tool(spec: ToolSpec, ctx: ToolContext, run: AgentRun) -> SdkMcpTool[Any]:
    """The SDK tool for one of our tools: times the call and records a tool_call event."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        started_at = time.time()
        started = time.monotonic()
        try:
            result = await spec.handler(ctx, args)
        except Exception as error:  # a tool bug must not end the run without a trace
            result = ToolResult(f"tool failed: {type(error).__name__}: {error}"[:ERROR_CHARS], True)
        run.events.append(
            {
                "event": "tool_call",
                "t": round(started_at, 3),
                "name": spec.name,
                "arguments": trim_arguments(args),
                "duration_s": round(time.monotonic() - started, 2),
                "output_chars": len(result.text),
                "is_error": result.is_error,
            }
        )
        reply: dict[str, Any] = {"content": [{"type": "text", "text": result.text}]}
        if result.is_error:
            reply["is_error"] = True
        return reply

    return tool(spec.name, spec.description, spec.input_schema)(handler)


async def edit_content_refusal(ctx: ToolContext, arguments: dict[str, Any]) -> str | None:
    """The content half of the edit guard: what the edit would write, judged before it is
    written. A malformed call is left to the tool, which explains the problem."""
    relative = safe_relative(str(arguments.get("path", "")), ctx.workspace.project_dir)
    if relative is None:
        return None
    before = await ctx.workspace.read_text(relative)
    after, _ = planned_content(before, arguments)
    return None if after is None else content_refusal(relative, before, after)


def build_hooks(ctx: ToolContext, run: AgentRun) -> dict[str, list[HookMatcher]]:
    async def guard(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        name = short_name(str(input_data.get("tool_name", "")))
        arguments = dict(input_data.get("tool_input") or {})
        reason = refusal_for(
            name, arguments, project_dir=ctx.workspace.project_dir, reported=ctx.accepted
        )
        if reason is None and name == "edit_model":
            reason = await edit_content_refusal(ctx, arguments)
        if reason is None:
            return {}
        run.events.append({"event": "hook_block", "t": round(time.time(), 3), "tool": name,
                           "reason": reason})  # fmt: skip
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    matcher = "|".join(mcp_name(name) for name in TOOL_NAMES)
    return {"PreToolUse": [HookMatcher(matcher=matcher, hooks=[guard])]}  # type: ignore[list-item]


def build_options(
    ctx: ToolContext, run: AgentRun, *, model: str, api_key: str, caps: RunCaps, cwd: Path
) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(
        name=SERVER, version=__version__, tools=[wrap_tool(spec, ctx, run) for spec in TOOLS]
    )
    return ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,  # a plain string replaces Claude Code's own prompt
        tools=[],  # no built-in tools: only ours
        mcp_servers={SERVER: server},
        allowed_tools=[mcp_name(name) for name in TOOL_NAMES],
        permission_mode="dontAsk",
        setting_sources=[],  # no user, project or local Claude settings
        hooks=build_hooks(ctx, run),  # type: ignore[arg-type]
        max_turns=caps.max_steps,
        max_budget_usd=sdk_budget_usd(caps.max_usd),
        env={"ANTHROPIC_API_KEY": api_key},  # to the CLI only, never the system env
        cwd=str(cwd),  # fixed across runs, so the cached prompt prefix is too
    )


def absorb(run: AgentRun, message: Any) -> None:
    """Fold one SDK message into the run record."""
    if isinstance(message, SystemMessage) and message.subtype == "init":
        run.tools_offered = [str(t) for t in message.data.get("tools", [])]
    elif isinstance(message, AssistantMessage):
        if message.message_id and message.usage:
            # Prompt-side counts are complete when the message starts; output is not
            # (docs/spikes.md), so cost always comes from the result, never from here.
            run.prompt_tokens[message.message_id] = usage_from_api(message.usage).prompt_tokens
        texts = [block.text for block in message.content if isinstance(block, TextBlock)]
        if texts:
            run.final_text = "\n".join(texts)
    elif isinstance(message, ResultMessage):
        run.cost_usd = message.total_cost_usd
        run.num_turns = message.num_turns
        run.subtype = message.subtype
        run.stop_reason = message.stop_reason
        run.model_usage = message.model_usage


async def run_agent(
    task_prompt: str,
    workspace: Workspace,
    *,
    model: str,
    api_key: str,
    caps: RunCaps,
    cwd: Path,
    output_chars: int = DEFAULT_OUTPUT_CHARS,
) -> AgentRun:
    """One attempt on one task. Never raises for a model or cap problem: the record says."""
    ctx = ToolContext(workspace, output_chars)
    run = AgentRun(model=model, prompt_sha=prompt_sha())
    cwd.mkdir(parents=True, exist_ok=True)
    options = build_options(ctx, run, model=model, api_key=api_key, caps=caps, cwd=cwd)
    started = time.monotonic()
    try:
        async for message in query(prompt=task_prompt, options=options):
            absorb(run, message)
    except ResultError:
        pass  # a cap stop: its ResultMessage has already been absorbed
    except Exception as error:
        run.error = f"{type(error).__name__}: {error}"[:ERROR_CHARS]
    finally:
        run.seconds = round(time.monotonic() - started, 1)
        run.report = ctx.report
        run.diffs = list(ctx.diffs)
    return run


def run_agent_sync(task_prompt: str, workspace: Workspace, **kwargs: Any) -> AgentRun:
    return asyncio.run(run_agent(task_prompt, workspace, **kwargs))
