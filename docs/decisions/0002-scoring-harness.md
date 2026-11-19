# 0002: Scoring harness

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-19.

## Chosen

1. **The benchmark grades, unchanged.** `bench/run.py` hands every batch to ADE-bench's own `Harness` inside the Linux runner from phase 1. Our code wraps the agent only: it times it, gives it its share of the batch budget and keeps its output. `bench/grade.py` only reads what ADE-bench wrote.
2. **Four agents behind one entry point.** `none` (the floor), `reference` (ADE-bench's `sage`, the task's own solution), `baseline` and, from phase 3, `verimend`. The first two cost nothing and prove the grader works: 0% and 100% on every kept task.
3. **The baseline is ADE-bench's own Claude Code agent.** Its tools are ADE-bench's baseline set: Bash, Edit, Write, Read, Glob and Grep. Four changes, each recorded in the batch:
   - the CLI is pinned to 2.1.328, the version the Agent SDK bundles;
   - `--tools` limits the tools the model is offered to those six. ADE-bench's `--allowedTools` only pre-approves them: in the first smoke run the model was still offered 24 Claude Code tools, subagents and workflows among them. With `--tools`, the largest prompt fell from 21,562 to 10,273 tokens;
   - `--max-turns` and `--max-budget-usd` are set from the run cap, at 80% of it, because the dollar cap can overshoot by one request;
   - WebSearch and WebFetch are off, since the reference solutions are public;
   - one line in the prompt asks for `VERDICT: fixed | not_fixed | escalate`.

   That last line is what makes the baseline's false-success rate measurable.
4. **A task check before the split.** A candidate is kept only if its reference solution passes and the do-nothing agent fails. Otherwise the task cannot tell a fix from no fix. Claude Code ran this check with output limited to task IDs and pass/fail, and its traces stay outside the repo without test names.
5. **The split copies task folders into the repo.** It writes `bench/dev/<source>/` and `bench/heldout/<source>/`, with the upstream LICENSE and commit. `bench/split.json` lists dev IDs and only a held-out count. The vendor checkouts' task folders are now off-limits to Claude Code too (settings deny rules, CLAUDE.md rule 2).
6. **Guards that ship with tests:**
   - every batch needs `--limit` and `--max-usd`, free agents included;
   - `--set heldout` is refused when `CLAUDECODE` is set, that is, from inside Claude Code;
   - `--set vendor` is refused once `bench/split.json` exists;
   - a paid run with no result line is charged its whole cap.

## Evidence

- Task check ([validation.json](../../bench/results/validate-2026-11-19/validation.json)): 33 of 37 candidates kept, 16 ADE-bench and 17 data-eng-bench. The four excluded tasks fail with their own reference solution.
- Split (seed 1119, run by Aditya, [split.json](../../bench/split.json)): ADE-bench 6 dev, 10 held out; data-eng-bench 7 dev, 10 held out.
- Dev set, no model calls: do-nothing 0 of 13 ([ADE](../../bench/results/dev-none-ade-bench-2026-11-19/summary.json), [DEB](../../bench/results/dev-none-data-eng-bench-2026-11-19/summary.json)), reference 13 of 13 ([ADE](../../bench/results/dev-reference-ade-bench-2026-11-19/summary.json), [DEB](../../bench/results/dev-reference-data-eng-bench-2026-11-19/summary.json)).
- Baseline on ADE-bench dev, one trial ([summary](../../bench/results/dev-baseline-ade-bench-2026-11-19/summary.json)): 5 of 6 pass. It claimed "fixed" on all six, so one is a false success (`airbnb009`). Mean $0.0061 a run, max $0.0128, total $0.0365, largest prompt 39,635 tokens. One trial on six tasks, so these are not results, only proof the measurement works.

## Rejected

- **Our own SDK baseline.** More code, and not the agent the benchmark publishes.
- **Task lists instead of copies.** The held-out content would have stayed in a folder Claude Code is allowed to read.
- **Checking tasks after the split.** A broken held-out task found later would shrink the held-out set after the fact.

## Open, for Aditya

- data-eng-bench joined the split (see `docs/spikes.md`, "Phase 2 addendum"), but only `none` and `reference` run on it. The baseline and VeriMend on it need a Harbor agent adapter in phase 3.
- The baseline's one false success came from a single trial. Three trials on dev would show whether it repeats, for about $0.11.
- The baseline's prompt suffix changes ADE-bench's published baseline by one line. Its numbers are ours, not comparable to ADE-bench's leaderboard.
