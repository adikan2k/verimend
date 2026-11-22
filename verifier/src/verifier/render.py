"""The verifier's result as Markdown, for a pull request comment or a job summary.

`python -m verifier render --result result.json` prints it. The first line is a hidden
marker, so the inspector Action can find and update its own comment on every push.
Status is always said in words, never by colour or an icon alone.
"""

from __future__ import annotations

from typing import Any

MARKER = "<!-- verimend-inspector -->"
DETAIL_CHARS = 700
LABELS = {
    "empty_diff": "Changes",
    "diff": "The diff",
    "tamper": "Tests and files",
    "masking": "Hidden failures",
    "clean_rebuild": "Clean rebuild",
    "collateral": "Collateral damage",
    "row_accounting": "Row accounting",
}
NOT_SEEN = (
    "It cannot tell a right business rule from a wrong one: a join that builds cleanly "
    "and changes no test, but counts the wrong rows, passes. A person still reviews."
)
CHECKS_TEXT = (
    "It rebuilds the project from the pull request's base on a fresh copy of the database, "
    "applies the change, and checks that no test was removed or weakened, that no failure "
    "was hidden, that `dbt build` passes, that models outside the change are unchanged, "
    "and that no LEFT join groups unmatched rows under a NULL key. Rows a LEFT join drops "
    "are reported, not failed."
)


def _cell(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > DETAIL_CHARS:
        text = text[: DETAIL_CHARS - 1] + "…"
    return text.replace("|", "\\|")


def render(result: dict[str, Any], *, title: str = "VeriMend inspector") -> str:
    lines = [MARKER]
    if "error" in result or "checks" not in result:
        lines += [
            f"### {title}: could not run",
            "",
            f"> {_cell(result.get('error') or 'no result')}",
            "",
            "This is a problem with the run, not a verdict on the change.",
        ]
        return "\n".join(lines) + "\n"
    failed = result.get("failed_check")
    if failed is None:
        lines.append(f"### {title}: passed")
    else:
        lines.append(f"### {title}: failed at {LABELS.get(str(failed), str(failed)).lower()}")
    lines += ["", "| Check | Result | Detail |", "| --- | --- | --- |"]
    for check in result["checks"]:
        verdict = "pass" if check["passed"] else "**FAIL**"
        name = LABELS.get(check["name"], check["name"])
        lines.append(f"| {name} | {verdict} | {_cell(check['detail'])} |")
    if result.get("duration_s") is not None:
        lines += ["", f"Checked in {result['duration_s']:.0f} s. No model was called."]
    lines += [
        "",
        "<details><summary>What this checks, and what it cannot see</summary>",
        "",
        CHECKS_TEXT,
        "",
        NOT_SEEN,
        "",
        "</details>",
    ]
    return "\n".join(lines) + "\n"
