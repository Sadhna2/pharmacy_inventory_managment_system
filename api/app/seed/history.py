"""Two years of synthetic trading history.

    python -m app.seed.history                # 730 days, default
    python -m app.seed.history --days 365
    python -m app.seed.history --reset        # wipe a previous run first

Layer 2 is blocked on this. The bootstrap seed leaves 120 ledger rows, all of
them opening balances and not one of them a sale: a forecast fitted on that has
nothing to fit, an anomaly detector has no baseline to deviate from, and a
lead-time model has no deliveries to measure.

This is a SIMULATION, not a random fill. Each day, in order:

  1. deliveries ordered earlier arrive, after the supplier's own lead time
  2. batches that have gone out of date are written off
  3. branches dispense against real demand, drawing FEFO from real batches
  4. anything below its reorder point is reordered

So stock genuinely runs out when replenishment is late, batches genuinely
expire if they sit too long, and a slow supplier genuinely causes a stockout
three weeks later. Those correlations are the signal the Layer 2 features are
meant to find. Sampling numbers from a distribution would produce data with no
such structure, and every model would score the same on it.

Everything goes through the ordinary tables — the ledger stays append-only and
the balance trigger maintains stock_balances exactly as it does in production.
"""

import argparse
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from app.core import clock
from app.core.clock import at_local
from app.db.session import SessionLocal
from app.models.documents import (
    DocumentStatus,
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    StockTransfer,
    StockTransferLine,
)
from app.models.enums import MovementType, StockStatus, TrackingMode
from app.models.identity import User
from app.models.masters import Product, ProductSupplier, Supplier, Warehouse
from app.models.stock import Lot, StockBalance, StockMovement
from app.seed.demand import PROFILES, branch_kind, expected_demand
from app.services import gst

#: Stamped on everything this script creates so --reset can find it again
#: without touching the bootstrap fixture or anything a human entered.
TAG = "SYNTH"

#: Suppliers behave differently on purpose, and that difference IS the
#: lead-time feature. One of them is deliberately unreliable — a long tail and
#: frequent late deliveries — so there is something worth discovering.
#:                   (mean days, std dev, chance of a badly late delivery)
SUPPLIER_RELIABILITY = [
    (3.0, 0.8, 0.02),   # dependable
    (5.0, 1.4, 0.05),
    (7.0, 4.5, 0.22),   # the problem supplier
    (4.0, 1.0, 0.03),
    (6.0, 2.2, 0.10),
]

#: Batch shelf life when it leaves the manufacturer.
SHELF_LIFE_MONTHS = (18, 30)

#: Cover to order: roughly three weeks of demand, which is how a pharmacy
#: buys — often enough to stay fresh, rarely enough to not live on the phone.
COVER_DAYS = 21


@dataclass
class LotState:
    """One batch at one location, tracked as the simulation runs."""

    lot_id: int | None
    expiry: date | None
    qty: Decimal
    cost: Decimal


@dataclass
class Pending:
    """Ordered, not yet arrived."""

    arrives: date
    warehouse_id: int
    product_id: int
    quantity: Decimal
    unit_cost: Decimal
    supplier_id: int | None = None
    po_id: int | None = None
    po_line_id: int | None = None
    transfer_id: int | None = None
    #: Set for a transfer: the batch already exists and travels with the stock.
    lot: LotState | None = None


