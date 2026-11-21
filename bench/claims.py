"""What an agent claimed at the end of a run.

VeriMend states its verdict through the `report` tool. The plain-agent baseline has no
such tool, so the harness appends one instruction to its prompt and parses the last
`VERDICT:` line of its final reply. A missing or unreadable line is recorded as `none`,
never guessed.
"""

import re
from enum import StrEnum


class Claim(StrEnum):
    FIXED = "fixed"
    NOT_FIXED = "not_fixed"
    ESCALATE = "escalate"
    NONE = "none"  # the agent made no readable claim


BASELINE_PROMPT_SUFFIX = (
    "When you are done, end your reply with exactly one line, one of: "
    "VERDICT: fixed, VERDICT: not_fixed or VERDICT: escalate."
)

_VERDICT_LINE = re.compile(
    r"^[\s*_`>#-]*VERDICT[\s*_`]*:[\s*_`]*(fixed|not[ _-]fixed|escalate)\b",
    re.IGNORECASE | re.MULTILINE,
)


_CATEGORY_LINE = re.compile(
    r"^[\s*_`>#-]*CATEGORY[\s*_`]*:[\s*_`]*"
    r"(schema[ _-]drift|logic|bad[ _-]data|config|missing[ _-]source|unknown)\b",
    re.IGNORECASE | re.MULTILINE,
)


def parse_category(text: str | None) -> str | None:
    """The root-cause category on the last CATEGORY line of the text, or None. Asked of
    the baseline on the fault suite only (bench/faults/container.py)."""
    matches = _CATEGORY_LINE.findall(text or "")
    if not matches:
        return None
    return re.sub(r"[ -]", "_", matches[-1].lower())


def claim_from_report(report: dict[str, object] | None) -> Claim:
    """VeriMend's claim: the verdict of its validated report, or none without one."""
    verdict = (report or {}).get("verdict")
    try:
        return Claim(str(verdict)) if verdict is not None else Claim.NONE
    except ValueError:
        return Claim.NONE


def parse_verdict(text: str | None) -> Claim:
    """The claim on the last VERDICT line of the text, or Claim.NONE."""
    matches = _VERDICT_LINE.findall(text or "")
    if not matches:
        return Claim.NONE
    word = re.sub(r"[ -]", "_", matches[-1].lower())
    return Claim(word)
