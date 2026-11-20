# 0003: Agent, first version

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-19.

## Chosen

1. **Eight tools over one `Workspace` interface.** The tools are plain async functions in `verimend/tools/` with no SDK import. A `Workspace` gives them a project folder, a DuckDB file, `exec`, read, write, remove and list. The same tool code runs in three places:
   - on a local copy (`LocalWorkspace`; the tests now, `verimend fix` in phase 4);
   - in an ADE-bench task container (`bench/ade/workspace.py`, through Docker);
   - in a Harbor task container (`bench/harbor/workspace.py`, through `environment.exec`).
2. **`read_file` is the eighth tool** (Aditya's decision on PR 1). It is read-only, limited to the project (`dbt_packages/` included), and takes an optional line range. It lists a folder when given one.
3. **Tool details:**
   - **`read_failure`** builds the whole project once and says plainly when nothing fails. Benchmark tasks often start from a project that builds cleanly, and then the task prompt is the only description of the bug.
   - **`query_data`** opens DuckDB read-only with `enable_external_access` off, so a SELECT cannot read other files either.
   - **`edit_model`** takes either the whole new content or an exact `old_text`/`new_text` replacement. The replacement costs fewer output tokens, and output is over half the cost.
   - **`run_dbt`** takes `full_refresh` (added in iteration 2), and it acts on the sandbox copy only.
   - **`report`** validates SPEC §4's verdict with pydantic and returns the error, so the model can correct it.
4. **Three guards, as one PreToolUse hook:**
   - the `edit_model` path allowlist;
   - a single SELECT for `query_data`;
   - nothing after `report`.

   The tools check the same predicates again, so a guard holds even without the hook. Each guard has a test that proves it blocks.
5. **One SDK call shape**, exactly as spike 1 recorded it:
   - no built-in tools, `dontAsk`, no setting sources, a fixed `cwd`;
   - the key passed through `env`;
   - the SDK's dollar cap at 80% of the run cap;
   - cost taken only from the `ResultMessage`.

   `AgentRun` (`verimend/runrecord.py`, stdlib only) carries the run to the harness, across a process boundary on Harbor.
6. **The system prompt is a fixed string.** Its sha256 is in every trace's `run_start`.
7. **data-eng-bench.** VeriMend is told the backend and its project path, because it has no shell to run the `echo $DB_TYPE` that the prompts ask for. The baseline gets the prompt unchanged.
8. **The baseline on data-eng-bench runs ADE-bench's exact Claude Code command** in the task container:
   - the same pinned CLI 2.1.328 and tools, the same `--tools`, turn and dollar caps, web tools off;
   - the same verdict line.

   **This differs from the plan**, which said Harbor's own Claude Code agent. That agent runs through a different bridge (ACP) and has no `--tools` option, so it would not have been the same baseline on the two benchmarks.

## Iteration on the development set

| Round | ADE-bench | data-eng-bench | False successes (of 13) | What changed after it |
| --- | --- | --- | --- | --- |
| 1 | 5 of 6 | 1 of 7 | 6 | see below |
| 2 | 5 of 6 | 2 of 7 | 3 | none: stopped here to avoid fitting the 13 tasks |

Round 1's traces showed three problems:
- **`airbnb009`.** The fix was right, but the model is incremental, and `run_dbt` could not full-refresh it. VeriMend escalated, saying exactly that. `run_dbt` gained `full_refresh`.
- **Six data-eng-bench claims of "fixed" failed** on partial fixes. For example, units sold came to 6,590 against 6,559. The prompt now asks to reconcile the output against the sources, and to check every problem the task lists, before claiming fixed.
- **Two data-eng-bench "fix" tasks ask for new development:**
  - `dbt-fix-product-metrics`: "Create a model called `fct_product_metrics`".
  - `dbt-fix-cac-payback-waterfall`: rebuild a report from another project.

  Aditya decided to keep every task. The prompt now says VeriMend repairs existing code and escalates new development. Both tasks now escalate. They still count as grader failures, and they will be listed under what VeriMend cannot fix.

In round 2, `airbnb009` used the full refresh but changed what `review_totals` counts. Its hidden totals check fails. This was not tuned further.

## Final development batches

Round 2's code and prompt, unchanged, at three trials. The full table is [`docs/results-dev.md`](../results-dev.md).

| Agent | Benchmark | Grader pass | False successes | Escalations | $ a run (mean / max) | Largest prompt |
| --- | --- | --- | --- | --- | --- | --- |
| VeriMend ×3 | ADE-bench | [15 of 18](../../bench/results/dev-verimend-ade-bench-2026-11-19/summary.json) | 3 (all `airbnb009`) | 0 | $0.0057 / $0.0152 | 27,693 |
| VeriMend ×3 | data-eng-bench | [6 of 21](../../bench/results/dev-verimend-data-eng-bench-2026-11-19/summary.json) | 12 | 3 | $0.0128 / $0.0256 | 50,339 |
| Baseline ×1 | data-eng-bench | [2 of 7](../../bench/results/dev-baseline-data-eng-bench-2026-11-19/summary.json) | 5 | 0 | $0.0157 / $0.0197 | 57,443 |

Every run ended with a `report` (or, for the baseline, a verdict line), none hit a cap, and there were no harness errors, so phase 3's done-when holds. Phase 3 spent $0.77 of its $5.

What the three trials show:
- **data-eng-bench varies a lot between trials:** 3, 1 and 2 of 7. Round 2's single trial (2 of 7) was in that range. Only `refund-reconciliation` passed all three times.
- **12 of 21 data-eng-bench runs claimed fixed and failed.** The reconcile rule from round 1 did not stop it. This is the number the verifier in phase 4 has to bring down.
- **The escalate-new-development rule is followed only some of the time.** `product-metrics` escalated in 2 of 3 trials and `cac-payback-waterfall` in 1 of 3. The other three runs built something and claimed fixed. The baseline, which has no such rule, passed `cac-payback-waterfall`.
- `git_dirty` is true in the baseline batch's summary because CLAUDE.md and this note were being edited while it ran. No code changed during the batches; both ran at cbd6178.

## Rejected

- **Harbor's own Claude Code agent for the baseline.** See point 8.
- **Building new models on request.** That is development, not a repair (SPEC §1), and it is not what the verifier is for.
- **A third prompt round.** With 13 dev tasks, further tuning starts fitting them.

## Open, for Aditya

- The data-eng-bench held-out set probably contains build tasks too. With the current prompt they become escalations when the rule holds: honest, but failures. Either the rule goes (and VeriMend builds new models, like the baseline) or it is enforced; as it stands it is neither.
- **`steps` in traces is the CLI's `num_turns`.** The step cap (`max_turns`) counts model requests, and one request can call several tools. The longest run made 34 requests against a cap of 40, while its `num_turns` was 55. The baseline's steps use the same field, so the two agents stay comparable.
- `read_failure` builds the whole data-eng-bench retail project, which takes about 2.5 minutes. That is wall time, not dollars.
