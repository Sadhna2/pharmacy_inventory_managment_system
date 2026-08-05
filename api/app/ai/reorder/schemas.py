"""Response and request shapes for reorder recommendations."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class SourcingOut(BaseModel):
    supplier_id: int | None
    supplier_name: str
    lead_time_days: float = Field(description="Median observed, or quoted if unmeasured")
    lead_time_sd: float
    p90_days: float
    measured: bool = Field(
        description="True when the lead time came from real deliveries rather "
                    "than the supplier's own quote"
    )
    unit_cost: float
    moq: float
    pack_qty: float
    via_central: bool = Field(
        description="Product policy routes this through the central warehouse"
    )


class RecommendationOut(BaseModel):
    key: str
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str

    on_hand: float
    on_order: float
    position: float
    drafted_qty: float = Field(
        description="On unapproved draft orders. Excluded from the position on "
                    "purpose — a draft is paper, not stock — but netted off the "
                    "suggestion so the same order is not raised twice."
    )
    draft_po_numbers: list[str]

    daily_demand: float
    forecast_confidence: str
    forecast_method: str

    lead_time_days: float
    safety_stock: float
    reorder_point: float
    order_up_to: float
    suggested_qty: float

    days_of_cover: float
    stockout_date: date | None
    urgency: str = Field(description="stockout | critical | soon | ok")
    service_level: str = Field(description="critical | high | standard")
    sourcing: SourcingOut
    estimated_cost: float
    reason: str
    workings: dict = Field(description="Every input to the arithmetic, for audit")


class ReorderSummaryOut(BaseModel):
    total: int
    stockout: int
    critical: int
    soon: int
    estimated_cost: float
    lines: int


class DraftOrderOut(BaseModel):
    """A purchase order this recommendation set would produce, before it exists."""

    supplier_id: int
    supplier_name: str
    warehouse_id: int
    warehouse_name: str
    lines: int
    units: float
    estimated_cost: float
    items: list[RecommendationOut]


class ReorderReportOut(BaseModel):
    generated_for: date
    horizon_days: int
    summary: ReorderSummaryOut
    recommendations: list[RecommendationOut]
    draft_orders: list[DraftOrderOut]


class RaiseOrderLineIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)


class RaiseOrderIn(BaseModel):
    """Convert a recommendation into a DRAFT purchase order.

    Quantities are sent back explicitly rather than recomputed server-side, so
    a buyer who edited a line gets what they approved. The server still prices
    the lines from the supplier record — a client that could set its own unit
    price could set it to zero.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "supplier_id": 3,
                "warehouse_id": 1,
                "lines": [
                    {"product_id": 12, "quantity": 2800},
                    {"product_id": 47, "quantity": 600},
                ],
                "notes": "Raised from the 12 Mar replenishment run.",
            }
        }
    )

    supplier_id: int
    warehouse_id: int
    lines: list[RaiseOrderLineIn] = Field(min_length=1)
    notes: str | None = None
