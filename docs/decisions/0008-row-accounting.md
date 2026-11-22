# 0008: A fifth check, row accounting

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-21. The predictions below were committed before any replay ran; the measured results are added underneath them, never in their place.

## Why

Every false success left after phase 6b, from both agents, builds cleanly, changes no test and gives wrong numbers (decisions 0004 and 0007). I read all 22 on the development set:

| Cause | Runs | Can a check that does not know the task's intent see it? |
| --- | --- | --- |
| Rows silently lost or kept at a LEFT join (`dbt-fix-product-metrics` ×4, `dbt-fix-category-revenue` dev6/3) | 5 | Yes: this check |
| A filter the base already had and the fix kept (`dbt-fix-inventory-model` dev6/3) | 1 | No: a WHERE is a deliberate choice |
| Business rules: returned orders kept, over-filtering (`dbt-customer-ltv-fix` ×6, `category-revenue` ×2) | 8 | No |
| `count(*)` changed to `count(col)` (`airbnb009` ×6) | 6 | No |
| Answer written outside the project (`cac-payback` ×2) | 2 | No, and not a numbers problem |

## Chosen (with Aditya)

1. **A pass/fail fifth check, `row_accounting`,** after collateral, on each SQL model whose file the diff touches and that built. It reads the model's compiled SQL with DuckDB's own parser (`json_serialize_sql`, no new dependency) and counts, in the database the fix built:
   - **Rule A, facts the spine drops.** A LEFT join whose right side is a CTE or subquery that sums something. Its rows with a non-NULL key and no partner in the spine are dropped, sums and all. It fails if any such row carries a nonzero sum. A filtered spine (WHERE, QUALIFY, HAVING, LIMIT, DISTINCT, SAMPLE or a non-LEFT join along it) explains the loss. A join the base model already had is left alone, because the loss is not the fix's doing.
   - **Rule B, dangling keys grouped under NULL.** In a grouped select, a group key from the right side of a LEFT join, or from anything joined through it. Rows whose join key is not NULL but found no partner, and that survive the select's WHERE, end up under a NULL key. It fails if there are such rows and the built model has a NULL in that column.
   - INNER, SEMI and ANTI joins, WHERE, QUALIFY and HAVING are deliberate and never counted. Whatever it cannot read or count is unknown, and unknown passes with the reason in the detail: a parse error, a join condition that is not plain column equalities, CTEs nested in a CTE, or over its limits (10 s a query, 25 queries a model, 120 s in all).
   - Incremental, ephemeral and Python models are not analysed.
2. **A free replay harness** (`bench/replay.py`). It takes the final diff of every run that claimed "fixed" in a recorded batch and dedupes the diffs. Then it snapshots each task once and verifies every diff with `verify --replay`, with no model calls. `--replay` leaves the snapshot as it was and lets row accounting run even after a collateral failure, so both are measured. It only runs in replays.

**One change from the approved plan, made before any replay.** The plan's rule A also counted a grouped select that sums a column of its LEFT join's right side. Reading the `dbt-fix-category-revenue` reference showed that this variant rejects it by construction: it sums `pv.cost_price` over a join to product variants, and variants with no sales are not lost facts. Only a pre-aggregated rollup counts as a measure now.

## Predicted, before any replay

On the 22 development false successes: **5 caught, 17 missed.**

| Diffs | Prediction |
| --- | --- |
| `dbt-fix-product-metrics` false successes ×4 (gate 1, 2, 3; dev6 3) | Fail, rule A: 12 order lines, 2,482.90 of revenue, whose product is not in the products spine |
| `dbt-fix-category-revenue` dev6/3, and the same diff wherever it recurs | Fail, rule B: 18 order lines with no order, grouped under a NULL month |
| `inventory` dev6/3, `category` gate 1 and 3, `airbnb009`, `cac-payback`, `customer-ltv-fix` | Pass, so missed |
| References and right fixes: `product-metrics`, `category-revenue`, `inventory`, `refund-reconciliation`, `dbt-fix-customer-ltv`, every ADE-bench task | Pass |
| The `dbt-customer-ltv-fix` reference | **At risk.** It is a new model with a LEFT join from an unfiltered customers spine onto summed orders, the same shape as the product-metrics wrong fixes. It fails if any order's customer is missing from the customers table. |
| The fault suite's references and right fixes | Pass: the raw demo data has no orphan payments or orders |

