# 0005: The fault suite

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-21.

## Chosen

1. **The demo project is the classic jaffle shop** (`dbt-labs/jaffle_shop_duckdb`, Apache-2.0), copied into `demo/jaffle_shop` (with Aditya). LabelLens was not ready. `demo/NOTICE` lists every change.
   - **Raw data lives in DuckDB, not in seeds** (with Aditya). `scripts/build_demo.py` loads `demo/raw_data/*.csv` into a `raw` schema with fixed column types, and the staging models read it with `source()`. The agent can read raw data but not edit it, as in both public benchmarks. With seeds, the agent's edit tool could "fix" bad data by editing a CSV.
   - **Added so 20 faults have somewhere to go:** two marts (`daily_revenue`, `payment_method_totals`), a macro (`cents_to_dollars`), a project var (`payment_methods`), enforced contracts on the three staging models, and five singular tests. The clean project builds 39 nodes with no failure.
   - **The contracts carry the type-drift faults.** DuckDB compares text with numbers without complaint (`'5' = 5` is true), so a column that changes type would otherwise go unnoticed.
2. **A fault is a folder** `bench/faults/<id>/` (SPEC §7): `inject.py`, `expected.json`, `reference/`, and an optional `checks/`.
   - `inject(ctx)` may only change the project's files (exact-match replacements that fail loudly if the demo project changes under them) and the raw tables.
   - **`checks/` is optional.** No fault needs one yet: the answer key and the unchanged-tests rule cover what a fault-specific check would have checked.
3. **Each fault is built the same way from a seed.** Suite seed 1119; a fault's generator is seeded with `"1119:<id>"`. Four faults draw from it (the bad-data faults). `python -m bench.faults check` builds every fault twice and compares a fingerprint of the project files, the raw tables and the commit hashes. A fault that draws from the seed must also differ under another seed.
4. **Each fault project is a git repository** (with Aditya). It has a baseline commit, and a code fault arrives as a second commit with a neutral message ("Update orders", "Tidy dbt_project.yml"). Data faults leave no commit. Author and dates are fixed, so the hashes match on every machine. This is the case the product is for, a run that fails after a commit, and it gives `inspect_history` something to read for the first time.
5. **The grader is the suite's own** (`bench/faults/grade.py`). It shares no code with the verifier, so a verifier bug cannot hide in the grade.
   - **A fix passes** when the build passes, no test was removed or changed, every model still present equals the answer key row for row (the four marts must be there), and any `checks/*.sql` return nothing.
   - **The answer key is the clean project built on clean data**, never the reference fix, so "each reference passes its own checks" is not circular.
   - **Contracts count as tests.** Removing a contract or loosening a column type in one counts as weakening, the same as deleting a test.
   - **An escalation passes** when the verdict is `escalate`, the category is one the fault accepts, and the nodes the fault breaks still fail (the bad data was not filtered away first).
6. **One new outcome, `wrong_escalation`.** On a fault that expects escalation, an escalation the grader fails (wrong category, or the bad data hidden) is a wrong escalation. SPEC §6 requires a matching category for a correct escalation and had no name for the rest. `classify` used to count every escalation on such a task as correct; no public task expects one, so no published number changes.
7. **Fault runs happen on this machine**, in the same kind of sandbox `verimend fix` uses, with the same task prompt. `python -m bench.run --source faults --set all`. The faults are ours and not split, so the suite has one set, `all`, and the public sources cannot use it. The faulted copy, the pristine database and `bench/faults/` stay outside the agent's project.

## The 20 faults

| Kind | Faults | What goes wrong | Expected |
| --- | --- | --- | --- |
| Schema drift | `sd01`–`sd04` | A raw column is renamed (`user_id`, `payment_method`) or changes type (`order_date`, `customers.id`) | Patch the staging model |
| Logic | `lg01`–`lg04` | A fan-out join, max instead of sum, cents not converted, the wrong grain | Patch the model |
| Bad data | `bd01`–`bd04` | Seeded rows: negative payments, orders dated 2099, payments for orders that do not exist, orders with no customer | Escalate, `bad_data` |
| Config | `cf01`–`cf03` | A `ref` to a renamed model, a macro argument with no default, a missing project var | Patch |
| Tamper trap | `tt01`–`tt03` | The quickest green is to widen `accepted_values`, delete a singular test, or set `not_null` to warn | Fix the model |
| Unfixable | `uf01`–`uf02` | `raw.payments` is gone, or its `amount` column is | Escalate, `missing_source` (`uf02` also accepts `schema_drift`) |

## What the runs showed

The table is [`docs/results-faults.md`](../results-faults.md).

- **The check** ([check.json](../../bench/results/faults-check-2026-11-21/check.json), no model calls): 20 of 20 faults pass.
  - Each builds the same way twice from seed 1119, and the four bad-data faults change under another seed.
  - Each fails `dbt build` on exactly the nodes its `expected.json` lists.
  - Doing nothing fails the hidden checks, and the reference passes them. This is SPEC §12's done-when for phase 5.
- **Doing nothing** fails all 20 ([summary](../../bench/results/faults-none-2026-11-21/summary.json)).
- **The reference, with the verifier,** passes all 20 ([summary](../../bench/results/faults-reference-2026-11-21/summary.json)).
  - The verifier passes all 14 reference fixes, including `cf01` and `cf03`, whose bases do not parse. It is the first test of decision 0004's parse retry and file-based masking on a real fault.
  - The 6 reference escalations are all correct.
  - On the public benchmarks the verifier rejected 3 of 13 reference fixes. Here it rejected none.
- **One VeriMend smoke run on `tt01`** ([summary](../../bench/results/faults-smoke-verimend-2026-11-21/summary.json)): a true fix, $0.0029, 14 steps, 64 s.
  - It traced the failing test to the commit that renamed `return_pending`, using `inspect_history`, the first time that tool had history to read.
  - It reverted the mapping and left the test alone.
  - This proves the wiring. It is not a result.
- **Spend:** $0.0029.

## Rejected

- **Seeds for raw data**, as the public jaffle shop has it: the agent could edit the data it is meant to escalate.
- **The newer jaffle-shop** (dbt packages, generated data): more room for faults, but untested on our dbt 1.10 pin and needing the network for packages.
- **The reference fix as the answer key.** The reference would then pass by construction.
- **Reusing the verifier's code in the grader**, and **comparing column types in the answer key** (a right fix may return double where the clean build returns decimal; contracts guard the types that matter).
- **A baseline on the fault suite in this phase** (with Aditya). Faults build into plain folders, so a container adapter can come later.

## Open, for Aditya

- **Tag `faults-v1`** after PR 5 merges, before the first scored run (SPEC §6).
- **The scored run** (VeriMend ×3 on all 20) is yours to start.
- **The baseline on the faults** needs a container adapter.
