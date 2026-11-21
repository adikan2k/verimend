"""cf01: stg_payments was renamed, and orders still refers to the old name."""

OLD, NEW = "stg_payments", "stg_payment_events"


def inject(ctx):
    ctx.rename(f"models/staging/{OLD}.sql", f"models/staging/{NEW}.sql")
    ctx.replace("models/staging/schema.yml", f"  - name: {OLD}\n", f"  - name: {NEW}\n")
    for rel in (
        "models/marts/customers.sql",
        "models/marts/payment_method_totals.sql",
        "tests/assert_order_amount_matches_payments.sql",
        "tests/assert_no_negative_payments.sql",
    ):
        ctx.replace(rel, f"ref('{OLD}')", f"ref('{NEW}')")
