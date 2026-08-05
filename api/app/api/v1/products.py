from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.deps import require_permission
from app.core.errors import ConflictError, NotFoundError
from app.db.session import get_db
from app.models.enums import StockStatus, TrackingMode
from app.models.identity import User
from app.models.masters import Product
from app.models.stock import StockBalance
from app.schemas.common import Message, Page, PageParams, paginate
from app.schemas.masters import ProductIn, ProductOut, ProductUpdate
from app.services import audit

router = APIRouter(prefix="/products", tags=["products"])


def _to_out(product: Product, on_hand: Decimal = Decimal("0"),
            reserved: Decimal = Decimal("0")) -> ProductOut:
    return ProductOut(
        id=product.id,
        sku=product.sku,
        name=product.name,
        description=product.description,
        category_id=product.category_id,
        category_name=product.category.name if product.category else None,
        uom_id=product.uom_id,
        uom_code=product.uom.code if product.uom else None,
        tracking_mode=product.tracking_mode,
        composition=product.composition,
        manufacturer=product.manufacturer,
        pack_size=product.pack_size,
        drug_schedule=product.drug_schedule,
        storage_condition=product.storage_condition,
        is_prescription_required=product.is_prescription_required,
        hsn_code=product.hsn_code,
        gst_rate=product.gst_rate,
        barcode=product.barcode,
        reorder_point=product.reorder_point,
        safety_stock_days=product.safety_stock_days,
        sourcing_policy=product.sourcing_policy,
        mrp=product.mrp,
        is_active=product.is_active,
        qty_on_hand=on_hand,
        qty_available=on_hand - reserved,
    )


@router.get("", response_model=Page[ProductOut])
def list_products(
    q: str | None = Query(None, description="Search SKU, name, composition or barcode"),
    category_id: int | None = None,
    # Typed as the enum, not `str`. As a plain string an unrecognised value
    # reached the query and Postgres rejected the comparison against the enum
    # column, which surfaced as an anonymous 500 — the same reply the client
    # gets when the server is genuinely broken. FastAPI now refuses it at the
    # edge with a 422 that names the parameter and lists what it accepts, and
    # the value appears in the OpenAPI schema so the caller need not guess.
    tracking_mode: TrackingMode | None = None,
    is_active: bool | None = None,
    below_reorder: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("product.view")),
) -> Page[ProductOut]:
    # Aggregate on-hand per product so the list can show stock without N+1.
    stock = (
        select(
            StockBalance.product_id.label("pid"),
            func.sum(StockBalance.qty_on_hand).label("on_hand"),
            func.sum(StockBalance.qty_reserved).label("reserved"),
        )
        .where(StockBalance.status == StockStatus.AVAILABLE)
        .group_by(StockBalance.product_id)
        .subquery()
    )

    stmt = (
        select(
            Product,
            func.coalesce(stock.c.on_hand, 0),
            func.coalesce(stock.c.reserved, 0),
        )
        .outerjoin(stock, stock.c.pid == Product.id)
        .options(selectinload(Product.category), selectinload(Product.uom))
    )

    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Product.sku.ilike(needle),
                Product.name.ilike(needle),
                Product.composition.ilike(needle),
                Product.barcode.ilike(needle),
            )
        )
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if tracking_mode:
        stmt = stmt.where(Product.tracking_mode == tracking_mode)
    if is_active is not None:
        stmt = stmt.where(Product.is_active == is_active)
    if below_reorder:
        stmt = stmt.where(
            func.coalesce(stock.c.on_hand, 0) <= Product.reorder_point,
            Product.reorder_point > 0,
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.execute(
        stmt.order_by(Product.name).offset(params.offset).limit(params.size)
    ).all()

    return paginate(
        [_to_out(p, Decimal(oh), Decimal(rs)) for p, oh, rs in rows], total, params
    )


@router.post("", response_model=ProductOut, status_code=201)
def create_product(
    payload: ProductIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("product.manage")),
) -> ProductOut:
    if db.scalar(select(Product).where(Product.sku == payload.sku)):
        raise ConflictError(f"SKU {payload.sku} already exists")

    product = Product(**payload.model_dump())
    db.add(product)
    db.flush()
    db.refresh(product)

    audit.record(
        db,
        action="product.create",
        entity_type="product",
        entity_id=product.id,
        actor_user_id=user.id,
        after=payload.model_dump(mode="json"),
    )
    return _to_out(product)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("product.view")),
) -> ProductOut:
    product = db.scalar(
        select(Product)
        .options(selectinload(Product.category), selectinload(Product.uom))
        .where(Product.id == product_id)
    )
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")

    row = db.execute(
        select(
            func.coalesce(func.sum(StockBalance.qty_on_hand), 0),
            func.coalesce(func.sum(StockBalance.qty_reserved), 0),
        ).where(
            StockBalance.product_id == product_id,
            StockBalance.status == StockStatus.AVAILABLE,
        )
    ).one()
    return _to_out(product, Decimal(row[0]), Decimal(row[1]))


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("product.manage")),
) -> ProductOut:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")

    # Snapshot exactly the fields being changed, nothing more. A fixed list
    # would report untouched fields as removals and miss any field not on it,
    # which makes the audit diff actively misleading. Same approach as
    # masters.py::_apply_update.
    changes = payload.model_dump(exclude_unset=True)
    before = {field: getattr(product, field) for field in changes}
    for field, value in changes.items():
        setattr(product, field, value)
    db.flush()

    # NOTE: tracking_mode is deliberately NOT updatable. Changing it would
    # invalidate every historical ledger row for this product.
    audit.record(
        db,
        action="product.update",
        entity_type="product",
        entity_id=product.id,
        actor_user_id=user.id,
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
    )
    db.refresh(product)
    return _to_out(product)


@router.delete("/{product_id}", response_model=Message)
def deactivate_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("product.manage")),
) -> Message:
    """Soft delete. Products are never removed — history references them."""
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")

    product.is_active = False
    audit.record(
        db,
        action="product.deactivate",
        entity_type="product",
        entity_id=product.id,
        actor_user_id=user.id,
    )
    return Message(message=f"{product.sku} deactivated")
