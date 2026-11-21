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

To be filled from `bench/results/replay-*/replay-summary.json`.
