"""Check 5, row accounting: rows a LEFT join silently loses, or turns into NULL group keys.

A fix can build cleanly, change no test and still give wrong numbers. Most wrong numbers
need the task's intent to judge, and the verifier never has it. One kind does not: rows
that a model's own SQL reads and then drops at a LEFT join without saying so. This module
reads a changed model's compiled SQL with DuckDB's own parser (`json_serialize_sql`),
finds its LEFT joins, and counts those rows in the database the fix built.

Two rules, and nothing else:

A. Facts the spine drops. A LEFT join whose right side is a CTE or subquery that sums
   something (a rollup of facts). Its rows whose join key is not NULL and that match no
   row of the left side (the spine) are dropped from the output, and their sums with
   them. Fails if any such row carries a nonzero sum. Explained, so not counted: a spine
   that is filtered (WHERE, QUALIFY, HAVING, LIMIT, DISTINCT, SAMPLE or a non-LEFT join
   along it), and a join the base model already had (the loss is not the fix's doing).

B. Dangling keys that become NULL group keys. In a grouped select, a group key that comes
   from the right side of a LEFT join (or from anything joined through it). Rows whose
   join key is not NULL but found no partner, and that survive the select's own WHERE,
   end up grouped under NULL. Fails if there are such rows and the built model has a NULL
   in that key's column.

INNER, SEMI and ANTI joins, WHERE, QUALIFY and HAVING drop rows on purpose and are never
counted. Whatever this module cannot read or count (a parse error, a join condition that
is not plain column equalities, a query over its time limit) is unknown, and unknown
passes: the detail says what was not analysed and why.

The JSON tree differs between DuckDB versions (the task containers run 1.2 and 1.3, this
machine 2.0), so queries are never built by writing JSON. Each piece of the model's own
tree is rendered back to SQL by the same connection that parsed it.
"""

from __future__ import annotations

import contextlib
import copy
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

SUM_FUNCTIONS = frozenset({"sum", "sum_no_overflow", "fsum", "sumkahan", "kahan_sum", "ksum"})
FILTERING_MODIFIERS = frozenset({"LIMIT_MODIFIER", "LIMIT_PERCENT_MODIFIER", "DISTINCT_MODIFIER"})
QUERY_TIMEOUT_S = 10.0
MAX_QUERIES_PER_MODEL = 25
TOTAL_BUDGET_S = 120.0
_IGNORED_KEYS = frozenset({"alias", "query_location", "query_location_length"})


class Unknown(Exception):
    """Something this module cannot read or count. Never a failure of the fix."""


@dataclass
class Finding:
    rule: str  # "lost_facts" or "null_keys"
    model: str
    scope: str
    join: str
    rows: int
    measures: dict[str, float] = field(default_factory=dict)
    key: str | None = None

    def describe(self) -> str:
        where = f"in {self.scope}" if self.scope != "final" else "in the final select"
        if self.rule == "lost_facts":
            sums = ", ".join(f"{name} {value:,.2f}" for name, value in self.measures.items())
            return (
                f"{self.model}: {self.rows} row(s) of a summed input ({sums}) match no row "
                f"of the left side and are dropped by `{self.join}` {where}"
            )
        return (
            f"{self.model}: {self.rows} row(s) have a join key with no match in "
            f"`{self.join}` {where}, and are grouped under a NULL {self.key}"
        )


@dataclass
class ModelResult:
    model: str
    findings: list[Finding] = field(default_factory=list)
    exempt: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    joins: int = 0
    skipped: str | None = None  # why the model was not analysed at all


# ---- Reading the tree -------------------------------------------------------------


def _lower(name: Any) -> str:
    return str(name or "").lower()


def _strip(node: Any) -> Any:
    """A node without its alias and source positions, for comparing two expressions."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in _IGNORED_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _cte_body(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry["value"]
    return value.get("query_node") or value["query"]["node"]  # 2.0, then 1.x


def _ctes(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_lower(e["key"]): _cte_body(e) for e in (node.get("cte_map") or {}).get("map", [])}


def _set_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    if node.get("children"):
        return list(node["children"])  # 2.0
    return [n for n in (node.get("left"), node.get("right")) if n]  # 1.x


def _table_refs(table: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not table:
        return []
    if table.get("type") == "JOIN":
        return _table_refs(table.get("left")) + _table_refs(table.get("right"))
    return [table]


def _alias(table: dict[str, Any]) -> str:
    return _lower(table.get("alias") or table.get("table_name"))


def _relation(table: dict[str, Any]) -> str:
    if table.get("type") == "BASE_TABLE":
        return _lower(table.get("table_name"))
    return f"({_lower(table.get('type'))} {_alias(table)})"


def _cte_ref(table: dict[str, Any], ctes: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The CTE a table reference names, or None. A reference qualified by a schema or a
    catalog (`main.customers`) is a relation of the project even when a CTE has its name:
    `with customers as (select * from main.customers)` reads the table, not itself."""
    if table.get("type") != "BASE_TABLE":
        return None
    if table.get("schema_name") or table.get("catalog_name"):
        return None
    return ctes.get(_lower(table.get("table_name")))


