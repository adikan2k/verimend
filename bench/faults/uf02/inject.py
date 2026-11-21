"""uf02: raw.payments lost its amount column, with nothing to replace it."""


def inject(ctx):
    ctx.sql("alter table raw.payments drop column amount")
