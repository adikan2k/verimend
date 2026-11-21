"""sd02: raw.payments.payment_method is renamed to method upstream."""


def inject(ctx):
    ctx.sql("alter table raw.payments rename column payment_method to method")
