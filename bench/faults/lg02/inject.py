"""lg02: orders takes the largest payment instead of the sum of an order's payments."""


def inject(ctx):
    ctx.replace(
        "models/marts/orders.sql", "sum(amount) as total_amount", "max(amount) as total_amount"
    )
