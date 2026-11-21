-- Every order's amount is the sum of its payments.
with paid as (

    select order_id, sum(amount) as paid
    from {{ ref('stg_payments') }}
    group by order_id

)

select orders.order_id, orders.amount, paid.paid
from {{ ref('orders') }} as orders
left join paid on orders.order_id = paid.order_id
where coalesce(orders.amount, 0) <> coalesce(paid.paid, 0)
