"""report: the agent's structured verdict (SPEC section 4). Calling it ends the run."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from verimend.tools.base import ToolContext, ToolResult


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RootCause(_Strict):
    category: Literal["schema_drift", "logic", "bad_data", "config", "missing_source", "unknown"]
    model: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=800, description="One or two sentences.")


class Evidence(_Strict):
    kind: Literal["log", "query", "lineage", "commit", "diff"]
    ref: str = Field(min_length=1, max_length=300, description="What was looked at.")
    finding: str = Field(min_length=1, max_length=600, description="What it showed.")


class Change(_Strict):
    path: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=600, description="What changed and why.")


class Verdict(_Strict):
    verdict: Literal["fixed", "not_fixed", "escalate"]
    root_cause: RootCause
    evidence: list[Evidence] = Field(min_length=1, max_length=12)
    changes: list[Change] = Field(max_length=20)
    confidence: Literal["low", "medium", "high"]


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic's JSON Schema with every $ref replaced by its definition, no $defs."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k not in {"$defs", "title"}}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


VERDICT_SCHEMA: dict[str, Any] = _inline_refs(Verdict.model_json_schema())

RECORDED = "Report recorded. The run is over: end your turn now and call no more tools."


async def report(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.report is not None:
        return ctx.result("report was already called; the first report stands.", True)
    try:
        verdict = Verdict.model_validate(args)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'report'}: {e['msg']}" for e in error.errors()
        )
        return ctx.result(f"Invalid report; fix it and call report again: {problems}", True)
    ctx.report = verdict.model_dump()
    return ctx.result(RECORDED)
