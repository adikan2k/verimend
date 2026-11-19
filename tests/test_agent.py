"""The agent loop without a model: options, tool wrappers, guard hooks and how SDK
messages fold into an AgentRun. Message shapes follow spike 1's recorded transcripts."""

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from verimend.agent.prompt import SYSTEM_PROMPT, prompt_sha
from verimend.agent.run import absorb, build_hooks, build_options, mcp_name, wrap_tool
from verimend.hooks.caps import RunCaps
from verimend.runrecord import AgentRun
from verimend.tools.base import ToolContext
from verimend.tools.registry import TOOL_NAMES, get_tool
from verimend.tools.workspace import LocalWorkspace

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_shop"
CAPS = RunCaps(max_usd=0.25, max_steps=40, max_attempts=1)


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    project = tmp_path / "tiny_shop"
    shutil.copytree(FIXTURE, project)
    return ToolContext(LocalWorkspace(project, project / "shop.duckdb"))


def test_prompt_is_stable_and_small() -> None:
    assert prompt_sha() == prompt_sha() and len(prompt_sha()) == 64
    assert len(SYSTEM_PROMPT) < 6_000
    for tool_name in ("read_failure", "edit_model", "run_dbt", "report", "query_data"):
        assert tool_name in SYSTEM_PROMPT


def test_options_offer_only_our_tools_with_caps(ctx: ToolContext, tmp_path: Path) -> None:
    run = AgentRun(model="m", prompt_sha="x")
    options = build_options(ctx, run, model="claude-haiku-5-5", api_key="k", caps=CAPS,
                            cwd=tmp_path)  # fmt: skip
    assert options.tools == []
    assert options.setting_sources == []
    assert options.permission_mode == "dontAsk"
    assert options.allowed_tools == [mcp_name(n) for n in TOOL_NAMES]
    assert len(options.allowed_tools) == 8
    assert options.max_turns == 40
    assert options.max_budget_usd == pytest.approx(0.2)  # 80% of the run cap
    assert options.env == {"ANTHROPIC_API_KEY": "k"}
    assert options.system_prompt == SYSTEM_PROMPT


def invoke(sdk_tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return await sdk_tool.handler(args)

    return asyncio.run(go())


def test_wrapped_tool_runs_and_records_a_tool_call(ctx: ToolContext) -> None:
    run = AgentRun(model="m", prompt_sha="x")
    sdk_tool = wrap_tool(get_tool("read_file"), ctx, run)
    reply = invoke(sdk_tool, {"path": "models/staging/stg_orders.sql"})
    assert "order_id" in reply["content"][0]["text"] and "is_error" not in reply
    (event,) = run.events
    assert event["event"] == "tool_call" and event["name"] == "read_file"
    assert event["output_chars"] == len(reply["content"][0]["text"])
    assert invoke(sdk_tool, {"path": "nope.sql"})["is_error"] is True


def test_wrapped_tool_survives_a_tool_bug(ctx: ToolContext) -> None:
    run = AgentRun(model="m", prompt_sha="x")
    sdk_tool = wrap_tool(get_tool("read_file"), ctx, run)
    path = "models/staging/stg_orders.sql"
    reply = invoke(sdk_tool, {"path": path, "start_line": "not a number"})
    assert reply["is_error"] is True
    assert reply["content"][0]["text"].startswith("tool failed: ValueError")
    assert run.events[-1]["event"] == "tool_call" and run.events[-1]["is_error"] is True


def deny_reason(reply: dict[str, Any]) -> str | None:
    output = reply.get("hookSpecificOutput")
    if not output:
        return None
    assert output["permissionDecision"] == "deny"
    return output["permissionDecisionReason"]


def test_guard_hook_blocks_and_records(ctx: ToolContext) -> None:
    run = AgentRun(model="m", prompt_sha="x")
    (matcher,) = build_hooks(ctx, run)["PreToolUse"]
    assert matcher.matcher is not None
    assert set(matcher.matcher.split("|")) == {mcp_name(n) for n in TOOL_NAMES}
    guard = matcher.hooks[0]

    def ask(tool_name: str, **tool_input: Any) -> dict[str, Any]:
        data = {"tool_name": mcp_name(tool_name), "tool_input": tool_input}
        return asyncio.run(guard(data, "id", None))  # type: ignore[arg-type]

    assert ask("edit_model", path="models/staging/stg_orders.sql", content="x") == {}
    assert deny_reason(ask("edit_model", path="tests/assert_positive_amount.sql", content="x"))
    assert deny_reason(ask("query_data", sql="drop table stg_orders"))
    assert [e["tool"] for e in run.events if e["event"] == "hook_block"] == [
        "edit_model",
        "query_data",
    ]
    ctx.report = {"verdict": "fixed"}
    assert deny_reason(ask("read_file", path="models"))  # nothing after report


def test_absorb_reads_init_usage_text_and_result() -> None:
    run = AgentRun(model="claude-haiku-5-5", prompt_sha="x")
    absorb(run, SystemMessage(subtype="init", data={"tools": [mcp_name("read_file")]}))
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 400,
             "cache_read_input_tokens": 1151, "output_tokens": 24,
             "cache_creation": {"ephemeral_5m_input_tokens": 400,
                                "ephemeral_1h_input_tokens": 0}}  # fmt: skip
    absorb(run, AssistantMessage(content=[TextBlock(text="Done.")], model="m", usage=usage,
                                 message_id="msg_1"))  # fmt: skip
    absorb(run, ResultMessage(subtype="error_max_turns", duration_ms=10, duration_api_ms=9,
                              is_error=True, num_turns=41, session_id="s",
                              total_cost_usd=0.0123))  # fmt: skip
    assert run.tools_offered == [mcp_name("read_file")]
    assert run.prompt_tokens == {"msg_1": 1553}
    assert run.final_text == "Done."
    assert run.cost_usd == 0.0123 and run.num_turns == 41
    assert run.stopped_by_cap
    assert AgentRun.from_dict(run.to_dict()) == run
