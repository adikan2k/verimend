"""Parsing the baseline's Claude Code stream-json log."""

import json

from bench.claude_stream import ARGUMENT_CHARS, parse_stream


def line(**data: object) -> str:
    return json.dumps(data)


LOG = "\n".join(
    [
        "AGENT RESPONSE: ",
        line(type="system", subtype="init", model="claude-haiku-5-5", tools=["Bash", "Read"]),
        line(
            type="assistant",
            message={
                "id": "msg_1",
                "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 1},
                "content": [
                    {"type": "text", "text": "Looking."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {"command": "x" * 1000},
                    },
                ],
            },
        ),
        line(
            type="user",
            message={
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "abc"}],
                        "is_error": True,
                    }
                ]
            },
        ),
        "some tmux noise {not json}",
        line(
            type="assistant",
            message={
                "id": "msg_2",
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 50,
                },
                "content": [{"type": "text", "text": "Done.\nVERDICT: fixed"}],
            },
        ),
        line(
            type="result",
            subtype="success",
            is_error=False,
            num_turns=3,
            total_cost_usd=0.0123,
            result="Done.\nVERDICT: fixed",
        ),
    ]
)


def test_parse_stream_collects_tools_prompts_and_totals() -> None:
    s = parse_stream(LOG)
    assert s.model == "claude-haiku-5-5"
    assert s.tools_offered == ["Bash", "Read"]
    assert [c.name for c in s.tool_calls] == ["Bash"]
    call = s.tool_calls[0]
    assert call.output_chars == 3 and call.is_error is True
    assert len(call.arguments["command"]) < ARGUMENT_CHARS + 50
    assert s.prompt_tokens == {"msg_1": 105, "msg_2": 352}
    assert s.largest_prompt_tokens == 352
    assert s.cost_usd == 0.0123 and s.num_turns == 3
    assert s.stop_subtype == "success"
    assert s.final_text.endswith("VERDICT: fixed")


def test_parse_stream_without_a_result_line_has_no_cost() -> None:
    s = parse_stream(LOG.rsplit("\n", 1)[0])
    assert s.cost_usd is None
    assert s.stop_subtype == "no_result"
    assert s.final_text.endswith("VERDICT: fixed")  # last assistant text still counts


def test_parse_stream_of_nothing() -> None:
    s = parse_stream("")
    assert s.tool_calls == [] and s.result is None and s.largest_prompt_tokens is None
