"""Reorder endpoints.

Reading recommendations needs `ai.view`. Turning one into a purchase order
needs `ai.act` — a distinct permission, because looking at a suggestion and
committing the company's money to it are different acts, and only one of them
should be available to everyone who can open the screen.

What `ai.act` grants is still only a DRAFT. Approval remains a separate step
under `po.approve`, and the existing separation-of-duties rule stands: the
person who raised it cannot be the person who approves it.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.leadtime import service as leadtime
from app.ai.reorder import service
from app.ai.reorder.schemas import (
    DraftOrderOut,
    RaiseOrderIn,
    RecommendationOut,
    ReorderReportOut,
    ReorderSummaryOut,
    SourcingOut,
)
from app.core import clock
from app.core.deps import require_feature, require_permission
from app.core.errors import NotFoundError, ValidationError
from app.db.session import get_db
from app.models.identity import User
from app.models.masters import Product, ProductSupplier, Supplier, Warehouse
from app.schemas.documents import PurchaseOrderOut
from app.services import audit, procurement
from app.services import settings as app_settings

router = APIRouter(prefix="/ai/reorder", tags=["AI · reorder"])

VIEW = require_permission("ai.view")
ACT = require_permission("ai.act")
LIVE = require_feature("features.reorder")


def _to_out(rec: service.Recommendation) -> RecommendationOut:
    return RecommendationOut(
        key=rec.key,
        product_id=rec.product_id,
        sku=rec.sku,
        product_name=rec.product_name,
        warehouse_id=rec.warehouse_id,
        warehouse_name=rec.warehouse_name,
        on_hand=rec.on_hand,
        on_order=rec.on_order,
        position=rec.position,
        drafted_qty=rec.drafted_qty,
        draft_po_numbers=rec.draft_po_numbers,
        daily_demand=rec.daily_demand,
        forecast_confidence=rec.forecast_confidence,
        forecast_method=rec.forecast_method,
        lead_time_days=rec.lead_time_days,
        safety_stock=rec.safety_stock,
        reorder_point=rec.reorder_point,
        order_up_to=rec.order_up_to,
        suggested_qty=rec.suggested_qty,
        days_of_cover=rec.days_of_cover,
        stockout_date=rec.stockout_date,
        urgency=rec.urgency,
        service_level=rec.service_level,
        sourcing=SourcingOut(
            supplier_id=rec.sourcing.supplier_id,
            supplier_name=rec.sourcing.supplier_name,
            lead_time_days=rec.sourcing.lead_time_days,
            lead_time_sd=rec.sourcing.lead_time_sd,
            p90_days=rec.sourcing.p90_days,
            measured=rec.sourcing.measured,
            unit_cost=float(rec.sourcing.unit_cost),
            moq=float(rec.sourcing.moq),
            pack_qty=float(rec.sourcing.pack_qty),
            via_central=rec.sourcing.via_central,
        ),
        estimated_cost=rec.estimated_cost,
        reason=rec.reason,
        workings=rec.workings,
    )


@router.get("", response_model=ReorderReportOut)
def list_recommendations(
    horizon_days: int | None = Query(
        None, ge=7, le=90,
        description="Omit to use the horizon set under Setup → Settings.",
    ),
    warehouse_id: int | None = None,
    include_ok: bool = Query(
        False, description="Include products that are comfortably stocked"
    ),
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
    __: None = Depends(LIVE),
) -> ReorderReportOut:
    """What to order, most urgent first, with the orders it would become."""
    horizon = horizon_days or app_settings.get(db, "forecast.horizon_days")
    recommendations = service.recommend(
        db,
        warehouse_id=warehouse_id,
        horizon_days=horizon,
        include_ok=include_ok,
    )

    names = dict(db.execute(select(Supplier.id, Supplier.name)).all())
    warehouses = dict(db.execute(select(Warehouse.id, Warehouse.name)).all())
    drafts = [
        DraftOrderOut(
            supplier_id=supplier_id,
            supplier_name=names.get(supplier_id, ""),
            warehouse_id=destination,
            warehouse_name=warehouses.get(destination, ""),
            lines=len(items),
            units=round(sum(i.suggested_qty for i in items), 1),
            estimated_cost=round(sum(i.estimated_cost for i in items), 2),
            items=[_to_out(i) for i in items],
        )
        for (supplier_id, destination), items in service.group_for_orders(
            recommendations
        ).items()
        if supplier_id is not None
    ]
    drafts.sort(key=lambda d: -d.estimated_cost)

    return ReorderReportOut(
        generated_for=clock.today(),
        horizon_days=horizon,
        summary=ReorderSummaryOut(**service.summarise(recommendations)),
        recommendations=[_to_out(r) for r in recommendations],
        draft_orders=drafts,
    )


@router.post("/orders", response_model=PurchaseOrderOut, status_code=201)
def raise_draft_order(
    payload: RaiseOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(ACT),
    _live: None = Depends(LIVE),
) -> PurchaseOrderOut:
    """Turn a recommendation into a DRAFT purchase order.

    Prices come from the supplier record, never from the request. The expected
    date comes from the supplier's measured p90 rather than their quoted lead
    time, so the date on the order is one the goods have a 90% chance of
    beating instead of one the distributor put on a brochure.
    """
    supplier = db.get(Supplier, payload.supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {payload.supplier_id} not found")
    if db.get(Warehouse, payload.warehouse_id) is None:
        raise NotFoundError(f"Warehouse {payload.warehouse_id} not found")

    lines = []
    for line in payload.lines:
        product = db.get(Product, line.product_id)
        if product is None:
            raise NotFoundError(f"Product {line.product_id} not found")
        link = db.scalar(
            select(ProductSupplier).where(
                ProductSupplier.product_id == line.product_id,
                ProductSupplier.supplier_id == payload.supplier_id,
            )
        )
        if link is None:
            raise ValidationError(
                f"{supplier.name} does not supply {product.name}. "
                "Add them on the product before ordering."
            )
        lines.append(
            {
                "product_id": line.product_id,
                "qty_ordered": line.quantity,
                "unit_price": link.unit_cost,
            }
        )

    prediction = leadtime_prediction(db, payload.supplier_id)

    po = procurement.create_purchase_order(
        db,
        supplier_id=payload.supplier_id,
        warehouse_id=payload.warehouse_id,
        lines=lines,
        user_id=user.id,
        expected_date=prediction,
        notes=payload.notes
        or "Raised from reorder recommendations — review before approving.",
    )
    audit.record(
        db,
        action="po.create_from_recommendation",
        entity_type="purchase_order",
        entity_id=po.id,
        actor_user_id=user.id,
        after={
            "po_number": po.po_number,
            "supplier_id": payload.supplier_id,
            "warehouse_id": payload.warehouse_id,
            "lines": len(lines),
            "source": "ai.reorder",
        },
    )
    db.commit()
    db.refresh(po)
    return PurchaseOrderOut.model_validate(po)


def leadtime_prediction(db: Session, supplier_id: int) -> date:
    """Expected delivery date, from the p90 rather than the median.

    A purchase order's date is a promise to a branch manager. Setting it from
    the median means half the orders miss it, and a date that is wrong half the
    time stops being read.
    """
    stats = leadtime.predict(db, supplier_id)
    if stats["stats"].reliable:
        return stats["plan_for_date"]
    link_days = db.scalar(
        select(ProductSupplier.lead_time_days)
        .where(ProductSupplier.supplier_id == supplier_id)
        .limit(1)
    )
    return clock.today() + timedelta(days=int(link_days or 7))
