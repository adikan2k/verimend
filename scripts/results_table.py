"""Build a Markdown results table from batch summaries under bench/results/.

    uv run python scripts/results_table.py --out docs/results-dev.md \
        --batch dev-none-ade-bench-2026-11-19 --batch dev-none-data-eng-bench-2026-11-19 ...

Every number in the table is computed from the runs listed in those summary.json files and
links to them (CLAUDE.md rule 8). Batches of the same agent are also combined into one
row over both benchmarks, recomputed from the runs, never averaged from averages. Real-world
batches are labelled by their kind of case and never combined (decision 0011).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bench.claims import Claim
from bench.outcomes import Outcome, RunResult, summarize

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "bench" / "results"
AGENT_ORDER = ["none", "reference", "baseline", "verimend"]
SOURCE_NAMES = {"ade-bench": "ADE-bench", "data-eng-bench": "data-eng-bench",
                "faults": "Fault suite", "realworld": "Real-world"}  # fmt: skip


def load_batch(name: str, results: Path = RESULTS) -> dict[str, Any]:
    path = results / name / "summary.json"
    if not path.is_file():
        raise FileNotFoundError(f"no summary.json for batch {name!r} under {results}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_from_dict(data: dict[str, Any]) -> RunResult:
    return RunResult(**{**data, "claim": Claim(data["claim"]), "outcome": Outcome(data["outcome"])})


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def number(value: float | None, digits: int = 0) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def row(agent: str, source: str, metrics: dict[str, Any], links: list[str]) -> str:
    spread = metrics["pass_rate_across_trials"]
    across = (
        f"{pct(spread['min'])}–{pct(spread['max'])}"
        if metrics["trials"] and metrics["trials"] > 1
        else "1 trial"
    )
    dollars = metrics["dollars"]
    cells = [
        f"`{agent}`",
        source,
        f"{metrics['runs']} ({metrics['tasks']} × {metrics['trials']})",
        pct(metrics["grader_pass_rate"]),
        across,
        pct(metrics["false_success_rate"]),
        str(metrics["claimed_fixed"]),
        escalation_cell(metrics),
        verifier_cell(metrics),
        f"{money(dollars['mean_per_run'])} / {money(dollars['max_per_run'])}",
        number(metrics["mean_seconds"]),
        number(metrics["mean_steps"], 1),
        str(metrics["hook_blocks"]),
        ", ".join(links),
    ]
    return "| " + " | ".join(cells) + " |"


def escalation_cell(metrics: dict[str, Any]) -> str:
    """Escalations: correct and wrong (on tasks that expect one), and unneeded."""
    o = metrics["outcomes"]
    return (
        f"{o.get('correct_escalation', 0)} / {o.get('wrong_escalation', 0)} / "
        f"{o.get('unneeded_escalation', 0)}"
    )


def verifier_cell(metrics: dict[str, Any]) -> str:
    """Caught by the verifier, and how the verifier decided on the runs it checked."""
    checked = metrics.get("verifier")
    if not checked:
        return "n/a"
    caught = metrics["outcomes"].get("caught_by_verifier", 0)
    attempts = number(checked["mean_attempts"], 1)
    return (
        f"{caught} caught; passed {checked['last_attempt_passed']} of "
        f"{checked['runs_checked']}; {attempts} attempts"
    )


HEADER = [
    "| Agent | Benchmark | Runs (tasks × trials) | Grader pass rate | Across trials "
    "| False-success rate | Claimed fixed | Escalations (correct / wrong / unneeded) "
    "| Verifier | $ per run (mean / max) | Seconds per run | Steps | Hook blocks | Data |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
]


def label(summary: dict[str, Any]) -> str:
    """A batch's row label: its benchmark, and for real-world cases its kind."""
    raw = str(summary.get("source") or "?")
    name = SOURCE_NAMES.get(raw, raw)
    return f"{name}, {summary['group']}" if summary.get("group") else name


def build_table(batches: dict[str, dict[str, Any]], link_prefix: str) -> str:
    by_agent: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, summary in batches.items():
        by_agent.setdefault(summary["agent"], []).append((name, summary))
    order = [a for a in AGENT_ORDER if a in by_agent] + sorted(set(by_agent) - set(AGENT_ORDER))
    lines = list(HEADER)
    for agent in order:
        entries = sorted(by_agent[agent], key=lambda e: e[1].get("source", ""))
        links = []
        for name, summary in entries:
            link = f"[{name}]({link_prefix}{name}/summary.json)"
            links.append(link)
            lines.append(row(agent, label(summary), summary["metrics"], [link]))
        if len(entries) > 1 and not any(s.get("source") == "realworld" for _, s in entries):
            runs = [run_from_dict(r) for _, s in entries for r in s["runs"]]
            lines.append(row(agent, "**both**", summarize(runs), links))
    return "\n".join(lines)


WATCHED_AGENTS = ("baseline",)  # agents the verifier only watched: its gate is measurable
GATE_HEADER = [
    "| Agent | Benchmark | Claimed fixed, checked | Right fix, let through "
    "| Right fix, blocked | Wrong fix, let through | Wrong fix, stopped | Stopped by | Data |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
]


