"""uf01: The raw.payments table is missing."""


def inject(ctx):
    ctx.sql("drop table raw.payments")
