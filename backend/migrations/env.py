"""Alembic environment.

Lives beside the models it migrates, so `app` imports normally — the package
is installed editable (`pip install -e ./backend`). The database URL always
comes from DATABASE_URL (or the backend's default), never from alembic.ini,
so no connection string is ever committed.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base, UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context) -> str | bool:
    """Render `UtcDateTime` as the plain SQLAlchemy type it compiles to.

    The decorator only normalizes Python-side values; its DDL is identical to
    `DateTime(timezone=True)`. Rendering it that way keeps generated
    migrations free of imports from the application package, so a later
    refactor of `app.models` can't break an old migration.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        # `sa` is already imported by the revision template.
        return "sa.DateTime(timezone=True)"
    return False


def get_url() -> str:
    return os.environ.get("DATABASE_URL") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations against the live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
            # SQLite can't ALTER most things in place; batch mode rewrites the
            # table instead, so local dev runs the same migrations as Postgres.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