**Stop rule, fixed before the replay:**
- The four existing checks must reproduce their recorded verdicts on at least 95% of the runs recorded with them as they are now. Otherwise the harness is not measuring what it claims.
- Rule A may fail a fix only if it rejects no grader-passed diff and no reference. Otherwise it becomes report-only, and rule B is judged on its own by the same test.
- If fewer than 4 of the 5 targets are caught, or more than 20% of analysed diffs have something not analysed, I stop and report. No rule is added for a particular task, and none is tuned after the replay; a change after it is a new version, named here.

## Measured

Three versions ran, each a new version named here, none tuned to a particular task. v2.0 is the version the predictions above were made for.

| Version | What changed | Replay |
| --- | --- | --- |
| v2.0 (`f3a421e`, `a33bb82`) | As planned, minus the dropped A2 variant | All three sources: [faults](../../bench/results/replay-faults-2026-11-21/replay-summary.json), [ADE-bench](../../bench/results/replay-ade-2026-11-21/replay-summary.json), [data-eng-bench](../../bench/results/replay-deb-2026-11-21/replay-summary.json) |
| v2.1 (`6a6a767`) | Three defects the v2.0 replay showed: a schema-qualified table taken for the CTE of the same name, which looped and passed as unknown; a built model it could not read passed silently; rule B said nothing when unmatched rows existed but no key was NULL. No new rule | The two affected tasks: [focus](../../bench/results/replay-deb-v21-focus-2026-11-21/replay-summary.json) |
| v2.2 (`305e273`) | Rule A report-only, because the stop rule says so (below) | All three sources: [faults](../../bench/results/replay-faults-v22-2026-11-21/replay-summary.json), [ADE-bench](../../bench/results/replay-ade-v22-2026-11-21/replay-summary.json), [data-eng-bench](../../bench/results/replay-deb-v22-2026-11-21/replay-summary.json) |

### Against the predictions (v2.0)

| Prediction | Measured |
| --- | --- |
| Four checks reproduce their recorded verdicts on at least 95% | **146 of 146** (faults 99, ADE-bench 25, data-eng-bench 22) |
| `product-metrics` false successes ×4 fail rule A, 12 order lines, 2,482.90 | **All four fail rule A, 12 lines each.** Two lose 2,482.90 of revenue as predicted; two lose 2,720.87, because their rollup reads a different revenue column. I checked each by hand against the diff |
| `category-revenue` dev6/3 fails rule B, 18 lines under a NULL month | **Missed.** The 18 order lines with no order exist, but this fix takes the month from the order lines, so no key is NULL in the built model and rule B has nothing to fail. v2.1 says so in the detail: "18 row(s) found no partner in `left join orders AS o`, but sales_month is never NULL in the built model" |
| The 17 others pass, so missed | All passed row accounting |
| References and right fixes pass | All passed. **But the `dbt-customer-ltv-fix` reference passed by a bug:** its CTE `customers` reads `main.customers`, v2.0 followed the name back into itself, gave up, and passed it as unknown |
| At most 20% of analysed diffs have something not analysed | **18 of 116 (16%) overall.** By source: faults 0 of 27, data-eng-bench 4 of 60, ADE-bench 14 of 29 (48%): 13 of those are incremental models, skipped by design, and one is a `USING` join. The bar was written for the whole replay; per source, ADE-bench is over it |

