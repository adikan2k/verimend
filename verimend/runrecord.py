"""What one agent run produced, as plain data. No SDK import, so the harness can read it.

`verimend/agent/run.py` fills it; `bench/assemble.py` turns it into a trace and a scored
run; the Harbor adapter carries it across a process boundary as JSON.
"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AgentRun:
    model: str
    prompt_sha: str
    tools_offered: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)  # tool_call, hook_block, ...
    report: dict[str, Any] | None = None  # the validated verdict, if report was called
    final_text: str = ""
    cost_usd: float | None = None  # from the SDK's result; None if no result arrived
    num_turns: int | None = None
    subtype: str | None = None  # "success", "error_max_turns", "error_max_budget_usd", ...
    stop_reason: str | None = None
    model_usage: dict[str, Any] | None = None
    prompt_tokens: dict[str, int] = field(default_factory=dict)  # per request (message id)
    seconds: float | None = None
    error: str | None = None  # an exception the SDK raised, other than a cap stop
    diffs: list[dict[str, str]] = field(default_factory=list)

    @property
    def largest_prompt_tokens(self) -> int:
        return max(self.prompt_tokens.values(), default=0)

    @property
    def tool_calls(self) -> int:
        return sum(1 for e in self.events if e.get("event") == "tool_call")

    @property
    def hook_blocks(self) -> int:
        return sum(1 for e in self.events if e.get("event") == "hook_block")

    @property
    def stopped_by_cap(self) -> bool:
        return (self.subtype or "").startswith("error_max")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRun":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})
