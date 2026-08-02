from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from app.core.deps import require_permission, scoped_warehouse_ids
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.models.enums import MovementType, StockStatus
from app.models.identity import User
from app.models.masters import Bin, Product, Warehouse
from app.models.stock import Lot, StockBalance, StockMovement
from app.schemas.common import Page, PageParams, paginate
from app.schemas.stock import (
    BalanceOut,
    ExpiringStockOut,
    LotIn,
    LotOut,
    MovementIn,
    MovementOut,
    ReverseMovementIn,
    StockSummary,
)
from app.services import audit, ledger

router = APIRouter(prefix="/stock", tags=["stock"])


@router.get("/balances", response_model=Page[BalanceOut])
def list_balances(
    product_id: int | None = None,
    warehouse_id: int | None = None,
    status: StockStatus | None = None,
    q: str | None = Query(
        None, description="Matches batch code, product name or SKU"
    ),
    expiry_within_days: int | None = Query(
        None,
        ge=0,
        le=3650,
        description="Batches expiring within this many days. Already-expired "
        "stock is always included — it is the most urgent case.",
    ),
    expired: bool | None = Query(
        None, description="true = only expired batches; false = exclude them"
    ),
    only_positive: bool = True,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.view")),
) -> Page[BalanceOut]:
    """Balances, with the two lookups the floor actually does.

    "Where is batch PAR240815?" during a recall, and "what expires this
    quarter?" when deciding what to push. Both were previously a scroll
    through every page.
    """
    stmt = (
        select(StockBalance, Product, Warehouse, Bin, Lot)
        .join(Product, Product.id == StockBalance.product_id)
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .outerjoin(Bin, Bin.id == StockBalance.bin_id)
        .outerjoin(Lot, Lot.id == StockBalance.lot_id)
    )

    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(StockBalance.warehouse_id.in_(allowed or [-1]))
    if product_id is not None:
        stmt = stmt.where(StockBalance.product_id == product_id)
    if warehouse_id is not None:
        stmt = stmt.where(StockBalance.warehouse_id == warehouse_id)
    if status is not None:
        stmt = stmt.where(StockBalance.status == status)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Lot.lot_code.ilike(pattern),
                Product.name.ilike(pattern),
                Product.sku.ilike(pattern),
            )
        )
    if expiry_within_days is not None:
        # `<=` with no lower bound on purpose: stock that expired last week is
        # a subset of "expiring within 30 days", not something to hide from it.
        stmt = stmt.where(
            Lot.expiry_date.is_not(None),
            Lot.expiry_date <= date.today() + timedelta(days=expiry_within_days),
        )
    if expired is not None:
        stmt = stmt.where(
            and_(Lot.expiry_date.is_not(None), Lot.expiry_date < date.today())
            if expired
            # Untracked goods have no expiry at all and are not expired, so
            # they belong in the "not expired" set rather than being filtered
            # out by a bare date comparison.
            else or_(Lot.expiry_date.is_(None), Lot.expiry_date >= date.today())
        )
    if only_positive:
        stmt = stmt.where(StockBalance.qty_on_hand > 0)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.execute(
        stmt.order_by(Product.name, Warehouse.name, Lot.expiry_date)
        .offset(params.offset)
        .limit(params.size)
    ).all()

    items = [
        BalanceOut(
            product_id=b.product_id,
            sku=p.sku,
            product_name=p.name,
            warehouse_id=b.warehouse_id,
            warehouse_name=w.name,
            bin_id=b.bin_id,
            bin_code=bn.code if bn else None,
            lot_id=b.lot_id,
            lot_code=lt.lot_code if lt else None,
            expiry_date=lt.expiry_date if lt else None,
            # The batch's own printed price wins; the product's is only a
            # stand-in for untracked goods and pre-existing batches.
            mrp=(lt.mrp if lt and lt.mrp is not None else p.mrp),
            status=b.status,
            qty_on_hand=b.qty_on_hand,
            qty_reserved=b.qty_reserved,
            qty_available=b.qty_on_hand - b.qty_reserved,
        )
        for b, p, w, bn, lt in rows
    ]
    return paginate(items, total, params)