On data-eng-bench, v2.0 as a gate moved wrong fixes let through from 22 to 18 and wrong fixes stopped from 6 to 10. Right fixes blocked stayed at 18, all blocked by the older checks on the tasks the verifier already cannot judge (decision 0007). Faults and ADE-bench: no change.

### The stop rule, applied

With the loop fixed, v2.1 counts the `dbt-customer-ltv-fix` reference: **16 customers, 4,677.38 of lifetime value, dropped by its LEFT join onto the customers spine.** The rows really are dropped; the grader passes the reference, so the task wants them dropped. Rule A cannot tell a deliberate exclusion from a lost fact, and the risk was named above before any replay. The stop rule then says rule A is report-only, so **v2.2 reports rule A's findings in the detail and fails only on rule B.**

Rule B is judged on its own by the same test. It failed no diff in any replay, right or wrong, so it stays a gate, and it catches nothing on the development set.

So as a gate, the fifth check catches **0 of the 5 targets**, under the bar of 4: by the stop rule, I stop and report. Nothing is added for a task and nothing is tuned. What stays:
- rule A's report, which names the four product-metrics losses with their rows and sums in the verifier's detail, where a reviewer reads it;
- rule B as a gate, with no false rejection measured;
- the replay harness, which re-verifies every recorded diff for free and found v2.0's defects before they shipped.

### v2.2, the version that ships

Replayed on all three sources, every recorded claimed fix (116 unique diffs, 245 runs):

| Source | Four checks reproduced (recorded with them as now) | Row accounting rejects | Rule A reports | Not fully analysed |
| --- | --- | --- | --- | --- |
| Faults | 99 of 99 | none | none | 0 of 27 |
| ADE-bench | 25 of 25 | none | none | 14 of 29 |
| data-eng-bench | 22 of 22 | none | 5 diffs: the 4 `product-metrics` false successes and the `customer-ltv-fix` reference | 3 of 60 |

As a gate, row accounting v2.2 changes no verdict anywhere. What it adds is the report: on the four wrong `product-metrics` fixes the verifier's detail now says that 12 order lines and their revenue are dropped, which is the bug, and on the reference it says the same of 16 customers, which is the intent.

### Also found

- **Collateral (check 4) is not fully reproducible on data-eng-bench's 2,870-node project.** In the v2.1 focus, one diff recorded as passing with the four checks failed collateral on replay, and one more diff did too: models outside the fix's lineage (`rpt_cannibalization_analysis` 214 → 215 rows, `rpt_rfm_analysis` contents) moved, though five rebuilds of the unchanged base did not. Both are wrong fixes, so the flip happened to land right, but it is noise, not a catch. The v2.0 replay had reproduced both, so the same diff gave different verdicts on two replays: 5 of the 6 four-check diffs in the focus reproduced. The v2.2 replay shows how far it goes: of data-eng-bench's 60 unique diffs, 5 got a different collateral verdict from the v2.0 replay, 3 flipping to pass and 2 to fail, all on the same three report models (`rpt_cannibalization_analysis`, `rpt_rfm_analysis`, `rpt_product_affinity_analysis`), which move only now and then, so the snapshot's two builds do not mark them unstable. Weighted by runs, the four-check gate on that source reads 22 wrong fixes let through and 6 stopped in one replay, 25 and 3 in the other, with right fixes blocked at 18 and 23. The runs recorded with today's four checks all reproduced (22 of 22), so this is noise in check 4 on one large project, not a change in the checks. A follow-up for check 4, not part of this check.
- **Dropped before any replay:** the A2 variant (above), and the optional paid VeriMend ×3 run on data-eng-bench, because a replay of recorded diffs measures the check without one.

## Rejected

- **Tuning rule A to pass the `customer-ltv-fix` reference,** for example by exempting a spine named `customers`. That is a rule written for one task, which the stop rule forbids, and it would pass the same shape when the loss is a bug.
- **Rule A as a gate with a label to override it.** That is what the inspector's `pr` policy does for test changes (decision 0009), but here it would fail right fixes by default, and the benchmark has no label.
