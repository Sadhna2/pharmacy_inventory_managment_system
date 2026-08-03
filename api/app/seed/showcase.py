"""The states the simulation never reaches, added on top of it.

    python -m app.seed.showcase

Run last, after bootstrap and history:

    scripts/db.sh reset
    alembic upgrade head
    python -m app.seed.bootstrap
    python -m app.seed.history --days 730
    python -m app.seed.showcase

The generator in `history` models a chain that works. Goods are ordered,
delivered, transferred and dispensed, and that loop is what Layer 2 needs. But
a chain that only ever works never produces the rows that half the product is
built to display: nothing is ever dropped, nothing comes back from a customer,
no batch fails QC, no product is ever withdrawn from the catalogue. Those
filters and badges were therefore all reachable and all empty — a status
dropdown with four options, three of which returned nothing.

Everything here goes through the ordinary services, so these records are
indistinguishable from ones made by hand in the UI: the ledger stays
append-only, the balance projection is maintained by the same trigger, and
separation of duties is respected (whoever raises a document is not whoever
approves it).

Idempotent. Re-running replaces the previous showcase rather than doubling it.
"""

import argparse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.documents import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Recall,
    SalesOrder,
    SalesOrderLine,
    Shipment,
    ShipmentLine,
    StockAdjustment,
    StockAdjustmentLine,
    StockTransfer,
    StockTransferLine,
)
from app.models.enums import (
    DrugSchedule,
    MovementType,
    RecallStatus,
    SourcingPolicy,
    StockStatus,
    StorageCondition,
    TrackingMode,
)
from app.models.identity import User
from app.models.masters import (
    Category,
    Customer,
    Product,
    ProductSupplier,
    Supplier,
    Uom,
    Warehouse,
)
from app.models.stock import Lot, StockBalance, StockReservation
from app.services import ledger, procurement, recall, sales, transfers

#: Stamped on every document this module creates, so a re-run can find and
#: remove its own previous output without touching anything else.
TAG = "SHOWCASE"


# ------------------------------------------------------------------ context


