with payments as (

    select * from {{ ref('stg_payments') }}

),

totals as (

    select
        payment_method,
        count(payment_id) as number_of_payments,
        sum(amount) as total_amount

    from payments

    group by payment_method

)

select * from totals
