from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MovementType, StockStatus


class LotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    lot_code: str
    mfg_date: date | None = None
    expiry_date: date | None = None
    supplier_id: int | None = None
    #: Printed on this batch's pack — the ceiling it may be sold at.
    mrp: Decimal | None = None
    purchase_cost: Decimal | None = None
    received_at: datetime
    days_to_expiry: int | None = None


class LotIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "lot_code": "PAR-650-B8",
            "mfg_date": "2026-03-01",
            "expiry_date": "2028-09-30",
            "supplier_id": 1
    }})

    product_id: int
    lot_code: str = Field(min_length=1, max_length=64)
    mfg_date: date | None = None
    expiry_date: date | None = None
    supplier_id: int | None = None


class BalanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str
    bin_id: int | None = None
    bin_code: str | None = None
    lot_id: int | None = None
    lot_code: str | None = None
    expiry_date: date | None = None
    #: From the batch, falling back to the product for untracked goods.
    mrp: Decimal | None = None
    status: StockStatus
    qty_on_hand: Decimal
    qty_reserved: Decimal
    qty_available: Decimal


class MovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    movement_type: MovementType
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    warehouse_id: int
    warehouse_name: str | None = None
    bin_id: int | None = None
    lot_id: int | None = None
    lot_code: str | None = None
    status: StockStatus
    quantity: Decimal
    unit_cost: Decimal | None = None
    reference_type: str | None = None
    reference_id: int | None = None
    #: Set when a later entry corrects this one. Lets a list show "Reversed"
    #: rather than offering a button that is guaranteed to be refused.
    reversed_by_id: int | None = None
    occurred_at: datetime
    created_by: int
    created_by_name: str | None = None
    notes: str | None = None


class MovementIn(BaseModel):
    """Direct ledger post. Used by opening balances and manual corrections;
    ordinary operations go through PO/GRN, SO/shipment or transfer endpoints."""
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "warehouse_id": 1,
            "quantity": 100,
            "movement_type": "ADJUSTMENT",
            "bin_id": 1,
            "lot_id": 10,
            "status": "AVAILABLE",
            "unit_cost": "18.50",
            "notes": "Found during stock take"
    }})


    product_id: int
    warehouse_id: int
    quantity: Decimal = Field(description="Signed: positive in, negative out")
    movement_type: MovementType
    bin_id: int | None = None
    lot_id: int | None = None
    status: StockStatus = StockStatus.AVAILABLE
    unit_cost: Decimal | None = Field(None, ge=0)
    occurred_at: datetime | None = None
    notes: str | None = None


class ReverseMovementIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "reason": "Posted against the wrong batch"
    }})

    reason: str = Field(min_length=3, max_length=500)


class ExpiringStockOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str
    lot_id: int
    lot_code: str
    expiry_date: date
    qty_on_hand: Decimal
    days_to_expiry: int


class StockSummary(BaseModel):
    """Dashboard tiles."""

    total_skus: int
    total_units: Decimal
    below_reorder_point: int
    expiring_30_days: int
    expired_on_hand: int
    quarantined_units: Decimal
    in_transit_units: Decimal
    stock_value: Decimal | None = None
