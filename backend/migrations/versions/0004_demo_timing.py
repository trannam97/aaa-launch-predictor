"""Demo timing.

Whether a title had a demo, and — critically — whether that demo predates
its release. Most demos on corpus titles were added *after* launch, so a
bare presence flag would correlate with disappointment rather than predict
success.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24 14:11:48.714484

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TIMING = sa.Enum(
    "pre_launch",
    "launch_window",
    "post_launch",
    "none_listed",
    "unknown",
    name="demo_timing",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    # demo_timing is NOT NULL, so existing rows need a value the moment the
    # column appears. A server default supplies one, then it is dropped so the
    # schema matches the model exactly and `alembic check` stays quiet.
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("demo_appid", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("demo_release_date", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("demo_timing", TIMING, nullable=False, server_default="unknown")
        )

    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.alter_column("demo_timing", existing_type=TIMING, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("historical_releases", schema=None) as batch_op:
        batch_op.drop_column("demo_timing")
        batch_op.drop_column("demo_release_date")
        batch_op.drop_column("demo_appid")
