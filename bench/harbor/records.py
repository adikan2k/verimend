"""What the Harbor-side agents need that does not import Harbor, so it is unit-tested on
the host: prompts, the baseline's command, and the per-trial record file."""

import json
import shlex
from pathlib import Path
from typing import Any

from bench.agents import BASELINE_TOOLS, CLAUDE_CODE_VERSION, baseline_cli_flags
from bench.claims import BASELINE_PROMPT_SUFFIX

RECORD_NAME = "verimend_record.json"
PROJECT_DIR = "/app/dbt_models_duckdb"  # the DuckDB copy; the verifier checks only this one
DEFAULT_DUCKDB_PATH = "/app/database/retail.duckdb"
WORKDIR = "/app"
AGENT_LOG = "/tmp/agent_output.log"

# data-eng-bench prompts describe both backends and ask the agent to check $DB_TYPE in a
# shell. VeriMend has no shell, so it is told what that check would show. The baseline has
# a shell and gets the prompt unchanged, plus the same verdict line as on ADE-bench.
VERIMEND_ENV_NOTE = (
    "Environment for this task: DB_TYPE is duckdb. Your dbt project is "
    f"{PROJECT_DIR}; the paths you pass to tools are relative to it, and it is the only "
    "copy to change. You have no shell: where the task says to run a command, use your "
    "tools instead."
)

INSTALL_CLAUDE_CODE = (
    f"npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION} && claude --version"
)


def verimend_prompt(instruction: str) -> str:
    return f"{VERIMEND_ENV_NOTE}\n\n{instruction}"


def baseline_command(instruction: str, model: str, run_cap_usd: float, max_turns: int) -> str:
    """ADE-bench's Claude Code command (claude_code_agent.py), with the same additions as
    the baseline on ADE-bench: the verdict line and bench.agents.baseline_cli_flags."""
    prompt = f"{instruction}\n\n{BASELINE_PROMPT_SUFFIX}"
    return (
        f"claude --output-format stream-json --verbose -p {shlex.quote(prompt)}"
        f" --model {model} --allowedTools {' '.join(BASELINE_TOOLS)}"
        + baseline_cli_flags(run_cap_usd, max_turns)
        + f" 2>&1 | tee {AGENT_LOG}"
    )


def write_record(logs_dir: Path, **record: Any) -> Path:
    """One trial's agent-side record, next to Harbor's own logs for that trial."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / RECORD_NAME
    path.write_text(json.dumps(record, default=str) + "\n", encoding="utf-8")
    return path


def read_record(trial_dir: Path) -> dict[str, Any] | None:
    found = sorted(trial_dir.rglob(RECORD_NAME))
    return json.loads(found[0].read_text(encoding="utf-8")) if found else None
