"""Publisher aggregate stats.

Per-publisher release profile, refreshed quarterly. `tier` stays null unless
the clustering job's stability check passes — see ml/company_tiering.py.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-25 02:14:15.487114

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publisher_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("title_count", sa.Integer(), nullable=False),
        sa.Column("mean_volume_percentile", sa.Float(), nullable=True),
        sa.Column("mean_positive_pct", sa.Float(), nullable=True),
        sa.Column("mean_platform_breadth", sa.Float(), nullable=True),
        sa.Column("first_year", sa.Integer(), nullable=True),
        sa.Column("last_year", sa.Integer(), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=True),
        sa.Column("tier_label", sa.String(length=32), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("publisher_stats", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_publisher_stats_name"), ["name"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("publisher_stats", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_publisher_stats_name"))

    op.drop_table("publisher_stats")
