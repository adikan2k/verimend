with source as (

    select * from {{ source('raw', 'payments') }}

),

renamed as (

    select
        id as payment_id,
        order_id,
        payment_method,

        -- `amount` is stored in cents, so we convert it to dollars
        {{ cents_to_dollars('amount') }} as amount

    from source

)

select * from renamed
