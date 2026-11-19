"""One JSON-lines trace per run, in the event vocabulary of SPEC section 8.

`run_start` is always the first line and `run_end` the last, whatever order the events
were recorded in, so a reader can stream a trace and know where it ends.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVENTS = frozenset(
    {
        "run_start",
        "tool_call",
        "hook_block",
        "model_usage",
        "attempt_end",
        "report",
        "grade",
        "run_end",
    }
)


class TraceError(ValueError):
    """A trace is missing an event it must have, or has one SPEC section 8 does not define."""


def redact_home(text: str, home: Path | None = None) -> str:
    """Replace the user's home folder with `~`: both slash styles, raw and JSON-escaped."""
    home = home or Path.home()
    plain = {str(home), home.as_posix()}
    escaped = {json.dumps(form)[1:-1] for form in plain}
    for form in sorted(plain | escaped, key=len, reverse=True):
        text = text.replace(form, "~")
    return text


@dataclass
class Trace:
    task: str
    trial: int
    events: list[dict[str, Any]] = field(default_factory=list)

    def add(self, event: str, *, t: float | None = None, **data: Any) -> dict[str, Any]:
        if event not in EVENTS:
            raise TraceError(f"unknown trace event {event!r}")
        record = {"event": event, "t": round(time.time() if t is None else t, 3), **data}
        self.events.append(record)
        return record

    def count(self, event: str) -> int:
        return sum(1 for e in self.events if e["event"] == event)

    def ordered(self) -> list[dict[str, Any]]:
        """Events with run_start first and run_end last; everything else keeps its order."""
        for edge in ("run_start", "run_end"):
            if self.count(edge) != 1:
                raise TraceError(f"a trace needs exactly one {edge}, found {self.count(edge)}")
        start = [e for e in self.events if e["event"] == "run_start"]
        end = [e for e in self.events if e["event"] == "run_end"]
        middle = [e for e in self.events if e["event"] not in {"run_start", "run_end"}]
        return start + middle + end

    def write(self, results_dir: Path, home: Path | None = None) -> Path:
        """Write to <results_dir>/<task>/<trial>.jsonl and return the path."""
        path = results_dir / self.task / f"{self.trial}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [redact_home(json.dumps(e, default=str), home) for e in self.ordered()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
