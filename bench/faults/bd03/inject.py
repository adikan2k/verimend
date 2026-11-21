"""bd03: A few new payments, made up by the seed, point at orders that do not exist."""

METHODS = ["credit_card", "coupon", "bank_transfer", "gift_card"]


def inject(ctx):
    (last,) = ctx.sql("select max(id) from raw.payments")[0]
    for n in range(1, 4):
        order_id = ctx.rng.randint(9000, 9999)
        method = ctx.rng.choice(METHODS)
        amount = ctx.rng.randint(1, 30) * 100
        ctx.sql(
            "insert into raw.payments values (?, ?, ?, ?)", [last + n, order_id, method, amount]
        )
