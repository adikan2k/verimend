"""What every tool shares: its result type, the per-run context and the output cap."""

from dataclasses import dataclass, field
from typing import Any

from verimend.tools.workspace import Workspace

DEFAULT_OUTPUT_CHARS = 4_000


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
    report: dict[str, Any] | None = None  # the validated verdict, once report succeeds
    diffs: list[dict[str, str]] = field(default_factory=list)

    def result(self, text: str, is_error: bool = False) -> ToolResult:
        return ToolResult(cap(text, self.output_chars), is_error)


def cap(text: str, limit: int = DEFAULT_OUTPUT_CHARS) -> str:
    """Trim tool output to `limit` characters and say so (CLAUDE.md rule 7)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[trimmed: showed {limit} of {len(text)} characters]"
