VeriMend looked at the failed dbt build at `29a527b` and did not open a pull request.

**Escalated.** The code looks right; a person needs to look at this.

## Root cause

**Bad data** in `raw.payments (raw_data/payments.csv) via stg_payments`. The November payments export (commit 29a527b, 'Load the November payments export') added three negative payments (ids 114, 115, 116 for orders 21, 57, 83). stg_payments converts cents to dollars correctly, so the model is right. The failing test assert_no_negative_payments correctly rejects them: the project rule is that payments are never negative and refunds are recorded as returned orders. Those three orders are completed/shipped, not returned, so there is no valid refund record behind the negative rows. The fix belongs with whoever owns the ingestion/export, not in the project.

## Evidence

- **log** `read_failure`: Only failure: test.jaffle_shop.assert_no_negative_payments, got 3 results. All other tests pass or are skipped.
- **query** `select ... from stg_payments where amount < 0`: Three rows: payment 114 (order 21, credit_card, -15.00), 115 (order 57, bank_transfer, -23.00), 116 (order 83, coupon, -9.00).
- **diff** `raw_data/payments.csv, commit 29a527b`: Lines 114,21,credit_card,-1500; 115,57,bank_transfer,-2300; 116,83,coupon,-900 were appended by the November export. Earlier rows are all non-negative.
- **lineage** `models/staging/stg_payments.sql and macros/cents_to_dollars.sql`: Conversion is (amount / 100)::numeric(16,2) with no sign handling. The model faithfully passes the source values through, so the logic is correct.
- **query** `stg_orders joined to stg_payments for orders 21, 57, 83`: Those orders are status completed (21, 57) and shipped (83), not returned. Each also has a positive payment (24, 65, 95) alongside the negative row, so no refund record explains the negatives.

## Cost

$0.0037 on claude-haiku-5-5, 17 steps, 30 s, 0 verifier runs.
