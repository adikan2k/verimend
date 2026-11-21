"""sd03: raw.orders.order_date changes from date to text (ISO dates) upstream."""


def inject(ctx):
    ctx.sql("alter table raw.orders alter column order_date type varchar")
