with payments as (

    select * from {{ ref('stg_payments') }}

),

-- Returned orders should not count towards revenue.
kept_orders as (

    select * from {{ ref('stg_orders') }}
    where status <> 'returned'

)

select
    date_trunc('month', o.order_date) as order_month,
    sum(p.amount) as revenue

from payments p
inner join kept_orders o on p.order_id = o.order_id

group by 1
