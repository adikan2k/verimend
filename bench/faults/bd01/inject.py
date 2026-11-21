"""bd01: A few payments, chosen by the seed, have negative amounts."""


def inject(ctx):
    ids = [row[0] for row in ctx.sql("select id from raw.payments order by id")]
    for payment_id in sorted(ctx.rng.sample(ids, 3)):
        ctx.sql("update raw.payments set amount = -amount where id = ?", [payment_id])