@router.get("/movements", response_model=Page[MovementOut])
def list_movements(
    product_id: int | None = None,
    warehouse_id: int | None = None,
    movement_type: MovementType | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.view")),
) -> Page[MovementOut]:
    # Self-join to the entry that corrects this one, if any, so the list can
    # mark a row as already reversed without an extra query per row.
    reversal = aliased(StockMovement)
    stmt = (
        select(StockMovement, Product, Warehouse, Lot, User, reversal.id)
        .join(Product, Product.id == StockMovement.product_id)
        .join(Warehouse, Warehouse.id == StockMovement.warehouse_id)
        .outerjoin(Lot, Lot.id == StockMovement.lot_id)
        .outerjoin(User, User.id == StockMovement.created_by)
        .outerjoin(
            reversal,
            and_(
                reversal.reference_type == "REVERSAL",
                reversal.reference_id == StockMovement.id,
            ),
        )
    )

    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(StockMovement.warehouse_id.in_(allowed or [-1]))
    if product_id is not None:
        stmt = stmt.where(StockMovement.product_id == product_id)
    if warehouse_id is not None:
        stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
    if movement_type is not None:
        stmt = stmt.where(StockMovement.movement_type == movement_type)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.execute(
        stmt.order_by(StockMovement.occurred_at.desc(), StockMovement.id.desc())
        .offset(params.offset)
        .limit(params.size)
    ).all()

    can_see_cost = "stock.view_cost" in set(user.permission_codes)
    items = [
        MovementOut(
            id=m.id,
            movement_type=m.movement_type,
            product_id=m.product_id,
            sku=p.sku,
            product_name=p.name,
            warehouse_id=m.warehouse_id,
            warehouse_name=w.name,
            bin_id=m.bin_id,
            lot_id=m.lot_id,
            lot_code=lt.lot_code if lt else None,
            status=m.status,
            quantity=m.quantity,
            # Cost visibility is a permission, not a role.
            unit_cost=m.unit_cost if can_see_cost else None,
            reference_type=m.reference_type,
            reference_id=m.reference_id,
            reversed_by_id=rev_id,
            occurred_at=m.occurred_at,
            created_by=m.created_by,
            created_by_name=u.full_name if u else None,
            notes=m.notes,
        )
        for m, p, w, lt, u, rev_id in rows
    ]
    return paginate(items, total, params)


@router.post("/movements", response_model=MovementOut, status_code=201)
def create_movement(
    payload: MovementIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.move")),
) -> MovementOut:
    movement = ledger.post_movement(
        db,
        product_id=payload.product_id,
        warehouse_id=payload.warehouse_id,
        quantity=payload.quantity,
        movement_type=payload.movement_type,
        user_id=user.id,
        bin_id=payload.bin_id,
        lot_id=payload.lot_id,
        status=payload.status,
        unit_cost=payload.unit_cost,
        reference_type="MANUAL",
        occurred_at=payload.occurred_at,
        notes=payload.notes,
    )
    audit.record(
        db,
        action="stock.movement",
        entity_type="stock_movement",
        entity_id=movement.id,
        actor_user_id=user.id,
        after={
            "product_id": payload.product_id,
            "quantity": str(payload.quantity),
            "type": payload.movement_type.value,
        },
    )
    db.refresh(movement)
    return MovementOut.model_validate(movement)


@router.post("/movements/{movement_id}/reverse", response_model=MovementOut,
             status_code=201)
def reverse_movement(
    movement_id: int,
    payload: ReverseMovementIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.adjust")),
) -> MovementOut:
    """The only way to undo a movement. The original stays in the ledger."""
    reversal = ledger.reverse_movement(
        db, movement_id, user_id=user.id, reason=payload.reason
    )
    audit.record(
        db,
        action="stock.reverse",
        entity_type="stock_movement",
        entity_id=reversal.id,
        actor_user_id=user.id,
        after={"reverses": movement_id, "reason": payload.reason},
    )
    db.refresh(reversal)
    return MovementOut.model_validate(reversal)


@router.get("/expiring", response_model=list[ExpiringStockOut])
def expiring_stock(
    days: int = Query(30, ge=0, le=365),
    warehouse_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.view")),
) -> list[ExpiringStockOut]:
    """Batches expiring within `days`. Pass days=0 for already-expired stock."""
    cutoff = date.today() + timedelta(days=days)
    stmt = (
        select(StockBalance, Product, Warehouse, Lot)
        .join(Product, Product.id == StockBalance.product_id)
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .join(Lot, Lot.id == StockBalance.lot_id)
        .where(
            and_(
                StockBalance.qty_on_hand > 0,
                Lot.expiry_date.isnot(None),
                Lot.expiry_date <= cutoff,
            )
        )
        .order_by(Lot.expiry_date)
    )

    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(StockBalance.warehouse_id.in_(allowed or [-1]))
    if warehouse_id is not None:
        stmt = stmt.where(StockBalance.warehouse_id == warehouse_id)

    return [
        ExpiringStockOut(
            product_id=b.product_id,
            sku=p.sku,
            product_name=p.name,
            warehouse_id=b.warehouse_id,
            warehouse_name=w.name,
            lot_id=lt.id,
            lot_code=lt.lot_code,
            expiry_date=lt.expiry_date,
            qty_on_hand=b.qty_on_hand,
            days_to_expiry=(lt.expiry_date - date.today()).days,
        )
        for b, p, w, lt in db.execute(stmt).all()
    ]


