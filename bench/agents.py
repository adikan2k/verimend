"""The agents the harness can run. Plain data; the adapters live in bench/ade/.

| name        | what it is                                         | model calls |
| ----------- | -------------------------------------------------- | ----------- |
| `none`      | does nothing: the floor, must score 0%              | no          |
| `reference` | applies the task's own reference solution: 100%    | no          |
| `baseline`  | ADE-bench's Claude Code agent with its default     | yes         |
|             | tools, capped, plus one verdict line in the prompt |             |
| `verimend`  | the agent this project builds (verimend/agent/)    | yes         |
"""

from dataclasses import dataclass
from typing import Literal

from bench.claims import Claim

ClaimRule = Literal["none", "fixed", "parsed", "report"]

# Allowed tools of ADE-bench's own baseline plugin set ("none": no skills, no MCP).
BASELINE_TOOLS = ("Bash", "Edit", "Write", "Read", "Glob", "Grep")
# The tasks' reference solutions are public, so the baseline gets no web access.
BASELINE_DISALLOWED_TOOLS = ("WebSearch", "WebFetch")
# Claude Code CLI version installed in the task container: the one bundled with the
# Agent SDK version pinned in pyproject.toml (docs/spikes.md, "Versions").
CLAUDE_CODE_VERSION = "2.1.328"
# The SDK's dollar cap can overshoot by one request (docs/spikes.md), so the CLI gets
# this fraction of the run's real cap.
CLI_BUDGET_FRACTION = 0.8


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    model_calls: bool
    estimated_usd_per_run: float
    claim_rule: ClaimRule
    available: bool = True
    unavailable_reason: str = ""

    def fixed_claim(self) -> Claim | None:
        """The claim of an agent that cannot speak for itself, or None if it is parsed."""
        return {"none": Claim.NONE, "fixed": Claim.FIXED}.get(self.claim_rule)


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec("none", "Does nothing.", False, 0.0, "none"),
        AgentSpec("reference", "Applies the task's reference solution.", False, 0.0, "fixed"),
        AgentSpec(
            "baseline",
            "ADE-bench's Claude Code agent: Bash, Edit, Write, Read, Glob, Grep.",
            True,
            # Not measured yet; Claude Code's own system prompt and tool definitions
            # make every request larger than the pilot's. Replaced in phase 2.
            0.05,
            "parsed",
        ),
        AgentSpec(
            "verimend",
            "VeriMend: eight data-aware tools, guards, structured report; one attempt.",
            True,
            # The phase 1 pilot measured $0.0012 to $0.0105 a run (docs/spikes.md).
            0.03,
            "report",
        ),
    )
}


class UnknownAgentError(KeyError):
    pass


def get_agent(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise UnknownAgentError(f"unknown agent {name!r}; known: {', '.join(AGENTS)}") from None


def cli_budget_usd(run_cap_usd: float) -> float:
    return round(run_cap_usd * CLI_BUDGET_FRACTION, 4)


def baseline_cli_flags(run_cap_usd: float, max_turns: int) -> str:
    """Flags appended to ADE-bench's `claude -p ...` command for the baseline.

    `--allowedTools` (set by ADE-bench) only pre-approves tools; without `--tools` the
    model is still offered every Claude Code tool, subagents and workflows included
    (seen in the phase 2 smoke run). `--tools` makes the offered set the six file and
    shell tools SPEC section 6 describes.
    """
    return (
        f" --tools {','.join(BASELINE_TOOLS)}"
        f" --max-turns {max_turns}"
        f" --max-budget-usd {cli_budget_usd(run_cap_usd):.4f}"
        f" --disallowedTools {' '.join(BASELINE_DISALLOWED_TOOLS)}"
    )
