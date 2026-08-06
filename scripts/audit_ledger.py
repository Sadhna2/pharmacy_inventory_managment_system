"""Prove the ledger and the balances it projects, from the data alone.

Read-only.

The append-only ledger is the spine: every quantity the system reports is
supposed to be a projection of it. So the checks that matter are not "does the
code look right" but "does the projection still equal the sum of the postings",
asked of every row rather than a sample.
"""

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select, text

from app.db.session import SessionLocal
from app.models.enums import MovementType
from app.models.stock import StockBalance, StockMovement

findings: list[str] = []
counts: dict[str, int] = defaultdict(int)


def check(ok: bool, key: str, detail: str) -> None:
    counts[key] += 1
    if not ok:
        findings.append(f"[{key}] {detail}")


#: What each posting is allowed to do to a balance.
#:
#: Three kinds, and telling them apart is the point. A single-leg type moves
#: stock in or out of the building and only ever has one sign. A paired type
#: does not change how much there is at all — it moves the same quantity from
#: one status to another, so it writes two rows that cancel, and a check
#: expecting one sign reports every one of them as broken. That is exactly
#: what the first run of this script did: four "failures" that were all this
#: classification being wrong rather than the ledger.
ALWAYS_IN = {
    MovementType.OPENING_BALANCE,
    MovementType.PURCHASE_RECEIPT,
    MovementType.RETURN_IN,
}
ALWAYS_OUT = {
    MovementType.SALE_ISSUE,
    MovementType.RETURN_OUT,
    MovementType.SCRAP,
    MovementType.EXPIRY_WRITEOFF,
}
#: Two legs that must cancel: out of one status, into another.
PAIRED = {
    MovementType.TRANSFER_DISPATCH,
    MovementType.TRANSFER_RECEIPT,
    MovementType.QC_RELEASE,
    MovementType.QC_REJECT,
    MovementType.DAMAGE,
    MovementType.STATUS_CHANGE,
}
#: Legitimately either way, one leg.
EITHER = {MovementType.ADJUSTMENT, MovementType.CYCLE_COUNT_ADJ}