def _leftmost(table: dict[str, Any]) -> dict[str, Any]:
    while table.get("type") == "JOIN":
        table = table["left"]
    return table


def _joins(table: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Joins of a FROM tree in the order they apply (innermost first)."""
    if not table or table.get("type") != "JOIN":
        return []
    return [*_joins(table.get("left")), table, *_joins(table.get("right"))]


def _column_refs(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("class") == "COLUMN_REF":
            found.append(node)
        elif node.get("class") == "SUBQUERY":
            return found  # columns inside a subquery belong to it
        for value in node.values():
            found += _column_refs(value)
    elif isinstance(node, list):
        for value in node:
            found += _column_refs(value)
    return found


def _qualifier(ref: dict[str, Any]) -> str | None:
    names = ref.get("column_names") or []
    return _lower(names[-2]) if len(names) >= 2 else None


def _has_sum(node: Any) -> bool:
    """A sum-family aggregate anywhere in an expression, outside window functions."""
    if isinstance(node, dict):
        if node.get("class") == "WINDOW":
            return False
        if node.get("class") == "FUNCTION" and _lower(node.get("function_name")) in SUM_FUNCTIONS:
            return True
        return any(_has_sum(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_sum(v) for v in node)
    return False


def _output_name(item: dict[str, Any]) -> str | None:
    if item.get("alias"):
        return _lower(item["alias"])
    if item.get("class") == "COLUMN_REF":
        return _lower(item["column_names"][-1])
    return None


def _equalities(
    join: dict[str, Any], right_alias: str, left_aliases: set[str]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The join's condition as (left column, right column) pairs, or Unknown."""
    if join.get("using_columns"):
        left_refs = _table_refs(join["left"])
        if len(left_refs) != 1:
            raise Unknown("USING over more than one relation")
        left = _alias(left_refs[0])
        return [
            (
                {
                    "class": "COLUMN_REF",
                    "type": "COLUMN_REF",
                    "alias": "",
                    "column_names": [left, col],
                },
                {
                    "class": "COLUMN_REF",
                    "type": "COLUMN_REF",
                    "alias": "",
                    "column_names": [right_alias, col],
                },
            )  # fmt: skip
            for col in join["using_columns"]
        ]
    condition = join.get("condition")
    if not condition:
        raise Unknown("a join with no condition")
    terms = condition["children"] if condition.get("type") == "CONJUNCTION_AND" else [condition]
    pairs = []
    for term in terms:
        if term.get("type") != "COMPARE_EQUAL":
            raise Unknown("a join condition that is not column equalities")
        a, b = term.get("left") or {}, term.get("right") or {}
        if a.get("class") != "COLUMN_REF" or b.get("class") != "COLUMN_REF":
            raise Unknown("a join condition that is not column equalities")
        qa, qb = _qualifier(a), _qualifier(b)
        if qa == right_alias and qb in left_aliases:
            pairs.append((b, a))
        elif qb == right_alias and qa in left_aliases:
            pairs.append((a, b))
        else:
            raise Unknown("a join condition whose columns are not qualified by side")
    return pairs


def _filtered(table: dict[str, Any], ctes: dict[str, dict[str, Any]], depth: int = 0) -> bool:
    """Whether a spine drops rows on purpose: a non-LEFT join in it, or a filter along
    the chain of CTEs its leftmost relation comes from."""
    if depth > 20:
        raise Unknown("a spine too deep to follow")
    for join in _joins(table):
        if join.get("join_type") != "LEFT" or join.get("ref_type") not in ("REGULAR", None):
            return True
    first = _leftmost(table)
    if first.get("type") == "SUBQUERY":
        body = first["subquery"]["node"]
    elif _cte_ref(first, ctes) is not None:
        body = _cte_ref(first, ctes) or {}
    else:
        return bool(first.get("sample"))  # a relation of the project: taken as it is
    if body.get("type") != "SELECT_NODE":
        raise Unknown("a spine built from a set operation")
    if body.get("where_clause") or body.get("having") or body.get("qualify") or body.get("sample"):
        return True
    if any(m.get("type") in FILTERING_MODIFIERS for m in body.get("modifiers") or []):
        return True
    if not body.get("from_table") or body["from_table"].get("type") == "EMPTY":
        return False
    return _filtered(body["from_table"], ctes, depth + 1)


def _selects(node: dict[str, Any], scope: str) -> list[tuple[str, dict[str, Any]]]:
    """Every SELECT in a query node, with a label: set operations and FROM subqueries."""
    if node.get("type") == "SET_OPERATION_NODE":
        found = []
        for i, child in enumerate(_set_children(node)):
            found += _selects(child, f"{scope}, part {i + 1}")
        return found
    if node.get("type") != "SELECT_NODE":
        return []
    found = [(scope, node)]
    for ref in _table_refs(node.get("from_table")):
        if ref.get("type") == "SUBQUERY":
            found += _selects(ref["subquery"]["node"], f"{scope}, subquery {_alias(ref)}")
    return found


def all_selects(top: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for name, body in _ctes(top).items():
        found += _selects(body, name)
    final = copy.copy(top)
    if final.get("cte_map"):
        final = {**final, "cte_map": {"map": []}}
    return found + _selects(final, "final")


def _signature(scope: str, join: dict[str, Any], pairs: list[tuple[dict, dict]]) -> tuple:
    left_rel = {_alias(t): _relation(t) for t in _table_refs(join["left"])}
    right = join["right"]
    keys = sorted(
        (
            left_rel.get(_qualifier(lc) or "", "?"),
            _lower(lc["column_names"][-1]),
            _lower(rc["column_names"][-1]),
        )  # fmt: skip
        for lc, rc in pairs
    )
    return (scope, _relation(_leftmost(join["left"])), _relation(right), tuple(keys))


def left_join_signatures(tree: dict[str, Any]) -> set[tuple]:
    """Every analysable LEFT join of a parsed statement, as a signature that survives
    renamed aliases: the scope, the spine's first relation, the joined relation, the keys."""
    signatures: set[tuple] = set()
    for scope, select in all_selects(tree):
        for join in _joins(select.get("from_table")):
            if join.get("join_type") != "LEFT":
                continue
            left_aliases = {_alias(t) for t in _table_refs(join["left"])}
            try:
                pairs = _equalities(join, _alias(join["right"]), left_aliases)
            except Unknown:
                continue
            signatures.add(_signature(scope, join, pairs))
    return signatures


# ---- Talking to DuckDB --------------------------------------------------------------


class Session:
    """One read-only connection to the database the fix built, with the limits."""

    def __init__(self, con: Any, deadline: float) -> None:
        self.con = con
        self.deadline = deadline
        self.queries = 0
        self._templates: dict[str, dict[str, Any]] = {}

    def parse(self, sql: str) -> dict[str, Any]:
        raw = self.con.execute("select json_serialize_sql(?)", [sql]).fetchone()[0]
        data = json.loads(raw)
        if data.get("error"):
            raise Unknown(f"the SQL does not parse: {str(data.get('error_message'))[:120]}")
        statements = data.get("statements") or []
        if len(statements) != 1:
            raise Unknown("not exactly one statement")
        return data

    def _template(self, sql: str) -> dict[str, Any]:
        if sql not in self._templates:
            self._templates[sql] = self.parse(sql)
        return copy.deepcopy(self._templates[sql])

    def _render(self, data: dict[str, Any]) -> str:
        return self.con.execute("select json_deserialize_sql(?)", [json.dumps(data)]).fetchone()[0]

    def _strip_prefix(self, text: str, prefix: str) -> str:
        if not text.upper().startswith(prefix.upper()):
            raise Unknown("an expression could not be rendered back to SQL")
        return text[len(prefix) :].strip()

    def table(self, node: dict[str, Any]) -> str:
        data = self._template("SELECT * FROM __vm_t")
        data["statements"][0]["node"]["from_table"] = node
        return self._strip_prefix(self._render(data), "SELECT * FROM ")

    def expr(self, node: dict[str, Any]) -> str:
        data = self._template("SELECT __vm_e")
        data["statements"][0]["node"]["select_list"] = [{**node, "alias": ""}]
        return self._strip_prefix(self._render(data), "SELECT ")

    def with_prefix(self, top: dict[str, Any]) -> str:
        if not (top.get("cte_map") or {}).get("map"):
            return ""
        data = self._template("SELECT 1 AS __vm_x")
        data["statements"][0]["node"]["cte_map"] = top["cte_map"]
        text = self._render(data)
        cut = text.rfind("SELECT 1 AS __vm_x")
        if cut <= 0:
            raise Unknown("the model's CTEs could not be rendered back to SQL")
        return text[:cut].rstrip() + " "

    def count(self, sql: str) -> tuple[Any, ...]:
        if self.queries >= MAX_QUERIES_PER_MODEL:
            raise Unknown(f"more than {MAX_QUERIES_PER_MODEL} queries for one model")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Unknown(f"the {TOTAL_BUDGET_S:.0f} s budget for this check ran out")
        self.queries += 1
        timer = threading.Timer(min(QUERY_TIMEOUT_S, left), self.con.interrupt)
        timer.start()
        try:
            row = self.con.execute(sql).fetchone()
        except Exception as error:  # duckdb.Error, InterruptException
            raise Unknown(f"a count query failed: {type(error).__name__}") from error
        finally:
            timer.cancel()
        return tuple(row or ())


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# ---- The two rules ---------------------------------------------------------------------


def _measures(right: dict[str, Any], ctes: dict[str, dict[str, Any]]) -> list[str]:
    """The summed output columns of a LEFT join's right side, if it is a rollup."""
    if right.get("type") == "SUBQUERY":
        body = right["subquery"]["node"]
    elif _cte_ref(right, ctes) is not None:
        body = _cte_ref(right, ctes) or {}
    else:
        return []
    if body.get("type") != "SELECT_NODE":
        return []
    names = []
    for item in body.get("select_list") or []:
        name = _output_name(item)
        if name and _has_sum(item):
            names.append(name)
    return names


def _lost_facts(
    s: Session, model: str, scope: str, join: dict, pairs: list, measures: list[str], prefix: str
) -> Finding | None:
    right = join["right"]
    r = _q(_alias(right))
    nonzero = " + ".join(f"coalesce(abs(try_cast({r}.{_q(m)} AS DOUBLE)), 0)" for m in measures)
    sums = ", ".join(f"sum(coalesce(try_cast({r}.{_q(m)} AS DOUBLE), 0))" for m in measures)
    keys = " AND ".join(f"{s.expr(rc)} IS NOT NULL" for _, rc in pairs)
    condition = " AND ".join(f"{s.expr(lc)} = {s.expr(rc)}" for lc, rc in pairs)
    sql = (
        f"{prefix}SELECT count(*), {sums} FROM {s.table(right)} "
        f"WHERE {keys} AND ({nonzero}) > 0 "
        f"AND NOT EXISTS (SELECT 1 FROM {s.table(join['left'])} WHERE {condition})"
    )
    row = s.count(sql)
    if not row or not row[0]:
        return None
    return Finding("lost_facts", model, scope, _join_text(s, join), int(row[0]),
                   {m: float(v or 0) for m, v in zip(measures, row[1:], strict=False)})  # fmt: skip


def _join_text(s: Session, join: dict[str, Any]) -> str:
    try:
        text = f"left join {s.table(join['right'])}"
    except Unknown:
        text = f"left join {_alias(join['right'])}"
    return text[:120]


def _position(constant: dict[str, Any]) -> int | None:
    """The number in GROUP BY 1: under "value" in DuckDB 1.x, "literal" in 2.0."""
    raw = (constant.get("value") or {}).get("value")
    if raw is None:
        literal = constant.get("literal") or {}
        raw = literal.get("text") if literal.get("kind") == "INTEGER" else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _group_keys(select: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(output column, expression) for each group key that can be named."""
    items = select.get("select_list") or []
    by_name = {_output_name(i): i for i in items if _output_name(i)}
    keys = []
    for group in select.get("group_expressions") or []:
        item = None
        if group.get("type") == "VALUE_CONSTANT":
            position = _position(group)
            if position is not None and 1 <= position <= len(items):
                item = items[position - 1]
        elif group.get("class") == "COLUMN_REF" and len(group["column_names"]) == 1:
            item = by_name.get(_lower(group["column_names"][0]))
        if item is None:
            stripped = _strip(group)
            item = next((i for i in items if _strip(i) == stripped), None)
        name = _output_name(item) if item else None
        if name and item is not None:
            keys.append((name, item))
    return keys


def _null_keys(
    s: Session, model: str, scope: str, select: dict, join: dict, pairs: list,
    nullable: set[str], prefix: str, output: str | None, columns: set[str], result: ModelResult,
) -> Finding | None:  # fmt: skip
    affected = [
        name for name, item in _group_keys(select)
        if any(_qualifier(ref) in nullable for ref in _column_refs(item))
    ]  # fmt: skip
    if not affected:
        return None
    where = select.get("where_clause")
    conditions = [f"({s.expr(where)})"] if where else []
    conditions += [f"{s.expr(rc)} IS NULL" for _, rc in pairs]
    conditions += [f"{s.expr(lc)} IS NOT NULL" for lc, _ in pairs]
    sql = (
        f"{prefix}SELECT count(*) FROM {s.table(select['from_table'])} "
        f"WHERE {' AND '.join(conditions)}"
    )
    row = s.count(sql)
    dangling = int(row[0]) if row and row[0] else 0
    if not dangling:
        return None
    if output is None:
        raise Unknown(f"{dangling} row(s) found no partner, but the built model could not be read")
    readable = [name for name in affected if name in columns]
    if not readable:
        raise Unknown(f"{dangling} row(s) found no partner, but no group key column is in the "
                      "built model under its own name")  # fmt: skip
    for name in readable:
        nulls = s.count(f"SELECT count(*) FROM {output} WHERE {_q(name)} IS NULL")
        if nulls and nulls[0]:
            return Finding("null_keys", model, scope, _join_text(s, join), dangling, key=name)
    result.notes.append(
        f"{scope}: {dangling} row(s) found no partner in `{_join_text(s, join)}`, but "
        f"{', '.join(readable)} is never NULL in the built model"
    )
    return None


def analyse(
    con: Any,
    model: str,
    sql: str,
    base_sql: str | None,
    output: str | None,
    deadline: float,
) -> ModelResult:
    """Rules A and B over one model's compiled SQL, counted on `con` (read-only, the
    database the fix built). `output` is the built relation, quoted; `base_sql` the base
    model's compiled SQL, if it had one."""
    result = ModelResult(model)
    s = Session(con, deadline)
    try:
        tree = s.parse(sql)
    except Unknown as why:
        result.unknown.append(str(why))
        return result
    top = tree["statements"][0]["node"]
    base_signatures: set[tuple] = set()
    if base_sql:
        with contextlib.suppress(Unknown):  # no exemption without a readable base
            base_signatures = left_join_signatures(s.parse(base_sql)["statements"][0]["node"])
    ctes = _ctes(top)
    try:
        prefix = s.with_prefix(top)
    except Unknown as why:
        result.unknown.append(str(why))
        return result
    columns: set[str] = set()
    if output is not None:
        try:
            described = s.con.execute(f"SELECT * FROM {output} LIMIT 0").description or []
            columns = {_lower(d[0]) for d in described}
        except Exception as error:
            result.unknown.append(f"the built model could not be read: {type(error).__name__}")
            output = None
    for scope, select in all_selects(top):
        if (select.get("cte_map") or {}).get("map") and select is not top:
            result.unknown.append(f"{scope}: CTEs nested inside a CTE")
            continue
        grouped = bool(select.get("group_expressions")) and not (
            select.get("having") or select.get("qualify")
        )
        joins = _joins(select.get("from_table"))
        for index, join in enumerate(joins):
            if join.get("join_type") != "LEFT":
                continue
            if join.get("ref_type") not in ("REGULAR", None):
                result.unknown.append(f"{scope}: a {_lower(join.get('ref_type'))} join")
                continue
            right_alias = _alias(join["right"])
            left_aliases = {_alias(t) for t in _table_refs(join["left"])}
            try:
                pairs = _equalities(join, right_alias, left_aliases)
                result.joins += 1
                measures = _measures(join["right"], ctes)
                if measures and not _filtered(join["left"], ctes):
                    if _signature(scope, join, pairs) in base_signatures:
                        result.exempt.append(f"{scope}: `{_join_text(s, join)}` was in the base")
                    else:
                        found = _lost_facts(s, model, scope, join, pairs, measures, prefix)
                        if found:
                            result.findings.append(found)
                if grouped:
                    nullable = _nullable_through(right_alias, joins[index + 1 :])
                    found = _null_keys(s, model, scope, select, join, pairs, nullable, prefix,
                                       output, columns, result)  # fmt: skip
                    if found:
                        result.findings.append(found)
            except Unknown as why:
                result.unknown.append(f"{scope}: {why}")
    return result


def _nullable_through(right_alias: str, later: list[dict[str, Any]]) -> set[str]:
    """The aliases that are NULL when a LEFT join finds no partner: its right side, and
    whatever is joined later on a condition that reads one of them."""
    nullable = {right_alias}
    for join in later:
        refs = {_qualifier(r) for r in _column_refs(join.get("condition"))}
        if join.get("using_columns") or nullable & refs:
            nullable.add(_alias(join["right"]))
    return nullable
