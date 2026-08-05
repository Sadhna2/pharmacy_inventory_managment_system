"""Layer 1 endpoints: purchasing, sales, transfers, adjustments, recalls."""

from dataclasses import asdict, dataclass

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.deps import (
    require_permission,
    scoped_customer_id,
    scoped_warehouse_ids,
)
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
)
from app.db.session import get_db
from app.models.documents import (
    DocumentStatus,
    GoodsReceipt,
    PurchaseOrder,
    Recall,
    SalesOrder,
    SalesOrderLine,
    StockAdjustment,
    StockTransfer,
)
from app.models.identity import User
from app.models.masters import Product, Warehouse
from app.models.stock import Lot
from app.schemas.common import Message, Page, PageParams, paginate
from app.schemas.documents import (
    AdjustmentIn,
    AdjustmentOut,
    AllocationOut,
    GoodsReceiptIn,
    GoodsReceiptOut,
    PurchaseOrderIn,
    PurchaseOrderOut,
    RecallImpactOut,
    RecallIn,
    RecallOut,
    SalesOrderIn,
    SalesOrderOut,
    ShipmentOut,
    TransferIn,
    TransferOut,
)
from app.services import audit, invoice_html, procurement, recall, sales, transfers


def customer_may_see(user: User, order: "SalesOrder") -> bool:
    """Whether this sales order is one this account is entitled to.

    Only ever restricts a CUSTOMER; every internal role gets None from
    `scoped_customer_id` and passes straight through.
    """
    buyer = scoped_customer_id(user)
    return buyer is None or order.customer_id == buyer


def in_scope(user: User, *warehouse_ids: int | None) -> bool:
    """Whether any of these warehouses is one this user is allowed to see.

    The list endpoints filter; a route that fetches one row by id has nothing
    to filter, so it asks this and then answers 404. Deliberately 404 and not
    403: the same reply as a document that does not exist, so walking the id
    space cannot be used to count another branch's orders. `scoped_warehouse_ids`
    returning None means unrestricted — managers and admins see the chain.

    Several warehouses because a transfer has two ends, and either one being
    this user's branch makes the document theirs to read.
    """
    allowed = scoped_warehouse_ids(user)
    if allowed is None:
        return True
    return any(wid in allowed for wid in warehouse_ids if wid is not None)


# ============================================================ purchase orders

def actor_names(db: Session, *ids: int | None) -> dict[int, str]:
    """Resolve user ids to the names a person would recognise.

    Every document here records who raised it and, where a second person is
    required, who approved it. Returning the raw ids makes the approver look
    up a number to find out whose work they are certifying, which in practice
    means they do not look at all.

    Resolved here rather than through a relationship on the model because
    `created_by` and `approved_by` both point at `users.id`, so SQLAlchemy
    would need an explicit `foreign_keys=` on each — a mapper-level change
    that buys nothing over one batched query. Batched because a 200-row list
    would otherwise issue four hundred of them.
    """
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return dict(
        db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all()
    )


po_router = APIRouter(prefix="/purchase-orders", tags=["purchasing"])


def _po_out(
    db: Session, po: PurchaseOrder, names: dict[int, str] | None = None
) -> PurchaseOrderOut:
    if names is None:
        names = actor_names(db, po.created_by, po.approved_by)
    out = PurchaseOrderOut.model_validate(po)
    out.supplier_name = po.supplier.name if po.supplier else None
    out.warehouse_name = po.warehouse.name if po.warehouse else None
    out.created_by_name = names.get(po.created_by)
    out.approved_by_name = names.get(po.approved_by) if po.approved_by else None
    for line, model in zip(out.lines, po.lines, strict=True):
        line.sku = model.product.sku
        line.product_name = model.product.name
        line.tracking_mode = model.product.tracking_mode
    return out


