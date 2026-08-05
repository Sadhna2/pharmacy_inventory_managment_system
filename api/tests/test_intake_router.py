"""The intake endpoint's contract (app/ai/intake/router.py).

Exercised through FastAPI's TestClient with the database and the extractor
both replaced, because the thing worth pinning down here is the *shape of the
promise* — that the endpoint creates nothing, that it reports what it could not
resolve rather than papering over it, and that a warehouse a user may not touch
is refused before an image is ever sent anywhere.

The end-to-end path over a real server and a real Postgres is `test_e2e.py`.
This file must keep running on a machine with no database, no API key and no
network, which is what CI has.
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.ai.intake import router as intake_router
from app.ai.intake import service
from app.core.deps import require_permission
from app.db.session import get_db
from app.main import app


def png() -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
            + chunk(b"IEND", b""))


EXTRACTION = {
    "invoice_number": "APX/26-27/8891",
    "invoice_date": "2026-08-02",
    "supplier_name": "APEX PHARMA TRADERS",
    "supplier_gstin": "27AAPFU0939F1ZV",
    "columns_seen": ["S.N.", "PRODUCT", "BATCH", "EXP", "QTY", "RATE", "AMOUNT"],
    "lines": [
        {
            "sn": 1, "product_name": "PARACETAMOL 650MG TAB", "pack": "10x10",
            "hsn": "3004", "batch_no": "ZP01-73", "expiry_date": "2027-08-31",
            "quantity": 10, "free_quantity": 0, "mrp": 116.00, "rate": 88.06,
            "discount_pct": 2.5, "gst_rate": 12,
            "taxable_amount": 858.59, "tax_amount": 103.03,
        }
    ],
    "totals": {"taxable_amount": 858.59, "cgst": 51.52, "sgst": 51.52,
               "igst": 0.0, "round_off": -0.03, "grand_total": 961.60},
}


@dataclass
class FakeWarehouse:
    id: int = 1
    state_code: str = "27"


@dataclass
class FakeSupplier:
    id: int = 5


@dataclass
class FakeRole:
    code: str = "ADMIN"


@dataclass
class FakeUser:
    """`scoped_warehouse_ids` reads `role.code`, so a bare ORM User with an
    unloaded relationship is not enough — and loading one would need the
    database this file exists to do without."""

    id: int = 1
    role: FakeRole = None
    warehouse_id: int | None = None
    permission_codes: tuple[str, ...] = ("grn.create",)

    def __post_init__(self):
        if self.role is None:
            self.role = FakeRole()


class FakeSession:
    """Just enough Session for the endpoint's own logic.

    The matcher and the audit recorder are patched out separately, so the only
    database access left in the handler is `db.get` for the three records it
    validates before doing any work.
    """

    def __init__(self, records=None):
        self.records = records or {}

    def get(self, model, pk):
        return self.records.get((model.__name__, pk))

    def flush(self):
        pass


@pytest.fixture
def client(monkeypatch):
    """A signed-in receiver, a stubbed extractor, and no database."""
    session = FakeSession({
        ("Warehouse", 1): FakeWarehouse(),
        ("Warehouse", 2): FakeWarehouse(id=2),
        ("Supplier", 5): FakeSupplier(),
    })
    user = FakeUser()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[intake_router.RECEIVER] = lambda: user
    app.dependency_overrides[require_permission("grn.create")] = lambda: user
    # These tests run against a fake session that holds no feature_flags rows,
    # so the real switch would read "off" and every case here would 404 on the
    # gate before reaching the code it means to exercise. Whether the switch
    # works is its own test, below.
    app.dependency_overrides[intake_router.LIVE] = lambda: None

    monkeypatch.setattr(service, "extract_invoice",
                        lambda image, mime_type="image/png": dict(EXTRACTION))
    monkeypatch.setattr(intake_router.service, "extract_invoice",
                        lambda image, mime_type="image/png": dict(EXTRACTION))
    monkeypatch.setattr(intake_router.matching, "learn_supplier_batch_shapes",
                        lambda db, supplier_id, **kw: None)
    monkeypatch.setattr(intake_router.matching, "match_lines",
                        lambda db, lines, **kw: _matches(lines))
    # The model-assisted pass is exercised in test_intake_match.py, where it
    # can be driven without a database. Here it must simply not reach out.
    monkeypatch.setattr(intake_router.matching, "suggest_unmatched",
                        lambda db, matches, **kw: 0)
    monkeypatch.setattr(intake_router.audit, "record",
                        lambda *a, **k: None)

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _matches(lines):
    from app.ai.intake.match import EXACT_NAME, LineMatch

    return [
        LineMatch(line_no=i, extracted=line, product_id=42, product_sku="PARA-650",
                  product_name="PARACETAMOL 650MG TAB", method=EXACT_NAME)
        for i, line in enumerate(lines, start=1)
    ]


def upload(client, **form):
    return client.post(
        "/api/v1/ai/intake/invoice",
        files={"file": ("invoice.png", io.BytesIO(png()), "image/png")},
        data={"warehouse_id": 1, **form},
    )


# ------------------------------------------------------------------ the happy path


def test_a_readable_invoice_returns_a_draft(client):
    response = upload(client)
    assert response.status_code == 200
    body = response.json()
    assert body["supplier_invoice_no"] == "APX/26-27/8891"
    assert body["supplier_invoice_date"] == "2026-08-02"
    assert len(body["lines"]) == 1


def test_the_draft_carries_both_what_was_printed_and_what_it_resolved_to(client):
    line = upload(client).json()["lines"][0]
    assert line["printed_name"] == "PARACETAMOL 650MG TAB"
    assert line["batch_no"] == "ZP01-73"
    assert line["expiry_date"] == "2027-08-31"
    assert line["quantity"] == 10
    assert line["rate"] == 88.06
    assert (line["product_id"], line["sku"]) == (42, "PARA-650")
    assert line["match_method"] == "exact_name"


def test_a_clean_invoice_is_ready_to_post(client):
    summary = upload(client).json()["summary"]
    assert summary == {"lines": 1, "resolved": 1, "unmatched": 0,
                       "blocking": 0, "ready": True}


# --------------------------------------------------------------- what it refuses


def test_an_unknown_warehouse_is_rejected(client):
    assert upload(client, warehouse_id=999).status_code == 404


def test_an_unknown_supplier_is_rejected(client):
    assert upload(client, supplier_id=404).status_code == 404


def test_a_branch_user_cannot_prepare_a_receipt_for_another_branch(client):
    """The same scoping the rest of stock obeys, applied before the upload.

    Without it, uploading a photograph would be a way around branch isolation:
    the draft names a warehouse, and the draft is what gets submitted.
    """
    app.dependency_overrides[intake_router.RECEIVER] = lambda: FakeUser(
        role=FakeRole(code="STAFF"), warehouse_id=2
    )
    response = upload(client, warehouse_id=1)
    assert response.status_code == 403


def test_a_branch_user_may_prepare_one_for_their_own_branch(client):
    app.dependency_overrides[intake_router.RECEIVER] = lambda: FakeUser(
        role=FakeRole(code="STAFF"), warehouse_id=1
    )
    assert upload(client, warehouse_id=1).status_code == 200


# ------------------------------------------------- reading before deciding


def test_the_paper_can_be_read_before_anyone_says_where_it_goes(client):
    """Where the stock lands is not a precondition for reading the invoice.

    It used to be required, which put the question in the wrong order — you
    photograph the carton to find out what arrived, and only then decide where
    to put it. Nothing is created here, so there is nothing to scope yet.
    """
    response = client.post(
        "/api/v1/ai/intake/invoice",
        files={"file": ("invoice.png", io.BytesIO(png()), "image/png")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["warehouse_id"] is None
    assert len(body["lines"]) == 1


def test_a_receiver_pinned_to_one_branch_does_not_have_to_say_which(client):
    """Staff are pinned, so the answer is already known — asking is noise."""
    app.dependency_overrides[intake_router.RECEIVER] = lambda: FakeUser(
        role=FakeRole(code="STAFF"), warehouse_id=1
    )
    response = client.post(
        "/api/v1/ai/intake/invoice",
        files={"file": ("invoice.png", io.BytesIO(png()), "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["warehouse_id"] == 1


def test_naming_no_warehouse_is_not_a_way_around_branch_scoping(client):
    """The guarantee has to survive the field becoming optional.

    A branch user who omits the destination gets a draft for their own branch,
    never a blank pass to prepare one for somebody else's.
    """
    app.dependency_overrides[intake_router.RECEIVER] = lambda: FakeUser(
        role=FakeRole(code="STAFF"), warehouse_id=2
    )
    # Omitted: falls back to their own branch, not to "anywhere".
    open_ended = client.post(
        "/api/v1/ai/intake/invoice",
        files={"file": ("invoice.png", io.BytesIO(png()), "image/png")},
    )
    assert open_ended.json()["warehouse_id"] == 2
    # Named explicitly: still refused, exactly as before.
    assert upload(client, warehouse_id=1).status_code == 403


def test_a_file_that_is_not_an_image_is_rejected(client, monkeypatch):
    def refuse(image, mime_type="image/png"):
        raise service.IntakeRejected("this file is not a PNG, JPEG, WEBP or PDF")

    monkeypatch.setattr(intake_router.service, "extract_invoice", refuse)
    assert upload(client).status_code == 422


def test_an_unconfigured_server_says_so_rather_than_failing_obscurely(
    client, monkeypatch
):
    def unavailable(image, mime_type="image/png"):
        raise service.IntakeUnavailable("invoice scanning is not configured")

    monkeypatch.setattr(intake_router.service, "extract_invoice", unavailable)
    response = upload(client)
    assert response.status_code == 502
    assert "not configured" in response.text


def test_an_unreadable_page_is_reported_not_returned_empty(client, monkeypatch):
    """An empty draft would look exactly like a clean invoice with no lines."""
    def failed(image, mime_type="image/png"):
        raise service.ExtractionFailed("the model returned no content")

    monkeypatch.setattr(intake_router.service, "extract_invoice", failed)
    assert upload(client).status_code == 502


# ------------------------------------------------------------------- reporting


def test_an_unresolved_line_is_reported_with_its_shortlist(client, monkeypatch):
    from app.ai.intake.match import UNMATCHED, LineMatch
    from app.ai.intake.validate import Flag, Severity

    def unmatched(db, lines, **kw):
        return [LineMatch(
            line_no=1, extracted=lines[0], method=UNMATCHED,
            candidates=[(3, "AMLO-5 — AMLODIPINE 5MG TAB")],
            flags=[Flag("product_name", Severity.BLOCK, "no match", 1)],
        )]

    monkeypatch.setattr(intake_router.matching, "match_lines", unmatched)
    body = upload(client).json()
    line = body["lines"][0]
    assert line["product_id"] is None
    assert line["candidates"] == [{"product_id": 3,
                                   "label": "AMLO-5 — AMLODIPINE 5MG TAB"}]
    assert body["summary"]["unmatched"] == 1
    assert body["summary"]["ready"] is False


def test_validator_findings_reach_the_line_they_belong_to(client, monkeypatch):
    """A batch the supplier has never shipped should land on that row."""
    broken = dict(EXTRACTION)
    broken["lines"] = [dict(EXTRACTION["lines"][0], batch_no="")]
    monkeypatch.setattr(intake_router.service, "extract_invoice",
                        lambda image, mime_type="image/png": dict(broken))
    line = upload(client).json()["lines"][0]
    assert any(f["field"] == "batch_no" and f["severity"] == "BLOCK"
               for f in line["flags"])


def test_a_document_with_no_lines_does_not_look_clean(client, monkeypatch):
    empty = dict(EXTRACTION, lines=[])
    monkeypatch.setattr(intake_router.service, "extract_invoice",
                        lambda image, mime_type="image/png": dict(empty))
    body = upload(client).json()
    assert body["summary"]["ready"] is False
    assert any(f["field"] == "lines" for f in body["flags"])


def test_switching_invoice_scanning_off_closes_the_endpoint(client, monkeypatch):
    """The administrator's switch has to be enforced here, not just in the menu.

    This was the one AI router that read the permission but never the feature
    flag, so turning invoice scanning off hid the button and left the endpoint
    answering — and still spending the extraction budget — for anyone with the
    URL or a stale tab.

    The gate is dropped for every other test in this file, so without this one
    nothing would notice if it were removed again.
    """
    from app.services import settings as settings_service

    # Drop the fixture's bypass so the real gate runs, and report every
    # feature as off underneath it. Patching `features` rather than the
    # dependency keeps the route's own registered dependency in play — the
    # thing being tested — and sidesteps the module-level cache inside it.
    del app.dependency_overrides[intake_router.LIVE]
    monkeypatch.setattr(settings_service, "features", lambda _db: {})

    assert upload(client).status_code == 404
