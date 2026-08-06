"""Thin the generated data down to a demo-sized set.

    python -m app.seed.compact                          # the defaults below
    python -m app.seed.compact --transfers 15 --purchase-orders 20
    python -m app.seed.compact --movement-days 21       # shorter ledger too
    python -m app.seed.compact --show                   # count, change nothing

WHY THIS IS SEPARATE FROM `--days`
----------------------------------
Two years of history is not the same knob as two years of paperwork. The
generator posts a replenishment cycle every few days, and each cycle writes one
transfer per branch and one purchase order per supplier, so 730 days produces
about 1,100 transfers and 270 orders, on top of fifty thousand ledger rows.
Re-seeding with a shorter history shrinks all of them together and in fixed
proportion. These are two knobs.

TRIMMING THE LEDGER WITHOUT LOSING THE STOCK
--------------------------------------------
The ledger is not a log — it is the stock. Every quantity in the system is
`SUM(quantity)` over these rows, so deleting the old ones would not shorten a
list, it would empty the warehouse.

So the deleted rows are not discarded, they are *carried forward*: their net
per product, location, batch, bin and status is written back as a single
OPENING_BALANCE dated at the cutoff, and the projection is then rebuilt from
what survives. On-hand comes out identical to the paise, and the ledger stays
internally consistent — the balance is still derivable from the movements, and
the append-only trigger still guards everything after the cutoff. This is what
a real system does when it closes a period, which is why `OPENING_BALANCE`
already existed in the enum.

WHAT THIS COSTS, PLAINLY
------------------------
Trimming the ledger throws away the demand signal, and three screens read it:

  Demand forecast    fits on the sales history — a three-week ledger cannot
                     show a season, so the forecast becomes near-useless
  Exceptions         needs a baseline to call anything anomalous
  Replenishment      reorder points are derived from observed demand

Supplier lead times reads orders against receipts rather than the ledger, so it
follows `--purchase-orders` instead. At the default twenty-five there are still
several deliveries per supplier — enough to show a distribution, not enough to
be a confident one.

So: trim the ledger while working on the operational screens, and put it back
before showing anything under Analysis. `--movement-days 0`, the default,
leaves the ledger alone.

REVERSIBLE
----------
Nothing here is a decision. `python -m app.seed.demo --rebuild` regenerates the
full set whenever the small one has served its purpose.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.documents import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    SalesOrder,
    SalesOrderLine,
    Shipment,
    ShipmentLine,
    StockAdjustment,
    StockAdjustmentLine,
    StockTransfer,
    StockTransferLine,
)
from app.models.identity import User
from app.models.stock import StockBalance, StockMovement, StockReservation
from app.seed.history import TAG as SYNTH

#: Enough to page through, few enough to talk over. A demo is read one screen
#: at a time, and the second screen of transfers says nothing the first did not.
DEFAULTS = {"transfers": 25, "purchase_orders": 25, "sales_orders": 25, "adjustments": 25}


def _newest_ids(db: Session, model, keep: int) -> set[int]:
    """The ids to spare — the most recent `keep`, by id.

    Documents are numbered in the order they were written, so newest-by-id is
    also newest-by-date, and it does not depend on which date column a given
    document happens to carry.
    """
    return set(
        db.scalars(
            select(model.id)
            .where(model.notes == SYNTH)
            .order_by(model.id.desc())
            .limit(keep)
        )
    )


def _generated(db: Session, model) -> int:
    return db.scalar(
        select(func.count()).select_from(model).where(model.notes == SYNTH)
    ) or 0


def _movements(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(StockMovement)) or 0


def _on_hand(db: Session) -> float:
    """Total units held. The invariant the carry-forward has to preserve."""
    return float(db.scalar(select(func.coalesce(func.sum(StockBalance.qty_on_hand), 0))))


def counts(db: Session) -> dict[str, int]:
    return {
        "transfers": _generated(db, StockTransfer),
        "purchase_orders": _generated(db, PurchaseOrder),
        "sales_orders": _generated(db, SalesOrder),
        "adjustments": _generated(db, StockAdjustment),
    }


def compact(db: Session, *, keep: dict[str, int]) -> dict[str, int]:
    """Delete every generated document except the newest few of each kind.

    Only rows tagged by the history generator. A document raised by hand
    through the interface, or one the showcase seed made to cover a status, is
    somebody's deliberate work and is never a candidate.
    """
    removed: dict[str, int] = {}

    # --- transfers ----------------------------------------------------------
    spared = _newest_ids(db, StockTransfer, keep["transfers"])
    doomed = select(StockTransfer.id).where(
        StockTransfer.notes == SYNTH, StockTransfer.id.notin_(spared or {-1})
    )
    db.execute(
        delete(StockTransferLine).where(
            StockTransferLine.stock_transfer_id.in_(doomed)
        )
    )
    removed["transfers"] = db.execute(
        delete(StockTransfer).where(
            StockTransfer.notes == SYNTH, StockTransfer.id.notin_(spared or {-1})
        )
    ).rowcount

    # --- purchase orders, and the receipts that point at them ---------------
    spared = _newest_ids(db, PurchaseOrder, keep["purchase_orders"])
    doomed = select(PurchaseOrder.id).where(
        PurchaseOrder.notes == SYNTH, PurchaseOrder.id.notin_(spared or {-1})
    )
    # Inwards from the leaves: a receipt line points at a receipt, a receipt at
    # an order, and an order line at the order. Any other order and the
    # foreign keys refuse — which is the schema doing its job.
    grns = select(GoodsReceipt.id).where(GoodsReceipt.purchase_order_id.in_(doomed))
    db.execute(
        delete(GoodsReceiptLine).where(GoodsReceiptLine.goods_receipt_id.in_(grns))
    )
    db.execute(delete(GoodsReceipt).where(GoodsReceipt.purchase_order_id.in_(doomed)))
    db.execute(
        delete(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id.in_(doomed)
        )
    )
    removed["purchase_orders"] = db.execute(
        delete(PurchaseOrder).where(
            PurchaseOrder.notes == SYNTH, PurchaseOrder.id.notin_(spared or {-1})
        )
    ).rowcount

    # --- sales orders, with their shipments and any held stock --------------
    spared = _newest_ids(db, SalesOrder, keep["sales_orders"])
    doomed = select(SalesOrder.id).where(
        SalesOrder.notes == SYNTH, SalesOrder.id.notin_(spared or {-1})
    )
    shipments = select(Shipment.id).where(Shipment.sales_order_id.in_(doomed))
    db.execute(delete(ShipmentLine).where(ShipmentLine.shipment_id.in_(shipments)))
    db.execute(delete(Shipment).where(Shipment.sales_order_id.in_(doomed)))
    db.execute(
        delete(StockReservation).where(
            StockReservation.sales_order_line_id.in_(
                select(SalesOrderLine.id).where(
                    SalesOrderLine.sales_order_id.in_(doomed)
                )
            )
        )
    )
    db.execute(
        delete(SalesOrderLine).where(SalesOrderLine.sales_order_id.in_(doomed))
    )
    removed["sales_orders"] = db.execute(
        delete(SalesOrder).where(
            SalesOrder.notes == SYNTH, SalesOrder.id.notin_(spared or {-1})
        )
    ).rowcount

    # --- adjustments --------------------------------------------------------
    spared = _newest_ids(db, StockAdjustment, keep["adjustments"])
    doomed = select(StockAdjustment.id).where(
        StockAdjustment.notes == SYNTH, StockAdjustment.id.notin_(spared or {-1})
    )
    db.execute(
        delete(StockAdjustmentLine).where(
            StockAdjustmentLine.stock_adjustment_id.in_(doomed)
        )
    )
    removed["adjustments"] = db.execute(
        delete(StockAdjustment).where(
            StockAdjustment.notes == SYNTH,
            StockAdjustment.id.notin_(spared or {-1}),
        )
    ).rowcount

    db.commit()
    return removed


def carry_forward(db: Session, *, keep_days: int, actor_id: int) -> tuple[int, int]:
    """Fold every movement older than `keep_days` into one opening balance each.

    Returns (rows removed, opening balances written).

    The trigger is suspended for the rewrite and restored immediately. This is
    the same licence `app.seed.history.reset` takes, for the same reason and
    with the same limit: generated history is not real history. Nothing here
    can run against a movement somebody posted through the interface today —
    that is what the cutoff is for.
    """
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)

    doomed = db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.occurred_at < cutoff)
    ) or 0
    if doomed == 0:
        return 0, 0

    # All three, not just the append-only one. `trg_movement_to_balance`
    # maintains the projection row by row and would see the opening balance
    # arrive while the movements it replaces are still there — the position
    # doubled, and it refused. The projection is rebuilt wholesale below, so
    # having it maintained during the rewrite is not merely unnecessary, it is
    # the thing that makes the rewrite impossible.
    triggers = (
        "trg_stock_movements_append_only",
        "trg_movement_tracking_mode",
        "trg_movement_to_balance",
    )
    for name in triggers:
        db.execute(text(f"ALTER TABLE stock_movements DISABLE TRIGGER {name}"))

    # Take the sums first, delete, then write them back. Computing them into a
    # temporary table rather than reading the live one twice means the numbers
    # cannot shift between the two statements.
    #
    # `HAVING <> 0` drops the batches that came in and went out again inside
    # the deleted window: they net to nothing, and a zero row would invent a
    # stock line for a product no longer held.
    db.execute(
        text("""
            CREATE TEMP TABLE carried_forward ON COMMIT DROP AS
            SELECT product_id, warehouse_id, bin_id, lot_id, status,
                   -- Denormalised onto every row by `trg_movement_tracking_mode`
                   -- and NOT NULL, so with that trigger off it has to be
                   -- carried across by hand. It is a property of the product,
                   -- so grouping by it changes no grain.
                   tracking_mode,
                   SUM(quantity) AS quantity
            FROM stock_movements
            WHERE occurred_at < :cutoff
            GROUP BY product_id, warehouse_id, bin_id, lot_id, status,
                     tracking_mode
            HAVING SUM(quantity) <> 0
        """),
        {"cutoff": cutoff},
    )

    removed = db.execute(
        delete(StockMovement).where(StockMovement.occurred_at < cutoff)
    ).rowcount

    written = db.execute(
        text("""
            INSERT INTO stock_movements
                (movement_type, product_id, warehouse_id, bin_id, lot_id,
                 status, tracking_mode, quantity, reference_type,
                 occurred_at, created_by)
            SELECT 'OPENING_BALANCE', product_id, warehouse_id, bin_id, lot_id,
                   status, tracking_mode, quantity, :tag, :cutoff, :actor
            FROM carried_forward
        """),
        {"cutoff": cutoff, "actor": actor_id, "tag": "CARRIED_FORWARD"},
    ).rowcount

    for name in triggers:
        db.execute(text(f"ALTER TABLE stock_movements ENABLE TRIGGER {name}"))

    # Rebuild rather than patch. The projection is derivable from the ledger by
    # definition, so recomputing it is both the simplest correct answer and a
    # check that the carry-forward added up.
    db.execute(
        text("""
            TRUNCATE stock_balances;
            INSERT INTO stock_balances
                (product_id, warehouse_id, bin_id, lot_id, status,
                 qty_on_hand, qty_reserved, updated_at)
            SELECT product_id, warehouse_id, bin_id, lot_id, status,
                   SUM(quantity), 0, now()
            FROM stock_movements
            GROUP BY product_id, warehouse_id, bin_id, lot_id, status
            HAVING SUM(quantity) <> 0;
        """)
    )
    db.commit()
    return removed, written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in DEFAULTS.items():
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            type=int,
            default=default,
            help=f"generated {name.replace('_', ' ')} to keep (default {default})",
        )
    parser.add_argument(
        "--movement-days",
        type=int,
        default=0,
        help=(
            "keep this many days of ledger, folding everything older into "
            "opening balances (0, the default, leaves the ledger alone). "
            "Costs the demand forecast, exceptions and replenishment — see the "
            "note at the top of this file"
        ),
    )
    parser.add_argument(
        "--show", action="store_true", help="report the counts and change nothing"
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        before = counts(db)
        movements_before = _movements(db)
        if args.show:
            for name, value in before.items():
                print(f"  {name:16} {value:,} generated")
            print(f"  {'movements':16} {movements_before:,} in the ledger")
            return

        keep = {name: getattr(args, name) for name in DEFAULTS}
        removed = compact(db, keep=keep)
        after = counts(db)

        on_hand_before = _on_hand(db)
        carried = written = 0
        if args.movement_days > 0:
            actor = db.scalar(select(User.id).order_by(User.id))
            carried, written = carry_forward(
                db, keep_days=args.movement_days, actor_id=actor
            )
        movements_after = _movements(db)
        on_hand_after = _on_hand(db)

    print("Trimmed the generated data.\n")
    for name in DEFAULTS:
        print(
            f"  {name:16} {before[name]:>6,} -> {after[name]:>4,}"
            f"   ({removed[name]:,} removed)"
        )
    if args.movement_days > 0:
        print(
            f"  {'movements':16} {movements_before:>6,} -> {movements_after:>4,}"
            f"   ({carried:,} folded into {written} opening balances)"
        )
        # The number that decides whether this was safe. Printed rather than
        # asserted quietly, because "the stock is unchanged" is the whole claim.
        verdict = "unchanged" if on_hand_before == on_hand_after else "CHANGED"
        print(f"\n  stock on hand    {on_hand_before:,.0f} -> {on_hand_after:,.0f}  {verdict}")
        if verdict == "CHANGED":
            raise SystemExit("Carry-forward did not balance — nothing should ship.")
    else:
        print(f"  {'movements':16} {movements_before:>6,}   (ledger untouched)")
    print("\n`python -m app.seed.demo --rebuild` puts the full set back.")


if __name__ == "__main__":
    main()
