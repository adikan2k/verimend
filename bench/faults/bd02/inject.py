"""bd02: A few orders, chosen by the seed, are dated in 2099."""


def inject(ctx):
    ids = [row[0] for row in ctx.sql("select id from raw.orders order by id")]
    for order_id in sorted(ctx.rng.sample(ids, 3)):
        day = ctx.rng.randint(1, 28)
        ctx.sql(
            f"update raw.orders set order_date = date '2099-01-{day:02d}' where id = ?", [order_id]
        )
