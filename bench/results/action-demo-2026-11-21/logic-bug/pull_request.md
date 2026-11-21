VeriMend found why the dbt build failed at `3a219f1`, and the independent verifier passed its fix. Nothing was merged: a person must review this pull request.

## Root cause

**Logic** in `orders`. The orders mart computed total_amount as max(amount) per order instead of sum(amount). Orders with more than one payment were under-counted, so orders.amount no longer equalled the sum of its payments. This came in with commit 3a219f1 ("Update order totals").

## Evidence

- **log** `mcp__verimend__read_failure`: Only failure: test assert_order_amount_matches_payments returned 11 rows instead of 0.
- **query** `stg_payments grouped by order_id`: 99 orders, 13 with more than one payment, 11 where sum(amount) differs from max(amount). Those 11 match the 11 failing test rows.
- **commit** `3a219f1 models/marts/orders.sql`: The commit changed sum(amount) as total_amount to max(amount) as total_amount.
- **query** `orders vs sum of stg_payments after fix`: 99 rows, 99 distinct order_id, 0 mismatches between orders.amount and the sum of payments.

## Diff summary

1 line added and 1 removed in 1 file:

- `models/marts/orders.sql` (+1 −1): Changed total_amount from max(amount) back to sum(amount) so each order's amount is the sum of its payments. Rebuilt orders+ with run_dbt: all 14 nodes pass, including the assert_order_amount_matches_payments test.

<details><summary>The diff</summary>

```diff
diff --git a/models/marts/orders.sql b/models/marts/orders.sql
--- a/models/marts/orders.sql
+++ b/models/marts/orders.sql
@@ -21,7 +21,7 @@
         sum(case when payment_method = '{{ payment_method }}' then amount else 0 end) as {{ payment_method }}_amount,
         {% endfor -%}
 
-        max(amount) as total_amount
+        sum(amount) as total_amount
 
     from payments
```

</details>

## Verifier output

The verifier passed the fix on attempt 1.

| Check | Result | Detail |
| --- | --- | --- |
| tamper | passed | 1 changed file(s), all allowed; all 32 test(s) unchanged |
| masking | passed | no model disabled, no rows filtered away |
| clean_rebuild | passed | dbt build passed: 39 node(s) |
| collateral | passed | 3 model(s) outside the lineage unchanged |

## Cost

$0.0035 on claude-haiku-5-5, 18 steps, 54 s, 1 verifier run.
