"""Release date change history.

Steam publishes only a game's current release date, with no history, so a
delay only exists in this database if it was caught between two refreshes.
This table records each observed move.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-24 19:13:22.778520

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_date_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_date", sa.Date(), nullable=True),
        sa.Column("previous_raw", sa.String(length=64), nullable=True),
        sa.Column("new_date", sa.Date(), nullable=True),
        sa.Column("new_raw", sa.String(length=64), nullable=True),
        sa.Column("days_moved", sa.Integer(), nullable=True),
        sa.Column("from_coarse_estimate", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("release_date_changes", schema=None) as batch_op:
        batch_op.create_index(
            "ix_release_date_changes_game_observed", ["game_id", "observed_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("release_date_changes", schema=None) as batch_op:
        batch_op.drop_index("ix_release_date_changes_game_observed")

    op.drop_table("release_date_changes")
