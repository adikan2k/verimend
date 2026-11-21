-- Seeded bug for the tool tests: the column is amount, not amout.
select
    customer_id,
    sum(amout) as total_amount
from {{ ref('stg_orders') }}
group by customer_id
