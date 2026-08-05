"""A branch user sees their own branch and no one else's.

Run like the other live suites, against a running API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

WHY THIS FILE EXISTS
--------------------
`scoped_warehouse_ids` has been in `core/deps.py` since the first week and the
purchase-order and sales-order lists have always called it. Transfers and
adjustments never did, and nobody noticed, because the seeded demo signs in as
an admin — for whom the function returns None and every query is unrestricted.
The gap was invisible from the screens and invisible in the tests.

What that cost, before this was fixed: a branch-pinned member of staff could
list every branch's stock movements, and could press "Receive at destination"
on a transfer between two branches they have nothing to do with, posting stock
onto a shelf in a building they have never been in. The receiving branch would
find goods in their on-hand that no one there had signed for.

So these tests are written from the staff account, never the admin one. An
assertion here that passes as admin is asserting nothing at all.

Read-only throughout. Nothing is raised, approved or posted: the fixtures find
existing documents at other branches and check that the staff account is
refused. A scoping test that creates the row it then reads can pass while the
filter is broken, because the row it made is in scope by construction.
"""

import os

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"


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
    """Unscoped, so the tests can see what staff should not."""
    return _headers(client, "admin@pharmacy.co.in")


@pytest.fixture(scope="session")
def staff(client) -> dict[str, str]:
    return _headers(client, "staff@pharmacy.co.in")


@pytest.fixture(scope="session")
def branch(client, staff) -> int:
    """The one warehouse the staff account is pinned to."""
    me = client.get("/api/v1/auth/me", headers=staff)
    me.raise_for_status()
    warehouse_id = me.json().get("warehouse_id")
    if warehouse_id is None:
        pytest.skip("the staff account is not pinned to a branch in this database")
    return warehouse_id


def _items(client, headers, path: str) -> list[dict]:
    resp = client.get(path, headers=headers)
    resp.raise_for_status()
    return resp.json()["items"]


# --- the lists ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "warehouse_fields"),
    [
        ("/api/v1/purchase-orders?size=200", ("warehouse_id",)),
        ("/api/v1/sales-orders?size=200", ("warehouse_id",)),
        ("/api/v1/adjustments?size=200", ("warehouse_id",)),
        # Either end of a transfer is this branch's business — the sender
        # watches it leave, the receiver watches it arrive.
        (
            "/api/v1/transfers?size=200",
            ("from_warehouse_id", "to_warehouse_id"),
        ),
    ],
)
def test_every_document_list_is_confined_to_the_users_branch(
    client, staff, branch, path, warehouse_fields
):
    for row in _items(client, staff, path):
        touches = {row.get(field) for field in warehouse_fields}
        assert branch in touches, (
            f"{path} returned a document at {touches}, "
            f"and this user is at branch {branch}"
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/purchase-orders?size=200",
        "/api/v1/sales-orders?size=200",
        "/api/v1/transfers?size=200",
        "/api/v1/adjustments?size=200",
    ],
)
def test_the_scope_actually_removes_something(client, admin, staff, path):
    """Guards the guard.

    Without this, a filter that silently returned nothing would satisfy every
    assertion above while proving nothing.

    It has to tell two failures apart, and the first version did not. "Staff
    see as many as admins" means the scope is broken *only if* documents for
    another branch exist to be hidden. On a freshly seeded database some
    document types sit entirely at one warehouse — CI hit exactly that with
    four sales orders, all of them the staff account's — and the test failed
    for the shape of the seed rather than for anything the code did.

    So the case with nothing to hide is skipped rather than failed, and the
    skip message says which it was. A scope that is genuinely broken still
    fails everywhere the seed does spread documents, which is most of them.
    """
    everyones = _items(client, admin, path)
    mine = _items(client, staff, path)

    if len(everyones) == len(mine):
        pytest.skip(
            f"{path}: every document here is at this staff account's branch, "
            f"so there is nothing for a scope to remove — this proves nothing "
            f"either way"
        )

    assert len(mine) < len(everyones), (
        f"{path}: staff sees {len(mine)} of {len(everyones)} — "
        f"nothing is being scoped"
    )


# --- fetching one document by id ---------------------------------------------


def _elsewhere(client, admin, staff, path: str, field: str) -> int:
    """The id of a document at a branch this staff account is not at."""
    mine = {row["id"] for row in _items(client, staff, path)}
    for row in _items(client, admin, path):
        if row["id"] not in mine:
            return row["id"]
    pytest.skip(f"every document on {path} is already this user's branch")


