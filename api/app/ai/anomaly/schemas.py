"""Response shapes for anomaly detection."""

from datetime import datetime

from pydantic import BaseModel, Field


class AnomalyOut(BaseModel):
    key: str = Field(description="Stable identity derived from the finding itself")
    kind: str = Field(description="consumption | shrinkage | write_off | after_hours | repeat_loss")
    severity: str = Field(description="high | medium | low")
    occurred_at: datetime
    product_id: int | None
    product_name: str
    sku: str
    warehouse_id: int
    warehouse_name: str
    quantity: float
    value: float = Field(description="Rupees at cost; 0 where the finding is not a loss")
    score: float = Field(description="Robust z-score; 0 for rule-based detectors")
    explanation: str
    baseline: dict = Field(description="What the finding was measured against")
    movement_ids: list[int] = Field(description="Ledger rows behind the finding")


class AnomalySummaryOut(BaseModel):
    total: int
    high: int
    medium: int
    low: int
    by_kind: dict[str, int]
    value_at_risk: float


class AnomalyReportOut(BaseModel):
    lookback_days: int
    summary: AnomalySummaryOut
    anomalies: list[AnomalyOut]
