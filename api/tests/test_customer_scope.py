"""A customer account sees its own orders, and only its own.

Run like the other live suites, against a running API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

WHAT WAS WRONG
--------------
The CUSTOMER role shipped in the first seed, is on the demo login card, and is
given "own orders only" by the SRS — but `users` recorded no customer, so
"own" pointed at nothing. Every scoped read went through
`scoped_warehouse_ids`, which pins a non-admin to `user.warehouse_id`; a buyer
has no branch, so that returned an empty list and the account was shown zero of
its ninety-nine orders. The role's only screen was an empty one.

Two failures are possible here and they are opposite, so both are tested. Too
little — the account sees nothing, which is the bug that was there. Too much —
the account sees another hospital's orders, their contact details and what they
bought, which would be considerably worse. The middle case, an account with no
buyer linked at all, must fall on the "too little" side: a misconfiguration
reads as zero, never as all.

Read-only. Nothing here raises or changes an order.
"""

import os

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: The seeded pairing, from `app/seed/bootstrap.py`.
CUSTOMER_ACCOUNT = "customer@cityhospital.co.in"
CUSTOMER_NAME = "City Hospital"


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        yield c


def _headers(client: httpx.Client, email: str) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="session")
def admin(client) -> dict[str, str]:
    return _headers(client, "admin@pharmacy.co.in")


@pytest.fixture(scope="session")
def buyer(client) -> dict[str, str]:
    return _headers(client, CUSTOMER_ACCOUNT)


def _orders(client, headers) -> dict:
    resp = client.get("/api/v1/sales-orders?size=200", headers=headers)
    resp.raise_for_status()
    return resp.json()


# --- enough ------------------------------------------------------------------


def test_the_account_can_see_its_own_orders_at_all(client, buyer):
    """The bug, stated directly: this used to be zero."""
    assert _orders(client, buyer)["total"] > 0


def test_every_order_returned_belongs_to_this_buyer(client, buyer):
    for order in _orders(client, buyer)["items"]:
        assert order["customer_name"] == CUSTOMER_NAME


def test_the_account_is_not_shown_the_whole_order_book(client, admin, buyer):
    """Guards the guard.

    If the seed ever gives City Hospital every order, the assertion above
    passes while the scope does nothing.
    """
    everyones = _orders(client, admin)["total"]
    theirs = _orders(client, buyer)["total"]

    assert 0 < theirs < everyones


def test_the_customer_is_not_branch_scoped(client, buyer):
    """A buyer orders from whichever branch has the stock.

    The old behaviour scoped them by warehouse, and a buyer has no warehouse,
    which is precisely how they ended up with nothing. If their orders all came
    from one branch this assertion is vacuous, so it is written against the
    seed's actual spread.
    """
    branches = {o["warehouse_id"] for o in _orders(client, buyer)["items"]}

    if len(branches) == 1:
        pytest.skip("this buyer has only ever ordered from one branch")
    assert len(branches) > 1


# --- not too much -------------------------------------------------------------


def _someone_elses(client, admin, buyer) -> int:
    mine = {o["id"] for o in _orders(client, buyer)["items"]}
    for order in _orders(client, admin)["items"]:
        if order["id"] not in mine:
            return order["id"]
    pytest.skip("every order in the database belongs to this buyer")


def test_another_buyers_order_is_not_readable(client, admin, buyer):
    """404, not 403 — the same answer as an order that does not exist."""
    response = client.get(
        f"/api/v1/sales-orders/{_someone_elses(client, admin, buyer)}", headers=buyer
    )

    assert response.status_code == 404


def test_another_buyers_invoice_is_not_printable(client, admin, buyer):
    """It carries the other hospital's address and GSTIN."""
    elsewhere = _someone_elses(client, admin, buyer)

    response = client.get(
        f"/api/v1/sales-orders/{elsewhere}/invoice", headers=buyer
    )

    assert response.status_code == 404


def test_the_buyer_can_read_one_of_their_own_by_id(client, buyer):
    """The other half: scoping that refuses everything is not scoping."""
    mine = _orders(client, buyer)["items"][0]["id"]

    response = client.get(f"/api/v1/sales-orders/{mine}", headers=buyer)

    assert response.status_code == 200
    assert response.json()["customer_name"] == CUSTOMER_NAME


# --- everything else stays shut ----------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/purchase-orders",
        "/api/v1/transfers",
        "/api/v1/adjustments",
        "/api/v1/products",
        "/api/v1/stock/summary",
        "/api/v1/audit",
        "/api/v1/users",
        "/api/v1/customers",
    ],
)
def test_the_role_gained_nothing_beyond_its_own_orders(client, buyer, path):
    """Linking an account to a buyer must not widen it anywhere else.

    `/customers` is the one worth naming: it is the list this account is now
    joined to, and it holds every other hospital's address, GSTIN and credit
    limit.
    """
    assert client.get(path, headers=buyer).status_code == 403


def test_the_internal_roles_are_unaffected(client, admin):
    """The customer scope must be inert for everyone who is not a customer.

    `scoped_customer_id` returns None for every internal role, so the extra
    WHERE clause is never added — and an admin still sees more than one
    hospital's orders, which is what "inert" means here in a way a status code
    could not show.
    """
    orders = _orders(client, admin)

    assert orders["total"] > 0
    buyers = {o["customer_name"] for o in orders["items"]}
    assert len(buyers) > 1, f"admin sees only {buyers} — the scope leaked"
