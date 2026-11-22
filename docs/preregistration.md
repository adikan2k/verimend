# Predictions, before the results

Every prediction made before a result, with the commit that recorded it, the commit of the result, and whether it came true. Wrong ones stay. A prediction is changed only by a new line, never in place.

| # | Date | Prediction | Recorded in | Result in | Outcome |
| --- | --- | --- | --- | --- | --- |
| 1 | 2026-11-21 | Row accounting fails the 4 `dbt-fix-product-metrics` false successes, rule A: 12 order lines, 2,482.90 of revenue | `525b638`, [decision 0008](decisions/0008-row-accounting.md) | `f208ae0`, [replay](../bench/results/replay-deb-2026-11-21/replay-summary.json) | **Right on rows, half right on sums.** 4 of 4 fail, 12 lines each; two lose 2,482.90, two 2,720.87 (a different revenue column) |
| 2 | 2026-11-21 | It fails `dbt-fix-category-revenue` dev6/3, rule B: 18 lines under a NULL month | `525b638` | `f208ae0`; explained by v2.1 in `305e273`, [focus](../bench/results/replay-deb-v21-focus-2026-11-21/replay-summary.json) | **Wrong.** The 18 lines exist, but this fix takes the month from the lines, so no key is NULL |
| 3 | 2026-11-21 | It passes the other 17 false successes | `525b638` | `f208ae0` | Right |
| 4 | 2026-11-21 | It passes every reference and right fix; the `dbt-customer-ltv-fix` reference is at risk | `525b638` | `f208ae0`, then `305e273` | **The risk was real.** v2.0 passed it by a bug; v2.1, with the bug fixed, rejects it: 16 customers, 4,677.38 of lifetime value |
| 5 | 2026-11-21 | The four existing checks reproduce at least 95% of their recorded verdicts | `525b638` | `f208ae0` | Right: 146 of 146, and again in the v2.2 replay. Runs recorded with older checks showed collateral flips on three report models of data-eng-bench (decision 0008) |
| 6 | 2026-11-21 | At most 20% of analysed diffs have something not analysed | `525b638` | `f208ae0` | Right overall (16%), wrong for ADE-bench alone (48%, mostly incremental models skipped by design) |
| 7 | 2026-11-21 | Stop rule: rule A becomes report-only if it rejects a reference; stop and report under 4 of 5 targets | `525b638` | `305e273` | **Applied.** Rule A is report-only from v2.2; the gate catches 0 of 5 targets, so the check is reported as a negative result |
