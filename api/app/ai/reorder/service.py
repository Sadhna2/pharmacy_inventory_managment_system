"""Reorder recommendations.

WHAT THIS COMPUTES
------------------
For every product at every location: how much cover is left, when it runs out,
and what to order. It is the one place where the other two Layer 2 features
become money — the demand forecast says how fast stock leaves, the lead-time
analysis says how long a replacement takes, and this puts them together.

    reorder point = demand over the lead time + safety stock
    order up to   = demand over (lead time + review period) + safety stock
    suggest       = order-up-to level − (on hand + already on order)

SAFETY STOCK, AND WHY IT NEEDS TWO VARIANCES
--------------------------------------------
The naive safety stock is "N days of cover". It is wrong in a specific and
expensive way: it treats a reliable supplier and an erratic one identically.
Running out has two independent causes — demand was higher than expected, or
the delivery was later than expected — so both belong in the formula:

    safety = Z * sqrt(L * sigma_demand^2 + demand^2 * sigma_leadtime^2)
                    \\_____________/         \\_________________/
                     demand risk              supply risk

The second term is why Apex Pharma Supply, whose deliveries range from 3 to 25
days, forces a branch to hold far more stock than MedPlus does for identical
demand — and the recommendation says so in words, because that is a fact worth
renegotiating a contract over.

sigma_demand comes from the forecast's own held-out error, not from a textbook
assumption. A product the model predicts well gets a thin buffer; one it
predicts badly gets a fat one, automatically.

SERVICE LEVEL IS A CLINICAL DECISION, NOT A STATISTICAL ONE
------------------------------------------------------------
Z is set per product from what the product is. Running out of vitamin tablets
is an inconvenience; running out of insulin is a hospital admission. Encoding
that in the drug schedule and storage condition means the buffer follows the
medicine rather than someone remembering to raise a number.

NOTHING HERE ORDERS ANYTHING
-----------------------------
The output is a suggestion with its arithmetic attached. Turning one into a
purchase order is a separate, permissioned, audited action that produces a
DRAFT — which still needs approval by someone other than its creator. An
automatic ordering loop is a fine idea for a warehouse of screws and a
terrible one for a pharmacy.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.forecasting import service as forecasting
from app.ai.leadtime import service as leadtime
from app.models.documents import PurchaseOrder, PurchaseOrderLine
from app.models.enums import (
    DocumentStatus,
    DrugSchedule,
    SourcingPolicy,
    StockStatus,
    StorageCondition,
)
from app.models.masters import Product, ProductSupplier, Supplier, Warehouse
from app.models.stock import StockBalance
from app.services import settings as app_settings

#: How often somebody actually looks at this screen and places orders. The
#: order-up-to level has to cover the gap until the next look, or every review
#: cycle starts fractionally behind.
REVIEW_PERIOD_DAYS = 7

#: Service levels, as the z-score of the normal distribution. The jump from
#: 95% to 99% roughly doubles the buffer, which is the right trade for a drug
#: someone is dependent on and the wrong one for cough syrup.
SERVICE_LEVELS: dict[str, float] = {
    "critical": 2.33,  # 99%   — insulin, cold chain, controlled
    "high": 1.88,      # 97%   — prescription-only
    "standard": 1.65,  # 95%   — everything else
}

#: Below this many units a day, the maths stops meaning anything: a product
#: that sells three a month has a lead-time demand of less than one unit and
#: any safety stock is a rounding artefact. These are ordered by eye.
MIN_DAILY_DEMAND = 0.2

#: Never suggest more than this many days of stock, whatever the arithmetic
#: says. A supplier with a 25-day p90 and a big MOQ can otherwise produce a
#: recommendation to buy eight months of a product that expires in six.
MAX_COVER_DAYS = 120


def service_level(
    product: Product, levels: dict[str, float] | None = None
) -> tuple[str, float]:
    """How hard this product should be protected from running out.

    Which class a product falls into is decided here, from the medicine — an
    administrator sets *how hard* each class is protected, not which drugs are
    critical. Letting the two be configured together would eventually put
    insulin on the same footing as vitamin tablets because somebody was tidying
    a settings screen.
    """
    levels = levels or SERVICE_LEVELS
    if (
        product.storage_condition is StorageCondition.COLD_CHAIN
        or product.drug_schedule in (DrugSchedule.H1, DrugSchedule.X)
    ):
        return "critical", levels["critical"]
    if product.drug_schedule is DrugSchedule.H or product.is_prescription_required:
        return "high", levels["high"]
    return "standard", levels["standard"]


@dataclass
class Sourcing:
    """Where a replenishment would come from, and on what terms."""

    supplier_id: int | None
    supplier_name: str
    #: Median observed lead time, or the quoted one when nothing is measured.
    lead_time_days: float
    #: Standard deviation of observed lead times. 0 when unmeasured.
    lead_time_sd: float
    p90_days: float
    measured: bool
    unit_cost: Decimal
    moq: Decimal
    pack_qty: Decimal
    via_central: bool


@dataclass
class Recommendation:
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str

    on_hand: float
    on_order: float
    #: on_hand + on_order — what the reorder point is actually compared against.
    position: float
    #: On unapproved drafts. Not part of the position; see _drafted().
    drafted_qty: float
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
    urgency: str
    service_level: str
    sourcing: Sourcing
    estimated_cost: float
    #: The sentence a buyer reads instead of the formula.
    reason: str
    #: Every input, so the number can be checked rather than believed.
    workings: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.warehouse_id}:{self.product_id}"


def _positions(db: Session) -> dict[tuple[int, int], float]:
    """Sellable stock on hand, per product and location.

    Available rather than on-hand: stock in quarantine or already reserved
    against a sales order cannot satisfy tomorrow's demand, and counting it
    would delay a reorder until the shelf is genuinely empty.
    """
    rows = db.execute(
        select(
            StockBalance.product_id,
            StockBalance.warehouse_id,
            func.sum(StockBalance.qty_on_hand - StockBalance.qty_reserved),
        )
        .where(StockBalance.status == StockStatus.AVAILABLE)
        .group_by(StockBalance.product_id, StockBalance.warehouse_id)
    ).all()
    return {(p, w): float(q or 0) for p, w, q in rows}


def _on_order(db: Session) -> dict[tuple[int, int], float]:
    """Quantity already bought and not yet received.

    Leaving this out is the classic double-ordering bug: the screen shows a
    branch below its reorder point on Monday, somebody orders, and on Tuesday
    it still shows below because the goods are on a lorry.
    """
    rows = db.execute(
        select(
            PurchaseOrderLine.product_id,
            PurchaseOrder.warehouse_id,
            func.sum(PurchaseOrderLine.qty_ordered - PurchaseOrderLine.qty_received),
        )
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(
            PurchaseOrder.status.in_(
                (
                    DocumentStatus.APPROVED,
                    DocumentStatus.PARTIALLY_RECEIVED,
                    DocumentStatus.PENDING_APPROVAL,
                )
            )
        )
        .group_by(PurchaseOrderLine.product_id, PurchaseOrder.warehouse_id)
    ).all()
    return {(p, w): max(0.0, float(q or 0)) for p, w, q in rows}


def _drafted(db: Session) -> dict[tuple[int, int], tuple[float, list[str]]]:
    """Quantity sitting on unapproved draft orders, with their numbers.

    Deliberately NOT counted as inventory position: a draft is not on order,
    it is a piece of paper, and treating it as incoming stock would let a
    branch run dry behind a PO nobody ever approved.

    But it cannot be ignored either. A buyer who raises a draft from this
    screen and refreshes it must not be shown the same suggestion again — that
    is how the same 5,000 syringes get ordered twice. So drafts suppress the
    suggested quantity and say why, while the underlying shortage stays
    visible until the goods are genuinely on their way.
    """
    rows = db.execute(
        select(
            PurchaseOrderLine.product_id,
            PurchaseOrder.warehouse_id,
            func.sum(PurchaseOrderLine.qty_ordered),
            func.array_agg(PurchaseOrder.po_number.distinct()),
        )
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(PurchaseOrder.status == DocumentStatus.DRAFT)
        .group_by(PurchaseOrderLine.product_id, PurchaseOrder.warehouse_id)
    ).all()
    return {(p, w): (float(q or 0), list(numbers or [])) for p, w, q, numbers in rows}


def _sourcing_options(db: Session) -> dict[int, list[ProductSupplier]]:
    links: dict[int, list[ProductSupplier]] = {}
    for link in db.scalars(select(ProductSupplier)):
        links.setdefault(link.product_id, []).append(link)
    # Preferred first, then cheapest — the order a buyer would consider them in.
    for options in links.values():
        options.sort(key=lambda link: (not link.is_preferred, link.unit_cost))
    return links


def _choose_sourcing(
    product: Product,
    warehouse: Warehouse,
    options: list[ProductSupplier],
    stats: dict[int, leadtime.LeadTimeStats],
    supplier_names: dict[int, str],
) -> Sourcing:
    """Which supplier, and what its lead time really looks like.

    Measured lead times beat quoted ones whenever there are enough deliveries
    to measure. A quoted 5-day lead time from a distributor whose median is 8
    is not a fact about the world, it is a sales document.
    """
    via_central = (
        product.sourcing_policy is SourcingPolicy.VIA_CENTRAL
        and not warehouse.is_central
    )

    if not options:
        return Sourcing(
            supplier_id=None,
            supplier_name="No supplier on file",
            lead_time_days=float(product.safety_stock_days or 7),
            lead_time_sd=0.0,
            p90_days=float(product.safety_stock_days or 7),
            measured=False,
            unit_cost=Decimal("0"),
            moq=Decimal("1"),
            pack_qty=Decimal("1"),
            via_central=via_central,
        )

    link = options[0]
    stat = stats.get(link.supplier_id)
    measured = bool(stat and stat.reliable)
    return Sourcing(
        supplier_id=link.supplier_id,
        supplier_name=supplier_names.get(link.supplier_id, f"Supplier {link.supplier_id}"),
        lead_time_days=stat.median_days if measured else float(link.lead_time_days),
        lead_time_sd=stat.std_dev if measured else 0.0,
        p90_days=stat.p90_days if measured else float(link.lead_time_days),
        measured=measured,
        unit_cost=link.unit_cost,
        moq=link.moq or Decimal("1"),
        pack_qty=link.pack_qty or Decimal("1"),
        via_central=via_central,
    )


def _round_to_pack(quantity: float, sourcing: Sourcing) -> float:
    """Up to the next whole pack, and never below the minimum order.

    Always up, never down: rounding a shortfall down guarantees the branch is
    still short after the delivery arrives, which is the one outcome the whole
    exercise exists to prevent.
    """
    pack = float(sourcing.pack_qty) or 1.0
    moq = float(sourcing.moq) or 1.0
    packs = max(1.0, np.ceil(quantity / pack))
    return float(max(moq, packs * pack))


def _urgency(
    on_hand: float, position: float, days_of_cover: float,
    lead_time: float, reorder_point: float, review_period: int = REVIEW_PERIOD_DAYS,
) -> str:
    """Ranked by whether the goods can arrive in time, not by raw days left.

    Ten days of cover is comfortable behind a 3-day supplier and an emergency
    behind a 15-day one. Sorting a buyer's list by days of stock alone puts the
    wrong branch at the top.

    The reorder point is the last word. A branch can look comfortable on days
    of cover and still sit below it — that is precisely the case the safety
    stock exists for, and calling it "ok" while suggesting an order would be
    the screen contradicting itself.
    """
    if on_hand <= 0:
        return "stockout"
    if days_of_cover < lead_time:
        return "critical"
    if position <= reorder_point or days_of_cover < lead_time + review_period:
        return "soon"
    return "ok"


URGENCY_ORDER = {"stockout": 0, "critical": 1, "soon": 2, "ok": 3}


def recommend(
    db: Session,
    *,
    warehouse_id: int | None = None,
    horizon_days: int | None = None,
    include_ok: bool = False,
) -> list[Recommendation]:
    """What to order, where, and why."""
    review_period = app_settings.get(db, "reorder.review_period_days")
    max_cover = app_settings.get(db, "reorder.max_cover_days")
    min_demand = app_settings.get(db, "reorder.min_daily_demand")
    levels = {
        "critical": app_settings.get(db, "reorder.service_critical"),
        "high": app_settings.get(db, "reorder.service_high"),
        "standard": app_settings.get(db, "reorder.service_standard"),
    }

    forecasts = forecasting.forecast_all(
        db, horizon=horizon_days, warehouse_id=warehouse_id
    )
    on_hand = _positions(db)
    on_order = _on_order(db)
    drafted = _drafted(db)
    links = _sourcing_options(db)
    supplier_names = dict(db.execute(select(Supplier.id, Supplier.name)).all())
    stats = {s.supplier_id: s for s in leadtime.all_suppliers(db)}
    products = {p.id: p for p in db.scalars(select(Product))}
    warehouses = {w.id: w for w in db.scalars(select(Warehouse))}

    out: list[Recommendation] = []
    for forecast in forecasts:
        product = products.get(forecast.product_id)
        warehouse = warehouses.get(forecast.warehouse_id)
        if product is None or warehouse is None or not product.is_active:
            continue

        demand = forecast.daily_mean
        if demand < min_demand:
            continue

        sourcing = _choose_sourcing(
            product, warehouse, links.get(product.id, []), stats, supplier_names
        )
        level_name, z = service_level(product, levels)

        lead = max(1.0, sourcing.lead_time_days)
        # The forecast's own held-out error stands in for the daily demand
        # deviation. It is the honest estimate: it already includes whatever
        # the model failed to capture.
        sigma_demand = max(forecast.accuracy.mae, demand * 0.15)
        sigma_lead = sourcing.lead_time_sd

        safety = z * float(
            np.sqrt(lead * sigma_demand**2 + (demand**2) * sigma_lead**2)
        )
        reorder_point = demand * lead + safety
        order_up_to = demand * (lead + review_period) + safety
        # The ceiling is on the resulting stock position, not on the order, so
        # a branch that is already overstocked is never told to buy more.
        order_up_to = min(order_up_to, demand * max_cover)

        key = (product.id, warehouse.id)
        have = on_hand.get(key, 0.0)
        ordered = on_order.get(key, 0.0)
        position = have + ordered
        draft_qty, draft_numbers = drafted.get(key, (0.0, []))

        cover = have / demand if demand else 0.0
        urgency = _urgency(have, position, cover, lead, reorder_point, review_period)
        if urgency == "ok" and not include_ok:
            continue

        # Drafts are netted off the suggestion but not off the position, so
        # the shortage stays visible while the order stops being repeated.
        shortfall = order_up_to - position - draft_qty
        suggested = _round_to_pack(shortfall, sourcing) if shortfall > 0 else 0.0

        stockout_on = (
            date.today() + timedelta(days=int(cover)) if demand > 0 else None
        )

        # Written as a buyer would say it, and naming the supplier's spread
        # when that is what is driving the buffer — that sentence is the thing
        # that gets a distributor renegotiated.
        supply_note = ""
        if sourcing.measured and sourcing.lead_time_sd >= 3:
            supply_note = (
                f" {sourcing.supplier_name} is erratic "
                f"({sourcing.lead_time_days:.0f}d typical, {sourcing.p90_days:.0f}d "
                f"at worst), which is most of the {safety:,.0f}-unit buffer."
            )
        if urgency == "stockout":
            reason = (
                f"Out of stock. {sourcing.supplier_name} takes about "
                f"{lead:.0f} days." + supply_note
            )
        elif urgency == "critical":
            reason = (
                f"{cover:.0f} days of cover left but {lead:.0f} days to "
                f"resupply — order today or the shelf empties first." + supply_note
            )
        elif urgency == "soon" and position <= reorder_point:
            reason = (
                f"{position:,.0f} in stock and on order, against a reorder point "
                f"of {reorder_point:,.0f}. Time to buy." + supply_note
            )
        elif urgency == "soon":
            reason = (
                f"{cover:.0f} days of cover. Due to order within "
                f"{max(0, cover - lead):.0f} days." + supply_note
            )
        else:
            reason = f"{cover:.0f} days of cover — comfortable."

        if draft_qty > 0:
            reason += (
                f" {draft_qty:,.0f} units are already on draft "
                f"{', '.join(draft_numbers)}, waiting for approval."
            )

        out.append(
            Recommendation(
                product_id=product.id,
                sku=product.sku,
                product_name=product.name,
                warehouse_id=warehouse.id,
                warehouse_name=warehouse.name,
                on_hand=round(have, 1),
                on_order=round(ordered, 1),
                position=round(position, 1),
                drafted_qty=round(draft_qty, 1),
                draft_po_numbers=draft_numbers,
                daily_demand=round(demand, 2),
                forecast_confidence=forecast.confidence,
                forecast_method=forecast.method,
                lead_time_days=round(lead, 1),
                safety_stock=round(safety, 1),
                reorder_point=round(reorder_point, 1),
                order_up_to=round(order_up_to, 1),
                suggested_qty=round(suggested, 1),
                days_of_cover=round(cover, 1),
                stockout_date=stockout_on,
                urgency=urgency,
                service_level=level_name,
                sourcing=sourcing,
                estimated_cost=round(suggested * float(sourcing.unit_cost), 2),
                reason=reason,
                workings={
                    "daily_demand": round(demand, 2),
                    "lead_time_days": round(lead, 1),
                    "demand_over_lead_time": round(demand * lead, 1),
                    "service_level": level_name,
                    "z": z,
                    "sigma_demand": round(sigma_demand, 2),
                    "sigma_lead_time": round(sigma_lead, 2),
                    "safety_stock": round(safety, 1),
                    "reorder_point": round(reorder_point, 1),
                    "order_up_to": round(order_up_to, 1),
                    "position": round(position, 1),
                    "on_draft_orders": round(draft_qty, 1),
                    "review_period_days": review_period,
                    "rounded_to_pack": float(sourcing.pack_qty),
                    "minimum_order": float(sourcing.moq),
                },
            )
        )

    out.sort(key=lambda r: (URGENCY_ORDER[r.urgency], -r.estimated_cost))
    return out


def summarise(recommendations: list[Recommendation]) -> dict:
    return {
        "total": len(recommendations),
        "stockout": sum(1 for r in recommendations if r.urgency == "stockout"),
        "critical": sum(1 for r in recommendations if r.urgency == "critical"),
        "soon": sum(1 for r in recommendations if r.urgency == "soon"),
        "estimated_cost": round(sum(r.estimated_cost for r in recommendations), 2),
        "lines": sum(1 for r in recommendations if r.suggested_qty > 0),
    }


def group_for_orders(
    recommendations: list[Recommendation],
) -> dict[tuple[int | None, int], list[Recommendation]]:
    """Bundle suggestions into the purchase orders they would become.

    One order per (supplier, destination). A buyer raising six separate POs
    on the same distributor in one morning is how delivery charges and
    reconciliation errors multiply.
    """
    grouped: dict[tuple[int | None, int], list[Recommendation]] = {}
    for rec in recommendations:
        if rec.suggested_qty <= 0 or rec.sourcing.supplier_id is None:
            continue
        # A VIA_CENTRAL product is bought by the central warehouse and moved
        # out on a transfer, so the purchase order is raised there — ordering
        # it straight to the branch would bypass the policy the product
        # explicitly declares.
        destination = rec.warehouse_id
        grouped.setdefault((rec.sourcing.supplier_id, destination), []).append(rec)
    return grouped
