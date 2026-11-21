"""lg01: customers no longer aggregates payments per customer, so the join fans out."""


def inject(ctx):
    rel = "models/marts/customers.sql"
    ctx.replace(rel, "sum(amount) as total_amount", "payments.amount as total_amount")
    ctx.replace(rel, "\n\n    group by orders.customer_id\n", "\n")
