# 0004: The verifier and the attempt loop

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-20.

## Chosen

1. **The verifier is a separate program.** `python -m verifier snapshot | verify` prints one JSON object. Its only dependencies are the standard library, PyYAML and DuckDB, and it imports nothing from `verimend`.
   - The agent loop, the CLI and the benchmark all run it as its own process.
   - In benchmark containers its source is uploaded to `/tmp/verimend-verifier` and run with the container's Python, next to the dbt that grades the task.
   - The agent's tools cannot reach `/tmp`: every tool path must stay inside the project.
2. **A snapshot before the agent starts.** The verifier copies the project and the DuckDB file into its own folder before the model is called, then builds that copy. That build is "the last good build" for the collateral-damage check.
   - It never writes to the real database. It writes a `profiles.yml` of its own that points the project's profile and target at its copy, keeping the file name, because DuckDB names the catalog after the file.
   - A snapshot that fails stops the run before any model call, and is recorded as a harness error.
   - **A base that does not parse is still snapshotted** (with Aditya). A compile error can be the bug itself. There is then no manifest and no base build: tamper and masking read the base's source files instead, and collateral has nothing to compare and says so.
   - **A parse that fails is tried again**, up to three times, before it counts (with Aditya, after dev6). This covers every dbt command the verifier runs, at snapshot and at verify. One data-eng-bench base failed to parse in 1 of 71 snapshots and parsed in every other one. A real compile error fails every try. The error kept is dbt's own error lines, not the end of its output, which was only a summary of deprecations.
   - **Masking without a base manifest** counts a model as removed only when its file is gone. The base's files also list the models the base itself disables, and no manifest lists those. If the diff touches `dbt_project.yml` or an `enabled` setting, every model in the base's files must still be enabled after the fix: the verifier cannot tell which ones the base already disabled, so it rejects rather than guesses.
3. **The verifier computes the diff itself.** With `--from-project`, it compares the snapshot with the project as the agent left it. It never takes the agent's word for what changed.
   - A human's diff goes through the same applier, `verify --diff`, which accepts git-style diffs and either line ending.
   - **`profiles.yml` is left out of a computed diff** (with Aditya). It describes the environment, not the project, and the verifier writes its own. A diff that names it is still rejected by tamper.
4. **The four checks of SPEC §5, in order:**

   | Check | How |
   | --- | --- |
   | Tamper | Changed paths must fall within the project's own model, macro, seed, snapshot and analysis folders. Then the base and patched manifests are compared: every test node must still exist with the same severity, `where`, `limit`, thresholds, tags, arguments and code. Unit tests and generic test macros count too. **Edits under `dbt_packages/` stay rejected** (with Aditya), although the `asana001` reference fix makes one. |
   | Masking | No enabled model, seed or snapshot may disappear, unless an enabled node of the same name replaces it (a package override). No `limit 0` or always-false filter may be added to a changed SQL file. |
   | Clean rebuild | A full `dbt build` on a fresh copy. **The fix and everything downstream of it must pass or warn, and nothing that passed in the base build may fail now.** Failures that were already there elsewhere are excused (with Aditya, who chose the lineage rule; it is implemented as "downstream", because what lies upstream of the fix already built in the base). A node the fix must pass that dbt skipped only because something upstream had already failed before the change is built on its own (`--select`), and judged on that. |
   | Collateral | Every model outside the lineage of the changed nodes must keep the row count and checksum of the base build. Changed nodes are those whose checksum or config changed, plus users of a changed macro; a change to `vars` counts as every node. Models that differ between builds of the unchanged base are compared by row count only, or not at all if their row count moves too (see 5). |

   The checksum is `sum(hash(t))` over the row struct. It is order-independent, and the exact SQL is in `docs/spikes.md`.
5. **Unstable models, found two ways** (with Aditya).
   - **Calibration at snapshot.** The unchanged base is built a second time on a fresh copy. Whatever differs between the two builds is unstable from the start.
   - **A targeted recheck before collateral rejects.** The models collateral would reject are rebuilt, with their parents, on a fresh copy of the unchanged base, up to five times. A model that moves there is unstable; a model whose row count changed is excused only if its row count moves on its own. A model that stays put in all five rebuilds is real collateral damage, and the fix is rejected.
   - The first version rebuilt the whole base once. The [stability probe](../../bench/results/probe-stability-2026-11-20/probe.json) showed why that was not enough (see "What the runs showed").
