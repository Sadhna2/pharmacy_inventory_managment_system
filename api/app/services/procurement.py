"""Purchase orders and goods receipts.

A PO is intent — it moves no stock. A GRN is reality: it is the moment stock
increases, and it goes through ledger.post_movement() like everything else.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.documents import (
    DocumentStatus,
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
)
from app.models.enums import MovementType, StockStatus, TrackingMode
from app.models.masters import Product, Supplier, Warehouse
from app.models.stock import Lot
from app.services import audit, gst, ledger, numbering, transfers


def create_purchase_order(
    db: Session,
    *,
    supplier_id: int,
    warehouse_id: int,
    lines: list[dict],
    user_id: int,
    order_date: date | None = None,
    expected_date: date | None = None,
    notes: str | None = None,
) -> PurchaseOrder:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise NotFoundError(f"Warehouse {warehouse_id} not found")
    if not lines:
        raise ValidationError("A purchase order needs at least one line")

    # Place of supply: the distributor's state vs the receiving warehouse's.
    interstate = gst.is_interstate(supplier.state_code, warehouse.state_code)

    po = PurchaseOrder(
        po_number=numbering.next_number(db, "PO"),
        supplier_id=supplier_id,
        warehouse_id=warehouse_id,
        status=DocumentStatus.DRAFT,
        order_date=order_date or clock.today(),
        expected_date=expected_date,
        notes=notes,
        created_by=user_id,
        is_interstate=interstate,
        place_of_supply=warehouse.state_code,
    )
    db.add(po)
    db.flush()

    breakdowns = []
    for line in lines:
        product = db.get(Product, line["product_id"])
        if product is None:
            raise NotFoundError(f"Product {line['product_id']} not found")

        tax = gst.compute_line_tax(
            quantity=Decimal(line["qty_ordered"]),
            unit_price=Decimal(line["unit_price"]),
            gst_rate=product.gst_rate,
            interstate=interstate,
        )
        breakdowns.append(tax)
        db.add(
            PurchaseOrderLine(
                purchase_order_id=po.id,
                product_id=product.id,
                qty_ordered=Decimal(line["qty_ordered"]),
                unit_price=Decimal(line["unit_price"]),
                taxable_value=tax.taxable_value,
                gst_rate=tax.gst_rate,
                cgst_amount=tax.cgst_amount,
                sgst_amount=tax.sgst_amount,
                igst_amount=tax.igst_amount,
                line_total=tax.line_total,
            )
        )

    totals = gst.compute_document_totals(breakdowns)
    po.subtotal = totals.subtotal
    po.tax_total = totals.tax_total
    po.round_off = totals.round_off
    po.grand_total = totals.grand_total
    db.flush()
    return po


def submit_purchase_order(db: Session, po_id: int) -> PurchaseOrder:
    po = _get_po(db, po_id)
    if po.status != DocumentStatus.DRAFT:
        raise ConflictError(f"Only draft orders can be submitted (this is {po.status.value})")
    po.status = DocumentStatus.PENDING_APPROVAL
    db.flush()
    return po


def approve_purchase_order(db: Session, po_id: int, *, user_id: int) -> PurchaseOrder:
    po = _get_po(db, po_id)
    if po.status not in (DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL):
        raise ConflictError(f"Cannot approve an order in state {po.status.value}")
    if po.created_by == user_id:
        # Separation of duties: the raiser cannot also approve.
        raise ValidationError(
            "A purchase order must be approved by someone other than its creator"
        )
    po.status = DocumentStatus.APPROVED
    po.approved_by = user_id
    po.approved_at = datetime.now(UTC)
    db.flush()
    return po


def cancel_purchase_order(db: Session, po_id: int) -> PurchaseOrder:
    po = _get_po(db, po_id)
    if po.status in (DocumentStatus.RECEIVED, DocumentStatus.CANCELLED):
        raise ConflictError(f"Cannot cancel an order in state {po.status.value}")
    po.status = DocumentStatus.CANCELLED
    db.flush()
    return po


def receive_goods(
    db: Session,
    *,
    warehouse_id: int,
    lines: list[dict],
    user_id: int,
    purchase_order_id: int | None = None,
    supplier_invoice_no: str | None = None,
    supplier_invoice_date: date | None = None,
    notes: str | None = None,
) -> GoodsReceipt:
    """Record what physically arrived. THIS is where stock increases.

    Each line may carry batch details; for LOT_EXPIRY products the lot is
    created on the fly if it does not already exist, because a receipt is
    normally the first time a batch is ever seen.
    """
    if not lines:
        raise ValidationError("A goods receipt needs at least one line")

    po = None
    if purchase_order_id is not None:
        po = _get_po(db, purchase_order_id)
        if po.status not in (
            DocumentStatus.APPROVED,
            DocumentStatus.PARTIALLY_RECEIVED,
        ):
            raise ConflictError(
                f"Cannot receive against an order in state {po.status.value}"
            )
        if po.warehouse_id != warehouse_id:
            # An order names the place it is being delivered to, and receiving
            # it somewhere else is almost always the wrong order picked from
            # the list rather than a genuine diversion. Left unchecked it books
            # stock to a branch that never saw it and closes an order that was
            # never delivered — two wrong balances from one mis-click. A real
            # redirection is a cross-dock, which is a per-line choice below.
            raise ValidationError(
                f"{po.po_number} is for delivery to {po.warehouse.name}, "
                f"not the location you are receiving into",
                [{"field": "warehouse_id", "message": (
                    f"This order is for {po.warehouse.name}."
                )}],
            )

    grn = GoodsReceipt(
        grn_number=numbering.next_number(db, "GRN"),
        purchase_order_id=purchase_order_id,
        warehouse_id=warehouse_id,
        supplier_invoice_no=supplier_invoice_no,
        supplier_invoice_date=supplier_invoice_date,
        received_by=user_id,
        notes=notes,
    )
    db.add(grn)
    db.flush()

    for line in lines:
        product = db.get(Product, line["product_id"])
        if product is None:
            raise NotFoundError(f"Product {line['product_id']} not found")

        quantity = Decimal(line["quantity"])
        if quantity <= 0:
            raise ValidationError("Received quantity must be positive")

        lot_id = _resolve_lot(db, product, line, supplier_id=po.supplier_id if po else None)

        # The product's MRP is the default offered for the next receipt, so
        # track the most recent pack. Batches already on the shelf keep theirs
        # and go on selling at the price printed on them.
        if line.get("mrp") is not None:
            product.mrp = Decimal(line["mrp"])

        db.add(
            GoodsReceiptLine(
                goods_receipt_id=grn.id,
                purchase_order_line_id=line.get("purchase_order_line_id"),
                product_id=product.id,
                lot_id=lot_id,
                bin_id=line.get("bin_id"),
                quantity=quantity,
                unit_cost=Decimal(line["unit_cost"]),
                cross_dock_warehouse_id=line.get("cross_dock_warehouse_id"),
            )
        )

        # Schedule H1/X and cold-chain goods land in QUARANTINE pending a
        # check; everything else is immediately available.
        status = (
            StockStatus.QUARANTINE
            if line.get("quarantine", False)
            else StockStatus.AVAILABLE
        )

        ledger.post_movement(
            db,
            product_id=product.id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            movement_type=MovementType.PURCHASE_RECEIPT,
            user_id=user_id,
            bin_id=line.get("bin_id"),
            lot_id=lot_id,
            status=status,
            unit_cost=Decimal(line["unit_cost"]),
            reference_type="GRN",
            reference_id=grn.id,
            notes=f"Receipt {grn.grn_number}",
        )

        # Credit the PO line. If the caller did not name one, match by product
        # against the outstanding lines — that is how receiving actually works
        # in practice, since a delivery note lists products, not line ids.
        po_line = None
        if line.get("purchase_order_line_id"):
            po_line = db.get(PurchaseOrderLine, line["purchase_order_line_id"])
        elif po is not None:
            po_line = next(
                (
                    candidate
                    for candidate in po.lines
                    if candidate.product_id == product.id
                    and candidate.qty_received < candidate.qty_ordered
                ),
                None,
            )
        if po_line is not None:
            po_line.qty_received += quantity

    if po is not None:
        _refresh_po_status(db, po)

    db.flush()
    _cross_dock(db, grn, lines, user_id=user_id)
    return grn


def _cross_dock(
    db: Session, grn: GoodsReceipt, lines: list[dict], *, user_id: int
) -> None:
    """Send lines marked for a branch straight back out, without shelving them.

    500 strips arrive at central and 100 are already spoken for by Andheri.
    The old path was: receive all 500, walk to the transfers screen, raise a
    document, approve it, dispatch it. Four steps and a physical put-away that
    gets undone an hour later.

    Marking the line "for Andheri" at receipt does the same thing in one
    action. Note what this is NOT: it is not a shortcut around the ledger. The
    stock is still received at central — it legally arrived there and the
    purchase is against that location — and a real transfer document is still
    created, approved and dispatched. The saving is the walk to the shelf and
    back, which is exactly what cross-docking means in a warehouse.

    Grouped by destination so ten lines for one branch make one truck, not ten.
    """
    by_destination: dict[int, list[dict]] = {}
    for line, receipt_line in zip(lines, grn.lines, strict=True):
        destination = line.get("cross_dock_warehouse_id")
        if destination is None:
            continue
        if destination == grn.warehouse_id:
            raise ValidationError(
                "Cross-dock destination must differ from the receiving location"
            )
        if db.get(Warehouse, destination) is None:
            raise NotFoundError(f"Warehouse {destination} not found")
        if line.get("quarantine", False):
            # Quarantined stock has not been checked yet. Forwarding it would
            # push an unverified batch onto a branch shelf, which is the one
            # thing quarantine exists to prevent.
            raise ValidationError(
                "A quarantined line cannot be cross-docked — release it first"
            )
        by_destination.setdefault(destination, []).append({
            "product_id": receipt_line.product_id,
            "lot_id": receipt_line.lot_id,
            "quantity": receipt_line.quantity,
        })

    for destination, transfer_lines in sorted(by_destination.items()):
        transfer = transfers.create_transfer(
            db,
            from_warehouse_id=grn.warehouse_id,
            to_warehouse_id=destination,
            lines=transfer_lines,
            user_id=user_id,
            notes=f"Cross-dock from {grn.grn_number}",
        )
        # Approved automatically: whoever recorded the receipt already decided
        # where the goods were going, and a separate approval step on a
        # document nobody composed by hand is theatre. The audit trail records
        # that it was system-approved as part of the receipt.
        transfers.approve_transfer(db, transfer.id, user_id=user_id)
        transfers.dispatch_transfer(db, transfer.id, user_id=user_id)
        audit.record(
            db,
            action="grn.cross_dock",
            entity_type="goods_receipt",
            entity_id=grn.id,
            actor_user_id=user_id,
            after={
                "transfer_number": transfer.transfer_number,
                "to_warehouse_id": destination,
                "lines": len(transfer_lines),
            },
        )


def _resolve_lot(
    db: Session, product: Product, line: dict, *, supplier_id: int | None
) -> int | None:
    """Find or create the batch for a receipt line."""
    if product.tracking_mode in (TrackingMode.NONE, TrackingMode.SERIAL):
        return None

    lot_code = line.get("lot_code")
    if not lot_code:
        raise ValidationError(
            f"{product.sku} is batch-tracked — a batch number is required"
        )

    expiry = line.get("expiry_date")
    if product.tracking_mode == TrackingMode.LOT_EXPIRY and not expiry:
        raise ValidationError(f"{product.sku} requires an expiry date")

    if expiry and isinstance(expiry, date) and expiry <= clock.today():
        raise ValidationError(
            f"Batch {lot_code} of {product.sku} expired on {expiry} — "
            "expired stock must not be received"
        )

    # The MRP printed on the carton. Falling back to the product keeps a
    # receipt possible when the box is already open, without inventing a price.
    mrp = line.get("mrp")
    if mrp is None:
        mrp = product.mrp

    existing = db.scalar(
        select(Lot).where(Lot.product_id == product.id, Lot.lot_code == lot_code)
    )
    if existing is not None:
        # A second delivery of a batch already on the shelf. Its printed price
        # cannot have changed — the packs are identical — so fill the gap only
        # if the batch never had one.
        if existing.mrp is None and mrp is not None:
            existing.mrp = mrp
        if existing.purchase_cost is None:
            existing.purchase_cost = line.get("unit_cost")
        return existing.id

    lot = Lot(
        product_id=product.id,
        lot_code=lot_code,
        mfg_date=line.get("mfg_date"),
        expiry_date=expiry,
        supplier_id=supplier_id,
        mrp=mrp,
        purchase_cost=line.get("unit_cost"),
    )
    db.add(lot)
    db.flush()
    return lot.id


def _refresh_po_status(db: Session, po: PurchaseOrder) -> None:
    # Flush BEFORE refresh: the qty_received increments are still pending in
    # the session, and refreshing first would discard them and re-read stale
    # values, leaving a fully-received PO stuck at APPROVED.
    db.flush()
    fully = all(line.qty_received >= line.qty_ordered for line in po.lines)
    any_received = any(line.qty_received > 0 for line in po.lines)
    if fully:
        po.status = DocumentStatus.RECEIVED
    elif any_received:
        po.status = DocumentStatus.PARTIALLY_RECEIVED


def _get_po(db: Session, po_id: int) -> PurchaseOrder:
    po = db.scalar(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(PurchaseOrder.id == po_id)
    )
    if po is None:
        raise NotFoundError(f"Purchase order {po_id} not found")
    return po
