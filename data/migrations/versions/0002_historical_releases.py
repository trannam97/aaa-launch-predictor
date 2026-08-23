"""Historical releases and windowed metrics.

Phase 0.5 tables: `historical_releases` (one row per past AAA release in the
training set) and `release_windows` (its metrics at named windows relative to
the Steam release date, rather than lifetime totals).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-23 20:28:40.502400

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "historical_releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("steam_appid", sa.Integer(), nullable=False),
        sa.Column("game_name", sa.String(length=255), nullable=False),
        sa.Column("developer", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("steam_release_date", sa.Date(), nullable=True),
        sa.Column("current_list_price_cents", sa.Integer(), nullable=True),
        sa.Column("price_currency", sa.String(length=8), nullable=True),
        sa.Column("on_windows", sa.Boolean(), nullable=False),
        sa.Column("on_mac", sa.Boolean(), nullable=False),
        sa.Column("on_linux", sa.Boolean(), nullable=False),
        sa.Column("metacritic_score", sa.Integer(), nullable=True),
        sa.Column("metacritic_url", sa.String(length=512), nullable=True),
        sa.Column("cohort_year", sa.Integer(), nullable=True),
        sa.Column("original_release_date", sa.Date(), nullable=True),
        sa.Column("launch_price_cents", sa.Integer(), nullable=True),
        sa.Column(
            "platform_launch_type",
            sa.Enum(
                "day_one_steam",
                "delayed_port",
                "former_exclusive",
                "unknown",
                name="platform_launch_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("platform_reach", sa.String(length=128), nullable=True),
        sa.Column(
            "budget_tier",
            sa.Enum(
                "aaa", "aa_leaning_aaa", "unknown", name="budget_tier", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("post_launch_support", sa.Text(), nullable=True),
        sa.Column("studio_outcome", sa.Text(), nullable=True),
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
        sa.Column(
            "label_confidence",
            sa.Enum("high", "medium", "low", name="label_confidence", native_enum=False, length=16),
            nullable=True,
        ),
        sa.Column(
            "research_status",
            sa.Enum(
                "not_researched",
                "researched",
                "unresolvable",
                name="research_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sources", sa.Text(), nullable=True),
        sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_historical_releases_cohort_year"), ["cohort_year"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_historical_releases_steam_appid"), ["steam_appid"], unique=True
        )

    op.create_table(
        "release_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column(
            "window_key",
            sa.Enum(
                "launch_2w",
                "launch_1m",
                "launch_3m",
                "lifetime",
                name="window_key",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("review_total", sa.Integer(), nullable=True),
        sa.Column("review_positive", sa.Integer(), nullable=True),
        sa.Column("review_negative", sa.Integer(), nullable=True),
        sa.Column("review_score_desc", sa.String(length=64), nullable=True),
        sa.Column("peak_concurrent_players", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["historical_releases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_id", "window_key", name="uq_release_windows_release_key"),
    )


def downgrade() -> None:
    op.drop_table("release_windows")
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_historical_releases_steam_appid"))
        batch_op.drop_index(batch_op.f("ix_historical_releases_cohort_year"))

    op.drop_table("historical_releases")