@router.get("/summary", response_model=StockSummary)
def stock_summary(
    warehouse_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.view")),
) -> StockSummary:
    allowed = scoped_warehouse_ids(user)

    def scope(stmt):
        if allowed is not None:
            stmt = stmt.where(StockBalance.warehouse_id.in_(allowed or [-1]))
        if warehouse_id is not None:
            stmt = stmt.where(StockBalance.warehouse_id == warehouse_id)
        return stmt

    totals = db.execute(
        scope(
            select(
                func.count(func.distinct(StockBalance.product_id)),
                func.coalesce(
                    func.sum(
                        case(
                            (StockBalance.status == StockStatus.AVAILABLE,
                             StockBalance.qty_on_hand),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (StockBalance.status == StockStatus.QUARANTINE,
                             StockBalance.qty_on_hand),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (StockBalance.status == StockStatus.IN_TRANSIT,
                             StockBalance.qty_on_hand),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(StockBalance.qty_on_hand > 0)
        )
    ).one()

    today = date.today()
    expiring = db.scalar(
        scope(
            select(func.count(func.distinct(StockBalance.lot_id)))
            .join(Lot, Lot.id == StockBalance.lot_id)
            .where(
                StockBalance.qty_on_hand > 0,
                Lot.expiry_date.between(today, today + timedelta(days=30)),
            )
        )
    ) or 0
    expired = db.scalar(
        scope(
            select(func.count(func.distinct(StockBalance.lot_id)))
            .join(Lot, Lot.id == StockBalance.lot_id)
            .where(StockBalance.qty_on_hand > 0, Lot.expiry_date < today)
        )
    ) or 0

    on_hand_sub = (
        select(
            StockBalance.product_id.label("pid"),
            func.sum(StockBalance.qty_on_hand).label("oh"),
        )
        .where(StockBalance.status == StockStatus.AVAILABLE)
        .group_by(StockBalance.product_id)
        .subquery()
    )
    below = db.scalar(
        select(func.count())
        .select_from(Product)
        .outerjoin(on_hand_sub, on_hand_sub.c.pid == Product.id)
        .where(
            Product.is_active.is_(True),
            Product.reorder_point > 0,
            func.coalesce(on_hand_sub.c.oh, 0) <= Product.reorder_point,
        )
    ) or 0

    value = None
    if "stock.view_cost" in set(user.permission_codes):
        # Value each batch at what that batch actually cost. Where there is no
        # batch — untracked goods, or stock received before costs were kept per
        # batch — fall back to the product's weighted average from the ledger
        # (Appendix A2). A balance with neither contributes nothing rather than
        # being valued at a price it never had.
        value = db.scalar(
            text("""
                SELECT COALESCE(
                         SUM(b.qty_on_hand * COALESCE(l.purchase_cost, c.avg_cost)),
                         0)
                FROM stock_balances b
                LEFT JOIN lots l ON l.id = b.lot_id
                LEFT JOIN (
                    SELECT product_id,
                           SUM(quantity * COALESCE(unit_cost,0))
                             / NULLIF(SUM(quantity), 0) AS avg_cost
                    FROM stock_movements
                    WHERE quantity > 0 AND unit_cost IS NOT NULL
                    GROUP BY product_id
                ) c ON c.product_id = b.product_id
                WHERE b.qty_on_hand > 0
            """)
        )

    return StockSummary(
        total_skus=totals[0] or 0,
        total_units=Decimal(totals[1] or 0),
        below_reorder_point=below,
        expiring_30_days=expiring,
        expired_on_hand=expired,
        quarantined_units=Decimal(totals[2] or 0),
        in_transit_units=Decimal(totals[3] or 0),
        stock_value=Decimal(value) if value is not None else None,
    )


# --- lots -------------------------------------------------------------------

lots_router = APIRouter(prefix="/lots", tags=["lots"])


@lots_router.get("", response_model=list[LotOut])
def list_lots(
    product_id: int | None = None,
    expiring_within_days: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("stock.view")),
) -> list[LotOut]:
    stmt = select(Lot)
    if product_id is not None:
        stmt = stmt.where(Lot.product_id == product_id)
    if expiring_within_days is not None:
        stmt = stmt.where(
            Lot.expiry_date <= date.today() + timedelta(days=expiring_within_days)
        )

    return [
        LotOut(
            id=lot.id,
            product_id=lot.product_id,
            lot_code=lot.lot_code,
            mfg_date=lot.mfg_date,
            expiry_date=lot.expiry_date,
            supplier_id=lot.supplier_id,
            received_at=lot.received_at,
            days_to_expiry=(lot.expiry_date - date.today()).days
            if lot.expiry_date
            else None,
        )
        for lot in db.scalars(stmt.order_by(Lot.expiry_date.nulls_last())).all()
    ]


@lots_router.post("", response_model=LotOut, status_code=201)
def create_lot(
    payload: LotIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("stock.move")),
) -> LotOut:
    product = db.get(Product, payload.product_id)
    if product is None:
        raise NotFoundError(f"Product {payload.product_id} not found")

    existing = db.scalar(
        select(Lot).where(
            Lot.product_id == payload.product_id, Lot.lot_code == payload.lot_code
        )
    )
    if existing:
        return LotOut.model_validate(existing)

    lot = Lot(**payload.model_dump())
    db.add(lot)
    db.flush()
    db.refresh(lot)
    return LotOut.model_validate(lot)
