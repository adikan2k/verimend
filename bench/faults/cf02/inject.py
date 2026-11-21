"""cf02: The cents_to_dollars macro gained a scale argument with no default; its caller does
not pass one.
"""

MACRO = """{% macro cents_to_dollars(column_name, scale) -%}
    ({{ column_name }} / 100)::numeric(16, {{ scale }})
{%- endmacro %}
"""


def inject(ctx):
    ctx.write("macros/cents_to_dollars.sql", MACRO)
