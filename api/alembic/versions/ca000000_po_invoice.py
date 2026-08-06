"""Keep the distributor's invoice against the order it was raised from.

Intake already read these files and threw them away. That was defensible while
scanning happened at receipt — the goods were in front of you and the paper
went in a folder — but it stops being defensible now the scan *raises* the
order: the order's own quantities and prices come off a document nobody can
produce afterwards. When a line is queried three months later, "what did their
invoice actually say" has to be answerable from the system.

A table of its own rather than a column on `purchase_orders`, because a bytea
there is fetched by every query that selects an order, and the list screen
would pull megabytes of photographs to render a page of numbers.

Bytes in Postgres rather than a bucket: no second store to provision, secure,
back up and keep in step with the row pointing at it, and no credentials to
hold. A scanned invoice is a couple of megabytes; the upload cap is enforced
at the endpoint. If the volume ever outgrows this, `content` is the one column
that has to move.

Revision ID: ca000000
Revises: c9000000
"""

import sqlalchemy as sa
from alembic import op

revision = "ca000000"
down_revision = "c9000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_order_invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        # CASCADE, because the file is part of the order rather than a record
        # in its own right. Nothing in the ledger points at it — no movement
        # was ever posted from a photograph — so an order that goes takes its
        # scan with it and leaves nothing dangling.
        sa.Column(
            "purchase_order_id",
            sa.Integer(),
            sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "uploaded_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # One per order. A second scan of the same delivery is a correction of
        # the first, not a second invoice, so the constraint is what makes
        # re-uploading replace rather than accumulate.
        sa.UniqueConstraint("purchase_order_id", name="uq_po_invoice_order"),
    )


def downgrade() -> None:
    op.drop_table("purchase_order_invoices")
