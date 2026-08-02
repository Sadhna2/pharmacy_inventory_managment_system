"""Cross-dock destination on goods receipt lines.

Revision ID: c5000000
Revises: c4000000
"""

import sqlalchemy as sa
from alembic import op

revision = "c5000000"
down_revision = "c4000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, and null means the ordinary case: the goods were shelved here.
    op.add_column(
        "goods_receipt_lines",
        sa.Column("cross_dock_warehouse_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_grn_lines_cross_dock_warehouse",
        "goods_receipt_lines",
        "warehouses",
        ["cross_dock_warehouse_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_grn_lines_cross_dock_warehouse", "goods_receipt_lines", type_="foreignkey"
    )
    op.drop_column("goods_receipt_lines", "cross_dock_warehouse_id")
