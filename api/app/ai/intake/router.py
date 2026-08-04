"""Invoice intake endpoints.

    POST /ai/intake/invoice   read a photographed invoice into a draft receipt
    POST /ai/intake/alias     remember what a distributor calls one of ours

THIS ENDPOINT CREATES NOTHING
-----------------------------
It reads an image and returns a proposal. No stock moves, no document is
written, no ledger row appears. Stock is still created only by `POST /grn`,
which is reached when a person submits the draft this returns — after looking
at it.

That is the whole safety argument, and it is structural rather than a promise:
there is no code path from here to `ledger.post_movement`. The worst outcome of
a misread invoice is a form with a wrong number in it, which somebody corrects
before pressing the button.

WHY IT NEEDS grn.create RATHER THAN ai.view
--------------------------------------------
Reading a supplier invoice is the first half of receiving goods, and it costs
money to run. Someone who may not sign for a delivery has no reason to be able
to spend the extraction budget on photographs, so the permission that gates the
receipt gates the proposal for it too.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.ai.intake import match as matching
from app.ai.intake import service
from app.ai.intake.schemas import (
    CandidateOut,
    DraftLineOut,
    FlagOut,
    IntakeSummaryOut,
    InvoiceIntakeOut,
    RememberAliasIn,
)
from app.ai.intake.validate import Flag, Severity, validate_invoice
from app.core.deps import require_permission, scoped_warehouse_ids
from app.core.errors import (
    ExternalServiceError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from app.db.session import get_db
from app.models.documents import PurchaseOrder
from app.models.identity import User
from app.models.masters import Supplier, Warehouse
from app.schemas.common import Message
from app.services import audit

router = APIRouter(prefix="/ai/intake", tags=["AI · invoice intake"])

RECEIVER = require_permission("grn.create")

#: Guards the upload before anything is read into memory. FastAPI streams to a
#: spooled file, so the size is only known after reading; this is the ceiling
#: the service enforces again on the bytes themselves.
MAX_UPLOAD = service.MAX_IMAGE_BYTES


def _flag_out(flag: Flag) -> FlagOut:
    return FlagOut(
        field=flag.field,
        severity=flag.severity.value,
        message=flag.message,
        line_no=flag.line_no,
        suggestion=flag.suggestion,
    )


def _number(value: object, default: float | None = None) -> float | None:
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _as_date(value: object):
    from datetime import date  # noqa: PLC0415

    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


@router.post("/invoice", response_model=InvoiceIntakeOut)
async def read_invoice(
    file: UploadFile = File(..., description="Photograph or PDF of the invoice"),
    warehouse_id: int | None = Form(
        None, description="Where the goods are being received, if decided yet"
    ),
    supplier_id: int | None = Form(None),
    purchase_order_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(RECEIVER),
) -> InvoiceIntakeOut:
    """Read an invoice into a draft goods receipt. Creates nothing."""
    order = None
    if purchase_order_id is not None:
        order = db.get(PurchaseOrder, purchase_order_id)
        if order is None:
            raise NotFoundError(f"Purchase order {purchase_order_id} not found")
        # Trust the order over the form. The supplier is a property of the
        # order, and letting the two disagree would match lines against one
        # distributor's catalogue while filing the receipt under another.
        supplier_id = order.supplier_id

    # WHERE THE GOODS LAND IS NOT A PRECONDITION FOR READING THE PAPER
    #
    # It used to be required, which put the question in the wrong order: you
    # photograph the invoice to find out what arrived, and only then decide
    # where to put it. Demanding the destination first made the scan button
    # dead until a field below it was filled, for no benefit — the draft is a
    # proposal, and the receipt endpoint scopes the actual movement when it is
    # submitted, which is the check that governs the ledger.
    #
    # So it is taken where it is already known, and simply not asked for
    # otherwise:
    #   the order says so — an order names its own delivery address
    #   the user is pinned to one branch — staff always are
    #   they chose one on the form
    if warehouse_id is None and order is not None:
        warehouse_id = order.warehouse_id
    if warehouse_id is None:
        warehouse_id = user.warehouse_id

    warehouse = None
    if warehouse_id is not None:
        warehouse = db.get(Warehouse, warehouse_id)
        if warehouse is None:
            raise NotFoundError(f"Warehouse {warehouse_id} not found")

        # Whenever a destination *is* named, it obeys the same scoping the rest
        # of stock does: a branch pharmacist cannot prepare a receipt for
        # another branch by uploading a photograph to it. Naming none is not a
        # way around this — a draft that names no warehouse cannot be submitted
        # to one either.
        allowed = scoped_warehouse_ids(user)
        if allowed is not None and warehouse_id not in allowed:
            raise PermissionDenied("You cannot receive stock at this warehouse")

    if supplier_id is not None and db.get(Supplier, supplier_id) is None:
        raise NotFoundError(f"Supplier {supplier_id} not found")

    image = await file.read()
    if len(image) > MAX_UPLOAD:
        raise ValidationError(
            f"The file is {len(image) / 1e6:.1f} MB; the limit is "
            f"{MAX_UPLOAD / 1e6:.0f} MB"
        )

    try:
        doc = service.extract_invoice(
            image, mime_type=file.content_type or "image/png"
        )
    except service.IntakeUnavailable as exc:
        raise ExternalServiceError(str(exc)) from exc
    except service.IntakeRejected as exc:
        raise ValidationError(str(exc)) from exc
    except service.ExtractionFailed as exc:
        raise ExternalServiceError(f"Could not read this invoice: {exc}") from exc

    # Which state we are in is a fact about us, not about the supplier's paper.
    # Supplying it here rather than asking the model for it is what lets the
    # validator treat a mismatched CGST/IGST split as evidence of a misread.
    #
    # With no destination yet there is no "us" to compare against, so the check
    # is skipped rather than guessed — every other check still runs, because
    # every other check reads the invoice against itself.
    if warehouse is not None:
        doc = service.derive_context(doc, our_state_code=warehouse.state_code)

    shapes = matching.learn_supplier_batch_shapes(db, supplier_id)
    findings = validate_invoice(doc, known_batch_shapes=shapes)

    extracted_lines = doc.get("lines") or []
    matches = matching.match_lines(
        db, extracted_lines,
        supplier_id=supplier_id, purchase_order_id=purchase_order_id,
    )

    # Whatever the rules could not name, ask about. Second call, text only, and
    # it can only choose from products that exist — every answer is then put
    # back through the same strength and dosage-form checks an ordinary match
    # obeys. Costs nothing when it fails: the line stays unmatched.
    matching.suggest_unmatched(db, matches)

    by_line: dict[int, list[Flag]] = {}
    header: list[Flag] = []
    for flag in findings:
        (by_line.setdefault(flag.line_no, []) if flag.line_no else header).append(flag)

    lines: list[DraftLineOut] = []
    for m in matches:
        raw = m.extracted
        lines.append(DraftLineOut(
            line_no=m.line_no,
            printed_name=str(raw.get("product_name") or ""),
            batch_no=(str(raw.get("batch_no")).strip() or None
                      if raw.get("batch_no") else None),
            expiry_date=_as_date(raw.get("expiry_date")),
            quantity=_number(raw.get("quantity"), 0) or 0,
            free_quantity=_number(raw.get("free_quantity"), 0) or 0,
            rate=_number(raw.get("rate")),
            mrp=_number(raw.get("mrp")),
            pack=str(raw.get("pack") or "") or None,
            hsn=str(raw.get("hsn") or "").strip() or None,
            # Zero is what comes back for a GST column the invoice never
            # printed, and four of the six layouts here do not print one. Sent
            # as a number it would arrive as a truthful-looking 0%, which is a
            # rate some goods really do carry — so a product created off such a
            # line would be marked exempt because nobody typed anything.
            gst_rate=_number(raw.get("gst_rate")) or None,
            product_id=m.product_id,
            sku=m.product_sku,
            product_name=m.product_name,
            match_method=m.method,
            po_line_id=m.po_line_id,
            qty_outstanding=(float(m.qty_outstanding)
                             if m.qty_outstanding is not None else None),
            candidates=[CandidateOut(product_id=pid, label=label)
                        for pid, label in m.candidates],
            flags=[_flag_out(f) for f in [*by_line.get(m.line_no, []), *m.flags]],
        ))

    # A finding about a line the extractor never produced still has to surface,
    # or an invoice whose lines failed to parse would come back looking clean.
    seen = {m.line_no for m in matches}
    orphans = [f for n, fs in by_line.items() if n not in seen for f in fs]

    blocking = sum(
        1 for line in lines for f in line.flags if f.severity == Severity.BLOCK.value
    ) + sum(1 for f in [*header, *orphans] if f.severity is Severity.BLOCK)
    unmatched = sum(1 for line in lines if line.product_id is None)

    audit.record(
        db, action="intake.read", entity_type="goods_receipt", entity_id=None,
        actor_user_id=user.id,
        after={
            "warehouse_id": warehouse_id,
            "supplier_id": supplier_id,
            "purchase_order_id": purchase_order_id,
            "lines": len(lines),
            "unmatched": unmatched,
            "blocking": blocking,
        },
    )

    return InvoiceIntakeOut(
        warehouse_id=warehouse_id,
        supplier_id=supplier_id,
        purchase_order_id=purchase_order_id,
        supplier_invoice_no=str(doc.get("invoice_number") or "") or None,
        supplier_invoice_date=_as_date(doc.get("invoice_date")),
        supplier_name_printed=str(doc.get("supplier_name") or "") or None,
        supplier_gstin_printed=str(doc.get("supplier_gstin") or "") or None,
        lines=lines,
        flags=[_flag_out(f) for f in [*header, *orphans]],
        summary=IntakeSummaryOut(
            lines=len(lines),
            resolved=len(lines) - unmatched,
            unmatched=unmatched,
            blocking=blocking,
            ready=bool(lines) and unmatched == 0 and blocking == 0,
        ),
    )


@router.post("/alias", response_model=Message)
def remember_alias(
    payload: RememberAliasIn,
    db: Session = Depends(get_db),
    user: User = Depends(RECEIVER),
) -> Message:
    """Record what a distributor calls one of our products.

    Called once, after a person resolves a line the catalogue could not. Every
    later invoice from that distributor matches it exactly — which is why the
    unmatched rate falls to nothing after the first delivery rather than
    staying where it started.
    """
    if db.get(Supplier, payload.supplier_id) is None:
        raise NotFoundError(f"Supplier {payload.supplier_id} not found")

    stored = matching.remember_alias(
        db,
        supplier_id=payload.supplier_id,
        product_id=payload.product_id,
        printed_name=payload.printed_name,
        unit_cost=payload.unit_cost,
    )
    if not stored:
        # A code somebody set deliberately. Not worth overwriting on the say-so
        # of one delivery, and saying so is more use than silently succeeding.
        raise ValidationError(
            "That product already has a code recorded for this distributor"
        )

    audit.record(db, action="intake.alias", entity_type="product",
                 entity_id=payload.product_id, actor_user_id=user.id,
                 after={"supplier_id": payload.supplier_id,
                        "printed_name": payload.printed_name})
    return Message(message=f"Remembered {payload.printed_name!r} for this supplier")


@router.get("/open-orders/{supplier_id}", response_model=list[dict])
def open_orders(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(RECEIVER),
) -> list[dict]:
    """Orders a delivery from this supplier could be against.

    Offered before the upload, because naming the order is what narrows product
    matching from the whole catalogue to a dozen lines — the difference between
    guessing and looking up.
    """
    orders = matching.open_orders_for(db, supplier_id)
    return [
        {"id": o.id, "po_number": o.po_number, "status": o.status.value,
         "order_date": o.order_date.isoformat() if o.order_date else None}
        for o in orders
    ]
