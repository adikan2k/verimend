-- The payment method totals reconcile to the raw payments, which are in cents.
with reported as (

    select sum(total_amount) * 100 as cents
    from {{ ref('payment_method_totals') }}

),

loaded as (

    select sum(amount) as cents
    from {{ source('raw', 'payments') }}

)

select reported.cents as reported_cents, loaded.cents as loaded_cents
from reported, loaded
where reported.cents <> loaded.cents
