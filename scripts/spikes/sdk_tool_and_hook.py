"""Spike 1: one custom tool and one hook that denies a tool call, on the Claude Agent SDK.

    uv run python scripts/spikes/sdk_tool_and_hook.py --max-usd 0.05

Throwaway evidence for docs/spikes.md. It is the stated exception to "only verimend/agent/
imports the Agent SDK", and nothing imports this file.

The agent is asked to make one allowed edit, one edit the hook must deny, and to try a
shell command it should have no tool for. Nothing is written to disk by the tool itself.
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from verimend.config import REPO_ROOT, load_settings
from verimend.pricing import Usage, request_cost_usd

SERVER = "verimend"
TOOL = f"mcp__{SERVER}__edit_model"
OUT_DIR = REPO_ROOT / "bench" / "results" / "spikes-2026-11-19"

SYSTEM_PROMPT = "You are a test agent inside a harness. Follow the steps exactly and be brief."
PROMPT = (
    "Do these steps in order, then stop.\n"
    "1. Call edit_model with path 'models/staging/stg_orders.sql' and content "
    "'select 1 as id'.\n"
    "2. Call edit_model with path 'tests/assert_positive_amount.sql' and content "
    "'select 1 where false'. If it is refused, do not retry.\n"
    "3. If you have any tool that can run a shell command or read a file, use it to run "
    "'echo hello'. If you have none, list the exact names of every tool you do have.\n"
    "Finish with two sentences saying what happened at each step."
)

blocked: list[dict[str, str]] = []


@tool(
    "edit_model",
    "Replace the content of a dbt model file. The path is relative to the dbt project.",
    {"path": str, "content": str},
)
async def edit_model(args: dict[str, Any]) -> dict[str, Any]:
    text = f"Wrote {len(args['content'])} characters to {args['path']} (spike: nothing saved)."
    return {"content": [{"type": "text", "text": text}]}


async def guard_edit_path(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
    """PreToolUse hook: edits are allowed under models/ only."""
    path = str(input_data["tool_input"].get("path", ""))
    if path.startswith("models/") and ".." not in path:
        return {}
    reason = f"edit_model may only touch files under models/; refused {path!r}"
    blocked.append({"tool": input_data["tool_name"], "reason": reason})
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def to_usage(raw: dict[str, Any]) -> Usage:
    """Map the SDK's per-message usage dict onto our Usage."""
    created = raw.get("cache_creation_input_tokens", 0) or 0
    split = raw.get("cache_creation") or {}
    one_hour = split.get("ephemeral_1h_input_tokens", 0) or 0
    five_min = split.get("ephemeral_5m_input_tokens", created - one_hour) or 0
    return Usage(
        input_tokens=raw.get("input_tokens", 0) or 0,
        output_tokens=raw.get("output_tokens", 0) or 0,
        cache_read_tokens=raw.get("cache_read_input_tokens", 0) or 0,
        cache_write_5m_tokens=five_min,
        cache_write_1h_tokens=one_hour,
    )


def build_options(model: str, api_key: str, max_usd: float, cwd: Path) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name=SERVER, version="0.0.1", tools=[edit_model])
    return ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[],  # remove every built-in tool from the model's view
        mcp_servers={SERVER: server},
        allowed_tools=[TOOL],
        permission_mode="dontAsk",  # anything not pre-approved is denied, never prompted
        setting_sources=[],  # do not load user, project or local Claude settings
        hooks={"PreToolUse": [HookMatcher(matcher=TOOL, hooks=[guard_edit_path])]},
        max_turns=6,
        max_budget_usd=max_usd,
        env={"ANTHROPIC_API_KEY": api_key},
        cwd=cwd,
    )


async def stream(mode: str, options: ClaudeAgentOptions):
    if mode == "query":
        async for message in query(prompt=PROMPT, options=options):
            yield message
    else:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(PROMPT)
            async for message in client.receive_response():
                yield message


async def run(mode: str, model: str, api_key: str, max_usd: float) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    usage_by_message: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="verimend-spike1-") as scratch:
        options = build_options(model, api_key, max_usd, Path(scratch))
        async for message in stream(mode, options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                keys = ("apiKeySource", "model", "permissionMode", "tools", "claude_code_version")
                init = {key: message.data.get(key) for key in keys}
                init["mcp_servers"] = message.data.get("mcp_servers")
                events.append({"event": "init", **init})
            elif isinstance(message, AssistantMessage):
                if message.message_id and message.usage:
                    usage_by_message[message.message_id] = message.usage
                for block in message.content:
                    if isinstance(block, TextBlock):
                        events.append({"event": "text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        events.append(
                            {"event": "tool_use", "name": block.name, "input": block.input}
                        )
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        events.append(
                            {
                                "event": "tool_result",
                                "is_error": block.is_error,
                                "content": block.content,
                            }
                        )
            elif isinstance(message, ResultMessage):
                result = {
                    "subtype": message.subtype,
                    "is_error": message.is_error,
                    "num_turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                    "total_cost_usd": message.total_cost_usd,
                    "usage": message.usage,
                    "model_usage": message.model_usage,
                    "permission_denials": message.permission_denials,
                }

    requests = [to_usage(raw) for raw in usage_by_message.values()]
    return {
        "mode": mode,
        "model": model,
        "events": events,
        "hook_blocks": blocked,
        "per_message_usage": list(usage_by_message.values()),
        "our_cost_usd": sum(request_cost_usd(model, usage) for usage in requests),
        "result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Spike 1: custom tool and deny hook.")
    parser.add_argument("--max-usd", type=float, required=True, help="dollar cap for this call")
    parser.add_argument("--mode", choices=["query", "client"], default="query")
    parser.add_argument("--label", default="1", help="suffix for the transcript file name")
    args = parser.parse_args()
    if not 0 < args.max_usd <= 0.10:
        parser.error("--max-usd must be above 0 and at most 0.10 for this spike")

    settings = load_settings()
    api_key = settings.require_api_key()
    model = settings.model_default
    print(f"Spike 1: one call to {model}, mode={args.mode}, cap ${args.max_usd:.2f}", flush=True)

    record = asyncio.run(run(args.mode, model, api_key, args.max_usd))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"sdk_tool_and_hook.{args.mode}.{args.label}.json"
    text = json.dumps(record, indent=2, default=str).replace(str(Path.home()), "~")
    out.write_text(text + "\n", encoding="utf-8")

    for event in record["events"]:
        print(json.dumps(event, default=str)[:600])
    print("hook blocks:", json.dumps(record["hook_blocks"]))
    print("result:", json.dumps(record["result"], default=str))
    print(f"our cost from pricing.py: ${record['our_cost_usd']:.6f}")
    print(f"transcript: {out.relative_to(REPO_ROOT).as_posix()}")


if __name__ == "__main__":
    main()
