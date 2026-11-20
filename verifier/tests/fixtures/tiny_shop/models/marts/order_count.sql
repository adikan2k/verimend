-- Outside customer_totals' lineage: the collateral-damage check compares it.
{{ config(materialized='table') }}
select count(*) as orders from {{ ref('raw_orders') }}