@po_router.get("", response_model=Page[PurchaseOrderOut])
def list_purchase_orders(
    status: DocumentStatus | None = None,
    supplier_id: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.view")),
) -> Page[PurchaseOrderOut]:
    stmt = select(PurchaseOrder).options(
        selectinload(PurchaseOrder.lines),
        selectinload(PurchaseOrder.supplier),
        selectinload(PurchaseOrder.warehouse),
    )
    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(PurchaseOrder.warehouse_id.in_(allowed or [-1]))
    if status is not None:
        stmt = stmt.where(PurchaseOrder.status == status)
    if supplier_id is not None:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(PurchaseOrder.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    names = actor_names(
        db, *(po.created_by for po in rows), *(po.approved_by for po in rows)
    )
    return paginate([_po_out(db, po, names) for po in rows], total, params)


@po_router.post("", response_model=PurchaseOrderOut, status_code=201)
def create_purchase_order(
    payload: PurchaseOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.create")),
) -> PurchaseOrderOut:
    po = procurement.create_purchase_order(
        db,
        supplier_id=payload.supplier_id,
        warehouse_id=payload.warehouse_id,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        order_date=payload.order_date,
        expected_date=payload.expected_date,
        notes=payload.notes,
    )
    audit.record(db, action="po.create", entity_type="purchase_order",
                 entity_id=po.id, actor_user_id=user.id)
    db.refresh(po)
    return _po_out(db, po)


@po_router.get("/{po_id}", response_model=PurchaseOrderOut)
def get_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.view")),
) -> PurchaseOrderOut:
    po = db.scalar(
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.lines),
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.warehouse),
        )
        .where(PurchaseOrder.id == po_id)
    )
    if po is None or not in_scope(user, po.warehouse_id):
        raise NotFoundError(f"Purchase order {po_id} not found")
    return _po_out(db, po)


@po_router.post("/{po_id}/submit", response_model=PurchaseOrderOut)
def submit_po(
    po_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.create")),
) -> PurchaseOrderOut:
    po = procurement.submit_purchase_order(db, po_id)
    db.refresh(po)
    return _po_out(db, po)


@po_router.post("/{po_id}/approve", response_model=PurchaseOrderOut)
def approve_po(
    po_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.approve")),
) -> PurchaseOrderOut:
    po = procurement.approve_purchase_order(db, po_id, user_id=user.id)
    audit.record(db, action="po.approve", entity_type="purchase_order",
                 entity_id=po.id, actor_user_id=user.id)
    db.refresh(po)
    return _po_out(db, po)


@po_router.post("/{po_id}/cancel", response_model=PurchaseOrderOut)
def cancel_po(
    po_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.approve")),
) -> PurchaseOrderOut:
    po = procurement.cancel_purchase_order(db, po_id)
    db.refresh(po)
    return _po_out(db, po)


# ============================================================ goods receipts

grn_router = APIRouter(prefix="/goods-receipts", tags=["purchasing"])


@grn_router.post("", response_model=GoodsReceiptOut, status_code=201)
def receive_goods(
    payload: GoodsReceiptIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("grn.create")),
) -> GoodsReceiptOut:
    """The moment stock increases."""
    # Cross-docking dispatches stock to another branch. Receiving goods and
    # redirecting a shipment are different authorities: a branch pharmacist
    # signs for what arrived, but should not be able to send it somewhere
    # else. Require the transfer permission for exactly those lines.
    if any(line.cross_dock_warehouse_id is not None for line in payload.lines):
        if "transfer.create" not in set(user.permission_codes):
            raise PermissionDenied(
                "Cross-docking a line to another branch requires: transfer.create"
            )

    grn = procurement.receive_goods(
        db,
        warehouse_id=payload.warehouse_id,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        purchase_order_id=payload.purchase_order_id,
        supplier_invoice_no=payload.supplier_invoice_no,
        supplier_invoice_date=payload.supplier_invoice_date,
        notes=payload.notes,
    )
    audit.record(db, action="grn.create", entity_type="goods_receipt",
                 entity_id=grn.id, actor_user_id=user.id)
    db.refresh(grn)
    out = GoodsReceiptOut.model_validate(grn)
    out.received_by_name = actor_names(db, grn.received_by).get(grn.received_by)
    for line_out, line in zip(out.lines, grn.lines, strict=True):
        line_out.sku = line.product.sku
        line_out.product_name = line.product.name
        if line.lot_id:
            lot = db.get(Lot, line.lot_id)
            line_out.lot_code = lot.lot_code
            line_out.expiry_date = lot.expiry_date
        if line.cross_dock_warehouse_id:
            branch = db.get(Warehouse, line.cross_dock_warehouse_id)
            line_out.cross_dock_warehouse_name = branch.name if branch else None
    out.cross_dock_transfers = [
        number for (number,) in db.execute(
            select(StockTransfer.transfer_number)
            .where(StockTransfer.notes == f"Cross-dock from {grn.grn_number}")
            .order_by(StockTransfer.id)
        ).all()
    ]
    return out


