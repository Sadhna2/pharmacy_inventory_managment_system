"""Move printed MRP and purchase cost onto the batch.

MRP is printed on the physical pack and is a legal ceiling for that pack, so
it belongs to the batch rather than the product. Two batches of the same drug
bought a year apart carry different printed prices and both sit on the shelf;
a single product-level MRP would overcharge whichever one is older.

The product keeps its `mrp` as the default for new batches and as the only
answer for untracked goods, where there is no batch to hang a price on.

Existing batches are backfilled from their product so nothing reads as null
on day one, and `purchase_cost` is recovered from the receipt that created
each batch — the ledger already recorded what every batch cost.

Revision ID: c4000000
Revises: c3000000
"""

from alembic import op
import sqlalchemy as sa

revision = "c4000000"
down_revision = "c3000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lots", sa.Column("mrp", sa.Numeric(12, 4), nullable=True))
    op.add_column(
        "lots", sa.Column("purchase_cost", sa.Numeric(12, 4), nullable=True)
    )

    # Every existing batch inherits the product price it was actually sold at
    # until now, so history does not suddenly read as "price unknown".
    op.execute(
        """
        UPDATE lots
           SET mrp = products.mrp
          FROM products
         WHERE products.id = lots.product_id
           AND products.mrp IS NOT NULL
        """
    )

    # The cost is already in the ledger — take it from the receipt that first
    # brought each batch in, ignoring later movements that carry no cost.
    op.execute(
        """
        UPDATE lots
           SET purchase_cost = first_receipt.unit_cost
          FROM (
                SELECT DISTINCT ON (lot_id) lot_id, unit_cost
                  FROM stock_movements
                 WHERE lot_id IS NOT NULL
                   AND unit_cost IS NOT NULL
                   AND movement_type IN ('PURCHASE_RECEIPT', 'OPENING_BALANCE')
              ORDER BY lot_id, occurred_at, id
               ) AS first_receipt
         WHERE first_receipt.lot_id = lots.id
        """
    )


def downgrade() -> None:
    op.drop_column("lots", "purchase_cost")
    op.drop_column("lots", "mrp")