class Ctx:
    """The handful of records everything below is built from."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.today = date.today()

        def one(model, *where):
            row = db.scalar(select(model).where(*where))
            if row is None:
                raise SystemExit(
                    f"No {model.__name__} matching {where} — run app.seed.bootstrap "
                    "and app.seed.history first."
                )
            return row

        self.admin = one(User, User.email.like("admin@%"))
        self.manager = one(User, User.email.like("manager@%"))
        self.staff = one(User, User.email.like("staff@%"))
        self.central = one(Warehouse, Warehouse.is_central)
        self.branches = list(
            db.scalars(
                select(Warehouse)
                .where(Warehouse.is_active, ~Warehouse.is_central)
                .order_by(Warehouse.id)
            )
        )
        if not self.branches:
            raise SystemExit("No branch warehouses found.")
        self.supplier = one(Supplier, Supplier.is_active)
        self.customer = one(Customer, Customer.is_active)
        self.uom = one(Uom)
        self.category = one(Category)

    def bin_at(self, warehouse_id: int) -> int | None:
        """The bin the rest of the data uses for this location.

        The balance projection is keyed on bin, so a movement that invents a
        different one lands on a row nothing else can see.
        """
        return self.db.scalar(
            select(func.min(StockBalance.bin_id)).where(
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.bin_id.is_not(None),
            )
        )

    def stocked(self, warehouse_id: int, *, minimum: int = 60) -> StockBalance | None:
        """An available, batch-tracked balance big enough to carve pieces off."""
        return self.db.scalar(
            select(StockBalance)
            .join(Product, Product.id == StockBalance.product_id)
            .where(
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.status == StockStatus.AVAILABLE,
                StockBalance.qty_on_hand - StockBalance.qty_reserved >= minimum,
                StockBalance.lot_id.is_not(None),
                Product.tracking_mode == TrackingMode.LOT_EXPIRY,
                Product.is_active,
            )
            .order_by(StockBalance.qty_on_hand.desc())
        )


# --------------------------------------------------------------------- undo


def clear(db: Session) -> None:
    """Remove a previous showcase run.

    Ledger rows are left alone. They are append-only by trigger and by
    principle: a demonstration of an audit trail that quietly deletes its own
    history would be demonstrating the opposite. Re-running therefore adds a
    second damage event rather than rewriting the first, which is correct —
    that is what would happen in production too.
    """
    for model, where in (
        (
            StockAdjustmentLine,
            StockAdjustmentLine.stock_adjustment_id.in_(
                select(StockAdjustment.id).where(StockAdjustment.notes == TAG)
            ),
        ),
        (StockAdjustment, StockAdjustment.notes == TAG),
        (
            StockTransferLine,
            StockTransferLine.stock_transfer_id.in_(
                select(StockTransfer.id).where(StockTransfer.notes == TAG)
            ),
        ),
        (StockTransfer, StockTransfer.notes == TAG),
        # A shipped order trails shipment lines and consumed reservations, and
        # both point back at the order line. Delete inwards from the leaves or
        # the foreign keys refuse — which is the schema doing its job.
        (
            ShipmentLine,
            ShipmentLine.shipment_id.in_(
                select(Shipment.id).where(
                    Shipment.sales_order_id.in_(
                        select(SalesOrder.id).where(SalesOrder.notes == TAG)
                    )
                )
            ),
        ),
        (
            Shipment,
            Shipment.sales_order_id.in_(
                select(SalesOrder.id).where(SalesOrder.notes == TAG)
            ),
        ),
        (
            StockReservation,
            StockReservation.sales_order_line_id.in_(
                select(SalesOrderLine.id).where(
                    SalesOrderLine.sales_order_id.in_(
                        select(SalesOrder.id).where(SalesOrder.notes == TAG)
                    )
                )
            ),
        ),
        (
            SalesOrderLine,
            SalesOrderLine.sales_order_id.in_(
                select(SalesOrder.id).where(SalesOrder.notes == TAG)
            ),
        ),
        (SalesOrder, SalesOrder.notes == TAG),
        (
            GoodsReceiptLine,
            GoodsReceiptLine.goods_receipt_id.in_(
                select(GoodsReceipt.id).where(
                    GoodsReceipt.purchase_order_id.in_(
                        select(PurchaseOrder.id).where(PurchaseOrder.notes == TAG)
                    )
                )
            ),
        ),
        (
            GoodsReceipt,
            GoodsReceipt.purchase_order_id.in_(
                select(PurchaseOrder.id).where(PurchaseOrder.notes == TAG)
            ),
        ),
        (
            PurchaseOrderLine,
            PurchaseOrderLine.purchase_order_id.in_(
                select(PurchaseOrder.id).where(PurchaseOrder.notes == TAG)
            ),
        ),
        (PurchaseOrder, PurchaseOrder.notes == TAG),
    ):
        db.execute(delete(model).where(where))
    db.commit()


# ------------------------------------------------------------- master data


def catalogue(ctx: Ctx) -> dict[str, int]:
    """Products that make the catalogue filters mean something.

    Before this the drug-schedule filter offered five choices and the data held
    three of them, the storage filter offered four and held two, and "Show
    retired" was a button that could not change what was on screen because no
    product had ever been retired.
    """
    db, made = ctx.db, {"created": 0, "retired": 0}

    #: sku -> (name, schedule, storage, tracking, rx, mrp, gst, active)
    EXTRA = [
        # Schedule X: narcotics. The strictest register in Indian pharmacy law,
        # and the reason the schedule filter exists at all.
        ("MOR-10", "Morphine Sulphate 10mg", DrugSchedule.X, StorageCondition.AMBIENT,
         TrackingMode.LOT_EXPIRY, True, "148.00", "12.0", True),
        # Schedule G: dispensed on prescription with a cautionary label.
        ("MTX-2.5", "Methotrexate 2.5mg", DrugSchedule.G, StorageCondition.AMBIENT,
         TrackingMode.LOT_EXPIRY, True, "236.50", "12.0", True),
        # COOL is 8-15 C — distinct from the 2-8 C cold chain, and the reason
        # the storage rule is a range rather than a flag.
        ("SUP-PRO", "Probiotic Suppository", DrugSchedule.OTC, StorageCondition.COOL,
         TrackingMode.LOT_EXPIRY, False, "310.00", "12.0", True),
        ("VAC-RAB", "Rabies Vaccine 1ml", DrugSchedule.H, StorageCondition.FROZEN,
         TrackingMode.LOT_EXPIRY, True, "1450.00", "5.0", True),
        # Withdrawn products. Retired rather than deleted: they are still on
        # ledger rows going back two years, and a catalogue that forgets them
        # makes that history unreadable.
        ("RAN-150", "Ranitidine 150mg", DrugSchedule.H, StorageCondition.AMBIENT,
         TrackingMode.LOT_EXPIRY, True, "28.00", "12.0", False),
        ("NIM-100", "Nimesulide 100mg", DrugSchedule.H, StorageCondition.AMBIENT,
         TrackingMode.LOT_EXPIRY, True, "34.50", "12.0", False),
    ]

    for sku, name, sched, storage, tracking, rx, mrp, gst, active in EXTRA:
        product = db.scalar(select(Product).where(Product.sku == sku))
        if product is None:
            product = Product(
                sku=sku,
                name=name,
                category_id=ctx.category.id,
                uom_id=ctx.uom.id,
                tracking_mode=tracking,
                drug_schedule=sched,
                storage_condition=storage,
                is_prescription_required=rx,
                hsn_code="30049099",
                gst_rate=Decimal(gst),
                mrp=Decimal(mrp),
                reorder_point=Decimal("40"),
                safety_stock_days=7,
                sourcing_policy=SourcingPolicy.VIA_CENTRAL,
                pack_size="1 x 10",
                manufacturer="Generic Pharma Ltd",
            )
            db.add(product)
            db.flush()
            db.add(
                ProductSupplier(
                    product_id=product.id,
                    supplier_id=ctx.supplier.id,
                    # Bought at a shade over two-thirds of the printed price,
                    # which is roughly the margin the rest of the fixture uses.
                    unit_cost=(Decimal(mrp) * Decimal("0.7")).quantize(Decimal("0.01")),
                    lead_time_days=7,
                    is_preferred=True,
                )
            )
            made["created"] += 1

        product.is_active = active
        if not active:
            made["retired"] += 1

    db.commit()
    return made


def opening_stock(ctx: Ctx) -> int:
    """Put the new products on a shelf, with expiries spread across the buckets.

    The expiry filter offers "within 30 / 60 / 90 / 180 days" and "already
    expired". Those windows are only demonstrable if batches actually sit in
    each of them, so the dates here are chosen to land one in every bucket
    rather than being drawn from a distribution that might miss three.
    """
    db, posted = ctx.db, 0
    windows = [-14, 21, 45, 75, 150, 400]

    active = list(
        db.scalars(
            select(Product).where(
                Product.is_active,
                Product.sku.in_(["MOR-10", "MTX-2.5", "SUP-PRO", "VAC-RAB"]),
            )
        )
    )
    for index, product in enumerate(active):
        for offset in (windows[index % len(windows)], windows[(index + 3) % len(windows)]):
            expiry = ctx.today + timedelta(days=offset)
            code = f"{TAG}-{product.sku}-{expiry:%y%m}"
            lot = db.scalar(select(Lot).where(Lot.lot_code == code))
            if lot is None:
                lot = Lot(
                    product_id=product.id,
                    lot_code=code,
                    expiry_date=expiry,
                    supplier_id=ctx.supplier.id,
                    mrp=product.mrp,
                )
                db.add(lot)
                db.flush()

            already = db.scalar(
                select(func.coalesce(func.sum(StockBalance.qty_on_hand), 0)).where(
                    StockBalance.lot_id == lot.id
                )
            )
            if already:
                continue

            ledger.post_movement(
                db,
                product_id=product.id,
                warehouse_id=ctx.central.id,
                bin_id=ctx.bin_at(ctx.central.id),
                lot_id=lot.id,
                quantity=Decimal("240"),
                movement_type=MovementType.OPENING_BALANCE,
                user_id=ctx.admin.id,
                unit_cost=(product.mrp or Decimal("100")) * Decimal("0.7"),
                reference_type=TAG,
                notes="Showcase opening stock",
            )
            posted += 1

    db.commit()
    return posted


# ------------------------------------------------------------ stock states


def stock_states(ctx: Ctx) -> dict[str, int]:
    """DAMAGED, RETURNED_PENDING, and the QC pair that resolves them.

    Each of these is a *status change*, posted as two balanced ledger rows:
    nothing is created or destroyed, it just stops being sellable. That is the
    whole argument for tracking status on the balance rather than deleting the
    stock — a carton crushed in the stockroom is still a carton, still counted
    at stocktake, and still someone's loss to explain.
    """
    db, made = ctx.db, {}

    # --- damaged in handling ------------------------------------------------
    for branch in ctx.branches[:2]:
        balance = ctx.stocked(branch.id, minimum=40)
        if balance is None:
            continue
        ledger.post_status_change(
            db,
            product_id=balance.product_id,
            warehouse_id=branch.id,
            bin_id=balance.bin_id,
            lot_id=balance.lot_id,
            quantity=Decimal("12"),
            from_status=StockStatus.AVAILABLE,
            to_status=StockStatus.DAMAGED,
            movement_type=MovementType.DAMAGE,
            user_id=ctx.staff.id,
            reference_type=TAG,
            notes="Carton crushed under a pallet in the stockroom",
        )
        made["damaged"] = made.get("damaged", 0) + 1

    # --- a customer sends stock back ---------------------------------------
    # It arrives back as RETURNED_PENDING, not AVAILABLE. Nobody knows yet
    # whether it was kept at the right temperature on the way, so it is on the
    # premises and counted but not sellable until someone looks at it.
    branch = ctx.branches[0]
    balance = ctx.stocked(branch.id, minimum=80)
    if balance is not None:
        ledger.post_movement(
            db,
            product_id=balance.product_id,
            warehouse_id=branch.id,
            bin_id=balance.bin_id,
            lot_id=balance.lot_id,
            quantity=Decimal("30"),
            movement_type=MovementType.RETURN_IN,
            user_id=ctx.staff.id,
            status=StockStatus.RETURNED_PENDING,
            reference_type=TAG,
            notes=f"Returned unopened by {ctx.customer.name}",
        )
        made["returned"] = 1

        # QC looks at it: most of it goes back on the shelf, the rest does not.
        ledger.post_status_change(
            db,
            product_id=balance.product_id,
            warehouse_id=branch.id,
            bin_id=balance.bin_id,
            lot_id=balance.lot_id,
            quantity=Decimal("18"),
            from_status=StockStatus.RETURNED_PENDING,
            to_status=StockStatus.AVAILABLE,
            movement_type=MovementType.QC_RELEASE,
            user_id=ctx.manager.id,
            reference_type=TAG,
            notes="Seals intact, cold chain log complete — released",
        )
        ledger.post_status_change(
            db,
            product_id=balance.product_id,
            warehouse_id=branch.id,
            bin_id=balance.bin_id,
            lot_id=balance.lot_id,
            quantity=Decimal("6"),
            from_status=StockStatus.RETURNED_PENDING,
            to_status=StockStatus.DAMAGED,
            movement_type=MovementType.QC_REJECT,
            user_id=ctx.manager.id,
            reference_type=TAG,
            notes="Blister foil punctured — rejected",
        )
        made["qc"] = 2

    # --- goods sent back to the distributor --------------------------------
    central = ctx.stocked(ctx.central.id, minimum=60)
    if central is not None:
        ledger.post_movement(
            db,
            product_id=central.product_id,
            warehouse_id=ctx.central.id,
            bin_id=central.bin_id,
            lot_id=central.lot_id,
            quantity=Decimal("-24"),
            movement_type=MovementType.RETURN_OUT,
            user_id=ctx.manager.id,
            reference_type=TAG,
            notes=f"Short-dated stock returned to {ctx.supplier.name} under credit note",
        )
        made["return_out"] = 1

    # --- a batch reaches its expiry date -----------------------------------
    dead = db.scalar(
        select(StockBalance)
        .join(Lot, Lot.id == StockBalance.lot_id)
        .where(
            StockBalance.status == StockStatus.AVAILABLE,
            StockBalance.qty_on_hand > 0,
            Lot.expiry_date < ctx.today,
        )
        .order_by(StockBalance.qty_on_hand.desc())
    )
    if dead is not None:
        ledger.post_movement(
            db,
            product_id=dead.product_id,
            warehouse_id=dead.warehouse_id,
            bin_id=dead.bin_id,
            lot_id=dead.lot_id,
            quantity=-min(dead.qty_on_hand, Decimal("40")),
            movement_type=MovementType.EXPIRY_WRITEOFF,
            user_id=ctx.manager.id,
            reference_type=TAG,
            notes="Past printed expiry — written off at the monthly sweep",
        )
        made["expired"] = 1

    db.commit()
    return made


# ------------------------------------------------------------- open documents


def documents(ctx: Ctx) -> dict[str, int]:
    """One live document in each status the operations screens can show.

    The screens badge DRAFT, PENDING_APPROVAL, APPROVED, PARTIALLY_RECEIVED,
    RECEIVED, ALLOCATED, SHIPPED, COMPLETED and CANCELLED. The simulation only
    ever produces the happy path, so most of those badges had no row to sit on.
    """
    db, made = ctx.db, {}
    product = db.scalar(
        select(Product).where(Product.is_active, Product.sku == "PAR-650")
    ) or db.scalar(select(Product).where(Product.is_active))

    # --- purchase orders awaiting a second pair of eyes --------------------
    po = procurement.create_purchase_order(
        db,
        supplier_id=ctx.supplier.id,
        warehouse_id=ctx.central.id,
        lines=[{"product_id": product.id, "qty_ordered": 500, "unit_price": "9.40"}],
        user_id=ctx.staff.id,
        order_date=ctx.today - timedelta(days=2),
        expected_date=ctx.today + timedelta(days=5),
        notes=TAG,
    )
    procurement.submit_purchase_order(db, po.id)
    made["po_pending"] = 1

    # A half-written order nobody has submitted, and one that was called off.
    procurement.create_purchase_order(
        db,
        supplier_id=ctx.supplier.id,
        warehouse_id=ctx.central.id,
        lines=[{"product_id": product.id, "qty_ordered": 200, "unit_price": "9.60"}],
        user_id=ctx.staff.id,
        order_date=ctx.today,
        notes=TAG,
    )
    made["po_draft"] = 1

    dropped = procurement.create_purchase_order(
        db,
        supplier_id=ctx.supplier.id,
        warehouse_id=ctx.central.id,
        lines=[{"product_id": product.id, "qty_ordered": 150, "unit_price": "9.80"}],
        user_id=ctx.staff.id,
        order_date=ctx.today - timedelta(days=4),
        notes=TAG,
    )
    procurement.cancel_purchase_order(db, dropped.id)
    made["po_cancelled"] = 1

    # Raised by one person, approved by another — the separation of duties the
    # Purchasing screen colours amber for "waiting on you".
    approved = procurement.create_purchase_order(
        db,
        supplier_id=ctx.supplier.id,
        warehouse_id=ctx.central.id,
        lines=[{"product_id": product.id, "qty_ordered": 300, "unit_price": "9.55"}],
        user_id=ctx.staff.id,
        order_date=ctx.today - timedelta(days=6),
        expected_date=ctx.today + timedelta(days=1),
        notes=TAG,
    )
    procurement.submit_purchase_order(db, approved.id)
    procurement.approve_purchase_order(db, approved.id, user_id=ctx.manager.id)
    made["po_approved"] = 1
    db.commit()

    # --- a part-delivered order --------------------------------------------
    # The most common real state and the one the receiving screen is built
    # around: 120 of 400 arrived, the rest is still outstanding.
    partial = procurement.create_purchase_order(
        db,
        supplier_id=ctx.supplier.id,
        warehouse_id=ctx.central.id,
        lines=[{"product_id": product.id, "qty_ordered": 400, "unit_price": "9.30"}],
        user_id=ctx.staff.id,
        order_date=ctx.today - timedelta(days=9),
        notes=TAG,
    )
    procurement.submit_purchase_order(db, partial.id)
    procurement.approve_purchase_order(db, partial.id, user_id=ctx.manager.id)
    db.commit()

    line = db.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == partial.id)
    )
    procurement.receive_goods(
        db,
        purchase_order_id=partial.id,
        warehouse_id=ctx.central.id,
        user_id=ctx.staff.id,
        supplier_invoice_no="APX/2026/7741",
        lines=[
            {
                "product_id": product.id,
                "purchase_order_line_id": line.id,
                "quantity": 120,
                "unit_cost": "9.30",
                "lot_code": f"{TAG}-PART-01",
                "expiry_date": (ctx.today + timedelta(days=540)).isoformat(),
            }
        ],
    )
    made["po_partial"] = 1
    db.commit()

    # --- sales orders part-way through the pick ----------------------------
    for state in ("draft", "cancelled", "allocated", "shipped"):
        so = sales.create_sales_order(
            db,
            customer_id=ctx.customer.id,
            warehouse_id=ctx.branches[0].id,
            lines=[{"product_id": product.id, "qty_ordered": 15, "unit_price": "14.50"}],
            user_id=ctx.staff.id,
            order_date=ctx.today - timedelta(days=1),
            notes=TAG,
        )
        db.commit()
        if state == "draft":
            made["so_draft"] = 1
            continue
        if state == "cancelled":
            sales.cancel_order(db, so.id)
            db.commit()
            made["so_cancelled"] = 1
            continue
        try:
            sales.allocate_order(db, so.id)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — reported, not silently skipped
            print(f"  ! could not allocate {so.so_number}: {exc}")
            db.rollback()
            continue
        if state == "allocated":
            made["so_allocated"] = 1
            continue
        try:
            sales.ship_order(db, so.id, user_id=ctx.staff.id)
            db.commit()
            made["so_shipped"] = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not ship {so.so_number}: {exc}")
            db.rollback()

    # --- a transfer someone still has to approve ---------------------------
    source = ctx.stocked(ctx.central.id, minimum=50)
    if source is not None:
        draft = transfers.create_transfer(
            db,
            from_warehouse_id=ctx.central.id,
            to_warehouse_id=ctx.branches[-1].id,
            lines=[{"product_id": source.product_id, "quantity": 25}],
            user_id=ctx.staff.id,
            notes=TAG,
        )
        made["transfer_draft"] = 1
        db.commit()

        moving = transfers.create_transfer(
            db,
            from_warehouse_id=ctx.central.id,
            to_warehouse_id=ctx.branches[0].id,
            lines=[{"product_id": source.product_id, "quantity": 20}],
            user_id=ctx.staff.id,
            notes=TAG,
        )
        db.commit()
        transfers.approve_transfer(db, moving.id, user_id=ctx.manager.id)
        transfers.dispatch_transfer(db, moving.id, user_id=ctx.staff.id)
        db.commit()
        made["transfer_in_transit"] = 1
        _ = draft

    # --- a stocktake correction waiting on approval ------------------------
    counted = ctx.stocked(ctx.branches[1].id, minimum=30) if len(ctx.branches) > 1 else None
    if counted is not None:
        transfers.create_adjustment(
            db,
            warehouse_id=ctx.branches[1].id,
            reason_code="CYCLE_COUNT",
            lines=[
                {
                    "product_id": counted.product_id,
                    "lot_id": counted.lot_id,
                    "bin_id": counted.bin_id,
                    "quantity": "-3",
                }
            ],
            user_id=ctx.staff.id,
            notes=TAG,
        )
        made["adjustment_pending"] = 1
        db.commit()

        # And one that went all the way through, because approval is what
        # posts an adjustment to the ledger — a screen showing only pending
        # ones never demonstrates that.
        settled = transfers.create_adjustment(
            db,
            warehouse_id=ctx.branches[1].id,
            reason_code="DAMAGE",
            lines=[
                {
                    "product_id": counted.product_id,
                    "lot_id": counted.lot_id,
                    "bin_id": counted.bin_id,
                    "quantity": "-2",
                }
            ],
            user_id=ctx.staff.id,
            notes=TAG,
        )
        db.commit()
        transfers.approve_adjustment(db, settled.id, user_id=ctx.manager.id)
        made["adjustment_approved"] = 1
        db.commit()

    return made


# -------------------------------------------------------------------- recalls


def recalls(ctx: Ctx) -> dict[str, int]:
    """One recall in each status, on batches that are genuinely in stock.

    A recall's whole value is the trace it produces, so these are raised
    against batches that have actually moved rather than against an empty lot
    that would list nobody.
    """
    db, made = ctx.db, {}

    held = list(
        db.scalars(
            select(Lot)
            .join(StockBalance, StockBalance.lot_id == Lot.id)
            .join(Product, Product.id == Lot.product_id)
            .where(
                StockBalance.qty_on_hand > 0,
                StockBalance.status == StockStatus.AVAILABLE,
                Product.is_active,
                Lot.expiry_date > ctx.today,
                ~Lot.id.in_(select(Recall.lot_id)),
            )
            .order_by(StockBalance.qty_on_hand.desc())
            .limit(2)
        )
    )

    reasons = [
        ("Dissolution failure at the 6-month stability pull", "CDSCO/RC/2026/0417"),
        ("Supplier notified a mislabelled strength on the carton", "CDSCO/RC/2026/0431"),
    ]
    for lot, (reason, ref) in zip(held, reasons, strict=False):
        impact = recall.initiate_recall(
            db, lot_id=lot.id, reason=reason, user_id=ctx.manager.id, regulator_ref=ref
        )
        made["initiated"] = made.get("initiated", 0) + 1
        db.commit()
        _ = impact

    # And one that ran its course, so the register shows both halves of the
    # lifecycle rather than only the alarming half.
    if held:
        first = db.scalar(
            select(Recall)
            .where(Recall.lot_id == held[0].id, Recall.status != RecallStatus.CLOSED)
            .order_by(Recall.id.desc())
        )
        if first is not None:
            first.initiated_at = datetime.now(UTC) - timedelta(days=21)
            recall.close_recall(db, first.id, user_id=ctx.manager.id)
            made["closed"] = 1
            db.commit()

    return made


# ----------------------------------------------------------------- reporting


def report(db: Session) -> None:
    """Print the coverage this run was for, as counts rather than a claim."""
    print("\nCoverage now on record:")
    for title, sql in (
        ("stock status", select(StockBalance.status, func.count())
            .where(StockBalance.qty_on_hand > 0).group_by(StockBalance.status)),
        ("purchase orders", select(PurchaseOrder.status, func.count())
            .group_by(PurchaseOrder.status)),
        ("sales orders", select(SalesOrder.status, func.count())
            .group_by(SalesOrder.status)),
        ("transfers", select(StockTransfer.status, func.count())
            .group_by(StockTransfer.status)),
        ("adjustments", select(StockAdjustment.status, func.count())
            .group_by(StockAdjustment.status)),
        ("recalls", select(Recall.status, func.count()).group_by(Recall.status)),
        ("products", select(Product.drug_schedule, func.count())
            .group_by(Product.drug_schedule)),
        ("storage", select(Product.storage_condition, func.count())
            .group_by(Product.storage_condition)),
    ):
        rows = db.execute(sql).all()
        pairs = ", ".join(
            f"{getattr(value, 'value', value)}={count}" for value, count in sorted(
                rows, key=lambda r: str(r[0])
            )
        )
        print(f"  {title:18} {pairs}")

    retired = db.scalar(
        select(func.count()).select_from(Product).where(~Product.is_active)
    )
    print(f"  {'retired products':18} {retired}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not clear a previous showcase run first",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        if not args.keep:
            clear(db)

        ctx = Ctx(db)
        print("Adding catalogue coverage…")
        print(f"  products {catalogue(ctx)}")
        print(f"  opening stock lines {opening_stock(ctx)}")
        print("Adding stock states…")
        print(f"  {stock_states(ctx)}")
        print("Adding open documents…")
        print(f"  {documents(ctx)}")
        print("Adding recalls…")
        print(f"  {recalls(ctx)}")

        report(db)


if __name__ == "__main__":
    main()
