"""What every tool shares: its result type, the per-run context and the output cap."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from verimend.tools.workspace import Workspace

DEFAULT_OUTPUT_CHARS = 4_000


class Verifier(Protocol):
    async def prepare(self) -> dict[str, Any]:
        """Snapshot the project before anything changes. Raises if it cannot."""
        ...

    async def check(self) -> dict[str, Any]:
        """SPEC section 5's result for the project as it is now. Raises if it cannot run."""
        ...


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False


@dataclass
class ToolContext:
    """One run's view of the workspace, plus what the tools remember between calls."""

    workspace: Workspace
    output_chars: int = DEFAULT_OUTPUT_CHARS
    failure_summary: str | None = None  # read_failure's first answer
    manifest: dict[str, Any] | None = None  # cleared after every edit
    report: dict[str, Any] | None = None  # the latest validated verdict
    diffs: list[dict[str, str]] = field(default_factory=list)
    accepted: bool = False  # a report ended the run; every later tool call is refused
    # The verifier loop (SPEC section 4, attempts). No verifier: a report is final.
    verifier: Verifier | None = None
    max_attempts: int = 1
    feedback: bool = True  # False for the "without the verifier's feedback" ablation
    attempts: list[dict[str, Any]] = field(default_factory=list)  # one per verifier run
    final_verdict: str | None = None  # the run's answer once a report is accepted
    verifier_error: str | None = None

    def result(self, text: str, is_error: bool = False) -> ToolResult:
        return ToolResult(cap(text, self.output_chars), is_error)


def cap(text: str, limit: int = DEFAULT_OUTPUT_CHARS) -> str:
    """Trim tool output to `limit` characters and say so (CLAUDE.md rule 7)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[trimmed: showed {limit} of {len(text)} characters]"