@grn_router.get("", response_model=Page[GoodsReceiptOut])
def list_receipts(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("po.view")),
) -> Page[GoodsReceiptOut]:
    stmt = select(GoodsReceipt).options(selectinload(GoodsReceipt.lines))
    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(GoodsReceipt.warehouse_id.in_(allowed or [-1]))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(GoodsReceipt.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    names = actor_names(db, *(g.received_by for g in rows))

    def _grn_row(grn: GoodsReceipt) -> GoodsReceiptOut:
        out = GoodsReceiptOut.model_validate(grn)
        out.received_by_name = names.get(grn.received_by)
        return out

    return paginate([_grn_row(g) for g in rows], total, params)


# ============================================================== sales orders

so_router = APIRouter(prefix="/sales-orders", tags=["sales"])


def _so_out(so: SalesOrder) -> SalesOrderOut:
    out = SalesOrderOut.model_validate(so)
    out.customer_name = so.customer.name if so.customer else None
    out.warehouse_name = so.warehouse.name if so.warehouse else None
    for line_out, line in zip(out.lines, so.lines, strict=True):
        line_out.sku = line.product.sku
        line_out.product_name = line.product.name
    return out


@so_router.get("", response_model=Page[SalesOrderOut])
def list_sales_orders(
    status: DocumentStatus | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.view")),
) -> Page[SalesOrderOut]:
    stmt = select(SalesOrder).options(
        selectinload(SalesOrder.lines),
        selectinload(SalesOrder.customer),
        selectinload(SalesOrder.warehouse),
    )
    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(SalesOrder.warehouse_id.in_(allowed or [-1]))
    # Both scopes, not either: a customer is limited by who they are, an
    # internal user by where they work, and the two are independent.
    buyer = scoped_customer_id(user)
    if buyer is not None:
        stmt = stmt.where(SalesOrder.customer_id == buyer)
    if status is not None:
        stmt = stmt.where(SalesOrder.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(SalesOrder.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    return paginate([_so_out(so) for so in rows], total, params)


@so_router.post("", response_model=SalesOrderOut, status_code=201)
def create_sales_order(
    payload: SalesOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.create")),
) -> SalesOrderOut:
    so = sales.create_sales_order(
        db,
        customer_id=payload.customer_id,
        warehouse_id=payload.warehouse_id,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        order_date=payload.order_date,
        notes=payload.notes,
    )
    audit.record(db, action="so.create", entity_type="sales_order",
                 entity_id=so.id, actor_user_id=user.id)
    db.refresh(so)
    return _so_out(so)


@so_router.get("/{so_id}", response_model=SalesOrderOut)
def get_sales_order(
    so_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.view")),
) -> SalesOrderOut:
    so = db.scalar(
        select(SalesOrder)
        .options(
            selectinload(SalesOrder.lines),
            selectinload(SalesOrder.customer),
            selectinload(SalesOrder.warehouse),
        )
        .where(SalesOrder.id == so_id)
    )
    if so is None or not in_scope(user, so.warehouse_id):
        raise NotFoundError(f"Sales order {so_id} not found")
    if not customer_may_see(user, so):
        raise NotFoundError(f"Sales order {so_id} not found")
    return _so_out(so)


@dataclass(frozen=True)
class _Seller:
    """The supplying end of the invoice, satisfying `invoice_html.Party`.

    The firm itself is not a table in this system and `core/config.py` holds
    no business name or GSTIN, so there is nothing to read either from. A new
    config key would only move the problem: unset, it would print its own
    default where the seller's registered name belongs, and a placeholder on a
    statutory document is worse than an honest one. The warehouse the order
    ships from stands in instead — its name, address and state code are all
    real records someone maintains — and the GSTIN prints as an em dash rather
    than as a number nobody entered.
    """

    name: str
    address: str | None
    gstin: str | None
    state_code: str


#: A tax invoice may only be raised for a supply that has actually happened.
#: Printing one for a draft would put a serial number on a supply that may
#: never occur, and printing one for a cancelled order leaves the buyer holding
#: a document they can claim credit against for a supply the seller's books say
#: never took place.
INVOICEABLE = (DocumentStatus.SHIPPED, DocumentStatus.COMPLETED)


@so_router.get("/{so_id}/invoice", response_class=HTMLResponse)
def sales_order_invoice(
    so_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.view")),
) -> HTMLResponse:
    """The order as a print-ready GST tax invoice, for the browser to print."""
    so = db.scalar(
        select(SalesOrder)
        .options(
            # `lines.product` (and its unit) on top of what get_sales_order
            # eager-loads: the invoice prints each line's product name, SKU,
            # pack size and unit, so without this a twenty-line order issues
            # forty extra queries while the renderer walks the table.
            selectinload(SalesOrder.lines)
            .selectinload(SalesOrderLine.product)
            .selectinload(Product.uom),
            selectinload(SalesOrder.customer),
            selectinload(SalesOrder.warehouse),
        )
        .where(SalesOrder.id == so_id)
    )
    if so is None:
        raise NotFoundError(f"Sales order {so_id} not found")

    # Every other read on this router is warehouse-scoped; this one prints a
    # customer's full address and GSTIN, so it must be too. Without it a branch
    # user could print the customer list of a branch they cannot otherwise see.
    if not in_scope(user, so.warehouse_id) or not customer_may_see(user, so):
        raise NotFoundError(f"Sales order {so_id} not found")

    # After the 404, not before it. This is a fact about the server, and it was
    # being announced to callers who are not allowed to know the order exists —
    # an id out of range, or another branch's order, answered "configure your
    # GSTIN" instead of "no such order". Whether the firm is registered is not
    # something an unauthorised caller gets to learn, and a scope check that
    # can be pre-empted by a config check is not reliably a scope check.
    if not settings.can_issue_tax_invoice:
        # Refusing beats emitting a document captioned TAX INVOICE with the
        # registration missing: the caption is the claim, and an invoice
        # without the supplier's GSTIN is not one.
        raise ConflictError(
            "This system has no GSTIN configured for the selling firm, so it "
            "cannot issue a tax invoice. Set SELLER_LEGAL_NAME and "
            "SELLER_GSTIN."
        )

    if so.status not in INVOICEABLE:
        raise ConflictError(
            f"{so.so_number} is {so.status.value.lower()}. A tax invoice is "
            f"raised against a supply that has happened, so it becomes "
            f"available once the order ships."
        )

    return HTMLResponse(
        invoice_html.render_tax_invoice(
            so,
            seller=_Seller(
                name=settings.seller_legal_name,
                # The branch address is the place of business the goods left,
                # which is what belongs on the document; the firm's registered
                # address is the fallback when a branch has none recorded.
                address=so.warehouse.address or settings.seller_address,
                gstin=settings.seller_gstin,
                state_code=so.warehouse.state_code,
            ),
            # The only name this system has for a state is the two-letter code
            # itself — nothing here stores "Maharashtra" — and the renderer
            # will not translate a numeric code back to letters, because
            # several states answer to two abbreviations. So the code is what
            # prints, alongside its statutory number: "MH (27)". An order with
            # no place of supply passes the empty string and the renderer's
            # own em dash covers it.
            place_of_supply_name=so.place_of_supply or "",
        )
    )


@so_router.post("/{so_id}/allocate", response_model=list[AllocationOut])
def allocate_order(
    so_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.fulfil")),
) -> list[AllocationOut]:
    """Reserve stock, choosing batches by FEFO.

    The response tells the picker exactly which batch to take off the shelf.
    """
    reservations = sales.allocate_order(db, so_id)
    out: list[AllocationOut] = []
    for reservation in reservations:
        product = db.get(Product, reservation.product_id)
        lot = db.get(Lot, reservation.lot_id) if reservation.lot_id else None
        out.append(
            AllocationOut(
                product_id=reservation.product_id,
                sku=product.sku,
                product_name=product.name,
                lot_id=reservation.lot_id,
                lot_code=lot.lot_code if lot else None,
                expiry_date=lot.expiry_date if lot else None,
                quantity=reservation.quantity,
                mrp=(lot.mrp if lot and lot.mrp is not None else product.mrp),
            )
        )
    return out


@so_router.post("/{so_id}/ship", response_model=ShipmentOut, status_code=201)
def ship_order(
    so_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.fulfil")),
) -> ShipmentOut:
    """The moment stock decreases. Records lot -> customer for recall tracing."""
    shipment = sales.ship_order(db, so_id, user_id=user.id)
    audit.record(db, action="so.ship", entity_type="shipment",
                 entity_id=shipment.id, actor_user_id=user.id)
    db.refresh(shipment)

    # Built explicitly rather than model_validate: ShipmentLine rows do not
    # match AllocationOut's shape (they carry no sku/lot_code), so ORM
    # attribute mapping would fail validation.
    out = ShipmentOut(
        id=shipment.id,
        shipment_number=shipment.shipment_number,
        sales_order_id=shipment.sales_order_id,
        shipped_at=shipment.shipped_at,
        shipped_by=shipment.shipped_by,
        lines=[],
    )
    for line in shipment.lines:
        product = db.get(Product, line.product_id)
        lot = db.get(Lot, line.lot_id) if line.lot_id else None
        out.lines.append(
            AllocationOut(
                product_id=line.product_id,
                sku=product.sku,
                product_name=product.name,
                lot_id=line.lot_id,
                lot_code=lot.lot_code if lot else None,
                expiry_date=lot.expiry_date if lot else None,
                quantity=line.quantity,
                mrp=(lot.mrp if lot and lot.mrp is not None else product.mrp),
            )
        )
    return out


@so_router.post("/{so_id}/cancel", response_model=SalesOrderOut)
def cancel_sales_order(
    so_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("so.create")),
) -> SalesOrderOut:
    so = sales.cancel_order(db, so_id)
    db.refresh(so)
    return _so_out(so)


# ================================================================= transfers

tr_router = APIRouter(prefix="/transfers", tags=["transfers"])


def _tr_out(
    db: Session, tr: StockTransfer, names: dict[int, str] | None = None
) -> TransferOut:
    if names is None:
        names = actor_names(db, tr.created_by, tr.approved_by)
    out = TransferOut.model_validate(tr)
    out.from_warehouse_name = tr.from_warehouse.name
    out.to_warehouse_name = tr.to_warehouse.name
    out.created_by_name = names.get(tr.created_by)
    out.approved_by_name = names.get(tr.approved_by) if tr.approved_by else None
    for line_out, line in zip(out.lines, tr.lines, strict=True):
        line_out.sku = line.product.sku
        line_out.product_name = line.product.name
    return out


def _load_transfer(db: Session, transfer_id: int) -> StockTransfer:
    tr = db.scalar(
        select(StockTransfer)
        .options(
            selectinload(StockTransfer.lines),
            selectinload(StockTransfer.from_warehouse),
            selectinload(StockTransfer.to_warehouse),
        )
        .where(StockTransfer.id == transfer_id)
    )
    if tr is None:
        raise NotFoundError(f"Transfer {transfer_id} not found")
    return tr


@tr_router.get("", response_model=Page[TransferOut])
def list_transfers(
    status: DocumentStatus | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.view")),
) -> Page[TransferOut]:
    stmt = select(StockTransfer).options(
        selectinload(StockTransfer.lines),
        selectinload(StockTransfer.from_warehouse),
        selectinload(StockTransfer.to_warehouse),
    )
    # Either end of the movement is this user's business — the branch sending
    # the stock needs to watch it leave, and the branch receiving it needs to
    # see it coming. Both, and nothing else: a transfer between two other
    # branches is not a document a third branch has any part in.
    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        visible = allowed or [-1]
        stmt = stmt.where(
            or_(
                StockTransfer.from_warehouse_id.in_(visible),
                StockTransfer.to_warehouse_id.in_(visible),
            )
        )
    if status is not None:
        stmt = stmt.where(StockTransfer.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(StockTransfer.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    names = actor_names(
        db, *(t.created_by for t in rows), *(t.approved_by for t in rows)
    )
    return paginate([_tr_out(db, t, names) for t in rows], total, params)


@tr_router.post("", response_model=TransferOut, status_code=201)
def create_transfer(
    payload: TransferIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.create")),
) -> TransferOut:
    # Raising it is the sending branch's act; the destination is any branch in
    # the chain, which is the whole point of a transfer.
    if not in_scope(user, payload.from_warehouse_id):
        raise PermissionDenied(
            "You can only raise a transfer out of your own branch"
        )
    tr = transfers.create_transfer(
        db,
        from_warehouse_id=payload.from_warehouse_id,
        to_warehouse_id=payload.to_warehouse_id,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        notes=payload.notes,
    )
    return _tr_out(db, _load_transfer(db, tr.id))


@tr_router.post("/{transfer_id}/approve", response_model=TransferOut)
def approve_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.approve")),
) -> TransferOut:
    transfer = _load_transfer(db, transfer_id)
    if not in_scope(user, transfer.from_warehouse_id, transfer.to_warehouse_id):
        raise NotFoundError(f"Transfer {transfer_id} not found")
    transfers.approve_transfer(db, transfer_id, user_id=user.id)
    return _tr_out(db, _load_transfer(db, transfer_id))


@tr_router.post("/{transfer_id}/cancel", response_model=TransferOut)
def cancel_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.create")),
) -> TransferOut:
    """Abandon a transfer before it ships.

    Gated on `transfer.create` rather than `transfer.approve`: abandoning a
    document moves no stock, so the branch that raised it can take it back
    without finding a second person to agree.
    """
    transfer = _load_transfer(db, transfer_id)
    if not in_scope(user, transfer.from_warehouse_id, transfer.to_warehouse_id):
        raise NotFoundError(f"Transfer {transfer_id} not found")
    transfers.cancel_transfer(db, transfer_id, user_id=user.id)
    audit.record(db, action="transfer.cancel", entity_type="stock_transfer",
                 entity_id=transfer_id, actor_user_id=user.id)
    return _tr_out(db, _load_transfer(db, transfer_id))


@tr_router.post("/{transfer_id}/dispatch", response_model=TransferOut)
def dispatch_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.approve")),
) -> TransferOut:
    """Stock leaves the source and becomes IN_TRANSIT at the destination."""
    # Sending is the source branch's act. Without this a branch-pinned user
    # could empty a shelf they have never stood in front of.
    if not in_scope(user, _load_transfer(db, transfer_id).from_warehouse_id):
        raise NotFoundError(f"Transfer {transfer_id} not found")
    transfers.dispatch_transfer(db, transfer_id, user_id=user.id)
    audit.record(db, action="transfer.dispatch", entity_type="stock_transfer",
                 entity_id=transfer_id, actor_user_id=user.id)
    return _tr_out(db, _load_transfer(db, transfer_id))


@tr_router.post("/{transfer_id}/receive", response_model=TransferOut)
def receive_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.move")),
) -> TransferOut:
    """IN_TRANSIT becomes AVAILABLE at the destination."""
    # Receiving is the destination branch's act, and it is the one that puts
    # stock on a shelf: unguarded, a branch user could post goods into another
    # branch's on-hand and no one at that branch would have signed for them.
    if not in_scope(user, _load_transfer(db, transfer_id).to_warehouse_id):
        raise NotFoundError(f"Transfer {transfer_id} not found")
    transfers.receive_transfer(db, transfer_id, user_id=user.id)
    audit.record(db, action="transfer.receive", entity_type="stock_transfer",
                 entity_id=transfer_id, actor_user_id=user.id)
    return _tr_out(db, _load_transfer(db, transfer_id))


