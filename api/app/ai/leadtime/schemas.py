"""Response shapes for lead-time analysis."""

from datetime import date

from pydantic import BaseModel, Field


class LeadTimeStatsOut(BaseModel):
    supplier_id: int
    supplier_name: str
    deliveries: int = Field(description="Orders measured in the window")
    median_days: float = Field(description="The typical wait — plan dates on this")
    p90_days: float = Field(description="9 in 10 land by here — size cover on this")
    mean_days: float
    std_dev: float
    min_days: int
    max_days: int
    on_time_rate: float = Field(
        description="Share of orders that met the date promised on the PO. "
                    "0 when no order carried a promised date."
    )
    trend_days: float = Field(
        description="Recent third minus oldest third, in days. Positive = slowing."
    )
    reliable: bool = Field(description="False when too few deliveries to conclude")
    verdict: str


class SupplierDeliveryOut(BaseModel):
    po_id: int
    po_number: str
    ordered: date
    promised: date | None
    received: date
    days: int
    late_by: int | None = Field(
        default=None, description="Days past the promised date; negative is early"
    )


class SupplierProductOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    receipts: int
    units: int


class LeadTimeOut(BaseModel):
    as_of: date
    lookback_days: int
    suppliers: list[LeadTimeStatsOut]


class LeadTimeDetailOut(BaseModel):
    as_of: date
    lookback_days: int
    stats: LeadTimeStatsOut
    expected_date: date = Field(description="Order today, expect it around here")
    plan_for_date: date = Field(description="Don't run dry before this date")
    safety_days: float = Field(description="p90 - median: the cover this supplier costs you")
    products: list[SupplierProductOut]
    deliveries: list[SupplierDeliveryOut]
