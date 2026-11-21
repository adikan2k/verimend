with orders as (

    select * from {{ ref('orders') }}

),

daily as (

    select
        order_date,
        count(order_id) as number_of_orders,
        sum(amount) as revenue

    from orders

    group by order_date

)

select * from daily