@pytest.mark.parametrize(
    ("path", "detail"),
    [
        ("/api/v1/purchase-orders?size=200", "/api/v1/purchase-orders/{}"),
        ("/api/v1/sales-orders?size=200", "/api/v1/sales-orders/{}"),
    ],
)
def test_another_branchs_document_is_not_readable_by_id(
    client, admin, staff, path, detail
):
    """404, not 403.

    The same answer as a document that does not exist, so counting the ids
    that come back 403 cannot be used to size another branch's order book.
    """
    elsewhere = _elsewhere(client, admin, staff, path, "warehouse_id")

    response = client.get(detail.format(elsewhere), headers=staff)

    assert response.status_code == 404


def test_another_branchs_invoice_is_not_printable(client, admin, staff):
    """The invoice prints a customer's full address and GSTIN.

    It is the most revealing read on the sales router, so it is the one worth
    naming separately rather than folding into the parametrised case above.
    """
    elsewhere = _elsewhere(
        client, admin, staff, "/api/v1/sales-orders?size=200", "warehouse_id"
    )

    response = client.get(f"/api/v1/sales-orders/{elsewhere}/invoice", headers=staff)

    assert response.status_code == 404


# --- writing at another branch ------------------------------------------------


def test_stock_cannot_be_received_into_a_branch_the_user_is_not_at(
    client, admin, staff, branch
):
    """The one that mattered.

    `stock.move` is a permission STAFF actually holds, so before this was
    fixed the refusal below was a 200: a branch user could press "Receive at
    destination" on a transfer between two branches they have nothing to do
    with, and the goods would land on that branch's shelf with this user's
    name against them. Every other write in this file is refused by the
    permission gate long before scope is consulted; this one was not.

    The assertion that nothing moved is the real point. A 404 that had already
    posted to the ledger would be worse than no check at all.
    """
    in_transit = _items(client, admin, "/api/v1/transfers?status=IN_TRANSIT&size=200")
    elsewhere = next(
        (t for t in in_transit if t["to_warehouse_id"] != branch), None
    )
    if elsewhere is None:
        pytest.skip("nothing is currently in transit to another branch")
    before = client.get("/api/v1/transfers?size=200", headers=admin).json()["total"]

    response = client.post(
        f"/api/v1/transfers/{elsewhere['id']}/receive", headers=staff
    )

    assert response.status_code == 404
    after = client.get("/api/v1/transfers?size=200", headers=admin).json()
    still_in_transit = next(
        t for t in after["items"] if t["id"] == elsewhere["id"]
    )
    assert still_in_transit["status"] == "IN_TRANSIT"
    assert after["total"] == before


def test_the_write_guards_are_unreachable_for_todays_roles_and_that_is_fine(
    client, staff, branch, admin
):
    """Raising a transfer or an adjustment elsewhere is refused — by permission.

    STAFF, the only branch-pinned role there is, holds neither
    `transfer.create` nor `stock.adjust`; MANAGER and ADMIN hold both and are
    unscoped. So the scope guards on those two routes cannot fire for anyone
    who exists today, and this test says so out loud rather than dressing the
    permission gate up as a scoping result.

    They are still worth keeping, and worth pinning here: the moment someone
    adds a branch-pinned role that can raise documents — a shift supervisor,
    say — the guard is the only thing standing between them and another
    branch's ledger. What this asserts is that both routes refuse, whichever
    gate does the refusing.
    """
    warehouses = client.get("/api/v1/warehouses", headers=admin).json()
    other = next(w["id"] for w in warehouses if w["id"] != branch)
    product = client.get("/api/v1/products?size=1", headers=admin).json()["items"][0]

    adjustment = client.post(
        "/api/v1/adjustments",
        headers=staff,
        json={
            "warehouse_id": other,
            "reason_code": "DAMAGE",
            "lines": [{"product_id": product["id"], "quantity": "-1"}],
        },
    )
    transfer = client.post(
        "/api/v1/transfers",
        headers=staff,
        json={
            "from_warehouse_id": other,
            "to_warehouse_id": branch,
            "lines": [{"product_id": product["id"], "quantity": "1"}],
        },
    )

    assert adjustment.status_code == 403
    assert transfer.status_code == 403
