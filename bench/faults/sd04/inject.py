"""sd04: raw.customers.id changes from integer to text upstream."""


def inject(ctx):
    ctx.sql("alter table raw.customers alter column id type varchar")
