from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DocumentStatus, RecallStatus, TrackingMode

# --- shared -----------------------------------------------------------------


class TaxLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    taxable_value: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal


class DocumentTotalsOut(BaseModel):
    subtotal: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal
    is_interstate: bool
    place_of_supply: str | None = None


# --- purchase ---------------------------------------------------------------


class POLineIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "qty_ordered": 500,
            "unit_price": "18.50"
    }})

    product_id: int
    qty_ordered: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class POLineOut(TaxLineOut):
    id: int
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    #: Carried so the receiving screen can pre-fill its lines from the order
    #: and still know which of them need a batch number before they will save.
    #: Without it the form would have to re-fetch every product just to decide
    #: whether to show one column.
    tracking_mode: TrackingMode | None = None
    qty_ordered: Decimal
    qty_received: Decimal
    unit_price: Decimal
    #: The line's own HSN, not the product's. The product's may have been
    #: corrected since this order was raised, and this order was taxed under
    #: the old one.
    hsn_code: str | None = None


class PurchaseOrderIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "supplier_id": 1,
            "warehouse_id": 1,
            "expected_date": "2026-08-15",
            "notes": "Monthly replenishment for the central warehouse",
            "lines": [
                    {
                            "product_id": 4,
                            "qty_ordered": 500,
                            "unit_price": "18.50"
                    },
                    {
                            "product_id": 6,
                            "qty_ordered": 300,
                            "unit_price": "9.20"
                    }
            ]
    }})

    supplier_id: int
    warehouse_id: int
    order_date: date | None = None
    expected_date: date | None = None
    notes: str | None = None
    lines: list[POLineIn] = Field(min_length=1)


class PurchaseOrderOut(DocumentTotalsOut):
    model_config = ConfigDict(from_attributes=True)
    id: int
    po_number: str
    supplier_id: int
    supplier_name: str | None = None
    warehouse_id: int
    warehouse_name: str | None = None
    status: DocumentStatus
    order_date: date
    expected_date: date | None = None
    notes: str | None = None
    created_by: int
    #: Resolved names, not just ids. An approver looking at this order is
    #: about to certify someone else's work, and "created_by: 2" does not
    #: tell them whose. Null only if the account has since been deleted.
    created_by_name: str | None = None
    approved_by: int | None = None
    approved_by_name: str | None = None
    approved_at: datetime | None = None
    lines: list[POLineOut] = []


class GRNLineIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "quantity": 500,
            "unit_cost": "18.50",
            "lot_code": "PAR-650-B7",
            "mfg_date": "2026-02-10",
            "expiry_date": "2028-06-30",
            "mrp": "30.00",
            "quarantine": False
    }})

    product_id: int
    quantity: Decimal = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)
    purchase_order_line_id: int | None = None
    bin_id: int | None = None
    # Required for batch-tracked products; created on the fly if new.
    lot_code: str | None = None
    mfg_date: date | None = None
    expiry_date: date | None = None
    #: Printed on the pack being received. Falls back to the product's MRP
    #: when the carton is not to hand.
    mrp: Decimal | None = Field(None, ge=0)
    quarantine: bool = False
    #: Cross-dock: forward this line straight on to a branch instead of
    #: shelving it here. The stock is still received at this location — it did
    #: physically arrive — and a real transfer document is raised for the leg
    #: out, so nothing bypasses the ledger. It just never reaches a shelf.
    cross_dock_warehouse_id: int | None = None


class GoodsReceiptIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "warehouse_id": 1,
            "supplier_invoice_no": "APX/2026/8891",
            "supplier_invoice_date": "2026-08-02",
            "notes": "Two cartons, seals intact",
            "lines": [
                    {
                            "product_id": 4,
                            "quantity": 500,
                            "unit_cost": "18.50",
                            "lot_code": "PAR-650-B7",
                            "mfg_date": "2026-02-10",
                            "expiry_date": "2028-06-30"
                    }
            ]
    }})

    warehouse_id: int
    purchase_order_id: int | None = None
    supplier_invoice_no: str | None = None
    supplier_invoice_date: date | None = None
    notes: str | None = None
    lines: list[GRNLineIn] = Field(min_length=1)


class GRNLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    lot_id: int | None = None
    lot_code: str | None = None
    expiry_date: date | None = None
    bin_id: int | None = None
    quantity: Decimal
    unit_cost: Decimal | None = None
    cross_dock_warehouse_id: int | None = None
    cross_dock_warehouse_name: str | None = None


class GoodsReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    grn_number: str
    purchase_order_id: int | None = None
    warehouse_id: int
    warehouse_name: str | None = None
    supplier_invoice_no: str | None = None
    supplier_invoice_date: date | None = None
    received_at: datetime
    received_by: int
    received_by_name: str | None = None
    notes: str | None = None
    lines: list[GRNLineOut] = []
    #: Transfer numbers raised for the cross-docked lines, so the receipt
    #: screen can say what left again immediately.
    cross_dock_transfers: list[str] = []


# --- sales ------------------------------------------------------------------


class SOLineIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "qty_ordered": 50
    }})

    product_id: int
    qty_ordered: Decimal = Field(gt=0)
    unit_price: Decimal | None = Field(None, ge=0)


