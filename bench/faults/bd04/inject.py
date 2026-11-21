"""bd04: A few orders, chosen by the seed, have no customer."""


def inject(ctx):
    ids = [row[0] for row in ctx.sql("select id from raw.orders order by id")]
    for order_id in sorted(ctx.rng.sample(ids, 3)):
        ctx.sql("update raw.orders set user_id = null where id = ?", [order_id])
