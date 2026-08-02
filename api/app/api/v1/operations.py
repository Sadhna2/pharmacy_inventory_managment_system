"""Layer 1 endpoints: purchasing, sales, transfers, adjustments, recalls."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.deps import require_permission, scoped_warehouse_ids
from app.core.errors import NotFoundError, PermissionDenied
from app.db.session import get_db
from app.models.documents import (
    DocumentStatus,
    GoodsReceipt,
    PurchaseOrder,
    Recall,
    SalesOrder,
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
from app.services import audit, procurement, recall, sales, transfers

# ============================================================ purchase orders

po_router = APIRouter(prefix="/purchase-orders", tags=["purchasing"])


def _po_out(db: Session, po: PurchaseOrder) -> PurchaseOrderOut:
    out = PurchaseOrderOut.model_validate(po)
    out.supplier_name = po.supplier.name if po.supplier else None
    out.warehouse_name = po.warehouse.name if po.warehouse else None
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
    return paginate([_po_out(db, po) for po in rows], total, params)


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
    _: User = Depends(require_permission("po.view")),
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
    if po is None:
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
    return paginate([GoodsReceiptOut.model_validate(g) for g in rows], total, params)


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
    _: User = Depends(require_permission("so.view")),
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
    if so is None:
        raise NotFoundError(f"Sales order {so_id} not found")
    return _so_out(so)


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


def _tr_out(tr: StockTransfer) -> TransferOut:
    out = TransferOut.model_validate(tr)
    out.from_warehouse_name = tr.from_warehouse.name
    out.to_warehouse_name = tr.to_warehouse.name
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
    _: User = Depends(require_permission("transfer.view")),
) -> Page[TransferOut]:
    stmt = select(StockTransfer).options(
        selectinload(StockTransfer.lines),
        selectinload(StockTransfer.from_warehouse),
        selectinload(StockTransfer.to_warehouse),
    )
    if status is not None:
        stmt = stmt.where(StockTransfer.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(StockTransfer.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    return paginate([_tr_out(t) for t in rows], total, params)


@tr_router.post("", response_model=TransferOut, status_code=201)
def create_transfer(
    payload: TransferIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.create")),
) -> TransferOut:
    tr = transfers.create_transfer(
        db,
        from_warehouse_id=payload.from_warehouse_id,
        to_warehouse_id=payload.to_warehouse_id,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        notes=payload.notes,
    )
    return _tr_out(_load_transfer(db, tr.id))


@tr_router.post("/{transfer_id}/approve", response_model=TransferOut)
def approve_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.approve")),
) -> TransferOut:
    transfers.approve_transfer(db, transfer_id, user_id=user.id)
    return _tr_out(_load_transfer(db, transfer_id))


@tr_router.post("/{transfer_id}/dispatch", response_model=TransferOut)
def dispatch_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("transfer.approve")),
) -> TransferOut:
    """Stock leaves the source and becomes IN_TRANSIT at the destination."""
    transfers.dispatch_transfer(db, transfer_id, user_id=user.id)
    audit.record(db, action="transfer.dispatch", entity_type="stock_transfer",
                 entity_id=transfer_id, actor_user_id=user.id)
    return _tr_out(_load_transfer(db, transfer_id))


@tr_router.post("/{transfer_id}/receive", response_model=TransferOut)
def receive_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.move")),
) -> TransferOut:
    """IN_TRANSIT becomes AVAILABLE at the destination."""
    transfers.receive_transfer(db, transfer_id, user_id=user.id)
    audit.record(db, action="transfer.receive", entity_type="stock_transfer",
                 entity_id=transfer_id, actor_user_id=user.id)
    return _tr_out(_load_transfer(db, transfer_id))


# =============================================================== adjustments

adj_router = APIRouter(prefix="/adjustments", tags=["adjustments"])


@adj_router.get("", response_model=Page[AdjustmentOut])
def list_adjustments(
    status: DocumentStatus | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("stock.view")),
) -> Page[AdjustmentOut]:
    stmt = select(StockAdjustment).options(selectinload(StockAdjustment.lines))
    if status is not None:
        stmt = stmt.where(StockAdjustment.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.scalars(
        stmt.order_by(StockAdjustment.id.desc()).offset(params.offset).limit(params.size)
    ).all()
    return paginate([AdjustmentOut.model_validate(a) for a in rows], total, params)


@adj_router.post("", response_model=AdjustmentOut, status_code=201)
def create_adjustment(
    payload: AdjustmentIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("stock.adjust")),
) -> AdjustmentOut:
    """Raises an adjustment for approval. Nothing posts until approved."""
    adjustment = transfers.create_adjustment(
        db,
        warehouse_id=payload.warehouse_id,
        reason_code=payload.reason_code,
        lines=[line.model_dump() for line in payload.lines],
        user_id=user.id,
        notes=payload.notes,
    )
    db.refresh(adjustment)
    return AdjustmentOut.model_validate(adjustment)


@adj_router.post("/{adjustment_id}/approve", response_model=AdjustmentOut)
def approve_adjustment(
    adjustment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("adjustment.approve")),
) -> AdjustmentOut:
    """Approval is what posts to the ledger. The raiser cannot approve."""
    adjustment = transfers.approve_adjustment(db, adjustment_id, user_id=user.id)
    audit.record(db, action="adjustment.approve", entity_type="stock_adjustment",
                 entity_id=adjustment.id, actor_user_id=user.id)
    db.refresh(adjustment)
    return AdjustmentOut.model_validate(adjustment)


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
