"""A customer's credit limit is a control, not a decoration.

Run like the other suites, against a live API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

If these fail, `credit_limit` is back to what it was: seeded, editable on the
Customers form, and read by nothing. That is worse than not offering the field
at all, because a manager who types ₹1,00,000 into it believes something is
watching, and nothing is — the first anyone hears of it is a hospital that
owes for eleven months of deliveries nobody was counting.

WHAT IS ACTUALLY BEING CAPPED
-----------------------------
Open order exposure, not a receivable. Payments are out of scope for this
system (SRS §9), so there is no unpaid balance to compute and the check does
not pretend there is: it caps the orders that are raised and not yet closed
out. The tests are written in those terms on purpose — cancelling an order
gives the headroom back, and so does *delivering* one, which a real credit
control would never allow. That last one is asserted rather than glossed over,
because it is the honest limitation of doing this without payments.
"""

import os
import uuid
from decimal import Decimal

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: Small enough that one of these orders can actually be picked and shipped
#: against seeded stock, which is what the delivered-order test needs.
ORDER_QTY = 5


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
    products = client.get("/api/v1/products?size=200", headers=manager).json()["items"]
    warehouses = client.get("/api/v1/warehouses", headers=manager).json()
    return {
        "product": next(p["id"] for p in products if p["sku"] == "PAR-650"),
        "central": next(w["id"] for w in warehouses if w["is_central"]),
    }


@pytest.fixture(scope="module")
def customer(client, manager) -> dict:
    """A customer of this file's own, whose limit these tests move around.

    The seeded customers carry the limits the demo was set up to show, and
    exposure is cumulative across everything ever raised against a customer,
    so borrowing one would both disturb the demo and make the arithmetic here
    depend on what other suites left open.
    """
    resp = client.post(
        "/api/v1/customers",
        headers=manager,
        json={
            "code": f"CREDIT-{uuid.uuid4().hex[:8].upper()}",
            "name": "Credit Limit Test Hospital",
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
def orders(client, manager) -> list[int]:
    """Sales orders this test left open, cancelled again when it ends.

    Without this, each test would raise the exposure the next one measures.
    """
    created: list[int] = []
    yield created
    for so_id in reversed(created):
        client.post(f"/api/v1/sales-orders/{so_id}/cancel", headers=manager)


# --- helpers ----------------------------------------------------------------


def set_limit(client, manager, customer, limit) -> None:
    resp = client.patch(
        f"/api/v1/customers/{customer['id']}",
        headers=manager,
        json={"credit_limit": str(limit)},
    )
    assert resp.status_code == 200, resp.text


def raise_order(client, manager, ids, customer, qty=ORDER_QTY) -> httpx.Response:
    return client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": customer["id"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["product"], "qty_ordered": qty}],
        },
    )


@pytest.fixture(scope="module")
def order_total(client, manager, ids, customer) -> Decimal:
    """What one standard test order comes to, tax and round-off included.

    Measured rather than written down, so every limit below can be expressed
    in orders — "one order", "one and a half orders" — and go on meaning that
    if the seeded price or GST rate of the product ever changes.
    """
    set_limit(client, manager, customer, "0")
    resp = raise_order(client, manager, ids, customer)
    assert resp.status_code == 201, resp.text
    so = resp.json()
    client.post(f"/api/v1/sales-orders/{so['id']}/cancel", headers=manager)
    return Decimal(so["grand_total"])


# --- the cap ----------------------------------------------------------------


def test_a_zero_credit_limit_means_no_limit_not_no_trading(
    client, manager, ids, customer, orders
):
    """Zero is the default the system writes, not a refusal.

    The seed gives a figure only to the institutional customers and leaves the
    walk-in counter at zero, and the Customers form saves zero for every
    non-institutional customer. Reading that as "refuse everything" would stop
    the shop selling to anyone who is not on account.
    """
    set_limit(client, manager, customer, "0")
    resp = raise_order(client, manager, ids, customer, qty=400)
    assert resp.status_code == 201, resp.text
    orders.append(resp.json()["id"])


def test_an_order_that_exactly_fills_the_limit_is_accepted(
    client, manager, ids, customer, order_total, orders
):
    """The limit is a ceiling to reach, not one to stay under."""
    set_limit(client, manager, customer, order_total)
    resp = raise_order(client, manager, ids, customer)
    assert resp.status_code == 201, resp.text
    orders.append(resp.json()["id"])


def test_an_order_over_the_limit_is_refused(
    client, manager, ids, customer, order_total, orders
):
    """One and a half orders of headroom takes one order, not two.

    The refusal also has to be actionable by the person reading it, so it
    names all three numbers: what is already committed, what the limit is,
    and by how much this order misses. The field error goes on `customer_id`
    because that is where the sales order form shows it.
    """
    # Halving is exact in Decimal, so the three numbers below are compared as
    # money and not as something a rounding mode could shift by a paisa.
    limit = order_total + order_total / 2
    set_limit(client, manager, customer, limit)

    first = raise_order(client, manager, ids, customer)
    assert first.status_code == 201, first.text
    orders.append(first.json()["id"])

    refused = raise_order(client, manager, ids, customer)
    assert refused.status_code == 422, refused.text
    body = refused.json()

    assert customer["name"] in body["detail"]
    assert f"₹{order_total:,.2f}" in body["detail"], "the committed exposure"
    assert f"₹{limit:,.2f}" in body["detail"], "the limit itself"
    assert f"₹{order_total * 2 - limit:,.2f}" in body["detail"], "the shortfall"
    assert body["errors"][0]["field"] == "customer_id"


def test_cancelling_an_open_order_gives_the_headroom_back(
    client, manager, ids, customer, order_total, orders
):
    """Exposure is what is still open, so closing an order releases it."""
    set_limit(client, manager, customer, order_total)

    first = raise_order(client, manager, ids, customer)
    assert first.status_code == 201, first.text
    assert raise_order(client, manager, ids, customer).status_code == 422

    cancelled = client.post(
        f"/api/v1/sales-orders/{first.json()['id']}/cancel", headers=manager
    )
    assert cancelled.status_code == 200, cancelled.text

    again = raise_order(client, manager, ids, customer)
    assert again.status_code == 201, again.text
    orders.append(again.json()["id"])


def test_a_delivered_order_stops_counting_against_the_limit(
    client, manager, ids, customer, order_total, orders
):
    """The honest limitation, asserted rather than left to be discovered.

    With no payments in the system there is no receivable, so an order that
    has been picked, shipped and completed drops out of the exposure even
    though nobody has paid for it. That is exactly what the check claims to
    do, and a test written the other way round would be describing a system
    this is not.
    """
    set_limit(client, manager, customer, order_total)

    first = raise_order(client, manager, ids, customer)
    assert first.status_code == 201, first.text
    so_id = first.json()["id"]

    allocated = client.post(f"/api/v1/sales-orders/{so_id}/allocate", headers=manager)
    assert allocated.status_code == 200, allocated.text
    shipped = client.post(f"/api/v1/sales-orders/{so_id}/ship", headers=manager)
    assert shipped.status_code == 201, shipped.text
    delivered = client.get(f"/api/v1/sales-orders/{so_id}", headers=manager).json()
    assert delivered["status"] == "COMPLETED"

    again = raise_order(client, manager, ids, customer)
    assert again.status_code == 201, again.text
    orders.append(again.json()["id"])
