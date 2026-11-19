from pathlib import Path

import pytest
from pydantic import ValidationError

from verimend.config import ConfigError, Settings


def bare(**overrides: object) -> Settings:
    """Settings built without reading any .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def no_key_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_paid_paths_refuse_to_run_without_a_key() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        bare().require_api_key()


def test_an_empty_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ConfigError):
        bare().require_api_key()


def test_key_is_returned_when_set_and_never_shown_in_repr() -> None:
    settings = bare(anthropic_api_key="test-key-not-real")
    assert settings.require_api_key() == "test-key-not-real"
    assert "test-key-not-real" not in repr(settings)


def test_defaults_match_the_spec() -> None:
    settings = bare()
    assert settings.max_usd_per_run == 0.25
    assert settings.max_attempts == 3
    assert settings.max_steps == 40
    assert settings.max_prompt_tokens == 90_000
    assert settings.tool_output_chars == 4_000


def test_caps_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_USD_PER_RUN", "0.10")
    monkeypatch.setenv("MODEL_DEFAULT", "some-other-model")
    settings = bare()
    assert settings.max_usd_per_run == 0.10
    assert settings.model_default == "some-other-model"


def test_a_zero_dollar_cap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        bare(max_usd_per_run=0)


def test_sandboxes_live_outside_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VERIMEND_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert bare().verimend_home == tmp_path / "verimend"
