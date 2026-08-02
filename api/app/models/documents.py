"""Layer 1 documents: purchase orders, goods receipts, sales orders,
shipments, transfers and adjustments.

Documents express *intent*; only a receipt/shipment/transfer-leg posts to the
ledger, and always through services/ledger.py::post_movement().
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import DocumentStatus, RecallStatus

# --- GST-bearing line mixin -------------------------------------------------


class TaxLineMixin:
    """CGST/SGST for intra-state, IGST for inter-state. Never both."""

    taxable_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    gst_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0"), nullable=False
    )
    cgst_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    sgst_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    igst_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    line_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )


class DocumentTotalsMixin:
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    round_off: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    grand_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    is_interstate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    place_of_supply: Mapped[str | None] = mapped_column(String(2))


# --- Purchasing -------------------------------------------------------------


class PurchaseOrder(Base, TimestampMixin, DocumentTotalsMixin):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        default=DocumentStatus.DRAFT,
        nullable=False,
        index=True,
    )
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    supplier: Mapped["Supplier"] = relationship()  # noqa: F821
    warehouse: Mapped["Warehouse"] = relationship()  # noqa: F821
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PurchaseOrderLine(Base, TaxLineMixin):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        CheckConstraint("qty_ordered > 0", name="chk_po_qty"),
        CheckConstraint("qty_received >= 0", name="chk_po_recv"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty_ordered: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qty_received: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()  # noqa: F821


class GoodsReceipt(Base, TimestampMixin):
    """The moment stock actually increases."""

    __tablename__ = "goods_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    grn_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    purchase_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_orders.id"), index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    supplier_invoice_no: Mapped[str | None] = mapped_column(String(64))
    supplier_invoice_date: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    received_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["GoodsReceiptLine"]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan"
    )
    purchase_order: Mapped[PurchaseOrder | None] = relationship()


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"
    __table_args__ = (CheckConstraint("quantity > 0", name="chk_grn_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    goods_receipt_id: Mapped[int] = mapped_column(
        ForeignKey("goods_receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_order_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_order_lines.id")
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    bin_id: Mapped[int | None] = mapped_column(ForeignKey("bins.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    #: Cross-docked straight on to this branch rather than shelved here. Kept
    #: on the line, not just implied by the transfer, so "where did this line
    #: go?" is answerable from the receipt a year later.
    cross_dock_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id")
    )

    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()  # noqa: F821


# --- Sales ------------------------------------------------------------------


class SalesOrder(Base, TimestampMixin, DocumentTotalsMixin):
    __tablename__ = "sales_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    so_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        default=DocumentStatus.DRAFT,
        nullable=False,
        index=True,
    )
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    customer: Mapped["Customer"] = relationship()  # noqa: F821
    warehouse: Mapped["Warehouse"] = relationship()  # noqa: F821
    lines: Mapped[list["SalesOrderLine"]] = relationship(
        back_populates="sales_order", cascade="all, delete-orphan"
    )


class SalesOrderLine(Base, TaxLineMixin):
    __tablename__ = "sales_order_lines"
    __table_args__ = (CheckConstraint("qty_ordered > 0", name="chk_so_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty_ordered: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qty_shipped: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    sales_order: Mapped[SalesOrder] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()  # noqa: F821


class Shipment(Base, TimestampMixin):
    """The moment stock actually decreases."""

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_number: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    sales_order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id"), nullable=False, index=True
    )
    shipped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    shipped_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    lines: Mapped[list["ShipmentLine"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class ShipmentLine(Base):
    """Records which LOT went out — this is what makes recall traceability work."""

    __tablename__ = "shipment_lines"
    __table_args__ = (CheckConstraint("quantity > 0", name="chk_ship_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sales_order_line_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_lines.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    shipment: Mapped[Shipment] = relationship(back_populates="lines")
    lot: Mapped["Lot | None"] = relationship()  # noqa: F821


# --- Transfers and adjustments ---------------------------------------------


class StockTransfer(Base, TimestampMixin):
    """Central warehouse to branch. Exercises the IN_TRANSIT status."""

    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_number: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    from_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    to_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        default=DocumentStatus.DRAFT,
        nullable=False,
        index=True,
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    from_warehouse: Mapped["Warehouse"] = relationship(  # noqa: F821
        foreign_keys=[from_warehouse_id]
    )
    to_warehouse: Mapped["Warehouse"] = relationship(  # noqa: F821
        foreign_keys=[to_warehouse_id]
    )
    lines: Mapped[list["StockTransferLine"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan"
    )


class StockTransferLine(Base):
    __tablename__ = "stock_transfer_lines"
    __table_args__ = (CheckConstraint("quantity > 0", name="chk_transfer_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_transfer_id: Mapped[int] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qty_received: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )

    transfer: Mapped[StockTransfer] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()  # noqa: F821


class StockAdjustment(Base, TimestampMixin):
    __tablename__ = "stock_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    adjustment_number: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        default=DocumentStatus.DRAFT,
        nullable=False,
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["StockAdjustmentLine"]] = relationship(
        back_populates="adjustment", cascade="all, delete-orphan"
    )


class StockAdjustmentLine(Base):
    __tablename__ = "stock_adjustment_lines"
    __table_args__ = (CheckConstraint("quantity <> 0", name="chk_adj_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_adjustment_id: Mapped[int] = mapped_column(
        ForeignKey("stock_adjustments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"))
    bin_id: Mapped[int | None] = mapped_column(ForeignKey("bins.id"))
    # Signed: positive increases stock, negative decreases it.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    adjustment: Mapped[StockAdjustment] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()  # noqa: F821


class Recall(Base):
    """Batch recall — freezes a lot chain-wide (ARCHITECTURE.md §6.10)."""

    __tablename__ = "recalls"

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    regulator_ref: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RecallStatus] = mapped_column(
        Enum(RecallStatus, name="recall_status"),
        default=RecallStatus.INITIATED,
        nullable=False,
    )
    initiated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    initiated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qty_quarantined: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )

    lot: Mapped["Lot"] = relationship()  # noqa: F821


class DocumentSequence(Base):
    """Gap-free per-prefix document numbering (PO-000001, GRN-000001, ...)."""

    __tablename__ = "document_sequences"

    prefix: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_value: Mapped[int] = mapped_column(default=0, nullable=False)


Index("ix_recalls_status", Recall.status)
