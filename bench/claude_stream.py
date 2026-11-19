"""Read the plain-agent baseline's output: Claude Code's `--output-format stream-json` log.

One JSON object per line: a `system`/`init` line, then `assistant` and `user` messages,
then a single `result` line with the run's totals. Lines that are not JSON (the shell
echo, tmux noise) are skipped.

Phase 1 found that per-message output token counts are not final, so cost and totals come
only from the `result` line. Per-message usage is used for prompt sizes alone, which are
complete when a message starts.
"""

import json
from dataclasses import dataclass, field
from typing import Any

ARGUMENT_CHARS = 300


def _cap(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= ARGUMENT_CHARS:
        return value
    return text[:ARGUMENT_CHARS] + f"... [trimmed: {len(text)} characters]"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    output_chars: int | None = None
    is_error: bool | None = None


@dataclass
class StreamSummary:
    model: str | None = None
    tools_offered: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: dict[str, int] = field(default_factory=dict)  # message id -> prompt size
    final_text: str = ""
    result: dict[str, Any] | None = None  # the `result` line, if the run got that far

    @property
    def cost_usd(self) -> float | None:
        if self.result is None:
            return None
        cost = self.result.get("total_cost_usd")
        return float(cost) if cost is not None else None

    @property
    def num_turns(self) -> int | None:
        return None if self.result is None else self.result.get("num_turns")

    @property
    def stop_subtype(self) -> str:
        """`success`, an `error_*` subtype such as `error_max_turns`, or `no_result`."""
        return "no_result" if self.result is None else str(self.result.get("subtype"))

    @property
    def largest_prompt_tokens(self) -> int | None:
        return max(self.prompt_tokens.values(), default=None)


def _prompt_size(usage: dict[str, Any]) -> int:
    return sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def parse_stream(log: str) -> StreamSummary:
    summary = StreamSummary()
    calls: dict[str, ToolCall] = {}
    for raw in log.splitlines():
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            summary.model = data.get("model")
            summary.tools_offered = [str(t) for t in data.get("tools") or []]
        elif kind == "assistant":
            message = data.get("message") or {}
            if message.get("id") and message.get("usage"):
                summary.prompt_tokens[message["id"]] = _prompt_size(message["usage"])
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    call = ToolCall(
                        id=str(block.get("id")),
                        name=str(block.get("name")),
                        arguments={k: _cap(v) for k, v in (block.get("input") or {}).items()},
                    )
                    calls[call.id] = call
                    summary.tool_calls.append(call)
                elif block.get("type") == "text" and block.get("text"):
                    summary.final_text = str(block["text"])
        elif kind == "user":
            for block in (data.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = calls.get(str(block.get("tool_use_id")))
                    if call is not None:
                        call.output_chars = len(_text_of(block.get("content")))
                        call.is_error = bool(block.get("is_error"))
        elif kind == "result":
            summary.result = data
            if data.get("result"):
                summary.final_text = str(data["result"])
    return summary
