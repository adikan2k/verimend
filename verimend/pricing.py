"""Price table and cost arithmetic.

Prices are US dollars per million tokens, copied from the Anthropic pricing page on the
date below. Re-check the page before any batch that will be published, and update
CHECKED_ON in the same commit.
"""

from dataclasses import dataclass
from datetime import date

CHECKED_ON = date(2026, 11, 19)
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

PER_MILLION = 1_000_000


class UnknownModelError(KeyError):
    """No price is recorded for this model, so its cost cannot be computed."""


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    # Above this many prompt tokens in ONE request, every price is multiplied.
    long_prompt_threshold: int | None = None
    long_prompt_multiplier: float = 1.0


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(
        input=0.10,
        output=0.50,
        cache_read=0.01,
        cache_write_5m=0.125,
        cache_write_1h=0.20,
        long_prompt_threshold=100_000,
        long_prompt_multiplier=5.0,
    ),
    "claude-sonnet-5-5": Price(
        input=2.00,
        output=10.00,
        cache_read=0.10,
        cache_write_5m=2.50,
        cache_write_1h=4.00,
    ),
}


@dataclass(frozen=True)
class Usage:
    """Token counts for one request to the model."""

    input_tokens: int = 0  # prompt tokens that were neither read from nor written to cache
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Everything the model read. This is what the long-prompt threshold counts."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )


def price_for(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(
            f"No price recorded for model {model!r}. Add it to verimend/pricing.py."
        ) from None


def is_long_prompt(model: str, usage: Usage) -> bool:
    threshold = price_for(model).long_prompt_threshold
    return threshold is not None and usage.prompt_tokens > threshold


def request_cost_usd(model: str, usage: Usage) -> float:
    """Cost of one request. The long-prompt tier is decided per request, never per run."""
    price = price_for(model)
    multiplier = price.long_prompt_multiplier if is_long_prompt(model, usage) else 1.0
    dollars = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_5m_tokens * price.cache_write_5m
        + usage.cache_write_1h_tokens * price.cache_write_1h
    ) / PER_MILLION
    return dollars * multiplier


def run_cost_usd(model: str, requests: list[Usage]) -> float:
    return sum(request_cost_usd(model, usage) for usage in requests)