6. **One addition to SPEC §5: an empty diff fails** (`empty_diff`). A "fix" that changes nothing has to fail. Without this rule, a project that already builds would pass all four checks unchanged.
7. **The attempt loop runs inside one SDK session.** A `fixed` report triggers the verifier from inside the `report` tool.
   - On a pass, the run is over. On a rejection, the tool returns which check failed and its message; the agent never sees the verifier's code.
   - After the third rejection the run ends as `not_fixed`. `escalate` and `not_fixed` skip the verifier.
   - Keeping one session keeps the prompt cache warm and the steps comparable to the baseline. A verifier call inside `report` took up to 253 s on data-eng-bench, and the SDK waited every time.
   - A `feedback` switch drops the check details, for SPEC §6's ablation.
8. **Two more guards on `edit_model`:**
   - A YAML test block may not be removed or changed. The hook parses the file before and after the edit, using the same code that makes the edit.
   - A generic test macro may not be added or changed. Changes to project-wide test settings in `dbt_project.yml` are refused too.

   The tool checks again, so both guards hold even without the hook. The verifier repeats the test check its own way, from the manifests.
9. **`verimend fix` works on a sandbox copy** in `~/.verimend/sandboxes/`, and writes `fix.diff` and `run.json`. The project it is pointed at is never changed. Opening a pull request is phase 6.

## What the runs showed

All runs are on the development set with Haiku 5.5. The table is [`docs/results-dev.md`](../results-dev.md).

### The verifier on known answers (no model calls)

- **Doing nothing** is rejected on all 13 tasks, with `empty_diff` ([ADE-bench](../../bench/results/verifier2-none-ade-2026-11-19/summary.json), [data-eng-bench](../../bench/results/verifier2-none-deb-2026-11-19/summary.json)).
- **The reference fixes** pass on 10 of 13 ([ADE-bench](../../bench/results/verifier6-reference-ade-2026-11-20/summary.json), 5 of 6; [data-eng-bench](../../bench/results/verifier5-reference-deb-2026-11-20/summary.json), 5 of 7). These are the verifier's known false rejections:

  | Task | Check | Why |
  | --- | --- | --- |
  | `asana001` | tamper | The reference fix edits a file under `dbt_packages/`. Kept as a rejection (with Aditya). |
  | `dbt-fix-cac-payback-waterfall` | empty diff | The reference solution changes no file the verifier compares. |
  | `dbt-fix-customer-ltv` | clean rebuild | A test on `dim_customers` has a `where` clause naming `customer_type`, a column the model never had. The test errors after any fix; the benchmark's grader never runs it. |

  Before the targeted recheck, `dbt-fix-product-metrics` was rejected too, for `rpt_cannibalization_analysis` changing from 214 to 215 rows. That was first put down to a model reading another without `ref`. The stability probe showed the real cause: that model's row count flips between builds of the unchanged base.

### The stability probe

Two data-eng-bench bases, each snapshotted and then built six more times with nothing changed ([probe.json](../../bench/results/probe-stability-2026-11-20/probe.json)):

- About 220 of the 2,216 models differ on every build, probably load timestamps. Calibration always catches them.
- Three more flip between two states on only some builds: `rpt_rfm_analysis` and `rpt_product_affinity_analysis` (contents), and `rpt_cannibalization_analysis` (row count). Two calibration builds missed at least one of them on both tasks.
- Adding a model nothing depends on changed nothing, and neither did `--threads 1`. The randomness is inside DuckDB's query execution.

Every collateral rejection in the first data-eng-bench batch ([dev5](../../bench/results/dev5-verimend-data-eng-bench-2026-11-20/summary.json)) named one of those three models. They were false rejections: the fixes were wrong anyway, but not for the reason the verifier gave.

### VeriMend ×3

