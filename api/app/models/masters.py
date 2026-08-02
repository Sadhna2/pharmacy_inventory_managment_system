"""Master data: categories, UOMs, products, suppliers, customers, locations."""

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import (
    DrugSchedule,
    SourcingPolicy,
    StorageCondition,
    TrackingMode,
)


class Category(Base, TimestampMixin):
    """Self-referential tree — therapeutic classes for a pharmacy."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    parent: Mapped["Category | None"] = relationship(remote_side=[id])


class Uom(Base):
    """Unit of measure. Single UOM per product in v1 — see ARCHITECTURE.md §21.5."""

    __tablename__ = "uoms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class Supplier(Base, TimestampMixin):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Customer(Base, TimestampMixin):
    """Retail walk-in or institutional (hospital, clinic, nursing home)."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_institutional: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    credit_limit: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Warehouse(Base, TimestampMixin):
    """Central warehouse or a branch pharmacy."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_central: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Place of supply for GST: determines CGST/SGST vs IGST.
    state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    allow_negative_stock: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    bins: Mapped[list["Bin"]] = relationship(back_populates="warehouse")


class Bin(Base, TimestampMixin):
    __tablename__ = "bins"
    __table_args__ = (UniqueConstraint("warehouse_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    zone: Mapped[str | None] = mapped_column(String(32))
    # Cold-chain products may only be binned here (ARCHITECTURE.md §6.10).
    is_cold_chain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_quarantine: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    warehouse: Mapped[Warehouse] = relationship(back_populates="bins")


class Product(Base, TimestampMixin):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("gst_rate >= 0 AND gst_rate <= 100", name="chk_gst_rate"),
        CheckConstraint("reorder_point >= 0", name="chk_reorder_point"),
        Index("ix_products_name", "name"),
        Index("ix_products_composition", "composition"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    uom_id: Mapped[int] = mapped_column(ForeignKey("uoms.id"), nullable=False)

    # How precisely this product's stock is tracked. Drives ledger validation.
    tracking_mode: Mapped[TrackingMode] = mapped_column(
        Enum(TrackingMode, name="tracking_mode"),
        default=TrackingMode.NONE,
        nullable=False,
    )

    # --- Pharmacy domain (ARCHITECTURE.md §6.10) ---
    composition: Mapped[str | None] = mapped_column(String(255))  # salt
    manufacturer: Mapped[str | None] = mapped_column(String(255))
    pack_size: Mapped[str | None] = mapped_column(String(64))
    drug_schedule: Mapped[DrugSchedule] = mapped_column(
        Enum(DrugSchedule, name="drug_schedule"),
        default=DrugSchedule.OTC,
        nullable=False,
    )
    storage_condition: Mapped[StorageCondition] = mapped_column(
        Enum(StorageCondition, name="storage_condition"),
        default=StorageCondition.AMBIENT,
        nullable=False,
    )
    is_prescription_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # --- GST ---
    hsn_code: Mapped[str | None] = mapped_column(String(8))
    gst_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("12"), nullable=False
    )

    # --- Replenishment ---
    barcode: Mapped[str | None] = mapped_column(String(64), index=True)
    reorder_point: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0"), nullable=False
    )
    safety_stock_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    sourcing_policy: Mapped[SourcingPolicy] = mapped_column(
        Enum(SourcingPolicy, name="sourcing_policy"),
        default=SourcingPolicy.EITHER,
        nullable=False,
    )
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    category: Mapped[Category | None] = relationship()
    uom: Mapped[Uom] = relationship()


class ProductSupplier(Base, TimestampMixin):
    """Which distributors supply a product, at what cost and lead time."""

    __tablename__ = "product_suppliers"
    __table_args__ = (UniqueConstraint("product_id", "supplier_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id"), nullable=False, index=True
    )
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id"), nullable=False, index=True
    )
    supplier_sku: Mapped[str | None] = mapped_column(String(64))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    moq: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("1"), nullable=False
    )
    pack_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("1"), nullable=False
    )
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    product: Mapped[Product] = relationship()
    supplier: Mapped[Supplier] = relationship()
