"""Pre-launch award nominations.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("historical_releases") as batch:
        batch.add_column(sa.Column("prelaunch_award_nominations", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("prelaunch_award_wins", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("historical_releases") as batch:
        batch.drop_column("prelaunch_award_wins")
        batch.drop_column("prelaunch_award_nominations")
