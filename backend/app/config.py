"""Application settings, loaded from the environment (see repo-root .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosted Postgres providers hand out `postgresql://...`, which SQLAlchemy
# routes to psycopg2. This project installs psycopg 3 (the `postgres` extra),
# so a URL pasted verbatim from Supabase or Render fails at import with
# "No module named 'psycopg2'" — in the API, in Alembic, and in every job.
# Normalising here rather than asking four separate places to remember it.
_PSYCOPG2_SCHEMES = ("postgresql://", "postgres://")


class Settings(BaseSettings):
    """Runtime configuration.

    Defaults are chosen so that `uvicorn app.main:app` works with no .env at
    all: SQLite on disk, no Steam key. Every Phase 0 Steam endpoint we use is
    public and unkeyed — STEAM_API_KEY is here because later phases need it.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./aaa_launch_predictor.db"
    steam_api_key: str | None = None
    anthropic_api_key: str | None = None

    # Steam requests are region- and language-sensitive: price and the
    # release-date string both change with these, so they are pinned rather
    # than left to whatever region the caller's IP resolves to.
    steam_country_code: str = "us"
    steam_language: str = "english"
    steam_timeout_seconds: float = 15.0

    # Comma-separated list of origins allowed to call the API from a browser.
    cors_allow_origins: str = "http://localhost:3000"

    @property
    def sqlalchemy_url(self) -> str:
        """`database_url` with a driver SQLAlchemy can actually load.

        `postgres://` is also accepted because several providers still emit
        the older form.
        """
        return normalize_database_url(self.database_url)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


def normalize_database_url(url: str) -> str:
    """Point a bare Postgres URL at psycopg 3, leaving everything else alone."""
    for scheme in _PSYCOPG2_SCHEMES:
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme) :]
    return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