- **ADE-bench** ([dev4](../../bench/results/dev4-verimend-ade-bench-2026-11-20/summary.json)): 15 of 18 pass, the same as phase 3. The verifier passed all 18 on the first attempt. The 3 false successes are `airbnb009`, every trial: the fix builds cleanly and changes a count the grader checks.
- **data-eng-bench, first verifier** ([dev5](../../bench/results/dev5-verimend-data-eng-bench-2026-11-20/summary.json)): 5 of 21 pass, 8 false successes (phase 3: 6 of 21 and 12). The verifier rejected 5 times. 3 were the unstable models above. 2 were clean-rebuild rejections on `dbt-fix-customer-ltv` (`dq_orphaned_records` stayed skipped even when built on its own), the task whose reference fix the verifier also rejects, so they are not counted as catches. It caught no wrong fix on its merits.
- **data-eng-bench, targeted recheck** ([dev6](../../bench/results/dev6-verimend-data-eng-bench-2026-11-20/summary.json)): 6 of 21 pass, 8 false successes, the same count as dev5.
  - Collateral rejected nothing.
  - The verifier rejected 5 times. 4 were clean rebuilds on `dbt-fix-customer-ltv`, the same skipped `dq_orphaned_records` as in dev5; all three of those runs then escalated.
  - The fifth, a masking rejection on `dbt-fix-category-revenue`, was false. The base failed to parse on that run only, and masking without a base manifest counted the 25 models the base itself disables as removed by the fix. The fix was wrong anyway (the grader failed it on revenue), but the table counts that run as caught by the verifier, and it was not. Both causes were fixed after the batch (item 2). The [ADE-bench reference run](../../bench/results/verifier6-reference-ade-2026-11-20/summary.json), repeated with the fixed verifier, still passes 5 of 6; `airbnb002`, whose base never parses, takes all three tries and passes.
  - One hook block: on `dbt-fix-cac-payback-waterfall` the agent tried to edit `profiles.yml`, was refused, and escalated.
  - Again the verifier caught no wrong fix on its merits. All 8 false successes passed all four checks.

### What the verifier can and cannot see

Every false success in phase 4 builds cleanly and passes all four checks with wrong numbers. The four checks catch a fix that breaks the build, hides a failure, weakens a test or damages a model outside its lineage. They cannot tell a right total from a wrong one. On data-eng-bench, a fix that is plausible but wrong is how the agent fails, so the verifier's effect on the false-success rate is small there. The lower false-success rate comes mostly from the agent itself escalating or stopping without claiming a fix.

SPEC §6 counts a run that escalates after a rejection as an escalation, not as caught by the verifier; the table follows it.

## Rejected

- **Running the verifier in the agent's process**, or importing it as a library. That is simpler, but the separation would rest on discipline instead of a process boundary.
- **Ending the SDK session after each `fixed` report and starting a new one with feedback.** That costs a cold prompt cache on every attempt and splits a run's steps across sessions. It stays the fallback if a long verifier call ever times out inside a tool call.
- **Finding tests by text patterns in YAML.** The manifest is what dbt actually runs, so the verifier compares manifests. The agent's guard parses YAML, because it has to decide before anything is built.
- **A clean rebuild that requires every node in the project to pass.** Benchmark projects carry old failures unrelated to the task; every fix, the reference included, would fail.
- **More calibration builds at snapshot.** Each costs about two minutes on data-eng-bench, on every run, and still misses a model that flips rarely. The targeted recheck costs seconds, and only when collateral is about to reject.
- **Comparing every model by row count only.** It would hide a real change to a model's contents.
- **A fifth check that reconciles numbers.** It is not in SPEC §5, and Aditya decided to build the four checks as specified and report what they miss.

## Open, for Aditya

- **The verifier cannot see wrong numbers.** Whether a later phase adds a check for them (against what?) is the main open question.
- **`dbt-fix-customer-ltv`**: the verifier rejects the reference fix because of a broken test in the task, and it rejected the agent's fixes on clean rebuild too. Excluding a test from the clean rebuild would weaken a check, so it stays as a known false rejection.
- A run whose verifier could not run is recorded as `verifier_error` with no claim, never as the agent's failure. None happened in phase 4.
- **Spend:** $0.81 of the $5 phase cap. Smoke runs $0.0276, ADE-bench dev4 $0.1064, an interrupted data-eng-bench dev4 $0.0463, dev5 $0.2986, dev6 $0.3276, and one `verimend fix` on the fixture $0.0018. The runs around `none` and `reference` cost nothing.