def gate_row(agent: str, source: str, gate: dict[str, Any], link: str) -> str:
    by = ", ".join(f"{check} {n}" for check, n in sorted(gate["stopped_by_check"].items()))
    cells = [
        f"`{agent}`",
        source,
        str(gate["claimed_fixed_and_checked"]),
        str(gate["right_let_through"]),
        str(gate["right_blocked"]),
        str(gate["wrong_let_through"]),
        f"**{gate['wrong_stopped']}**",
        by or "n/a",
        link,
    ]
    return "| " + " | ".join(cells) + " |"


def build_gate_table(batches: dict[str, dict[str, Any]], link_prefix: str) -> str | None:
    """The verifier as a gate on an agent it only watched, one row per batch."""
    rows = []
    for name, summary in sorted(batches.items(), key=lambda e: str(e[1].get("source"))):
        gate = summary["metrics"].get("verifier_vs_grader")
        if summary["agent"] not in WATCHED_AGENTS or not gate:
            continue
        link = f"[{name}]({link_prefix}{name}/summary.json)"
        rows.append(gate_row(summary["agent"], label(summary), gate, link))
    return "\n".join([*GATE_HEADER, *rows]) if rows else None


def banner(batches: dict[str, dict[str, Any]]) -> str:
    """What the numbers are: the development set, our own fault suite, or real breakages."""
    if batches and all(s.get("source") == "realworld" for s in batches.values()):
        return (
            "**Real breakages, not tuned on (decision 0011).** Each case is a Fivetran dbt "
            "package at the parent of a merged fix, graded against the human's fix. Bug "
            "fixes and DuckDB ports are separate rows, never combined; the bug fixes are 4 "
            "cases, under the bar of 8. The fixes merged between 2026-08-18 and 2026-11-04, "
            "which lowers but does not rule out that a model has seen them."
        )
    if batches and all(s.get("source") == "faults" for s in batches.values()):
        return (
            "**Our own fault suite (SPEC section 7).** VeriMend was not tuned on these faults, "
            "and they are not split: all twenty are in every run."
        )
    return (
        "**This is the development set, not a result.** VeriMend's prompt and tools were "
        "tuned on these tasks. Only held-out numbers are published (SPEC section 6)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Markdown results table from batch summaries.")
    parser.add_argument("--batch", action="append", required=True, help="bench/results/<name>")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--title", default="Development set")
    parser.add_argument(
        "--note", action="append", default=[], help="a line under the table, e.g. a caveat"
    )
    args = parser.parse_args(argv)
    try:
        batches = {name: load_batch(name) for name in args.batch}
    except FileNotFoundError as error:
        print(f"results_table: {error}", file=sys.stderr)
        return 2
    out = args.out.resolve()
    prefix = Path("../" * (len(out.parent.relative_to(REPO).parts))).as_posix() + "/"
    prefix += "bench/results/"
    models = sorted({str(s.get("model")) for s in batches.values() if s.get("model")})
    shas = sorted({str(s.get("git_sha", "?"))[:7] for s in batches.values()})
    body = [
        f"# {args.title}",
        "",
        "Generated by `scripts/results_table.py` from the batch summaries linked in each row. "
        "Do not edit by hand.",
        "",
        banner(batches),
        "",
        f"Model: {', '.join(models) or 'n/a'}. Code at {', '.join(shas)}.",
        "",
        build_table(batches, prefix),
        "",
        "- **Grader pass rate:** share of runs the benchmark's hidden checks passed.",
        "- **False-success rate:** share of runs whose final answer was `fixed` and the "
        "grader failed. For VeriMend the final answer is `fixed` only once the verifier passed "
        "it; the baseline has no verifier, so its own claim counts.",
        "- **Verifier:** runs caught by the verifier (final answer `not_fixed` after a "
        "rejection; a run that escalates after a rejection counts as an escalation, SPEC "
        "section 6), how many runs' last verifier check passed, and the mean number of "
        "verifier runs. "
        "For `none` and `reference` it is a measure of the verifier itself, with no agent.",
        "- **Escalations:** correct and wrong are escalations on tasks that expect one (the "
        "fault suite's bad-data and unfixable faults); wrong means the grader failed it, for "
        "the wrong root cause or hidden bad data. Unneeded is an escalation on a task that "
        "expects a fix.",
        "- **Across trials:** lowest and highest pass rate of a single trial.",
        *[f"- {note}" for note in args.note],
        "",
    ]
    gate = build_gate_table(batches, prefix)
    if gate:
        body += [
            "## The verifier as a gate on the plain agent",
            "",
            "The verifier watched the baseline: it took its snapshot before the agent and "
            "checked after, and told the agent nothing. Counted over runs where the baseline "
            "claimed `fixed` and the verifier could check. A wrong fix is one the grader "
            "failed.",
            "",
            gate,
            "",
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(body), encoding="utf-8")
    print(f"Wrote {out.relative_to(REPO)} from {len(batches)} batches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
