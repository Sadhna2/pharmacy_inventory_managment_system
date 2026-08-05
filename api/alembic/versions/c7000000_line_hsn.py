"""Snapshot the HSN code onto purchase and sales order lines.

A line already stores the GST rate it was taxed at, but not the HSN code that
justified that rate — the code was only ever on the product. So correcting a
product's HSN, which is an ordinary thing to do, silently rewrote every
document ever raised for it: an invoice reprinted a year later would carry a
code that was not on the copy the customer holds.

That is the same principle the rest of the system already keeps — posted tax
is never recomputed from current master data — so the line now carries its own
copy, exactly as it carries its own `gst_rate`.

Existing lines are backfilled from their product, because the product's code
today is what those documents were already printing; writing it down changes
nothing except that it can no longer drift underneath them. A line whose
product never had an HSN stays null rather than being given an invented one.

Revision ID: c7000000
Revises: c6000000
"""

import sqlalchemy as sa
from alembic import op

revision = "c7000000"
down_revision = "c6000000"
branch_labels = None
depends_on = None

#: Both line tables, because the column lives on the shared TaxLineMixin — a
#: purchase invoice from a distributor needs the same protection as one we
#: issue ourselves.
LINE_TABLES = ("purchase_order_lines", "sales_order_lines")


def upgrade() -> None:
    for table in LINE_TABLES:
        op.add_column(
            table, sa.Column("hsn_code", sa.String(length=8), nullable=True)
        )
        op.execute(
            f"""
            UPDATE {table}
               SET hsn_code = products.hsn_code
              FROM products
             WHERE products.id = {table}.product_id
               AND products.hsn_code IS NOT NULL
            """
        )


def downgrade() -> None:
    for table in LINE_TABLES:
        op.drop_column(table, "hsn_code")
