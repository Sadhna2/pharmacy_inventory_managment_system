"""Link a customer account to the buyer it speaks for.

The CUSTOMER role has existed since the first seed and has never worked. The
SRS gives it "own orders only" (§3.1), the permission table comments say
"heavily scoped: their own orders and nothing else", and the account is on the
demo login card — but nothing on `users` recorded *which* buyer the account
belonged to, so "own" had no referent.

What happened instead: every scoped read runs through `scoped_warehouse_ids`,
which pins a non-admin to `user.warehouse_id`. A customer has no branch — they
buy from whichever one holds the stock — so that returns an empty list and the
account was shown zero of its 64 orders. The role's only screen was a screen
with nothing on it, and the dashboard behind it three refusals rendered as
"nothing expiring" and "no movements yet".

So the column is the missing half of a control, not a new feature. It is
nullable because it is meaningless on the three internal roles, and it carries
no default: an unlinked customer account is refused its orders rather than
shown someone else's.

The demo account is linked here rather than only in the seed, so an existing
database gets a working customer login without being reseeded. Matched on the
customer's name, and skipped silently if it does not match — a migration must
not fail because a demo row was renamed.

Revision ID: c8000000
Revises: c7000000
"""

import sqlalchemy as sa
from alembic import op

revision = "c8000000"
down_revision = "c7000000"
branch_labels = None
depends_on = None

#: The seeded pairing. Both sides come from `seed/bootstrap.py`.
DEMO_ACCOUNT = "customer@cityhospital.co.in"
DEMO_CUSTOMER = "City Hospital"


def upgrade() -> None:
    op.add_column("users", sa.Column("customer_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_users_customer_id", "users", "customers", ["customer_id"], ["id"]
    )
    # Partial: the column is null on every internal account, and a plain index
    # over mostly-nulls earns nothing.
    op.create_index(
        "ix_users_customer_id",
        "users",
        ["customer_id"],
        postgresql_where=sa.text("customer_id IS NOT NULL"),
    )

    op.execute(
        sa.text(
            """
            UPDATE users
               SET customer_id = c.id
              FROM customers c
             WHERE users.email = :email
               AND c.name = :name
            """
        ).bindparams(email=DEMO_ACCOUNT, name=DEMO_CUSTOMER)
    )


def downgrade() -> None:
    op.drop_index("ix_users_customer_id", table_name="users")
    op.drop_constraint("fk_users_customer_id", "users", type_="foreignkey")
    op.drop_column("users", "customer_id")
