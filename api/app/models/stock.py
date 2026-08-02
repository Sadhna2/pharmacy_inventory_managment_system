"""The stock ledger and its derived balance projection.

This is the heart of the system (ARCHITECTURE.md §6.4-6.7):

  - `stock_movements` is APPEND-ONLY. UPDATE and DELETE are revoked from the
    application role in the migration. Corrections are reversing entries.
  - `stock_balances` is a DERIVED projection maintained by a database trigger
    inside the same transaction, so it can never drift from the ledger.
  - Only `services/ledger.py::post_movement()` ever inserts here.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import (
    MovementType,
    ReservationStatus,
    SerialStatus,
    StockStatus,
    TrackingMode,
)


class Lot(Base):
    """A batch of a product, as received from a manufacturer.

    For LOT_EXPIRY products, `expiry_date` drives FEFO allocation and is the
    unit of a drug recall.

    Price lives here, not on the product, because MRP is printed on the pack
    and is a legal ceiling for *that* pack. Two batches bought a year apart
    carry different printed prices, both sit on the shelf at once, and each
    must be sold at its own. A single product-level MRP cannot express that,
    and rounding it to "the latest one" would mean overcharging on old stock.
    """

    __tablename__ = "lots"
    __table_args__ = (
        UniqueConstraint("product_id", "lot_code"),
        Index("ix_lots_expiry", "expiry_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id"), nullable=False, index=True
    )
    lot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    mfg_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    #: Printed on this batch's pack. Null when unknown or untracked, in which
    #: case the product's MRP stands in.
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    #: What this batch actually cost per unit, for margin without re-reading
    #: the ledger.
    purchase_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product: Mapped["Product"] = relationship()  # noqa: F821


class Serial(Base):
    """One row per physical unit. Schema-only in v1 — no UI (ARCHITECTURE.md §21.6)."""

    __tablename__ = "serials"
    __table_args__ = (UniqueConstraint("product_id", "serial_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    serial_no: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    status: Mapped[SerialStatus] = mapped_column(
        Enum(SerialStatus, name="serial_status"),
        default=SerialStatus.IN_STOCK,
        nullable=False,
    )
    current_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id")
    )
    current_bin_id: Mapped[int | None] = mapped_column(ForeignKey("bins.id"))
    warranty_end: Mapped[date | None] = mapped_column(Date)


class StockMovement(Base):
    """APPEND-ONLY ledger. Never UPDATE, never DELETE — post a reversal instead."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("quantity <> 0", name="chk_qty_nonzero"),
        # Tracking-mode integrity enforced by the DATABASE, not the application.
        CheckConstraint(
            "(tracking_mode = 'NONE'       AND lot_id IS NULL     AND serial_id IS NULL) OR "
            "(tracking_mode = 'LOT'        AND lot_id IS NOT NULL AND serial_id IS NULL) OR "
            "(tracking_mode = 'LOT_EXPIRY' AND lot_id IS NOT NULL AND serial_id IS NULL) OR "
            "(tracking_mode = 'SERIAL'     AND serial_id IS NOT NULL)",
            name="chk_tracking",
        ),
        # A serialised unit moves one at a time, always.
        CheckConstraint(
            "tracking_mode <> 'SERIAL' OR abs(quantity) = 1", name="chk_serial_qty"
        ),
        Index("ix_mov_product_wh_time", "product_id", "warehouse_id", "occurred_at"),
        Index("ix_mov_reference", "reference_type", "reference_id"),
        Index("ix_mov_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    movement_type: Mapped[MovementType] = mapped_column(
        Enum(MovementType, name="movement_type"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    bin_id: Mapped[int | None] = mapped_column(ForeignKey("bins.id"))
    status: Mapped[StockStatus] = mapped_column(
        Enum(StockStatus, name="stock_status"),
        server_default=text("'AVAILABLE'"),
        nullable=False,
    )
    # Denormalised from products so the CHECK constraints above can see it.
    tracking_mode: Mapped[TrackingMode] = mapped_column(
        Enum(TrackingMode, name="tracking_mode"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    serial_id: Mapped[int | None] = mapped_column(ForeignKey("serials.id"))

    # SIGNED: positive is stock in, negative is stock out.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[int | None] = mapped_column()
    # Makes retried jobs and double-clicked buttons safe.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    product: Mapped["Product"] = relationship()  # noqa: F821
    warehouse: Mapped["Warehouse"] = relationship()  # noqa: F821
    bin: Mapped["Bin | None"] = relationship()  # noqa: F821
    lot: Mapped[Lot | None] = relationship()


class StockBalance(Base):
    """Derived projection of the ledger. Rebuildable, never authoritative.

    Grain: (product, warehouse, bin, lot, status) — see ARCHITECTURE.md §6.5.
    Maintained by an AFTER INSERT trigger on stock_movements.
    """

    __tablename__ = "stock_balances"
    __table_args__ = (
        CheckConstraint("qty_on_hand >= 0", name="chk_no_negative"),
        CheckConstraint(
            "qty_reserved >= 0 AND qty_reserved <= qty_on_hand", name="chk_reserved_sane"
        ),
        # Cannot reserve quarantined or damaged stock.
        CheckConstraint(
            "status = 'AVAILABLE' OR qty_reserved = 0", name="chk_reserve_available"
        ),
        Index("ix_balance_product_wh", "product_id", "warehouse_id"),
        Index("ix_balance_lot", "lot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    bin_id: Mapped[int | None] = mapped_column(ForeignKey("bins.id"))
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    status: Mapped[StockStatus] = mapped_column(
        Enum(StockStatus, name="stock_status"),
        server_default=text("'AVAILABLE'"),
        nullable=False,
    )
    qty_on_hand: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), server_default=text("0"), nullable=False
    )
    qty_reserved: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), server_default=text("0"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product: Mapped["Product"] = relationship()  # noqa: F821
    warehouse: Mapped["Warehouse"] = relationship()  # noqa: F821
    bin: Mapped["Bin | None"] = relationship()  # noqa: F821
    lot: Mapped[Lot | None] = relationship()

    @property
    def qty_available(self) -> Decimal:
        return self.qty_on_hand - self.qty_reserved


class StockReservation(Base, TimestampMixin):
    """Soft-allocates stock to a sales order line before it is picked."""

    __tablename__ = "stock_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_reservation_qty"),
        Index("ix_reservation_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    bin_id: Mapped[int | None] = mapped_column(ForeignKey("bins.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    sales_order_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_lines.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status"),
        default=ReservationStatus.ACTIVE,
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lot: Mapped[Lot | None] = relationship()
