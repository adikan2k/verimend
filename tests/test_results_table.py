"""scripts/results_table.py on summaries written by bench/assemble.py."""

from pathlib import Path

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.grade import Grade
from scripts.results_table import build_table, load_batch
from verimend.runrecord import AgentRun


def batch(results: Path, name: str, agent: str, source: str, passed: list[bool]) -> None:
    records, grades = [], []
    for i, ok in enumerate(passed):
        task = f"{source}-t{i}"  # task names never repeat across benchmarks
        run = AgentRun(model="m", prompt_sha="s", report={"verdict": "fixed"}, cost_usd=0.01,
                       num_turns=10, subtype="success", seconds=20.0)  # fmt: skip
        records.append(AgentRecord(task, 1, 0.0, 20.0, run_cap_usd=0.25,
                                   agent_run=run if agent == "verimend" else None))  # fmt: skip
        grades.append(Grade(task_id=task, trial=1, passed=ok, grader="g"))
    meta = {"model": "m", "git_sha": "abcdef123", "source": source, "max_steps": 40}
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
