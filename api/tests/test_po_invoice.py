"""The distributor's invoice, kept against the order it raised.

Run like the other suites, against a live API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

Scanning moved from the receipt to the order, which changed what the file is.
On a receipt it was an input the reader consumed and discarded — the goods
were in front of you and the paper went in a folder. On an order it is the
evidence: the quantities and the prices on the order came off a document, and
"what did their invoice actually say" has to stay answerable once the person
who scanned it has gone home.

So the two things worth defending are that the bytes come back exactly as they
went in, and that the endpoint refuses anything that is not an invoice — the
download hands the stored content type straight back to the browser, and an
unchecked one is how a file gets served as HTML and runs in the reader's page.
"""

import os
import subprocess
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: A real PDF header, so the bytes are the shape the thing claims to be.
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        yield c


def _token(client: httpx.Client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def manager(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, 'manager@pharmacy.co.in')}"}


@pytest.fixture(scope="session")
def staff(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, 'staff@pharmacy.co.in')}"}


def _sql(query: str) -> None:
    """Reach past the API to clean up, the way test_e2e already does.

    There is no endpoint for this and there should not be: a purchase order is
    a document, and documents are cancelled rather than deleted. But a draft
    raised by a test and immediately abandoned is not a document anybody
    raised — it posted nothing to the ledger and settled nothing — and leaving
    nine of them per run on the Purchasing screen makes the demo data steadily
    less like a chain that trades and more like a chain that is being tested.
    """
    subprocess.run(
        [str(ROOT / "scripts" / "db.sh"), "psql", "-qtAc", query],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def order(client, manager) -> dict:
    """An order of this suite's own, so nothing here touches a seeded one."""
    products = client.get("/api/v1/products?size=200", headers=manager).json()["items"]
    suppliers = client.get("/api/v1/suppliers", headers=manager).json()
    warehouses = client.get("/api/v1/warehouses", headers=manager).json()
    central = next(w for w in warehouses if w["is_central"])

    created = client.post(
        "/api/v1/purchase-orders",
        headers=manager,
        json={
            "supplier_id": suppliers[0]["id"],
            "warehouse_id": central["id"],
            "lines": [
                {
                    "product_id": products[0]["id"],
                    "qty_ordered": 10,
                    "unit_price": "12.50",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    raised = created.json()
    yield raised
    # The invoice goes with it: the foreign key cascades, which is the point of
    # the file belonging to the order rather than standing on its own.
    _sql(f"DELETE FROM purchase_orders WHERE id = {int(raised['id'])}")


def _put(client, headers, po_id, content=PDF, name="invoice.pdf", mime="application/pdf"):
    return client.put(
        f"/api/v1/purchase-orders/{po_id}/invoice",
        headers=headers,
        files={"file": (name, content, mime)},
    )


def test_a_new_order_carries_no_invoice(order):
    assert order["has_invoice"] is False


def test_the_bytes_come_back_exactly_as_they_went_in(client, manager, order):
    assert _put(client, manager, order["id"]).status_code == 200

    got = client.get(
        f"/api/v1/purchase-orders/{order['id']}/invoice", headers=manager
    )
    assert got.status_code == 200
    assert got.content == PDF, "a stored document that changes is not evidence"
    assert got.headers["content-type"].startswith("application/pdf")


def test_the_download_is_named_for_the_order(client, manager, order):
    _put(client, manager, order["id"], name="IMG_4471.pdf")
    got = client.get(
        f"/api/v1/purchase-orders/{order['id']}/invoice", headers=manager
    )
    assert order["po_number"] in got.headers["content-disposition"], (
        "a folder of downloads called IMG_4471.pdf is a folder nobody can search"
    )
    assert got.headers["content-disposition"].endswith('.pdf"'), (
        "the extension has to survive or the file will not open"
    )


def test_the_order_says_it_has_one(client, manager, order):
    _put(client, manager, order["id"])
    fetched = client.get(
        f"/api/v1/purchase-orders/{order['id']}", headers=manager
    ).json()
    assert fetched["has_invoice"] is True, (
        "the receiving screen decides whether to offer the download from this"
    )


def test_a_second_scan_replaces_rather_than_accumulates(client, manager, order):
    """A re-scan of the same delivery is a correction, not a second invoice."""
    _put(client, manager, order["id"], content=b"%PDF-1.4 first\n")
    _put(client, manager, order["id"], content=b"%PDF-1.4 second\n")

    got = client.get(
        f"/api/v1/purchase-orders/{order['id']}/invoice", headers=manager
    )
    assert got.content == b"%PDF-1.4 second\n"


def test_something_that_is_not_an_invoice_is_refused(client, manager, order):
    """The stored type is handed back to the browser on download.

    A file accepted as `text/html` here is served as `text/html` later, in the
    reader's own session — so the list of what an invoice can be is checked
    rather than trusted.
    """
    refused = _put(
        client, manager, order["id"], content=b"<script>x</script>", mime="text/html"
    )
    assert refused.status_code == 422
    assert "not an invoice" in refused.json()["detail"]


def test_an_empty_file_is_refused(client, manager, order):
    refused = _put(client, manager, order["id"], content=b"")
    assert refused.status_code == 422


def test_storing_needs_the_right_to_raise_an_order(client, staff, order):
    """Staff receive stock; they do not raise the orders it comes against."""
    refused = _put(client, staff, order["id"])
    assert refused.status_code in (403, 404)


def test_an_order_with_no_invoice_says_so_rather_than_returning_nothing(
    client, manager, order
):
    missing = client.get(
        f"/api/v1/purchase-orders/{order['id']}/invoice", headers=manager
    )
    assert missing.status_code == 404
    assert order["po_number"] in missing.json()["detail"]
