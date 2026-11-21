"""sd01: raw.orders.user_id is renamed to customer_id upstream."""


def inject(ctx):
    ctx.sql("alter table raw.orders rename column user_id to customer_id")
