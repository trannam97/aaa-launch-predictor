"""Launch window start, for releases whose store date is not their launch.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("historical_releases") as batch:
        batch.add_column(sa.Column("launch_window_start", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("historical_releases") as batch:
        batch.drop_column("launch_window_start")
