"""Response shapes for demand forecasting."""

from datetime import date

from pydantic import BaseModel, Field


class AccuracyOut(BaseModel):
    method: str
    mae: float = Field(description="Mean absolute error, in units")
    wape: float = Field(
        description="Total error over total actual. 0.18 means the forecast was "
                    "off by 18% of the volume it was predicting."
    )
    hit_rate: float = Field(description="Share of held-out days landed within ±20%")


class ForecastOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str
    method: str = Field(description="The method that won this series' backtest")
    confidence: str = Field(description="high | medium | low")
    start: date = Field(description="First forecast day (tomorrow)")
    daily: list[float]
    lower: list[float]
    upper: list[float]
    total: float = Field(description="Units expected across the whole horizon")
    daily_mean: float
    accuracy: AccuracyOut
    alternatives: list[AccuracyOut] = Field(
        description="How the methods that lost did, so the choice can be audited"
    )
    history_days: int
    stockout_days: int = Field(
        description="Days excluded as censored — the branch had nothing to sell"
    )


class ForecastListOut(BaseModel):
    horizon_days: int
    generated_for: date
    forecasts: list[ForecastOut]
