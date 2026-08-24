"""Structured studio and support signals.

The rubric cannot run on prose. These two enums are the machine-readable
counterparts to `studio_outcome` and `post_launch_support`, and together they
are what separates Flop from Underperform.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24 00:40:49.971698

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STUDIO = sa.Enum(
    "grew",
    "continued",
    "severe_layoffs",
    "closed",
    "unknown",
    name="studio_signal",
    native_enum=False,
    length=32,
)
SUPPORT = sa.Enum(
    "sustained",
    "curtailed",
    "abandoned",
    "unknown",
    name="support_signal",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    # The columns are NOT NULL, so existing rows need a value at the moment
    # the column appears. A server default supplies one, then it is dropped
    # so the resulting schema matches the model exactly (and `alembic check`
    # stays quiet).
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("studio_signal", STUDIO, nullable=False, server_default="unknown")
        )
        batch_op.add_column(
            sa.Column("support_signal", SUPPORT, nullable=False, server_default="unknown")
        )

    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.alter_column("studio_signal", existing_type=STUDIO, server_default=None)
        batch_op.alter_column("support_signal", existing_type=SUPPORT, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.drop_column("support_signal")
        batch_op.drop_column("studio_signal")