with SessionLocal() as db:
    total = db.scalar(select(func.count()).select_from(StockMovement))
    print(f"ledger rows: {total:,}")

    # 1. Sign convention, per movement type.
    rows = db.execute(
        select(
            StockMovement.movement_type,
            func.count().filter(StockMovement.quantity > 0),
            func.count().filter(StockMovement.quantity < 0),
            func.count().filter(StockMovement.quantity == 0),
        ).group_by(StockMovement.movement_type)
    ).all()
    for mt, pos, neg, zero in rows:
        if mt in ALWAYS_IN:
            check(neg == 0, "sign-convention", f"{mt.value}: {neg:,} negative postings")
        if mt in ALWAYS_OUT:
            check(pos == 0, "sign-convention", f"{mt.value}: {pos:,} positive postings")
        if mt in PAIRED:
            # Both signs present in equal number, because each event writes
            # one of each. An unequal count means somebody posted a status
            # move with only its outbound half, and stock left the building
            # under a movement type that claims not to remove any.
            check(
                pos == neg,
                "status-moves-are-paired",
                f"{mt.value}: {pos:,} positive rows against {neg:,} negative",
            )
        # A zero-quantity posting is a row that changes nothing and can only
        # confuse a reader of the movement history.
        check(zero == 0, "no-zero-postings", f"{mt.value}: {zero:,} zero-quantity rows")

    # 2. The projection equals the sum of the postings. This is the whole
    #    contract of `trg_movement_to_balance` and `rebuild_balances()`, and
    #    it is checked here per (product, warehouse, lot, status) key rather
    #    than in aggregate, so offsetting errors cannot hide each other.
    ledger = {
        (p, w, lot, st): q
        for p, w, lot, st, q in db.execute(
            select(
                StockMovement.product_id,
                StockMovement.warehouse_id,
                StockMovement.lot_id,
                StockMovement.status,
                func.sum(StockMovement.quantity),
            ).group_by(
                StockMovement.product_id,
                StockMovement.warehouse_id,
                StockMovement.lot_id,
                StockMovement.status,
            )
        ).all()
    }
    balances = {
        (p, w, lot, st): q
        for p, w, lot, st, q in db.execute(
            select(
                StockBalance.product_id,
                StockBalance.warehouse_id,
                StockBalance.lot_id,
                StockBalance.status,
                func.sum(StockBalance.qty_on_hand),
            ).group_by(
                StockBalance.product_id,
                StockBalance.warehouse_id,
                StockBalance.lot_id,
                StockBalance.status,
            )
        ).all()
    }
    print(f"balance keys: {len(balances):,}   ledger keys: {len(ledger):,}")

    for key in set(ledger) | set(balances):
        posted = Decimal(ledger.get(key, 0) or 0)
        projected = Decimal(balances.get(key, 0) or 0)
        check(
            posted == projected,
            "projection-matches-ledger",
            f"product={key[0]} wh={key[1]} lot={key[2]} status={key[3]}: "
            f"ledger says {posted}, balance says {projected}",
        )

    # 2b. Paired types must net to zero in quantity as well as in row count:
    #     a status move reclassifies stock, it never creates or destroys any.
    for mt, net in db.execute(
        select(StockMovement.movement_type, func.sum(StockMovement.quantity))
        .where(StockMovement.movement_type.in_(PAIRED))
        .group_by(StockMovement.movement_type)
    ).all():
        check(
            Decimal(net) == 0,
            "status-moves-net-zero",
            f"{mt.value}: legs net to {net}, so stock was created or destroyed",
        )

    # 3. Nothing on hand may be negative. A negative balance is stock that was
    #    issued twice, or issued from somewhere it never was.
    for pid, wid, lot, st, qty in db.execute(
        select(
            StockBalance.product_id,
            StockBalance.warehouse_id,
            StockBalance.lot_id,
            StockBalance.status,
            StockBalance.qty_on_hand,
        ).where(StockBalance.qty_on_hand < 0)
    ).all():
        check(False, "no-negative-stock", f"product={pid} wh={wid} lot={lot} {st}: {qty}")
    counts["no-negative-stock"] += 1

    # 4. Reserved can never exceed what is actually there — allocating more
    #    than exists is how two orders get promised the same box.
    for pid, wid, lot, on_hand, res in db.execute(
        select(
            StockBalance.product_id,
            StockBalance.warehouse_id,
            StockBalance.lot_id,
            StockBalance.qty_on_hand,
            StockBalance.qty_reserved,
        ).where(StockBalance.qty_reserved > StockBalance.qty_on_hand)
    ).all():
        check(
            False,
            "reserved-within-on-hand",
            f"product={pid} wh={wid} lot={lot}: reserved {res} of {on_hand}",
        )
    counts["reserved-within-on-hand"] += 1

    # 5. A transfer posts a matching pair — out of the source, into the
    #    destination. The two legs must cancel, or stock is created or
    #    destroyed in flight.
    pairs = db.execute(
        select(
            StockMovement.reference_id,
            StockMovement.product_id,
            func.sum(StockMovement.quantity),
            func.count(),
        )
        .where(
            StockMovement.reference_type == "TRANSFER",
            StockMovement.movement_type.in_(
                [MovementType.TRANSFER_DISPATCH, MovementType.TRANSFER_RECEIPT]
            ),
        )
        .group_by(StockMovement.reference_id, StockMovement.product_id)
    ).all()
    for ref, pid, net, n in pairs:
        # Net zero once received. A transfer still in flight has only its
        # dispatch legs, which net to zero too because dispatch writes the
        # in-transit leg at the destination.
        check(
            Decimal(net) == 0,
            "transfer-legs-cancel",
            f"transfer {ref} product {pid}: {n} legs netting {net}",
        )

    # 6. Idempotency keys must be unique where present, or a retried request
    #    posts the same movement twice.
    dupes = db.execute(
        select(StockMovement.idempotency_key, func.count())
        .where(StockMovement.idempotency_key.isnot(None))
        .group_by(StockMovement.idempotency_key)
        .having(func.count() > 1)
    ).all()
    for k, n in dupes:
        check(False, "idempotency-unique", f"{k!r} posted {n} times")
    counts["idempotency-unique"] += 1

    # 7. The append-only guarantee, asserted rather than assumed: the triggers
    #    have to actually be attached to the table.
    trigs = {
        r[0]
        for r in db.execute(
            text(
                "SELECT tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'stock_movements' AND NOT t.tgisinternal"
            )
        ).all()
    }
    for name in ("trg_stock_movements_append_only", "trg_movement_to_balance"):
        check(name in trigs, "triggers-attached", f"{name} is not on stock_movements")

    # 8. rebuild_balances() is the disaster-recovery path: it must reproduce
    #    the live projection exactly. Run inside a transaction that is rolled
    #    back, so the audit stays read-only.
    before = dict(balances)
    db.execute(text("SELECT rebuild_balances()"))
    after = {
        (p, w, lot, st): q
        for p, w, lot, st, q in db.execute(
            select(
                StockBalance.product_id,
                StockBalance.warehouse_id,
                StockBalance.lot_id,
                StockBalance.status,
                func.sum(StockBalance.qty_on_hand),
            ).group_by(
                StockBalance.product_id,
                StockBalance.warehouse_id,
                StockBalance.lot_id,
                StockBalance.status,
            )
        ).all()
    }
    for key in set(before) | set(after):
        check(
            Decimal(before.get(key, 0) or 0) == Decimal(after.get(key, 0) or 0),
            "rebuild-reproduces-live",
            f"{key}: live {before.get(key)}, rebuilt {after.get(key)}",
        )
    db.rollback()

print()
for key in sorted(counts):
    bad = sum(1 for f in findings if f.startswith(f"[{key}]"))
    mark = "FAIL" if bad else "ok  "
    print(f"  {mark}  {key:26} {counts[key]:>7,} checked, {bad} bad")

print(f"\n{len(findings)} findings")
for f in findings[:25]:
    print("   ", f)
if len(findings) > 25:
    print(f"    ... and {len(findings) - 25} more")
