"""Read-only lead-time endpoints.

Gated on `ai.view`, which managers and admins hold. Nothing here writes, so
there is no `ai.act` path to guard — acting on this happens in the reorder
feature, which raises a draft purchase order and is gated separately.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.leadtime import service
from app.ai.leadtime.schemas import (
    LeadTimeDetailOut,
    LeadTimeOut,
    LeadTimeStatsOut,
    SupplierDeliveryOut,
)
from app.core.deps import require_feature, require_permission
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.models.identity import User
from app.models.masters import Supplier
from app.services import settings as app_settings

router = APIRouter(prefix="/ai/lead-times", tags=["AI · lead time"])

VIEW = require_permission("ai.view")
LIVE = require_feature("features.leadtime")


def _to_out(stats: service.LeadTimeStats) -> LeadTimeStatsOut:
    return LeadTimeStatsOut(
        supplier_id=stats.supplier_id,
        supplier_name=stats.supplier_name,
        deliveries=stats.deliveries,
        median_days=stats.median_days,
        p90_days=stats.p90_days,
        mean_days=stats.mean_days,
        std_dev=stats.std_dev,
        min_days=stats.min_days,
        max_days=stats.max_days,
        on_time_rate=stats.on_time_rate,
        trend_days=stats.trend_days,
        reliable=stats.reliable,
        verdict=stats.verdict,
    )


@router.get("", response_model=LeadTimeOut)
def list_lead_times(
    lookback_days: int | None = Query(
        None, ge=30, le=3650,
        description="How far back to measure. Shorter reacts faster to a "
                    "supplier who has recently changed; longer is steadier. "
                    "Omit to use the window set under Setup → Settings.",
    ),
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
    __: None = Depends(LIVE),
) -> LeadTimeOut:
    """Every supplier with deliveries on record, slowest first."""
    window = lookback_days or app_settings.get(db, "leadtime.lookback_days")
    stats = service.all_suppliers(db, lookback_days=window)
    return LeadTimeOut(
        as_of=date.today(),
        lookback_days=window,
        suppliers=[_to_out(s) for s in stats],
    )


@router.get("/{supplier_id}", response_model=LeadTimeDetailOut)
def supplier_detail(
    supplier_id: int,
    lookback_days: int | None = Query(None, ge=30, le=3650),
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
    __: None = Depends(LIVE),
) -> LeadTimeDetailOut:
    """One supplier, with the deliveries the numbers were computed from.

    The delivery list is the point. A percentile nobody can trace back to real
    orders is a number to argue with; one that lists the orders is evidence.
    """
    if db.scalar(select(Supplier.id).where(Supplier.id == supplier_id)) is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")

    window = lookback_days or app_settings.get(db, "leadtime.lookback_days")
    prediction = service.predict(db, supplier_id, lookback_days=window)
    deliveries = service.load_deliveries(db, supplier_id=supplier_id)

    return LeadTimeDetailOut(
        as_of=date.today(),
        lookback_days=window,
        stats=_to_out(prediction["stats"]),
        expected_date=prediction["expected_date"],
        plan_for_date=prediction["plan_for_date"],
        safety_days=prediction["safety_days"],
        products=service.by_product(db, supplier_id),
        deliveries=[
            SupplierDeliveryOut(
                po_id=d.po_id,
                po_number=d.po_number,
                ordered=d.ordered,
                promised=d.promised,
                received=d.received,
                days=d.days,
                late_by=d.late_by,
            )
            # Newest first, capped: this is context for the numbers above, not
            # a full purchase history — that already has its own screen.
            for d in sorted(deliveries, key=lambda x: x.ordered, reverse=True)[:40]
        ],
    )
