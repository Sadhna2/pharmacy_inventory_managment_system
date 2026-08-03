"""Supplier lead-time analysis.

How long does a distributor actually take, measured rather than assumed? The
supplier record carries `payment_terms_days` and each product/supplier link
carries a quoted `lead_time_days`, but a quote is a sales promise. The purchase
orders and goods receipts already in the ledger say what happened.

WHY THE MEDIAN AND P90, NOT THE MEAN
------------------------------------
Delivery times are not symmetric. A supplier cannot arrive minus three days
early, but can arrive twelve days late, so the distribution has a long right
tail and the mean sits above the typical delivery while understating the bad
one. Both numbers matter and they answer different questions:

    median  what to expect  -> when will this order land?
    p90     what to survive -> how much safety stock do I need?

Planning on the mean is the classic mistake: it is neither the normal case nor
the bad case, so it is wrong in both directions. Reorder recommendations
consume `p90_days` from here for exactly that reason.

This is deliberately statistics, not machine learning. With a few hundred
deliveries per supplier, percentiles over the actual record are more accurate
AND more explainable than any model fitted to them — and a buyer who is going
to phone a distributor about being late needs to be able to say why.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median, pstdev

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.models.documents import GoodsReceipt, GoodsReceiptLine, PurchaseOrder
from app.models.masters import Product, Supplier
from app.services import settings as app_settings

#: Below this, percentiles are noise dressed up as insight. Reported, but
#: flagged so nothing downstream treats it as established.
#:
#: The shipped default; an administrator can raise or lower it under
#: Setup -> Settings. Read through `app.services.settings` at call time rather
#: than captured at import, so a change takes effect on the next request
#: instead of the next deploy.
MIN_SAMPLE = 5

#: A delivery is "on time" if it lands by the date promised on the order.
#: Same day counts.
ON_TIME_GRACE_DAYS = 0


@dataclass
class Delivery:
    """One order-to-arrival observation."""

    supplier_id: int
    po_id: int
    po_number: str
    ordered: date
    promised: date | None
    received: date
    days: int

    @property
    def late_by(self) -> int | None:
        if self.promised is None:
            return None
        return (self.received - self.promised).days


@dataclass
class LeadTimeStats:
    supplier_id: int
    supplier_name: str
    deliveries: int
    median_days: float
    p90_days: float
    mean_days: float
    std_dev: float
    min_days: int
    max_days: int
    on_time_rate: float
    #: Median of the most recent third versus the oldest third. Positive means
    #: they are getting slower.
    trend_days: float
    #: False when the sample is too small to draw a conclusion from.
    reliable: bool

    @property
    def verdict(self) -> str:
        """One sentence a buyer can act on."""
        if not self.reliable:
            return f"Only {self.deliveries} deliveries on record — too few to judge."
        spread = self.p90_days - self.median_days
        if self.on_time_rate >= 0.9 and spread <= 2:
            return "Dependable — plan on the median and hold little cover."
        if spread >= 5:
            return (
                f"Erratic. Usually {self.median_days:.0f} days, but 1 in 10 takes "
                f"{self.p90_days:.0f}+. Carry cover for the bad case."
            )
        if self.on_time_rate < 0.7:
            return (
                f"Misses its own promised date {(1 - self.on_time_rate):.0%} of "
                f"the time. Order earlier than they quote."
            )
        return "Broadly predictable, with occasional slippage."


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    No interpolation on purpose: with 20 observations an interpolated p90
    invents a delivery time that never happened. The rank method always returns
    a duration the supplier actually took.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(fraction * len(ordered) + 0.5)))
    return float(ordered[rank - 1])


def _deliveries_query(since: date | None) -> Select:
    """PO order date to first receipt against it.

    First receipt, not last: a part-shipment means the goods started arriving,
    and measuring to the final trickle would blame a supplier for a back-order
    they told you about. `MIN(received_at)` per order is the honest reading.
    """
    first_receipt = (
        select(
            GoodsReceipt.purchase_order_id.label("po_id"),
            func.min(GoodsReceipt.received_at).label("received_at"),
        )
        .where(GoodsReceipt.purchase_order_id.is_not(None))
        .group_by(GoodsReceipt.purchase_order_id)
        .subquery()
    )
    stmt = (
        select(
            PurchaseOrder.supplier_id,
            PurchaseOrder.id,
            PurchaseOrder.po_number,
            PurchaseOrder.order_date,
            PurchaseOrder.expected_date,
            first_receipt.c.received_at,
        )
        .join(first_receipt, first_receipt.c.po_id == PurchaseOrder.id)
        .order_by(PurchaseOrder.order_date)
    )
    if since:
        stmt = stmt.where(PurchaseOrder.order_date >= since)
    return stmt


def load_deliveries(
    db: Session, *, since: date | None = None, supplier_id: int | None = None
) -> list[Delivery]:
    stmt = _deliveries_query(since)
    if supplier_id is not None:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)

    out: list[Delivery] = []
    for sup_id, po_id, po_number, ordered, promised, received_at in db.execute(stmt):
        received = received_at.date()
        days = (received - ordered).days
        # A receipt dated before its own order is a data-entry error, not a
        # delivery in negative time. Drop it rather than let it drag the median.
        if days < 0:
            continue
        out.append(
            Delivery(
                supplier_id=sup_id,
                po_id=po_id,
                po_number=po_number,
                ordered=ordered,
                promised=promised,
                received=received,
                days=days,
            )
        )
    return out


def summarise(
    deliveries: list[Delivery], name: str, supplier_id: int, *,
    min_sample: int = MIN_SAMPLE,
) -> LeadTimeStats:
    days = [float(d.days) for d in deliveries]
    n = len(days)
    if n == 0:
        return LeadTimeStats(
            supplier_id=supplier_id, supplier_name=name, deliveries=0,
            median_days=0, p90_days=0, mean_days=0, std_dev=0,
            min_days=0, max_days=0, on_time_rate=0, trend_days=0, reliable=False,
        )

    promised = [d for d in deliveries if d.promised is not None]
    on_time = (
        sum(1 for d in promised if (d.late_by or 0) <= ON_TIME_GRACE_DAYS) / len(promised)
        if promised
        else 0.0
    )

    # Trend needs enough history to split into thirds and still say anything.
    trend = 0.0
    if n >= 9:
        third = n // 3
        ordered_by_date = sorted(deliveries, key=lambda d: d.ordered)
        early = [float(d.days) for d in ordered_by_date[:third]]
        late = [float(d.days) for d in ordered_by_date[-third:]]
        trend = round(median(late) - median(early), 1)

    return LeadTimeStats(
        supplier_id=supplier_id,
        supplier_name=name,
        deliveries=n,
        median_days=round(median(days), 1),
        p90_days=round(_percentile(days, 0.9), 1),
        mean_days=round(sum(days) / n, 1),
        std_dev=round(pstdev(days), 1) if n > 1 else 0.0,
        min_days=int(min(days)),
        max_days=int(max(days)),
        on_time_rate=round(on_time, 3),
        trend_days=trend,
        reliable=n >= min_sample,
    )


def all_suppliers(
    db: Session, *, lookback_days: int | None = None
) -> list[LeadTimeStats]:
    lookback_days = lookback_days or app_settings.get(db, "leadtime.lookback_days")
    min_sample = app_settings.get(db, "leadtime.min_sample")
    since = clock.today() - timedelta(days=lookback_days) if lookback_days else None
    deliveries = load_deliveries(db, since=since)

    by_supplier: dict[int, list[Delivery]] = {}
    for delivery in deliveries:
        by_supplier.setdefault(delivery.supplier_id, []).append(delivery)

    names = dict(db.execute(select(Supplier.id, Supplier.name)).all())
    stats = [
        summarise(items, names.get(sid, f"Supplier {sid}"), sid, min_sample=min_sample)
        for sid, items in by_supplier.items()
    ]
    # Worst first: this list exists to start conversations with distributors.
    return sorted(stats, key=lambda s: (-s.p90_days, s.supplier_name))


def predict(db: Session, supplier_id: int, *, lookback_days: int | None = None) -> dict:
    """What to expect, and what to plan for, on the next order.

    Two numbers rather than one, because a single "predicted lead time" would
    have to choose between describing the normal case and protecting against
    the bad one, and cannot do both.
    """
    lookback_days = lookback_days or app_settings.get(db, "leadtime.lookback_days")
    since = clock.today() - timedelta(days=lookback_days) if lookback_days else None
    deliveries = load_deliveries(db, since=since, supplier_id=supplier_id)
    supplier = db.get(Supplier, supplier_id)
    name = supplier.name if supplier else f"Supplier {supplier_id}"
    stats = summarise(
        deliveries, name, supplier_id,
        min_sample=app_settings.get(db, "leadtime.min_sample"),
    )

    today = clock.today()
    return {
        "stats": stats,
        "expected_date": today + timedelta(days=round(stats.median_days)),
        "plan_for_date": today + timedelta(days=round(stats.p90_days)),
        # What the reorder engine adds on top of demand cover. Never negative:
        # a supplier who beats their median does not earn you negative stock.
        "safety_days": max(0.0, round(stats.p90_days - stats.median_days, 1)),
    }


def by_product(db: Session, supplier_id: int, *, limit: int = 20) -> list[dict]:
    """Which products this supplier actually delivers, and how much of them.

    Lead time is a property of the relationship, but the exposure is per
    product: a slow supplier on a fast-moving line is a very different problem
    from a slow supplier on something that sells twice a month.
    """
    rows = db.execute(
        select(
            Product.id,
            Product.sku,
            Product.name,
            func.count(GoodsReceiptLine.id).label("receipts"),
            func.sum(GoodsReceiptLine.quantity).label("units"),
        )
        .join(GoodsReceiptLine, GoodsReceiptLine.product_id == Product.id)
        .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id)
        .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
        .where(PurchaseOrder.supplier_id == supplier_id)
        .group_by(Product.id, Product.sku, Product.name)
        .order_by(func.sum(GoodsReceiptLine.quantity).desc())
        .limit(limit)
    ).all()
    return [
        {
            "product_id": pid,
            "sku": sku,
            "product_name": name,
            "receipts": receipts,
            "units": units,
        }
        for pid, sku, name, receipts, units in rows
    ]
