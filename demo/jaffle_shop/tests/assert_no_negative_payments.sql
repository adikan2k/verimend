-- A payment is never negative; refunds are recorded as returned orders.
select payment_id, order_id, amount
from {{ ref('stg_payments') }}
where amount < 0
