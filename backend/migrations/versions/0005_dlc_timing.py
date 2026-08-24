"""DLC timing and in-app purchases.

Splits add-on content by when it shipped. Launch-day DLC is a pre-launch
monetization decision and safe to forecast on; DLC added later reflects how
the launch went. `dlc_count` counts only content sold as separate Steam apps,
so `has_in_app_purchases` is stored alongside to catch in-game-currency
models it cannot see.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24 15:09:41.224373

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dlc_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("launch_day_dlc_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("post_launch_dlc_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_dlc_days_after_launch", sa.Integer(), nullable=True))
        batch_op.add_column(
            # NOT NULL on a populated table needs a value at the moment the
            # column appears; the default is dropped below so the schema
            # matches the model exactly.
            sa.Column(
                "has_in_app_purchases",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.alter_column(
            "has_in_app_purchases", existing_type=sa.Boolean(), server_default=None
        )


def downgrade() -> None:
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.drop_column("has_in_app_purchases")
        batch_op.drop_column("last_dlc_days_after_launch")
        batch_op.drop_column("post_launch_dlc_count")
        batch_op.drop_column("launch_day_dlc_count")
        batch_op.drop_column("dlc_count")
