-- The shop opened on 2018-01-01, and no order is dated in the future.
select order_id, order_date
from {{ ref('stg_orders') }}
where order_date < date '2018-01-01' or order_date > current_date
