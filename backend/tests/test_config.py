"""Tests for settings, and for the database URL a hosted provider actually gives you."""

from __future__ import annotations

import pytest

from app.config import Settings, normalize_database_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # What Supabase and Render hand you. SQLAlchemy reads this as psycopg2,
        # which this project does not install.
        (
            "postgresql://user:pw@db.example.supabase.co:5432/postgres",
            "postgresql+psycopg://user:pw@db.example.supabase.co:5432/postgres",
        ),
        # Several providers still emit the older scheme.
        ("postgres://user:pw@host:5432/db", "postgresql+psycopg://user:pw@host:5432/db"),
    ],
)
def test_a_pasted_postgres_url_is_pointed_at_the_installed_driver(given: str, expected: str):
    assert normalize_database_url(given) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Already explicit — must not be rewritten into nonsense.
        "postgresql+psycopg://user:pw@host:5432/db",
        # Someone deliberately choosing psycopg2 keeps it.
        "postgresql+psycopg2://user:pw@host:5432/db",
        "sqlite:///./aaa_launch_predictor.db",
        "sqlite://",
    ],
)
def test_everything_else_is_left_alone(url: str):
    assert normalize_database_url(url) == url


def test_the_query_string_survives_normalization():
    # Supabase appends connection parameters; dropping them would silently
    # change SSL behaviour.
    given = "postgresql://u:p@host:5432/postgres?sslmode=require"
    assert normalize_database_url(given).endswith("/postgres?sslmode=require")


def test_settings_exposes_the_normalized_url():
    settings = Settings(database_url="postgresql://u:p@host:5432/db")
    assert settings.database_url == "postgresql://u:p@host:5432/db"
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@host:5432/db"
