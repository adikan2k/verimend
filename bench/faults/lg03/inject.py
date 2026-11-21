"""lg03: stg_payments casts the amount in cents instead of converting it to dollars."""


def inject(ctx):
    ctx.replace(
        "models/staging/stg_payments.sql",
        "{{ cents_to_dollars('amount') }} as amount",
        "amount::numeric(16, 2) as amount",
    )
