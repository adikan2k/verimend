"""report: the agent's structured verdict (SPEC section 4), and the verifier loop.

A `fixed` report goes to the verifier. If it passes, the run is over. If it fails and
attempts remain, the agent hears which check failed and why, and may try again. After
the last failed attempt the run ends as not fixed. `escalate` and `not_fixed` end the run
without the verifier.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from verimend.tools.base import ToolContext, ToolResult
from verimend.tools.verify import feedback_text

DIFF_CHARS = 20_000  # how much of each attempt's diff the trace keeps


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

END = "The run is over: end your turn now and call no more tools."
RECORDED = f"Report recorded. {END}"
VERIFIED = f"The verifier passed the fix. Report recorded. {END}"


def _accept(ctx: ToolContext, final_verdict: str | None, text: str) -> ToolResult:
    ctx.accepted = True
    ctx.final_verdict = final_verdict
    return ctx.result(text)


async def report(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.accepted:
        return ctx.result("The run is already over; the accepted report stands.", True)
    try:
        verdict = Verdict.model_validate(args)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'report'}: {e['msg']}" for e in error.errors()
        )
        return ctx.result(f"Invalid report; fix it and call report again: {problems}", True)
    ctx.report = verdict.model_dump()
    if verdict.verdict != "fixed" or ctx.verifier is None:
        return _accept(ctx, verdict.verdict, RECORDED)

    attempt = len(ctx.attempts) + 1
    try:
        result = await ctx.verifier.check()
    except Exception as error:  # the verifier could not run: a harness problem
        ctx.verifier_error = f"{type(error).__name__}: {error}"[:800]
        return _accept(ctx, None, f"The verifier could not run. {END}")
    record = {k: v for k, v in result.items() if k != "diff"}
    record["attempt"] = attempt
    record["diff"] = str(result.get("diff", ""))[:DIFF_CHARS]
    ctx.attempts.append(record)
    if result.get("passed"):
        return _accept(ctx, "fixed", VERIFIED)
    if attempt >= ctx.max_attempts:
        last = (
            f"The verifier rejected the fix (attempt {attempt} of {ctx.max_attempts}, "
            f"check {result.get('failed_check')}). No attempts are left; the run ends as not "
            f"fixed. {END}"
        )
        return _accept(ctx, "not_fixed", last)
    return ctx.result(feedback_text(result, attempt, ctx.max_attempts, ctx.feedback))