# =============================================================== adjustments

adj_router = APIRouter(prefix="/adjustments", tags=["adjustments"])


def _adj_out(
    db: Session, adj: StockAdjustment, names: dict[int, str] | None = None
) -> AdjustmentOut:
    if names is None:
        names = actor_names(db, adj.created_by, adj.approved_by)
    out = AdjustmentOut.model_validate(adj)
    out.created_by_name = names.get(adj.created_by)
    out.approved_by_name = names.get(adj.approved_by) if adj.approved_by else None
    return out


@adj_router.get("", response_model=Page[AdjustmentOut])
def list_adjustments(
    status: DocumentStatus | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.view")),
) -> Page[AdjustmentOut]:
    stmt = select(StockAdjustment).options(selectinload(StockAdjustment.lines))
    allowed = scoped_warehouse_ids(user)
    if allowed is not None:
        stmt = stmt.where(StockAdjustment.warehouse_id.in_(allowed or [-1]))
    if status is not None:
        stmt = stmt.where(StockAdjustment.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(StockAdjustment.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    names = actor_names(
        db, *(a.created_by for a in rows), *(a.approved_by for a in rows)
    )
    return paginate([_adj_out(db, a, names) for a in rows], total, params)


@adj_router.post("", response_model=AdjustmentOut, status_code=201)
def create_adjustment(
    payload: AdjustmentIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.adjust")),
) -> AdjustmentOut:
    """Raises an adjustment for approval. Nothing posts until approved."""
    if not in_scope(user, payload.warehouse_id):
        raise PermissionDenied(
            "You can only adjust stock at your own branch"
        )
    adjustment = transfers.create_adjustment(
        db,
        warehouse_id=payload.warehouse_id,
        reason_code=payload.reason_code,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        notes=payload.notes,
    )
    db.refresh(adjustment)
    return _adj_out(db, adjustment)


@adj_router.post("/{adjustment_id}/approve", response_model=AdjustmentOut)
def approve_adjustment(
    adjustment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("adjustment.approve")),
) -> AdjustmentOut:
    """Approval is what posts to the ledger. The raiser cannot approve."""
    pending = db.get(StockAdjustment, adjustment_id)
    if pending is None or not in_scope(user, pending.warehouse_id):
        raise NotFoundError(f"Adjustment {adjustment_id} not found")
    adjustment = transfers.approve_adjustment(db, adjustment_id, user_id=user.id)
    audit.record(db, action="adjustment.approve", entity_type="stock_adjustment",
                 entity_id=adjustment.id, actor_user_id=user.id)
    db.refresh(adjustment)
    return _adj_out(db, adjustment)


@adj_router.post("/{adjustment_id}/cancel", response_model=AdjustmentOut)
def cancel_adjustment(
    adjustment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.adjust")),
) -> AdjustmentOut:
    """Withdraw an adjustment before it posts.

    Gated on `stock.adjust` rather than `adjustment.approve`: withdrawing a
    document moves no stock, so the raiser is entitled to take back their own
    mistake without finding a second person to agree.
    """
    pending = db.get(StockAdjustment, adjustment_id)
    if pending is None or not in_scope(user, pending.warehouse_id):
        raise NotFoundError(f"Adjustment {adjustment_id} not found")
    adjustment = transfers.cancel_adjustment(db, adjustment_id, user_id=user.id)
    audit.record(db, action="adjustment.cancel", entity_type="stock_adjustment",
                 entity_id=adjustment.id, actor_user_id=user.id)
    db.refresh(adjustment)
    return _adj_out(db, adjustment)


# =================================================================== recalls

rc_router = APIRouter(prefix="/recalls", tags=["recalls"])


@rc_router.post("", response_model=RecallImpactOut, status_code=201)
def initiate_recall(
    payload: RecallIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("recall.initiate")),
) -> RecallImpactOut:
    """Freeze a batch chain-wide and trace who already received it."""
    impact = recall.initiate_recall(
        db,
        lot_id=payload.lot_id,
        reason=payload.reason,
        user_id=user.id,
        regulator_ref=payload.regulator_ref,
    )
    audit.record(db, action="recall.initiate", entity_type="recall",
                 entity_id=impact.recall_id, actor_user_id=user.id,
                 after={"lot_id": payload.lot_id, "reason": payload.reason})
    return RecallImpactOut(**asdict(impact))


@rc_router.get("", response_model=list[RecallOut])
def list_recalls(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("stock.view")),
) -> list[RecallOut]:
    recalls = db.scalars(
        select(Recall).options(selectinload(Recall.lot)).order_by(Recall.id.desc())
    ).all()
    out = []
    for r in recalls:
        item = RecallOut.model_validate(r)
        item.lot_code = r.lot.lot_code
        item.product_sku = r.lot.product.sku
        out.append(item)
    return out


@rc_router.get("/{recall_id}/impact", response_model=RecallImpactOut)
def recall_impact(
    recall_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("stock.view")),
) -> RecallImpactOut:
    return RecallImpactOut(**asdict(recall.get_impact(db, recall_id)))


@rc_router.post("/{recall_id}/close", response_model=Message)
def close_recall(
    recall_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("recall.initiate")),
) -> Message:
    """Scrap the quarantined stock and close the recall."""
    recall.close_recall(db, recall_id, user_id=user.id)
    audit.record(db, action="recall.close", entity_type="recall",
                 entity_id=recall_id, actor_user_id=user.id)
    return Message(message=f"Recall {recall_id} closed and stock scrapped")
