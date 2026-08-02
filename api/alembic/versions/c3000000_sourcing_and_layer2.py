"""Sourcing policy, feature flags, and the Layer 2 forecasting tables.

Three additive changes, none of which touch the ledger:

  - `products.sourcing_policy` makes the hybrid supply model explicit. A real
    chain routes bulk, cold-chain and controlled drugs through the central
    warehouse (so near-expiry stock can be redistributed between branches) and
    lets fast movers go straight from distributor to branch. Declaring it per
    product lets the reorder engine recommend the right route instead of
    assuming one.

  - `feature_flags` so every AI capability can be switched off independently
    at runtime — demo insurance, and an honest statement of what is built.

  - The forecasting tables. Created now rather than later so the models in
    `app/models/forecasting.py` are never ahead of the database.

Revision ID: c3000000
Revises: b2000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c3000000"
down_revision = "b2000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- sourcing policy ---------------------------------------------------
    sourcing = postgresql.ENUM(
        "VIA_CENTRAL", "DIRECT", "EITHER", name="sourcing_policy", create_type=False
    )
    sourcing.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "products",
        sa.Column(
            "sourcing_policy",
            sourcing,
            nullable=False,
            server_default="EITHER",
        ),
    )

    # Cold-chain and controlled drugs go through the hub by default: one
    # validated cold room beats six, and fewer custody points for Schedule
    # H1/X means fewer places to audit.
    op.execute(
        """
        UPDATE products SET sourcing_policy = 'VIA_CENTRAL'
        WHERE storage_condition IN ('COLD_CHAIN', 'FROZEN')
           OR drug_schedule IN ('H1', 'X')
        """
    )

    # --- feature flags -----------------------------------------------------
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False, server_default="AI"),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_implemented", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id")),
    )

    # --- forecasting -------------------------------------------------------
    op.create_table(
        "calendar_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column(
            "is_weekend", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_flu_season", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_monsoon", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_festival", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("festival_name", sa.String(64)),
        sa.Column("temp_band", sa.String(8), nullable=False, server_default="WARM"),
    )
    op.create_index(
        "ix_calendar_flags", "calendar_days", ["is_festival", "is_flu_season"]
    )

    forecast_status = postgresql.ENUM(
        "RUNNING", "SUCCEEDED", "FAILED", name="forecast_status", create_type=False
    )
    forecast_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "forecast_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status", forecast_status, nullable=False, server_default="RUNNING"
        ),
        sa.Column("model_name", sa.String(64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False, server_default="42"),
        sa.Column("cutoff_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("series_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Numeric(10, 2)),
        sa.Column("error", sa.Text()),
    )

    op.create_table(
        "forecasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("forecast_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False
        ),
        sa.Column(
            "warehouse_id",
            sa.Integer(),
            sa.ForeignKey("warehouses.id"),
            nullable=False,
        ),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("yhat", sa.Numeric(18, 4), nullable=False),
        sa.Column("yhat_lower", sa.Numeric(18, 4)),
        sa.Column("yhat_upper", sa.Numeric(18, 4)),
        sa.UniqueConstraint(
            "run_id",
            "product_id",
            "warehouse_id",
            "forecast_date",
            name="ux_forecast_point",
        ),
        sa.CheckConstraint("yhat >= 0", name="chk_forecast_nonneg"),
    )
    op.create_index(
        "ix_forecast_lookup",
        "forecasts",
        ["product_id", "warehouse_id", "forecast_date"],
    )

    op.create_table(
        "forecast_accuracy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("forecast_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False
        ),
        sa.Column(
            "warehouse_id",
            sa.Integer(),
            sa.ForeignKey("warehouses.id"),
            nullable=False,
        ),
        sa.Column("model_name", sa.String(64), nullable=False),
        sa.Column("mape", sa.Numeric(10, 4)),
        sa.Column("mase", sa.Numeric(10, 4)),
        sa.Column("rmse", sa.Numeric(18, 4)),
        sa.Column("n_observations", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "run_id", "product_id", "warehouse_id", name="ux_accuracy_series"
        ),
    )

    suggestion_status = postgresql.ENUM(
        "PENDING",
        "ACCEPTED",
        "REJECTED",
        "EXPIRED",
        name="suggestion_status",
        create_type=False,
    )
    suggestion_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "reorder_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("forecast_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False
        ),
        sa.Column(
            "warehouse_id",
            sa.Integer(),
            sa.ForeignKey("warehouses.id"),
            nullable=False,
        ),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id")),
        sa.Column("qty_on_hand", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "qty_incoming", sa.Numeric(18, 4), nullable=False, server_default="0"
        ),
        sa.Column("avg_daily_demand", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "demand_stddev", sa.Numeric(18, 4), nullable=False, server_default="0"
        ),
        sa.Column("lead_time_days", sa.Numeric(8, 2), nullable=False),
        sa.Column(
            "lead_time_stddev", sa.Numeric(8, 2), nullable=False, server_default="0"
        ),
        sa.Column("safety_stock", sa.Numeric(18, 4), nullable=False),
        sa.Column("reorder_point", sa.Numeric(18, 4), nullable=False),
        sa.Column("suggested_qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("days_of_cover", sa.Numeric(8, 2)),
        sa.Column(
            "capped_by_shelf_life",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "status", suggestion_status, nullable=False, server_default="PENDING"
        ),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column(
            "purchase_order_id", sa.Integer(), sa.ForeignKey("purchase_orders.id")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "run_id", "product_id", "warehouse_id", name="ux_suggestion_series"
        ),
        sa.CheckConstraint("suggested_qty > 0", name="chk_suggestion_positive"),
    )
    op.create_index(
        "ix_suggestion_status", "reorder_suggestions", ["status", "warehouse_id"]
    )


def downgrade() -> None:
    op.drop_table("reorder_suggestions")
    op.drop_table("forecast_accuracy")
    op.drop_table("forecasts")
    op.drop_table("forecast_runs")
    op.drop_table("calendar_days")
    op.drop_table("feature_flags")
    op.drop_column("products", "sourcing_policy")

    for name in ("suggestion_status", "forecast_status", "sourcing_policy"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
