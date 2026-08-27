"""Regional availability and pricing for tracked games.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.add_column(sa.Column("regional_offers", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("regional_offers_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.drop_column("regional_offers_at")
        batch.drop_column("regional_offers")
