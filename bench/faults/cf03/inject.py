"""cf03: The payment_methods var that orders needs is gone from dbt_project.yml."""


def inject(ctx):
    ctx.replace(
        "dbt_project.yml",
        "vars:\n  payment_methods: ['credit_card', 'coupon', 'bank_transfer', 'gift_card']\n\n",
        "",
    )
