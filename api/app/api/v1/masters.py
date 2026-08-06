"""Master data CRUD: categories, UOMs, warehouses, bins, suppliers, customers.

Editing and retiring follow the same rules as products: the `code` of a record
is never editable because documents refer to it, and nothing is ever deleted —
`is_active` is flipped instead, so a supplier you stopped using last year still
resolves on the purchase orders that name it.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.session import get_db
from app.models.enums import StockStatus
from app.models.identity import User
from app.models.masters import (
    Bin,
    Category,
    Customer,
    Product,
    Supplier,
    Uom,
    Warehouse,
)
from app.models.stock import StockBalance
from app.schemas.common import Message
from app.schemas.masters import (
    BinIn,
    BinOut,
    BinUpdate,
    CategoryIn,
    CategoryOut,
    CategoryUpdate,
    CustomerIn,
    CustomerOut,
    CustomerUpdate,
    SupplierIn,
    SupplierOut,
    SupplierUpdate,
    UomIn,
    UomOut,
    WalkInCustomerIn,
    WarehouseIn,
    WarehouseOut,
    WarehouseUpdate,
)
from app.services import audit, gst
from app.services.numbering import next_number

router = APIRouter(tags=["master data"])

VIEW = require_permission("master.view")
MANAGE = require_permission("master.manage")


def _apply_update(
    db: Session,
    record: Any,
    payload: Any,
    *,
    entity: str,
    user: User,
) -> None:
    """Patch changed fields and record what they were, for the history view.

    Only the fields the caller actually sent are touched, so a form that posts
    a subset never blanks the rest, and `before` holds only those same fields —
    a diff, not a full snapshot of every column.
    """
    changes = payload.model_dump(exclude_unset=True)
    before = {field: getattr(record, field) for field in changes}
    for field, value in changes.items():
        setattr(record, field, value)
    db.flush()
    audit.record(
        db,
        action=f"{entity}.update",
        entity_type=entity,
        entity_id=record.id,
        actor_user_id=user.id,
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
    )


def _retire(db: Session, record: Any, *, entity: str, user: User) -> Message:
    record.is_active = False
    db.flush()
    audit.record(
        db,
        action=f"{entity}.deactivate",
        entity_type=entity,
        entity_id=record.id,
        actor_user_id=user.id,
    )
    return Message(message=f"{record.code} retired")


# --- categories & UOMs ------------------------------------------------------


def _product_counts(db: Session, column: Any) -> dict[int, int]:
    """products-per-category / per-UOM in one grouped query.

    Counted rather than joined per row so the list stays one round trip, and
    counted over all products including retired ones — a category holding only
    discontinued lines is still in use as far as history is concerned.
    """
    rows = db.execute(
        select(column, func.count(Product.id)).where(column.is_not(None)).group_by(column)
    ).all()
    return dict(rows)  # type: ignore[arg-type]


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
):
    stmt = select(Category).order_by(Category.name)
    if is_active is not None:
        stmt = stmt.where(Category.is_active == is_active)
    counts = _product_counts(db, Product.category_id)
    return [
        CategoryOut(
            id=c.id,
            name=c.name,
            parent_id=c.parent_id,
            is_active=c.is_active,
            product_count=counts.get(c.id, 0),
        )
        for c in db.scalars(stmt)
    ]


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(
    payload: CategoryIn,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    if payload.parent_id is not None and db.get(Category, payload.parent_id) is None:
        raise NotFoundError(f"Category {payload.parent_id} not found")
    category = Category(**payload.model_dump())
    db.add(category)
    db.flush()
    audit.record(
        db,
        action="category.create",
        entity_type="category",
        entity_id=category.id,
        actor_user_id=user.id,
        after=payload.model_dump(mode="json"),
    )
    return CategoryOut.model_validate(category, from_attributes=True)


@router.patch("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    category = db.get(Category, category_id)
    if category is None:
        raise NotFoundError(f"Category {category_id} not found")

    parent_id = payload.model_dump(exclude_unset=True).get("parent_id", category.parent_id)
    if parent_id is not None:
        if parent_id == category_id:
            raise ConflictError("A category cannot be its own parent")
        if db.get(Category, parent_id) is None:
            raise NotFoundError(f"Category {parent_id} not found")
        # Categories are a tree, and the model is self-referential with nothing
        # stopping A → B → A. A cycle would make any recursive walk of the tree
        # hang, so refuse to create one.
        seen = {category_id}
        cursor = db.get(Category, parent_id)
        while cursor is not None:
            if cursor.id in seen:
                raise ConflictError(
                    f"That would make {category.name} a descendant of itself"
                )
            seen.add(cursor.id)
            cursor = db.get(Category, cursor.parent_id) if cursor.parent_id else None

    _apply_update(db, category, payload, entity="category", user=user)
    db.refresh(category)
    return CategoryOut.model_validate(category, from_attributes=True)


@router.delete("/categories/{category_id}", response_model=Message)
def retire_category(
    category_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
) -> Message:
    category = db.get(Category, category_id)
    if category is None:
        raise NotFoundError(f"Category {category_id} not found")

    children = db.scalar(
        select(func.count(Category.id)).where(
            Category.parent_id == category_id, Category.is_active
        )
    )
    if children:
        raise ConflictError(
            f"{category.name} still has {children} sub-categorie(s). Retire those first."
        )

    products = db.scalar(
        select(func.count(Product.id)).where(
            Product.category_id == category_id, Product.is_active
        )
    )
    if products:
        raise ConflictError(
            f"{category.name} still classifies {products} active product(s). "
            f"Reclassify them before retiring it."
        )

    category.is_active = False
    db.flush()
    audit.record(
        db,
        action="category.deactivate",
        entity_type="category",
        entity_id=category.id,
        actor_user_id=user.id,
    )
    return Message(message=f"{category.name} retired")


@router.get("/uoms", response_model=list[UomOut])
def list_uoms(db: Session = Depends(get_db), _: User = Depends(VIEW)):
    counts = _product_counts(db, Product.uom_id)
    return [
        UomOut(id=u.id, code=u.code, name=u.name, product_count=counts.get(u.id, 0))
        for u in db.scalars(select(Uom).order_by(Uom.code))
    ]


@router.post("/uoms", response_model=UomOut, status_code=201)
def create_uom(
    payload: UomIn, db: Session = Depends(get_db), user: User = Depends(MANAGE)
):
    """Create only — no edit, no delete.

    A UOM is the meaning of every quantity ever recorded against it. Renaming
    STRIP to BOX would silently reinterpret years of ledger rows, and there is
    no `is_active` column to retire one with. Adding is safe; changing is not.
    """
    code = payload.code.upper()
    if db.scalar(select(Uom.id).where(Uom.code == code)):
        raise ConflictError(f"UOM {code} already exists")
    uom = Uom(code=code, name=payload.name)
    db.add(uom)
    db.flush()
    audit.record(
        db,
        action="uom.create",
        entity_type="uom",
        entity_id=uom.id,
        actor_user_id=user.id,
        after={"code": uom.code, "name": uom.name},
    )
    return UomOut(id=uom.id, code=uom.code, name=uom.name, product_count=0)


# --- warehouses & bins ------------------------------------------------------


@router.get("/warehouses", response_model=list[WarehouseOut])
def list_warehouses(
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
):
    stmt = select(Warehouse).order_by(Warehouse.is_central.desc(), Warehouse.name)
    if is_active is not None:
        stmt = stmt.where(Warehouse.is_active == is_active)
    return db.scalars(stmt).all()


@router.post("/warehouses", response_model=WarehouseOut, status_code=201)
def create_warehouse(
    payload: WarehouseIn,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    if db.scalar(select(Warehouse).where(Warehouse.code == payload.code)):
        raise ConflictError(f"Warehouse code {payload.code} already exists")
    warehouse = Warehouse(**payload.model_dump())
    db.add(warehouse)
    db.flush()
    audit.record(
        db,
        action="warehouse.create",
        entity_type="warehouse",
        entity_id=warehouse.id,
        actor_user_id=user.id,
        after=payload.model_dump(mode="json"),
    )
    return warehouse


def _refuse_a_registration_from_another_state(
    *, gstin: str | None, state_code: str | None
) -> None:
    """The cross-field half of the GSTIN rule, applied to a merged record.

    Only the pairing. The shape and the checksum are the schema's job and have
    already run on anything the caller sent; what cannot be judged there is
    whether the number belongs to this branch's state, because a patch may
    carry either field alone.
    """
    if gst.gstin_state_matches(gstin or "", state_code or "") is False:
        message = (
            f"a GSTIN opens with its state's code, so a branch in "
            f"{state_code} needs one starting "
            f"{gst.gstin_prefix_for_state(state_code or '')}, not "
            f"{(gstin or '')[:2]} — a registration belongs to one state"
        )
        # Named against the field as well as spelled out in the detail, so the
        # form can put it under the input the user just typed in.
        raise ValidationError(message, [{"field": "gstin", "message": message}])


@router.patch("/warehouses/{warehouse_id}", response_model=WarehouseOut)
def update_warehouse(
    warehouse_id: int,
    payload: WarehouseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise NotFoundError(f"Warehouse {warehouse_id} not found")

    # The schema checks a GSTIN against the state in the *same* payload, and a
    # patch need not carry both. Sending only the number therefore reached the
    # database unchecked — precisely the mistake the column exists to prevent,
    # because pasting head office's registration onto a branch is a one-field
    # edit. Here the row is in hand, so the value not being changed can be read
    # rather than assumed.
    sent = payload.model_dump(exclude_unset=True)
    if "gstin" in sent or "state_code" in sent:
        _refuse_a_registration_from_another_state(
            gstin=sent.get("gstin", warehouse.gstin),
            state_code=sent.get("state_code", warehouse.state_code),
        )

    _apply_update(db, warehouse, payload, entity="warehouse", user=user)
    db.refresh(warehouse)
    return warehouse


@router.delete("/warehouses/{warehouse_id}", response_model=Message)
def retire_warehouse(
    warehouse_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
) -> Message:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise NotFoundError(f"Warehouse {warehouse_id} not found")

    # Unlike a supplier, a location physically holds things. Retiring one that
    # still has stock would strand it: invisible in pickers, yet still counted
    # in the totals. Make someone move it out first.
    on_hand = db.scalar(
        select(func.coalesce(func.sum(StockBalance.qty_on_hand), 0)).where(
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.status != StockStatus.IN_TRANSIT,
        )
    )
    if on_hand and on_hand > 0:
        # NUMERIC comes back as 12191.0000; normalize so the message reads like
        # a person wrote it.
        raise ConflictError(
            f"{warehouse.name} still holds {on_hand.normalize():f} units. "
            f"Transfer or write off the stock before retiring it."
        )
    return _retire(db, warehouse, entity="warehouse", user=user)


@router.get("/warehouses/{warehouse_id}/bins", response_model=list[BinOut])
def list_bins(
    warehouse_id: int,
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
):
    stmt = select(Bin).where(Bin.warehouse_id == warehouse_id).order_by(Bin.code)
    if is_active is not None:
        stmt = stmt.where(Bin.is_active == is_active)
    return db.scalars(stmt).all()


@router.post("/warehouses/{warehouse_id}/bins", response_model=BinOut, status_code=201)
def create_bin(
    warehouse_id: int,
    payload: BinIn,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    if db.get(Warehouse, warehouse_id) is None:
        raise NotFoundError(f"Warehouse {warehouse_id} not found")
    if db.scalar(
        select(Bin.id).where(Bin.warehouse_id == warehouse_id, Bin.code == payload.code)
    ):
        # There is a unique constraint behind this; catching it here turns a
        # 500 from the driver into a sentence the storekeeper can act on.
        raise ConflictError(f"Bin {payload.code} already exists at this location")
    bin_ = Bin(warehouse_id=warehouse_id, **payload.model_dump())
    db.add(bin_)
    db.flush()
    audit.record(
        db,
        action="bin.create",
        entity_type="bin",
        entity_id=bin_.id,
        actor_user_id=user.id,
        after={"warehouse_id": warehouse_id, **payload.model_dump(mode="json")},
    )
    return bin_


@router.patch("/bins/{bin_id}", response_model=BinOut)
def update_bin(
    bin_id: int,
    payload: BinUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    bin_ = db.get(Bin, bin_id)
    if bin_ is None:
        raise NotFoundError(f"Bin {bin_id} not found")

    changes = payload.model_dump(exclude_unset=True)
    # Flipping a shelf to cold-chain while ambient stock sits on it would mark
    # that stock as correctly stored when it never was. Empty the bin first.
    if changes.get("is_cold_chain") is True and not bin_.is_cold_chain:
        on_hand = db.scalar(
            select(func.coalesce(func.sum(StockBalance.qty_on_hand), 0)).where(
                StockBalance.bin_id == bin_id
            )
        )
        if on_hand and on_hand > 0:
            raise ConflictError(
                f"{bin_.code} holds {on_hand.normalize():f} units. Move them "
                f"before designating it cold-chain."
            )

    _apply_update(db, bin_, payload, entity="bin", user=user)
    db.refresh(bin_)
    return bin_


@router.delete("/bins/{bin_id}", response_model=Message)
def retire_bin(
    bin_id: int, db: Session = Depends(get_db), user: User = Depends(MANAGE)
) -> Message:
    bin_ = db.get(Bin, bin_id)
    if bin_ is None:
        raise NotFoundError(f"Bin {bin_id} not found")

    on_hand = db.scalar(
        select(func.coalesce(func.sum(StockBalance.qty_on_hand), 0)).where(
            StockBalance.bin_id == bin_id
        )
    )
    if on_hand and on_hand > 0:
        raise ConflictError(
            f"{bin_.code} still holds {on_hand.normalize():f} units. "
            f"Move them to another bin first."
        )
    return _retire(db, bin_, entity="bin", user=user)


# --- suppliers & customers --------------------------------------------------


def _matching(model, q: str):
    """Name, code or GSTIN contains this, case-insensitively.

    Filtered on the server rather than in the browser because these lists only
    grow. Institutional customers stay in the dozens, but every walk-in served
    at a counter becomes a row here — a chain of five branches produces
    thousands in a year, and shipping all of them to filter three characters
    against would get slower every month it ran.

    GSTIN is in the list because it is how a buyer identifies themselves on
    paper. Someone holding an invoice has the number in front of them and the
    trading name may not match what was typed into this system.
    """
    needle = f"%{q.strip()}%"
    return or_(
        model.name.ilike(needle),
        model.code.ilike(needle),
        model.gstin.ilike(needle),
    )


@router.get("/suppliers", response_model=list[SupplierOut])
def list_suppliers(
    q: str | None = Query(None, description="Search name, code or GSTIN"),
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
):
    stmt = select(Supplier).order_by(Supplier.name)
    if q:
        stmt = stmt.where(_matching(Supplier, q))
    if is_active is not None:
        stmt = stmt.where(Supplier.is_active == is_active)
    return db.scalars(stmt).all()


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
def create_supplier(
    payload: SupplierIn, db: Session = Depends(get_db), user: User = Depends(MANAGE)
):
    if db.scalar(select(Supplier).where(Supplier.code == payload.code)):
        raise ConflictError(f"Supplier code {payload.code} already exists")
    supplier = Supplier(**payload.model_dump())
    db.add(supplier)
    db.flush()
    audit.record(
        db,
        action="supplier.create",
        entity_type="supplier",
        entity_id=supplier.id,
        actor_user_id=user.id,
    )
    return supplier


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")
    _apply_update(db, supplier, payload, entity="supplier", user=user)
    db.refresh(supplier)
    return supplier


@router.delete("/suppliers/{supplier_id}", response_model=Message)
def retire_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
) -> Message:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")
    return _retire(db, supplier, entity="supplier", user=user)


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(
    q: str | None = Query(None, description="Search name, code or GSTIN"),
    is_institutional: bool | None = None,
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
):
    stmt = select(Customer).order_by(Customer.name)
    if q:
        stmt = stmt.where(_matching(Customer, q))
    if is_institutional is not None:
        stmt = stmt.where(Customer.is_institutional == is_institutional)
    if is_active is not None:
        stmt = stmt.where(Customer.is_active == is_active)
    return db.scalars(stmt).all()


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(
    payload: CustomerIn, db: Session = Depends(get_db), user: User = Depends(MANAGE)
):
    if db.scalar(select(Customer).where(Customer.code == payload.code)):
        raise ConflictError(f"Customer code {payload.code} already exists")
    customer = Customer(**payload.model_dump())
    db.add(customer)
    db.flush()
    audit.record(
        db,
        action="customer.create",
        entity_type="customer",
        entity_id=customer.id,
        actor_user_id=user.id,
    )
    return customer


@router.post("/customers/walk-in", response_model=CustomerOut, status_code=201)
def create_walk_in_customer(
    payload: WalkInCustomerIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.create")),
):
    """Capture the person at the counter without leaving the sales order.

    Guarded by `so.create` rather than `master.manage`, because this is part of
    ringing up a sale rather than an act of master-data administration. Whoever
    may raise the order may name the buyer on it; anything more — a credit
    limit, an address, retiring the record — still needs the master data screen.

    The record is a real customer, so the sale has a real counterparty on it
    and the same person coming back next month is found by name rather than
    entered twice. What it is not is institutional: no credit limit, so the
    order is settled at the counter, which is what a walk-in is.
    """
    state_code = (payload.state_code or "").strip().upper()
    if not state_code:
        # The branch the operator works at, and failing that the central
        # warehouse — an admin has no home branch, and refusing them the form
        # over a field they were never going to type would be absurd.
        home = db.get(Warehouse, user.warehouse_id) if user.warehouse_id else None
        if home is None:
            home = db.scalar(select(Warehouse).where(Warehouse.is_central))
        if home is None:
            raise ValidationError(
                "no state to fall back on — this system has no central "
                "warehouse, so the buyer's state has to be given"
            )
        state_code = home.state_code

    # The schema checked the shape and the checksum, and the pairing too if a
    # state was typed. It cannot have checked the pairing against a state that
    # was filled in here, which is the ordinary path.
    _refuse_a_registration_from_another_state(
        gstin=payload.gstin, state_code=state_code
    )

    customer = Customer(
        code=next_number(db, "WI"),
        name=payload.name.strip(),
        is_institutional=False,
        gstin=payload.gstin or None,
        state_code=state_code,
        # Blank rather than empty string, so "not recorded" is one value in
        # the column and not two. The invoice prints these only when set.
        phone=(payload.phone or "").strip() or None,
        email=(payload.email or "").strip() or None,
    )
    db.add(customer)
    db.flush()
    audit.record(
        db,
        action="customer.create",
        entity_type="customer",
        entity_id=customer.id,
        actor_user_id=user.id,
        after={"name": customer.name, "code": customer.code, "walk_in": True},
    )
    return customer


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(f"Customer {customer_id} not found")
    _apply_update(db, customer, payload, entity="customer", user=user)
    db.refresh(customer)
    return customer


@router.delete("/customers/{customer_id}", response_model=Message)
def retire_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
) -> Message:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(f"Customer {customer_id} not found")
    return _retire(db, customer, entity="customer", user=user)
