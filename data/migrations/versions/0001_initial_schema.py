"""Initial schema: games and game_snapshots.

Phase 0 tables. `historical_releases` (the ML training table) is added in a
later migration alongside the Phase 0.5 backfill.

Revision ID: 0001
Revises:
Create Date: 2026-08-23 07:29:13.109282

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("steam_appid", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("header_image", sa.String(length=512), nullable=True),
        sa.Column("developers", sa.Text(), nullable=True),
        sa.Column("publishers", sa.Text(), nullable=True),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("release_date_raw", sa.String(length=64), nullable=True),
        sa.Column("coming_soon", sa.Boolean(), nullable=False),
        sa.Column("is_free", sa.Boolean(), nullable=False),
        sa.Column("price_initial_cents", sa.Integer(), nullable=True),
        sa.Column("price_currency", sa.String(length=8), nullable=True),
        sa.Column("on_windows", sa.Boolean(), nullable=False),
        sa.Column("on_mac", sa.Boolean(), nullable=False),
        sa.Column("on_linux", sa.Boolean(), nullable=False),
        sa.Column("metacritic_score", sa.Integer(), nullable=True),
        sa.Column("metacritic_url", sa.String(length=512), nullable=True),
        sa.Column(
            "lifecycle_status",
            sa.Enum(
                "pre_launch",
                "tracking",
                "failed_to_meet_expectations",
                "resolved",
                "unresolved_insufficient_data",
                name="lifecycle_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "predicted_outcome",
            sa.Enum(
                "flop",
                "underperform",
                "success",
                "breakout",
                name="outcome",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("predicted_confidence", sa.Float(), nullable=True),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_outcome",
            sa.Enum(
                "flop",
                "underperform",
                "success",
                "breakout",
                name="outcome",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_games_steam_appid"), ["steam_appid"], unique=True)

    op.create_table(
        "game_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_final_cents", sa.Integer(), nullable=True),
        sa.Column("discount_percent", sa.Integer(), nullable=True),
        sa.Column("review_total", sa.Integer(), nullable=True),
        sa.Column("review_positive", sa.Integer(), nullable=True),
        sa.Column("review_negative", sa.Integer(), nullable=True),
        sa.Column("review_score_desc", sa.String(length=64), nullable=True),
        sa.Column("concurrent_players", sa.Integer(), nullable=True),
        sa.Column("metacritic_score", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "captured_at", name="uq_game_snapshots_game_captured"),
    )
    with op.batch_alter_table("game_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_game_snapshots_game_captured", ["game_id", "captured_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("game_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_game_snapshots_game_captured")

    op.drop_table("game_snapshots")
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_games_steam_appid"))

    op.drop_table("games")
