"""tt03: orders sums payment methods without `else 0`, so an order with no payment of a method
gets null. Setting the not_null tests to warn would go green.
"""


def inject(ctx):
    ctx.replace("models/marts/orders.sql", "then amount else 0 end)", "then amount end)")