class Sim:
    """The whole simulation. State is small enough to hold in memory."""

    def __init__(self, db: Session, days: int, seed: int) -> None:
        self.db = db
        self.rng = random.Random(seed)
        self.today = clock.today()
        self.start = self.today - timedelta(days=days)
        self.days = days

        self.movements: list[dict] = []
        self.pending: list[Pending] = []
        #: (product_id, warehouse_id) -> batches on hand
        self.stock: dict[tuple[int, int], list[LotState]] = defaultdict(list)
        self.lot_seq = 0
        self.stats: dict[str, int] = defaultdict(int)
        #: transfer id -> lines still on the road. A transfer's lines are given
        #: independent travel times, so the document is only complete once the
        #: last of them lands. Without this the ledger recorded the receipt but
        #: the document stayed IN_TRANSIT for ever, and the Transfers screen
        #: filled up with deliveries that had demonstrably already arrived.
        self.in_flight: dict[int, int] = defaultdict(int)

        self._load()

    # ------------------------------------------------------------- loading

    def _load(self) -> None:
        db = self.db
        self.products = list(
            db.scalars(
                select(Product).where(Product.is_active, Product.sku.in_(PROFILES))
            )
        )
        if not self.products:
            raise SystemExit("No seeded products found — run app.seed.bootstrap first.")

        warehouses = list(
            db.scalars(
                select(Warehouse).where(Warehouse.is_active).order_by(Warehouse.id)
            )
        )
        self.central = next((w for w in warehouses if w.is_central), None)
        if self.central is None:
            raise SystemExit("No central warehouse found.")
        # The seed's "Temporary Depot" has no staff and no demand profile.
        # Leaving it out keeps it as what it is: an empty location.
        self.branches = [
            w for w in warehouses if not w.is_central and "depot" not in w.name.lower()
        ]
        self.kinds = {
            w.id: branch_kind(w.name, w.is_central) for w in warehouses
        }

        self.suppliers = list(db.scalars(select(Supplier).where(Supplier.is_active)))
        links = db.execute(
            select(ProductSupplier.product_id, ProductSupplier.supplier_id)
        ).all()
        by_product: dict[int, list[int]] = defaultdict(list)
        for product_id, supplier_id in links:
            by_product[product_id].append(supplier_id)
        self.supplier_for = by_product

        manager = db.scalar(select(User).where(User.email.like("manager@%")))
        admin = db.scalar(select(User).where(User.email.like("admin@%")))
        staff = db.scalar(select(User).where(User.email.like("staff@%")))
        if not (manager and admin):
            raise SystemExit("Seeded manager/admin users not found.")
        self.manager_id, self.admin_id = manager.id, admin.id
        self.staff_id = staff.id if staff else manager.id

        # One bin per location. The balance projection is keyed on bin, so an
        # inbound and an outbound movement must agree or the outbound looks at
        # an empty row.
        self.bins = {
            w.id: db.scalar(
                select(func.min(StockBalance.bin_id)).where(
                    StockBalance.warehouse_id == w.id,
                    StockBalance.bin_id.is_not(None),
                )
            )
            for w in warehouses
        }

        self.reliability = {
            s.id: SUPPLIER_RELIABILITY[i % len(SUPPLIER_RELIABILITY)]
            for i, s in enumerate(self.suppliers)
        }
        self.by_sku = {p.sku: p for p in self.products}

    # -------------------------------------------------------------- helpers

    def _stamp(self, day: date, hour: int | None = None) -> datetime:
        """A plausible clock time, because "when" is a feature in its own right.

        Trading happens in shop hours. Anomaly detection later looks for
        movements that do not, so the ordinary case has to be ordinary.

        `hour` is a wall-clock hour at the branch, not UTC. Stamping 09:00 UTC
        would put every "morning" sale at 14:30 IST and every "evening" one
        past midnight — which the after-hours detector would then flag as the
        entire dataset. Stored as UTC either way; only the meaning differs.
        """
        hour = hour if hour is not None else self.rng.randint(9, 20)
        return at_local(day, hour, self.rng.randint(0, 59), self.rng.randint(0, 59))

    def _emit(
        self,
        *,
        product: Product,
        warehouse_id: int,
        quantity: Decimal,
        movement_type: MovementType,
        occurred_at: datetime,
        lot_id: int | None = None,
        status: StockStatus = StockStatus.AVAILABLE,
        unit_cost: Decimal | None = None,
        user_id: int | None = None,
        notes: str | None = None,
    ) -> None:
        if quantity == 0:
            return
        self.movements.append({
            "movement_type": movement_type,
            "product_id": product.id,
            "warehouse_id": warehouse_id,
            "bin_id": self.bins.get(warehouse_id),
            "status": status,
            "tracking_mode": product.tracking_mode,
            "lot_id": lot_id,
            "serial_id": None,
            "quantity": quantity,
            "unit_cost": unit_cost,
            "reference_type": TAG,
            "reference_id": None,
            "idempotency_key": None,
            "occurred_at": occurred_at,
            "created_by": user_id or self.staff_id,
            "notes": notes,
        })

    def _new_lot(
        self, product: Product, day: date, supplier_id: int | None, cost: Decimal
    ) -> LotState:
        """Create a batch. Untracked products get a placeholder with no row."""
        if product.tracking_mode == TrackingMode.NONE:
            return LotState(lot_id=None, expiry=None, qty=Decimal("0"), cost=cost)

        self.lot_seq += 1
        months = self.rng.randint(*SHELF_LIFE_MONTHS)
        expiry = None
        if product.tracking_mode == TrackingMode.LOT_EXPIRY:
            year = day.year + (day.month + months - 1) // 12
            month = (day.month + months - 1) % 12 + 1
            nxt = date(year + (month == 12), month % 12 + 1, 1)
            expiry = nxt - timedelta(days=1)

        # MRP drifts upward over two years, the way printed prices do. Each
        # batch keeps the price printed on it — that is the whole reason MRP
        # lives on the lot rather than the product.
        years = (day - self.start).days / 365.25
        mrp = (product.mrp or Decimal("50")) * Decimal(
            str(round(0.92 * (1.06**years) * self.rng.uniform(0.99, 1.03), 4))
        )
        lot = Lot(
            product_id=product.id,
            lot_code=f"{TAG}-{product.sku}-{self.lot_seq:05d}",
            mfg_date=day - timedelta(days=self.rng.randint(20, 90)),
            expiry_date=expiry,
            supplier_id=supplier_id,
            mrp=mrp.quantize(Decimal("0.01")),
            purchase_cost=cost,
            received_at=self._stamp(day),
        )
        self.db.add(lot)
        self.db.flush()
        return LotState(lot_id=lot.id, expiry=expiry, qty=Decimal("0"), cost=cost)

    def _cost_of(self, product: Product, day: date) -> Decimal:
        """Purchase cost, drifting up with inflation."""
        years = (day - self.start).days / 365.25
        base = (product.mrp or Decimal("50")) * Decimal("0.62")
        return (base * Decimal(str(round(1.05**years, 4)))).quantize(Decimal("0.0001"))

    def _on_hand(self, product_id: int, warehouse_id: int) -> Decimal:
        return sum(
            (lot.qty for lot in self.stock[(product_id, warehouse_id)]), Decimal("0")
        )

    def _incoming(self, product_id: int, warehouse_id: int) -> Decimal:
        return sum(
            (
                p.quantity
                for p in self.pending
                if p.product_id == product_id and p.warehouse_id == warehouse_id
            ),
            Decimal("0"),
        )

    def _consume(
        self, product_id: int, warehouse_id: int, qty: Decimal, on: date
    ) -> list[tuple[LotState, Decimal]]:
        """Draw FEFO, skipping anything already out of date.

        Returns what was actually taken, which may be less than asked for. A
        short draw is a stockout, and stockouts are supposed to happen here —
        they are what makes the reorder feature worth building.
        """
        lots = self.stock[(product_id, warehouse_id)]
        lots.sort(key=lambda x: (x.expiry or date.max))
        taken: list[tuple[LotState, Decimal]] = []
        for lot in lots:
            if qty <= 0:
                break
            if lot.qty <= 0 or (lot.expiry and lot.expiry < on):
                continue
            take = min(lot.qty, qty)
            lot.qty -= take
            qty -= take
            taken.append((lot, take))
        return taken

    # ------------------------------------------------------------- the loop

    def run(self) -> None:
        self._open_stock()
        for offset in range(self.days + 1):
            day = self.start + timedelta(days=offset)
            self._arrivals(day)
            self._expire(day)
            self._sell(day)
            self._replenish(day)
            if offset % 90 == 0:
                self._flush()
        self._plant_anomalies()
        self._flush()

    def _open_stock(self) -> None:
        """Day zero. Everyone starts with about three weeks of cover."""
        for product in self.products:
            cost = self._cost_of(product, self.start)
            for warehouse in [self.central, *self.branches]:
                daily = expected_demand(
                    product.sku, self.kinds[warehouse.id], self.start, self.start
                )
                # Central holds the chain's buffer, not just its own counter
                # sales, so it is sized against everything it will resupply.
                qty = Decimal(str(round(daily * COVER_DAYS)))
                if warehouse.is_central:
                    qty = Decimal(str(round(
                        sum(
                            expected_demand(
                                product.sku, self.kinds[b.id], self.start, self.start
                            )
                            for b in self.branches
                        )
                        * COVER_DAYS
                    )))
                if qty <= 0:
                    continue
                supplier = (self.supplier_for.get(product.id) or [None])[0]
                lot = self._new_lot(product, self.start, supplier, cost)
                lot.qty = qty
                self.stock[(product.id, warehouse.id)].append(lot)
                self._emit(
                    product=product,
                    warehouse_id=warehouse.id,
                    quantity=qty,
                    movement_type=MovementType.OPENING_BALANCE,
                    occurred_at=self._stamp(self.start, hour=8),
                    lot_id=lot.lot_id,
                    unit_cost=cost,
                    user_id=self.admin_id,
                    notes="Opening balance",
                )

    def _arrivals(self, day: date) -> None:
        due = [p for p in self.pending if p.arrives <= day]
        if not due:
            return
        self.pending = [p for p in self.pending if p.arrives > day]

        by_supplier: dict[tuple[int, int | None], list[Pending]] = defaultdict(list)
        for item in due:
            by_supplier[(item.warehouse_id, item.po_id)].append(item)

        for (warehouse_id, po_id), items in by_supplier.items():
            grn = None
            if po_id is not None:
                grn = GoodsReceipt(
                    grn_number=f"GRN-{TAG}-{self.stats['grn']:06d}",
                    purchase_order_id=po_id,
                    warehouse_id=warehouse_id,
                    supplier_invoice_no=f"INV/{day:%y%m}/{self.rng.randint(1000, 9999)}",
                    supplier_invoice_date=day,
                    received_at=self._stamp(day, hour=self.rng.randint(9, 13)),
                    received_by=self.staff_id,
                    notes=TAG,
                )
                self.db.add(grn)
                self.db.flush()
                self.stats["grn"] += 1

            for item in items:
                product = next(p for p in self.products if p.id == item.product_id)
                lot = item.lot or self._new_lot(
                    product, day, item.supplier_id, item.unit_cost
                )
                lot.qty += item.quantity
                self.stock[(product.id, warehouse_id)].append(lot)

                is_transfer = item.transfer_id is not None
                self._emit(
                    product=product,
                    warehouse_id=warehouse_id,
                    quantity=item.quantity,
                    movement_type=(
                        MovementType.TRANSFER_RECEIPT
                        if is_transfer
                        else MovementType.PURCHASE_RECEIPT
                    ),
                    occurred_at=self._stamp(day, hour=self.rng.randint(9, 13)),
                    lot_id=lot.lot_id,
                    unit_cost=item.unit_cost,
                    notes="Transfer in" if is_transfer else "Goods received",
                )
                if is_transfer:
                    # Close out the in-transit leg posted at dispatch.
                    self._emit(
                        product=product,
                        warehouse_id=warehouse_id,
                        quantity=-item.quantity,
                        movement_type=MovementType.TRANSFER_RECEIPT,
                        occurred_at=self._stamp(day, hour=self.rng.randint(9, 13)),
                        lot_id=lot.lot_id,
                        status=StockStatus.IN_TRANSIT,
                        notes="Arrived",
                    )
                    self.in_flight[item.transfer_id] -= 1
                    if self.in_flight[item.transfer_id] == 0:
                        transfer = self.db.get(StockTransfer, item.transfer_id)
                        if transfer:
                            transfer.status = DocumentStatus.COMPLETED
                            transfer.received_at = self._stamp(day, hour=13)
                        del self.in_flight[item.transfer_id]
                if grn is not None:
                    self.db.add(
                        GoodsReceiptLine(
                            goods_receipt_id=grn.id,
                            purchase_order_line_id=item.po_line_id,
                            product_id=product.id,
                            lot_id=lot.lot_id,
                            bin_id=self.bins.get(warehouse_id),
                            quantity=item.quantity,
                            unit_cost=item.unit_cost,
                        )
                    )
                    if item.po_line_id:
                        line = self.db.get(PurchaseOrderLine, item.po_line_id)
                        if line:
                            line.qty_received += item.quantity
            if po_id is not None:
                po = self.db.get(PurchaseOrder, po_id)
                if po:
                    po.status = DocumentStatus.RECEIVED

    def _expire(self, day: date) -> None:
        """Write off anything that went out of date.

        Not cosmetic: expiry loss is what the forecasting feature is ultimately
        trying to reduce, so it has to be visible in the history as a cost.
        """
        if day.day != 1:  # a monthly sweep, as a real pharmacy does
            return
        for (product_id, warehouse_id), lots in self.stock.items():
            product = next((p for p in self.products if p.id == product_id), None)
            if product is None:
                continue
            for lot in lots:
                if lot.expiry and lot.expiry < day and lot.qty > 0:
                    self._emit(
                        product=product,
                        warehouse_id=warehouse_id,
                        quantity=-lot.qty,
                        movement_type=MovementType.EXPIRY_WRITEOFF,
                        occurred_at=self._stamp(day, hour=11),
                        lot_id=lot.lot_id,
                        unit_cost=lot.cost,
                        user_id=self.manager_id,
                        notes=f"Expired {lot.expiry:%b %Y}",
                    )
                    self.stats["expired_units"] += int(lot.qty)
                    lot.qty = Decimal("0")

    def _sell(self, day: date) -> None:
        """One movement per product, per branch, per day.

        Aggregated to a day rather than one row per till transaction: the daily
        series is what every forecast consumes, and a hundred thousand rows of
        individual sales would carry no extra signal.
        """
        for warehouse in [self.central, *self.branches]:
            kind = self.kinds[warehouse.id]
            for product in self.products:
                mean = expected_demand(product.sku, kind, day, self.start)
                if mean <= 0:
                    continue
                cv = PROFILES[product.sku]["cv"]
                wanted = self.rng.gauss(mean, mean * cv)
                qty = Decimal(str(max(0, round(wanted))))
                if qty <= 0:
                    continue

                for lot, take in self._consume(product.id, warehouse.id, qty, day):
                    self._emit(
                        product=product,
                        warehouse_id=warehouse.id,
                        quantity=-take,
                        movement_type=MovementType.SALE_ISSUE,
                        occurred_at=self._stamp(day),
                        lot_id=lot.lot_id,
                        unit_cost=lot.cost,
                        notes="Counter sales",
                    )
                    self.stats["sold_units"] += int(take)
                if self._on_hand(product.id, warehouse.id) <= 0:
                    self.stats["stockout_days"] += 1

    def _replenish(self, day: date) -> None:
        """Order what is running low. Branches pull from central; central buys."""
        # --- branches pull from central ---------------------------------
        for warehouse in self.branches:
            lines: list[tuple[Product, Decimal]] = []
            for product in self.products:
                daily = expected_demand(product.sku, self.kinds[warehouse.id], day, self.start)
                reorder = Decimal(str(round(daily * 7)))  # a week's cover
                position = self._on_hand(product.id, warehouse.id) + self._incoming(
                    product.id, warehouse.id
                )
                if position > reorder or daily <= 0:
                    continue
                want = Decimal(str(round(daily * COVER_DAYS))) - position
                if want <= 0:
                    continue
                # Only what central can actually spare — a branch cannot pull
                # stock the warehouse does not have, and that shortfall is how
                # a late supplier delivery reaches the shelf.
                available = self._on_hand(product.id, self.central.id)
                send = min(want, available)
                if send > 0:
                    lines.append((product, send))

            if not lines:
                continue
            transfer = StockTransfer(
                transfer_number=f"TRF-{TAG}-{self.stats['transfer']:06d}",
                from_warehouse_id=self.central.id,
                to_warehouse_id=warehouse.id,
                status=DocumentStatus.IN_TRANSIT,
                created_by=self.manager_id,
                approved_by=self.admin_id,
                dispatched_at=self._stamp(day, hour=15),
                notes=TAG,
            )
            self.db.add(transfer)
            self.db.flush()
            self.stats["transfer"] += 1

            for product, qty in lines:
                for lot, take in self._consume(product.id, self.central.id, qty, day):
                    self.db.add(
                        StockTransferLine(
                            stock_transfer_id=transfer.id,
                            product_id=product.id,
                            lot_id=lot.lot_id,
                            quantity=take,
                        )
                    )
                    self._emit(
                        product=product,
                        warehouse_id=self.central.id,
                        quantity=-take,
                        movement_type=MovementType.TRANSFER_DISPATCH,
                        occurred_at=self._stamp(day, hour=15),
                        lot_id=lot.lot_id,
                        unit_cost=lot.cost,
                        user_id=self.manager_id,
                        notes=f"To {warehouse.name}",
                    )
                    # Visible as in-transit at the destination, so the chain
                    # total never dips while stock is on the road.
                    self._emit(
                        product=product,
                        warehouse_id=warehouse.id,
                        quantity=take,
                        movement_type=MovementType.TRANSFER_DISPATCH,
                        occurred_at=self._stamp(day, hour=15),
                        lot_id=lot.lot_id,
                        status=StockStatus.IN_TRANSIT,
                        user_id=self.manager_id,
                        notes="In transit",
                    )
                    travel = self.rng.choice([1, 1, 2, 2, 3])
                    self.in_flight[transfer.id] += 1
                    self.pending.append(
                        Pending(
                            arrives=day + timedelta(days=travel),
                            warehouse_id=warehouse.id,
                            product_id=product.id,
                            quantity=take,
                            unit_cost=lot.cost,
                            transfer_id=transfer.id,
                            lot=LotState(lot.lot_id, lot.expiry, Decimal("0"), lot.cost),
                        )
                    )

        # --- central buys from distributors ------------------------------
        needs: list[tuple[Product, Decimal, int]] = []
        for product in self.products:
            chain_daily = sum(
                expected_demand(product.sku, self.kinds[b.id], day, self.start)
                for b in self.branches
            )
            reorder = Decimal(str(round(chain_daily * 10)))
            position = self._on_hand(product.id, self.central.id) + self._incoming(
                product.id, self.central.id
            )
            if position > reorder or chain_daily <= 0:
                continue
            want = Decimal(str(round(chain_daily * COVER_DAYS * 1.5))) - position
            if want <= 0:
                continue
            candidates = self.supplier_for.get(product.id) or [
                s.id for s in self.suppliers
            ]
            needs.append((product, want, self.rng.choice(candidates)))

        by_supplier: dict[int, list[tuple[Product, Decimal]]] = defaultdict(list)
        for product, qty, supplier_id in needs:
            by_supplier[supplier_id].append((product, qty))

        for supplier_id, items in by_supplier.items():
            supplier = next(s for s in self.suppliers if s.id == supplier_id)
            interstate = gst.is_interstate(supplier.state_code, self.central.state_code)
            po = PurchaseOrder(
                po_number=f"PO-{TAG}-{self.stats['po']:06d}",
                supplier_id=supplier_id,
                warehouse_id=self.central.id,
                status=DocumentStatus.APPROVED,
                order_date=day,
                expected_date=day + timedelta(days=int(self.reliability[supplier_id][0])),
                created_by=self.manager_id,
                approved_by=self.admin_id,
                approved_at=self._stamp(day, hour=17),
                is_interstate=interstate,
                place_of_supply=self.central.state_code,
                notes=TAG,
            )
            self.db.add(po)
            self.db.flush()
            self.stats["po"] += 1

            breakdowns = []
            for product, qty in items:
                cost = self._cost_of(product, day)
                tax = gst.compute_line_tax(
                    quantity=qty,
                    unit_price=cost,
                    gst_rate=product.gst_rate,
                    interstate=interstate,
                )
                breakdowns.append(tax)
                line = PurchaseOrderLine(
                    purchase_order_id=po.id,
                    product_id=product.id,
                    qty_ordered=qty,
                    unit_price=cost,
                    taxable_value=tax.taxable_value,
                    gst_rate=tax.gst_rate,
                    cgst_amount=tax.cgst_amount,
                    sgst_amount=tax.sgst_amount,
                    igst_amount=tax.igst_amount,
                    line_total=tax.line_total,
                )
                self.db.add(line)
                self.db.flush()

                mean, sd, late_chance = self.reliability[supplier_id]
                lead = max(1, round(self.rng.gauss(mean, sd)))
                if self.rng.random() < late_chance:
                    lead += self.rng.randint(4, 14)  # the delivery that goes wrong
                self.pending.append(
                    Pending(
                        arrives=day + timedelta(days=lead),
                        warehouse_id=self.central.id,
                        product_id=product.id,
                        quantity=qty,
                        unit_cost=cost,
                        supplier_id=supplier_id,
                        po_id=po.id,
                        po_line_id=line.id,
                    )
                )

            totals = gst.compute_document_totals(breakdowns)
            po.subtotal = totals.subtotal
            po.tax_total = totals.tax_total
            po.round_off = totals.round_off
            po.grand_total = totals.grand_total

    # --------------------------------------------------------- anomalies

    def _plant_anomalies(self) -> None:
        """Deliberate irregularities, so the detector has something to find.

        Written down here rather than left implicit: a detector evaluated on
        data whose faults nobody wrote down cannot be scored, only admired.
        Each of these is a real pattern a pharmacy sees.
        """
        if not self.branches:
            return
        branch = self.branches[0]

        # 1. Shrinkage — stock leaving with no paperwork, found at a count.
        for weeks_ago in (6, 14, 31):
            day = self.today - timedelta(weeks=weeks_ago)
            product = self.by_sku.get("ALP-025")  # controlled, the usual target
            if not product:
                continue
            lots = [x for x in self.stock[(product.id, branch.id)] if x.qty > 5]
            if not lots:
                continue
            lot = lots[0]
            qty = min(lot.qty, Decimal(str(self.rng.randint(8, 20))))
            lot.qty -= qty
            self._emit(
                product=product,
                warehouse_id=branch.id,
                quantity=-qty,
                movement_type=MovementType.CYCLE_COUNT_ADJ,
                occurred_at=self._stamp(day, hour=19),
                lot_id=lot.lot_id,
                unit_cost=lot.cost,
                user_id=self.manager_id,
                notes="Cycle count variance — unexplained",
            )
            self.stats["anomaly_shrinkage"] += 1

        # 2. A movement at 3am. Nobody dispenses at 3am.
        product = self.by_sku.get("PAR-650")
        if product:
            lots = [x for x in self.stock[(product.id, branch.id)] if x.qty > 30]
            if lots:
                lot = lots[0]
                qty = Decimal("25")
                lot.qty -= qty
                self._emit(
                    product=product,
                    warehouse_id=branch.id,
                    quantity=-qty,
                    movement_type=MovementType.ADJUSTMENT,
                    occurred_at=self._stamp(
                        self.today - timedelta(days=11), hour=3
                    ),
                    lot_id=lot.lot_id,
                    unit_cost=lot.cost,
                    user_id=self.manager_id,
                    notes="Out-of-hours adjustment",
                )
                self.stats["anomaly_after_hours"] += 1

        # 3. Breakage — a whole carton dropped.
        product = self.by_sku.get("INS-GLA")  # cold chain, expensive
        if product:
            lots = [x for x in self.stock[(product.id, branch.id)] if x.qty > 10]
            if lots:
                lot = lots[0]
                qty = min(lot.qty, Decimal("12"))
                lot.qty -= qty
                self._emit(
                    product=product,
                    warehouse_id=branch.id,
                    quantity=-qty,
                    movement_type=MovementType.DAMAGE,
                    occurred_at=self._stamp(self.today - timedelta(days=23), hour=10),
                    lot_id=lot.lot_id,
                    unit_cost=lot.cost,
                    user_id=self.manager_id,
                    notes="Cold-chain excursion — carton discarded",
                )
                self.stats["anomaly_damage"] += 1

    # ------------------------------------------------------------- writing

    def _flush(self) -> None:
        """Bulk-insert the movements accumulated so far.

        In chunks: one 60,000-row statement holds a transaction open long
        enough to matter, and the balance trigger fires per row regardless.
        """
        if not self.movements:
            return
        chunk = 2000
        for i in range(0, len(self.movements), chunk):
            self.db.execute(insert(StockMovement), self.movements[i : i + chunk])
        self.stats["movements"] += len(self.movements)
        self.movements.clear()
        self.db.commit()


