-- Daily revenue adds up to the orders it is built from.
with daily as (

    select sum(revenue) as revenue, sum(number_of_orders) as number_of_orders
    from {{ ref('daily_revenue') }}

),

orders as (

    select sum(amount) as revenue, count(order_id) as number_of_orders
    from {{ ref('orders') }}

)

select *
from daily, orders
where daily.revenue <> orders.revenue
   or daily.number_of_orders <> orders.number_of_orders
