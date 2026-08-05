"""HSN belongs to the document line, not to the product it was copied from.

Run like the other suites, against a live API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

If these fail, correcting a product's HSN — a mistyped digit, a
reclassification, a distributor's catalogue catching up with a circular —
silently rewrites every document ever raised for that product. An invoice
reprinted a year later would carry a code that is not on the copy the customer
holds, sitting next to the `gst_rate` the line *did* freeze, so the document
would contradict itself and the tax charged on it could not be defended to
anyone asking. Nothing else in this system recomputes posted tax from current
master data; the classification that tax was charged under is part of that.
"""

import os
import uuid

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: A different real-looking HSN to correct the product to. It has to differ
#: from the seeded one for any of this to be worth asserting.
CORRECTED_HSN = "30049011"


# --- fixtures ---------------------------------------------------------------


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
def ids(client, manager) -> dict:
    """The seeded product, warehouse and supplier these documents are raised on."""
    products = client.get("/api/v1/products?size=200", headers=manager).json()["items"]
    warehouses = client.get("/api/v1/warehouses", headers=manager).json()
    suppliers = client.get("/api/v1/suppliers", headers=manager).json()
    by_sku = {p["sku"]: p for p in products}
    return {
        "product": by_sku["PAR-650"],
        "central": next(w["id"] for w in warehouses if w["is_central"]),
        "supplier": suppliers[0]["id"],
    }


@pytest.fixture(scope="module")
def customer(client, manager) -> dict:
    """A customer of this file's own, on no credit limit.

    Sales orders are now capped by the customer's open-order exposure, so
    borrowing a seeded customer would make these assertions depend on whatever
    the other suites happened to leave open.
    """
    resp = client.post(
        "/api/v1/customers",
        headers=manager,
        json={
            "code": f"HSN-{uuid.uuid4().hex[:8].upper()}",
            "name": "HSN Snapshot Test Buyer",
            "is_institutional": True,
            "state_code": "MH",
            "credit_limit": "0",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    yield body
    client.delete(f"/api/v1/customers/{body['id']}", headers=manager)


@pytest.fixture
def raised(client, manager) -> list[tuple[str, int]]:
    """Documents this test raised, cancelled again when it ends.

    These suites run against the demo database, and an order left standing
    would turn up in the Operations list of whoever next shows the product.
    """
    created: list[tuple[str, int]] = []
    yield created
    for path, doc_id in reversed(created):
        client.post(f"/api/v1/{path}/{doc_id}/cancel", headers=manager)


# --- helpers ----------------------------------------------------------------


def raise_po(client, manager, ids, raised, product_id: int) -> dict:
    resp = client.post(
        "/api/v1/purchase-orders",
        headers=manager,
        json={
            "supplier_id": ids["supplier"],
            "warehouse_id": ids["central"],
            "lines": [
                {"product_id": product_id, "qty_ordered": 10, "unit_price": "18.50"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    raised.append(("purchase-orders", body["id"]))
    return body


def raise_so(client, manager, ids, customer, raised, product_id: int) -> dict:
    resp = client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": customer["id"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": product_id, "qty_ordered": 2}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    raised.append(("sales-orders", body["id"]))
    return body


# --- the snapshot -----------------------------------------------------------


def test_a_purchase_order_line_records_the_hsn_it_was_raised_under(
    client, manager, ids, raised
):
    po = raise_po(client, manager, ids, raised, ids["product"]["id"])
    assert po["lines"][0]["hsn_code"] == ids["product"]["hsn_code"]


def test_a_sales_order_line_records_the_hsn_it_was_raised_under(
    client, manager, ids, customer, raised
):
    so = raise_so(client, manager, ids, customer, raised, ids["product"]["id"])
    assert so["lines"][0]["hsn_code"] == ids["product"]["hsn_code"]


def test_a_product_with_no_hsn_leaves_the_line_blank_rather_than_guessing(
    client, manager, ids, raised
):
    """Null is the honest answer. An invented code would be filed with GST."""
    created = client.post(
        "/api/v1/products",
        headers=manager,
        json={
            "sku": f"NOHSN-{uuid.uuid4().hex[:6].upper()}",
            "name": "Unclassified Test Item",
            "uom_id": ids["product"]["uom_id"],
        },
    )
    assert created.status_code == 201, created.text
    product = created.json()
    try:
        assert product["hsn_code"] is None
        po = raise_po(client, manager, ids, raised, product["id"])
        assert po["lines"][0]["hsn_code"] is None
    finally:
        client.delete(f"/api/v1/products/{product['id']}", headers=manager)


def test_correcting_a_products_hsn_leaves_documents_already_raised_alone(
    client, manager, ids, customer, raised
):
    """The correction applies from now on, not backwards.

    This is the whole reason the column exists. A purchase order and a sales
    order are raised, the product's HSN is then corrected, and both documents
    must still read back under the code they were taxed on — while the next
    documents raised pick the correction up immediately.
    """
    product = ids["product"]
    original = product["hsn_code"]
    assert original and original != CORRECTED_HSN, "the seeded HSN must differ"

    before_po = raise_po(client, manager, ids, raised, product["id"])
    before_so = raise_so(client, manager, ids, customer, raised, product["id"])

    try:
        corrected = client.patch(
            f"/api/v1/products/{product['id']}",
            headers=manager,
            json={"hsn_code": CORRECTED_HSN},
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["hsn_code"] == CORRECTED_HSN

        reread_po = client.get(
            f"/api/v1/purchase-orders/{before_po['id']}", headers=manager
        ).json()
        reread_so = client.get(
            f"/api/v1/sales-orders/{before_so['id']}", headers=manager
        ).json()
        assert reread_po["lines"][0]["hsn_code"] == original
        assert reread_so["lines"][0]["hsn_code"] == original

        after_po = raise_po(client, manager, ids, raised, product["id"])
        after_so = raise_so(client, manager, ids, customer, raised, product["id"])
        assert after_po["lines"][0]["hsn_code"] == CORRECTED_HSN
        assert after_so["lines"][0]["hsn_code"] == CORRECTED_HSN
    finally:
        client.patch(
            f"/api/v1/products/{product['id']}",
            headers=manager,
            json={"hsn_code": original},
        )
