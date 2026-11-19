import pytest

from verimend.config import Settings
from verimend.pricing import (
    PRICES,
    UnknownModelError,
    Usage,
    is_long_prompt,
    request_cost_usd,
    run_cost_usd,
)

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def test_both_configured_models_have_a_price() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_default in PRICES
    assert settings.model_compare in PRICES


def test_haiku_plain_request() -> None:
    # 10,000 in at $0.10/M = $0.0010; 2,000 out at $0.50/M = $0.0010
    usage = Usage(input_tokens=10_000, output_tokens=2_000)
    assert request_cost_usd(HAIKU, usage) == pytest.approx(0.0020)


def test_haiku_cached_request() -> None:
    # 1,000 in = $0.0001; 500 out = $0.00025; 40,000 cache read at $0.01/M = $0.0004;
    # 8,000 cache write (5 min) at $0.125/M = $0.0010
    usage = Usage(
        input_tokens=1_000,
        output_tokens=500,
        cache_read_tokens=40_000,
        cache_write_5m_tokens=8_000,
    )
    assert request_cost_usd(HAIKU, usage) == pytest.approx(0.00175)


def test_haiku_long_prompt_costs_five_times_as_much() -> None:
    # The threshold counts cached tokens too: 60,000 + 40,001 = 100,001 prompt tokens.
    usage = Usage(input_tokens=60_000, output_tokens=1_000, cache_read_tokens=40_001)
    assert is_long_prompt(HAIKU, usage)
    standard = (60_000 * 0.10 + 1_000 * 0.50 + 40_001 * 0.01) / 1_000_000
    assert request_cost_usd(HAIKU, usage) == pytest.approx(5 * standard)


def test_haiku_exactly_at_the_threshold_is_standard_price() -> None:
    usage = Usage(input_tokens=100_000)
    assert not is_long_prompt(HAIKU, usage)
    assert request_cost_usd(HAIKU, usage) == pytest.approx(0.01)


def test_long_prompt_tier_is_per_request_not_per_run() -> None:
    # Two 60,000-token requests total 120,000 tokens but neither passes the threshold.
    requests = [Usage(input_tokens=60_000), Usage(input_tokens=60_000)]
    assert run_cost_usd(HAIKU, requests) == pytest.approx(0.012)


def test_sonnet_has_no_long_prompt_tier() -> None:
    usage = Usage(input_tokens=500_000, output_tokens=10_000)
    assert not is_long_prompt(SONNET, usage)
    assert request_cost_usd(SONNET, usage) == pytest.approx(1.00 + 0.10)


def test_unknown_model_is_an_error_not_zero_dollars() -> None:
    with pytest.raises(UnknownModelError):
        request_cost_usd("some-model-we-never-priced", Usage(input_tokens=1))
