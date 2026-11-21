"""lg04: daily_revenue groups by date and status, so a day can appear more than once."""


def inject(ctx):
    ctx.replace(
        "models/marts/daily_revenue.sql", "group by order_date", "group by order_date, status"
    )
