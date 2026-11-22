"""scripts/results_table.py on summaries written by bench/assemble.py."""

from pathlib import Path

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.grade import Grade
from scripts.results_table import build_table, load_batch
from verimend.runrecord import AgentRun


def batch(
    results: Path, name: str, agent: str, source: str, passed: list[bool], group: str = ""
) -> None:
    records, grades = [], []
    for i, ok in enumerate(passed):
        task = f"{source}-t{i}"  # task names never repeat across benchmarks
        run = AgentRun(model="m", prompt_sha="s", report={"verdict": "fixed"}, cost_usd=0.01,
                       num_turns=10, subtype="success", seconds=20.0,
                       final_verdict="fixed")  # fmt: skip
        records.append(AgentRecord(task, 1, 0.0, 20.0, run_cap_usd=0.25,
                                   agent_run=run if agent == "verimend" else None))  # fmt: skip
        grades.append(Grade(task_id=task, trial=1, passed=ok, grader="g"))
    meta = {"model": "m", "git_sha": "abcdef123", "source": source, "max_steps": 40}
    if group:
        meta["group"] = group
    write_batch(results / name, records, grades, get_agent(agent), meta)


def test_table_has_a_row_per_batch_and_a_combined_row(tmp_path: Path) -> None:
    batch(tmp_path, "v-ade", "verimend", "ade-bench", [True, True, False])
    batch(tmp_path, "v-deb", "verimend", "data-eng-bench", [True])
    batch(tmp_path, "none-ade", "none", "ade-bench", [False, False, False])
    batches = {n: load_batch(n, tmp_path) for n in ("v-ade", "v-deb", "none-ade")}
    table = build_table(batches, "../bench/results/").splitlines()
    rows = table[2:]
    assert rows[0].startswith("| `none` | ADE-bench | 3 (3 × 1) | 0% |")
    assert rows[1].startswith("| `verimend` | ADE-bench | 3 (3 × 1) | 67% |")
    assert rows[2].startswith("| `verimend` | data-eng-bench | 1 (1 × 1) | 100% |")
    both = rows[3]
    # Recomputed from the four runs: 3 of 4 passed, one claimed fixed and failed.
    assert both.startswith("| `verimend` | **both** | 4 (4 × 1) | 75% | 1 trial | 25% | 4 |")
    assert "[v-ade](../bench/results/v-ade/summary.json)" in both
    assert "[v-deb](../bench/results/v-deb/summary.json)" in both
    assert "$0.0100 / $0.0100" in rows[1]


def test_the_gate_table_shows_only_agents_the_verifier_watched(tmp_path: Path) -> None:
    from scripts.results_table import build_gate_table

    def watched_batch(name: str, agent: str) -> None:
        records, grades = [], []
        for i, (graded, verified) in enumerate([(True, True), (False, False), (False, True)]):
            task = f"t{i}"
            attempt = {"attempt": 1, "passed": verified,
                       "failed_check": None if verified else "tamper", "checks": []}  # fmt: skip
            log = '{"type":"result","subtype":"success","result":"VERDICT: fixed"}'
            records.append(AgentRecord(task, 1, 0.0, 20.0, run_cap_usd=0.25,
                                       stream_log=log if agent == "baseline" else None,
                                       verifier={"snapshot": {}, "attempts": [attempt],
                                                 "error": None}))  # fmt: skip
            grades.append(Grade(task_id=task, trial=1, passed=graded, grader="g"))
        meta = {"model": "m", "git_sha": "abc", "source": "ade-bench", "max_steps": 40}
        write_batch(tmp_path / name, records, grades, get_agent(agent), meta)

    watched_batch("b", "baseline")
    watched_batch("r", "reference")
    table = build_gate_table({n: load_batch(n, tmp_path) for n in ("b", "r")}, "x/")
    assert table is not None
    rows = table.splitlines()[2:]
    assert len(rows) == 1  # the reference is not an agent whose claims are in question
    assert rows[0].startswith("| `baseline` | ADE-bench | 3 | 1 | 0 | 1 | **1** | tamper 1 |")


def test_real_world_kinds_are_separate_rows_and_never_combined(tmp_path: Path) -> None:
    batch(tmp_path, "v-bugs", "verimend", "realworld", [True, False], group="bug fixes")
    batch(tmp_path, "v-ports", "verimend", "realworld", [True], group="DuckDB ports")
    batches = {n: load_batch(n, tmp_path) for n in ("v-bugs", "v-ports")}
    rows = build_table(batches, "../bench/results/").splitlines()[2:]
    assert [r.split(" | ")[1] for r in rows] == [
        "Real-world, bug fixes",
        "Real-world, DuckDB ports",
    ]
    assert not any("**both**" in r for r in rows)