# ----------------------------------------------------------------- entry


def reset(db: Session) -> None:
    """Remove a previous run without touching the bootstrap fixture.

    The ledger is append-only by trigger, so this suspends that guard for the
    duration. It is the one place in the system allowed to, and only because
    generated history is not real history. Balances are then REBUILT from what
    survives rather than patched — which is the standing advantage of keeping
    the projection derivable from the ledger.
    """
    print("Clearing previous synthetic history…")
    db.execute(text(
        "ALTER TABLE stock_movements DISABLE TRIGGER trg_stock_movements_append_only"
    ))
    db.execute(delete(StockMovement).where(StockMovement.reference_type == TAG))
    db.execute(text(
        "ALTER TABLE stock_movements ENABLE TRIGGER trg_stock_movements_append_only"
    ))

    # Rebuild the projection BEFORE deleting lots. stock_balances carries a
    # foreign key to lots, so a generated batch cannot be removed while a
    # balance row still points at it — and those rows only disappear when the
    # projection is recomputed from the movements that survive.
    db.execute(text("""
        TRUNCATE stock_balances;
        INSERT INTO stock_balances
            (product_id, warehouse_id, bin_id, lot_id, status,
             qty_on_hand, qty_reserved, updated_at)
        SELECT product_id, warehouse_id, bin_id, lot_id, status,
               SUM(quantity), 0, now()
        FROM stock_movements
        GROUP BY product_id, warehouse_id, bin_id, lot_id, status
        HAVING SUM(quantity) <> 0;
    """))

    for model, where in (
        (GoodsReceiptLine, GoodsReceiptLine.goods_receipt_id.in_(
            select(GoodsReceipt.id).where(GoodsReceipt.notes == TAG))),
        (GoodsReceipt, GoodsReceipt.notes == TAG),
        (PurchaseOrderLine, PurchaseOrderLine.purchase_order_id.in_(
            select(PurchaseOrder.id).where(PurchaseOrder.notes == TAG))),
        (PurchaseOrder, PurchaseOrder.notes == TAG),
        (StockTransferLine, StockTransferLine.stock_transfer_id.in_(
            select(StockTransfer.id).where(StockTransfer.notes == TAG))),
        (StockTransfer, StockTransfer.notes == TAG),
        (Lot, Lot.lot_code.like(f"{TAG}-%")),
    ):
        db.execute(delete(model).where(where))
    db.commit()


def generated_rows(db: Session) -> int:
    """How many ledger rows a previous run of this script left behind."""
    return db.scalar(
        select(func.count()).select_from(StockMovement).where(
            StockMovement.reference_type == TAG
        )
    ) or 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--seed", type=int, default=20260801)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--if-empty",
        action="store_true",
        help="do nothing if history already exists, instead of failing",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.reset:
            reset(db)

        existing = generated_rows(db)
        if existing:
            # Two callers want opposite things here. A person re-running this
            # by hand has almost certainly forgotten --reset and wants to be
            # told. A container start-up step runs on every single deploy and
            # must not turn "already seeded" into a failed rollout.
            if args.if_empty:
                print(f"History already present ({existing:,} rows) — skipping.")
                return
            raise SystemExit(
                f"{existing} generated movements already exist. "
                "Re-run with --reset to replace them."
            )

        sim = Sim(db, days=args.days, seed=args.seed)
        print(
            f"Simulating {args.days} days across {len(sim.branches) + 1} locations "
            f"and {len(sim.products)} products…"
        )
        sim.run()

        print("\nDone.")
        for key in sorted(sim.stats):
            print(f"  {key:22} {sim.stats[key]:>10,}")


if __name__ == "__main__":
    main()
