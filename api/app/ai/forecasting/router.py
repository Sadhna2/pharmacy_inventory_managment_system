"""Read-only forecasting endpoints.

Forecasts are computed on request, not stored. Fitting a dozen series takes
well under a second, and a stored forecast is a forecast that silently goes
stale — the one failure mode nobody notices until an order is wrong.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.ai.forecasting import service
from app.ai.forecasting.schemas import AccuracyOut, ForecastListOut, ForecastOut
from app.core.deps import require_feature, require_permission
from app.db.session import get_db
from app.models.identity import User
from app.services import settings as app_settings

router = APIRouter(prefix="/ai/forecast", tags=["AI · forecasting"])

VIEW = require_permission("ai.view")
LIVE = require_feature("features.forecast")


def _to_out(forecast: service.Forecast) -> ForecastOut:
    return ForecastOut(
        product_id=forecast.product_id,
        sku=forecast.sku,
        product_name=forecast.product_name,
        warehouse_id=forecast.warehouse_id,
        warehouse_name=forecast.warehouse_name,
        method=forecast.method,
        confidence=forecast.confidence,
        start=forecast.start,
        daily=forecast.daily,
        lower=forecast.lower,
        upper=forecast.upper,
        total=round(forecast.total, 1),
        daily_mean=forecast.daily_mean,
        accuracy=AccuracyOut(**vars(forecast.accuracy)),
        alternatives=[AccuracyOut(**vars(a)) for a in forecast.alternatives],
        history_days=forecast.history_days,
        stockout_days=forecast.stockout_days,
    )


@router.get("", response_model=ForecastListOut)
def list_forecasts(
    horizon_days: int | None = Query(
        None, ge=7, le=90,
        description="Omit to use the horizon set under Setup → Settings.",
    ),
    warehouse_id: int | None = None,
    product_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
    __: None = Depends(LIVE),
) -> ForecastListOut:
    """One forecast per product and location, busiest first."""
    # Resolved here rather than defaulted in the signature: a literal in the
    # signature would quietly outrank the administrator's setting, which is the
    # sort of thing that makes a settings screen look wired to nothing.
    horizon = horizon_days or app_settings.get(db, "forecast.horizon_days")
    forecasts = service.forecast_all(
        db,
        horizon=horizon,
        warehouse_id=warehouse_id,
        product_id=product_id,
    )
    return ForecastListOut(
        horizon_days=horizon,
        generated_for=date.today(),
        forecasts=[_to_out(f) for f in forecasts],
    )
