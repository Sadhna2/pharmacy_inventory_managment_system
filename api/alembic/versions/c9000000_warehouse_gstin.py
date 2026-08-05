"""Give a warehouse its own GST registration.

`settings.seller_gstin` is one value for the whole chain, and config.py argues
for that: "a branch is a place, and it is the business that holds the
registration." That is true of a business in one state. It is wrong the moment
a branch opens in another, because GST registration is per state — a Gujarat
branch of a Mumbai firm is a separately registered person with its own GSTIN,
and the first two characters of a GSTIN *are* the state code.

What that cost, before this column existed: the invoice route already printed
the seller's state from the warehouse (`state_code=so.warehouse.state_code`)
while taking the GSTIN from the global setting. An order shipped from the
seeded Ahmedabad branch printed "State: GJ (24)" beside a GSTIN opening "27".
The document contradicted itself on its own face, and the buyer could not have
claimed input credit against it.

Nullable, and not backfilled here. A warehouse with no registration recorded
falls back to the firm's configured GSTIN, which is exactly the behaviour
every existing row has today — so an established database upgrades without a
change in what it prints. The seed fills the demo branches in; a real one is
entered per branch, where the person entering it knows the number.

Revision ID: c9000000
Revises: c8000000
"""

import sqlalchemy as sa
from alembic import op

revision = "c9000000"
down_revision = "c8000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("warehouses", sa.Column("gstin", sa.String(15), nullable=True))


def downgrade() -> None:
    op.drop_column("warehouses", "gstin")
