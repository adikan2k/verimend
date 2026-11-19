"""Models, caps and paths, read from the environment and from `.env`.

This is the only place model names and spend caps have defaults. The API key is held as a
secret, never printed, and handed to the Agent SDK explicitly by the caller.
"""

import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def default_home() -> Path:
    """Where sandboxes, database copies and benchmark checkouts live.

    Kept outside the repository on purpose, so a synced folder such as OneDrive never
    locks or re-uploads worktrees and DuckDB files.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "verimend"
    return Path.home() / ".local" / "share" / "verimend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = None
    model_default: str = "claude-haiku-5-5"
    model_compare: str = "claude-sonnet-5-5"

    max_usd_per_run: float = Field(default=0.25, gt=0)
    max_usd_per_batch: float = Field(default=5.00, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    max_steps: int = Field(default=40, ge=1)
    max_prompt_tokens: int = Field(default=90_000, ge=1)
    tool_output_chars: int = Field(default=4_000, ge=200)

    dbt_project_dir: Path = Path("demo/jaffle_shop")
    dbt_profiles_dir: Path = Path("demo/jaffle_shop")
    duckdb_path: Path = Path("demo/jaffle_shop/warehouse.duckdb")
    verimend_home: Path = Field(default_factory=default_home)

    github_token: SecretStr | None = None

    def require_api_key(self) -> str:
        """The API key, or a ConfigError. Paid code paths call this before anything else.

        Without a key the Agent SDK's CLI would fall back to whatever login it finds on
        the machine, and the run would be billed somewhere we are not measuring.
        """
        if self.anthropic_api_key is None:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example); "
                "do not set it as a system environment variable."
            )
        return self.anthropic_api_key.get_secret_value()


def load_settings() -> Settings:
    return Settings()