class SOLineOut(TaxLineOut):
    id: int
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    qty_ordered: Decimal
    qty_shipped: Decimal
    unit_price: Decimal
    #: Read from the line, never from the product: this is the code an invoice
    #: prints, and a reprint has to match the copy the customer was given.
    hsn_code: str | None = None


class SalesOrderIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "customer_id": 1,
            "warehouse_id": 1,
            "notes": "City Hospital \u2014 ward stock",
            "lines": [
                    {
                            "product_id": 4,
                            "qty_ordered": 50
                    },
                    {
                            "product_id": 6,
                            "qty_ordered": 20
                    }
            ]
    }})

    customer_id: int
    warehouse_id: int
    order_date: date | None = None
    notes: str | None = None
    lines: list[SOLineIn] = Field(min_length=1)


class SalesOrderOut(DocumentTotalsOut):
    model_config = ConfigDict(from_attributes=True)
    id: int
    so_number: str
    customer_id: int
    customer_name: str | None = None
    warehouse_id: int
    warehouse_name: str | None = None
    status: DocumentStatus
    order_date: date
    notes: str | None = None
    lines: list[SOLineOut] = []


class AllocationOut(BaseModel):
    """What FEFO chose — shown to the picker so they know which batch to take."""

    product_id: int
    sku: str | None = None
    product_name: str | None = None
    lot_id: int | None = None
    lot_code: str | None = None
    expiry_date: date | None = None
    quantity: Decimal
    #: The price printed on the batch FEFO picked. When one order line spans
    #: two batches with different MRPs, the picker has to see both.
    mrp: Decimal | None = None


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shipment_number: str
    sales_order_id: int
    shipped_at: datetime
    shipped_by: int
    lines: list[AllocationOut] = []


# --- transfers & adjustments ------------------------------------------------


class TransferLineIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "quantity": 100
    }})

    product_id: int
    quantity: Decimal = Field(gt=0)
    lot_id: int | None = None


class TransferLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    lot_id: int | None = None
    lot_code: str | None = None
    quantity: Decimal
    qty_received: Decimal


class TransferIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "from_warehouse_id": 1,
            "to_warehouse_id": 2,
            "notes": "Weekly replenishment to Andheri",
            "lines": [
                    {
                            "product_id": 4,
                            "quantity": 100
                    }
            ]
    }})

    from_warehouse_id: int
    to_warehouse_id: int
    notes: str | None = None
    lines: list[TransferLineIn] = Field(min_length=1)


class TransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    transfer_number: str
    from_warehouse_id: int
    from_warehouse_name: str | None = None
    to_warehouse_id: int
    to_warehouse_name: str | None = None
    status: DocumentStatus
    #: A transfer carried neither of these before. It has always recorded who
    #: raised and who approved it — the columns were simply never returned,
    #: so the screen had nothing to show.
    created_by: int
    created_by_name: str | None = None
    approved_by: int | None = None
    approved_by_name: str | None = None
    dispatched_at: datetime | None = None
    received_at: datetime | None = None
    notes: str | None = None
    lines: list[TransferLineOut] = []


class AdjustmentLineIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "product_id": 4,
            "quantity": -5,
            "lot_id": 10
    }})

    product_id: int
    quantity: Decimal = Field(description="Signed: positive increases stock")
    lot_id: int | None = None
    bin_id: int | None = None


class AdjustmentIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "warehouse_id": 1,
            "reason_code": "CYCLE_COUNT",
            "notes": "Physical count found 5 fewer strips",
            "lines": [
                    {
                            "product_id": 4,
                            "quantity": -5,
                            "lot_id": 10
                    }
            ]
    }})

    warehouse_id: int
    reason_code: str = Field(min_length=1, max_length=32)
    notes: str | None = None
    lines: list[AdjustmentLineIn] = Field(min_length=1)


class AdjustmentLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    lot_id: int | None = None
    quantity: Decimal


class AdjustmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    adjustment_number: str
    warehouse_id: int
    reason_code: str
    status: DocumentStatus
    created_by: int
    created_by_name: str | None = None
    approved_by: int | None = None
    approved_by_name: str | None = None
    notes: str | None = None
    lines: list[AdjustmentLineOut] = []


# --- recall -----------------------------------------------------------------


class RecallIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "lot_id": 10,
            "reason": "Contamination detected during QC",
            "regulator_ref": "CDSCO-2026-114"
    }})

    lot_id: int
    reason: str = Field(min_length=3, max_length=1000)
    regulator_ref: str | None = Field(None, max_length=64)


class LocationImpactOut(BaseModel):
    warehouse_id: int
    warehouse_name: str
    qty_quarantined: Decimal


class CustomerImpactOut(BaseModel):
    customer_id: int
    customer_name: str
    quantity: Decimal
    shipment_numbers: list[str]


class RecallImpactOut(BaseModel):
    recall_id: int
    lot_code: str
    product_sku: str
    product_name: str
    expiry_date: str | None = None
    total_quarantined: Decimal
    locations: list[LocationImpactOut]
    customers: list[CustomerImpactOut]


class RecallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    lot_id: int
    lot_code: str | None = None
    product_sku: str | None = None
    reason: str
    regulator_ref: str | None = None
    status: RecallStatus
    initiated_by: int
    initiated_at: datetime
    closed_at: datetime | None = None
    qty_quarantined: Decimal
