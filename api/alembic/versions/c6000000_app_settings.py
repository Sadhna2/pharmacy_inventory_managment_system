"""Administrator-tunable settings.

`feature_flags` already existed (c3000000) for switching capabilities on and
off. This adds the other half: the numbers behind them — sensitivity, service
levels, trading hours, lookback windows.

Only overrides are stored. A key absent from this table means "use the shipped
default", so the defaults keep improving with the code instead of being frozen
the first time somebody opens the settings screen.

Revision ID: c6000000
Revises: c5000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c6000000"
down_revision = "c5000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        # JSONB rather than text so a boolean survives a round trip as a
        # boolean and a float as a float.
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
