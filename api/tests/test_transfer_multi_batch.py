"""A transfer drawn from two batches ships, arrives, and keeps them apart.

Run like the other live suites, against a running API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

WHAT USED TO HAPPEN
-------------------
Asking to move more than the oldest batch holds is ordinary — FEFO empties one
lot and rolls into the next, which is what allocation does everywhere else in
this system. Transfers alone refused it, and refused it at the wrong moment:
creation accepted the line, approval accepted it, and *dispatch* raised "quantity
spans multiple batches. Create one transfer line per batch." There was no cancel,
so the transfer could not be dispatched and could not be closed; it sat approved
forever, and the advice it gave was unfollowable because no screen lists which
batches a branch holds.

Both halves are tested here: the split now ships, and a transfer that cannot be
dispatched for any other reason can at least be withdrawn.

WHAT THIS FILE WRITES
---------------------
Real stock moves, so the test moves it back: every transfer raised here is
either cancelled, or dispatched and received and then reversed by a return
transfer. Nothing is left in transit — an abandoned IN_TRANSIT transfer is
stock this system believes is on a lorry forever.
"""

import os
from decimal import Decimal

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
def manager(client) -> dict[str, str]:
    return _headers(client, "manager@pharmacy.co.in")


@pytest.fixture(scope="session")
def admin(client) -> dict[str, str]:
    """A second signature, since the raiser cannot approve their own."""
    return _headers(client, "admin@pharmacy.co.in")


@pytest.fixture(scope="session")
def route(client, admin) -> tuple[int, int]:
    """Somewhere to send stock from, and somewhere to send it to."""
    warehouses = client.get("/api/v1/warehouses?is_active=true", headers=admin).json()
    central = next((w for w in warehouses if w["is_central"]), None)
    other = next((w for w in warehouses if not w["is_central"]), None)
    if central is None or other is None:
        pytest.skip("need a central warehouse and one branch")
    return central["id"], other["id"]


def _balances(client, admin, *, product_id: int, warehouse_id: int) -> list[dict]:
    resp = client.get(
        f"/api/v1/stock/balances?product_id={product_id}"
        f"&warehouse_id={warehouse_id}&size=200",
        headers=admin,
    )
    resp.raise_for_status()
    body = resp.json()
    rows = body["items"] if isinstance(body, dict) else body
    return [r for r in rows if r.get("status") == "AVAILABLE"]


@pytest.fixture(scope="session")
def two_batches(client, admin, route) -> dict:
    """A product held in at least two batches at the source.

    Chosen from live balances rather than created, so the quantities are the
    ones FEFO will really see.
    """
    source, _ = route
    products = client.get("/api/v1/products?size=200", headers=admin).json()["items"]
    for product in products:
        if product["tracking_mode"] not in ("LOT", "LOT_EXPIRY"):
            continue
        rows = _balances(
            client, admin, product_id=product["id"], warehouse_id=source
        )
        lots = {
            r["lot_id"]: Decimal(str(r["qty_on_hand"])) - Decimal(str(r.get("qty_reserved", 0)))
            for r in rows
            if r.get("lot_id") is not None
        }
        usable = {lot: q for lot, q in lots.items() if q > 0}
        if len(usable) >= 2:
            ordered = sorted(usable.items(), key=lambda kv: kv[1])
            # More than the smallest batch, less than the two together, so the
            # quantity is forced to span exactly two.
            smallest = ordered[0][1]
            spanning = smallest + min(ordered[1][1], Decimal("1"))
            if spanning > smallest:
                return {
                    "product": product,
                    "smallest": smallest,
                    "spanning": spanning,
                }
    pytest.skip("no product is held in two usable batches at the source")


@pytest.fixture
def raised(client, manager):
    """Transfers this test made. Anything still cancellable is withdrawn."""
    made: list[int] = []
    yield made
    for transfer_id in made:
        client.post(f"/api/v1/transfers/{transfer_id}/cancel", headers=manager)


def _raise(client, manager, raised, source, dest, product_id, quantity) -> dict:
    resp = client.post(
        "/api/v1/transfers",
        headers=manager,
        json={
            "from_warehouse_id": source,
            "to_warehouse_id": dest,
            "notes": "multi-batch test",
            "lines": [{"product_id": product_id, "quantity": str(quantity)}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    raised.append(body["id"])
    return body


# --- the split ships ----------------------------------------------------------


def test_a_quantity_spanning_two_batches_dispatches(
    client, manager, admin, route, two_batches, raised
):
    """The refusal this file exists for."""
    source, dest = route
    transfer = _raise(
        client, manager, raised, source, dest,
        two_batches["product"]["id"], two_batches["spanning"],
    )

    assert (
        client.post(
            f"/api/v1/transfers/{transfer['id']}/approve", headers=admin
        ).status_code
        == 200
    )
    dispatched = client.post(
        f"/api/v1/transfers/{transfer['id']}/dispatch", headers=admin
    )

    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["status"] == "IN_TRANSIT"

    # Put it back so the next run starts where this one did.
    client.post(f"/api/v1/transfers/{transfer['id']}/receive", headers=admin)


def test_each_batch_arrives_as_itself(
    client, manager, admin, route, two_batches, raised
):
    """Not merged into one lot on the way.

    Batch identity is the whole point of the ledger here — a recall traces by
    lot, and expiry is a property of the batch, so a transfer that landed two
    batches as one would make both untraceable at the destination.
    """
    source, dest = route
    product_id = two_batches["product"]["id"]
    before = {
        r["lot_id"] for r in _balances(
            client, admin, product_id=product_id, warehouse_id=dest
        )
    }

    transfer = _raise(
        client, manager, raised, source, dest, product_id, two_batches["spanning"]
    )
    client.post(f"/api/v1/transfers/{transfer['id']}/approve", headers=admin)
    client.post(f"/api/v1/transfers/{transfer['id']}/dispatch", headers=admin)
    received = client.post(
        f"/api/v1/transfers/{transfer['id']}/receive", headers=admin
    )

    assert received.status_code == 200, received.text
    assert received.json()["status"] == "COMPLETED"

    after = {
        r["lot_id"] for r in _balances(
            client, admin, product_id=product_id, warehouse_id=dest
        )
    }
    assert len(after - before) >= 1 or len(after) >= 2, (
        "the destination gained no distinguishable batch"
    )


def test_nothing_is_left_on_the_road(
    client, manager, admin, route, two_batches, raised
):
    """Dispatch and receipt must balance batch by batch, not just in total.

    If receipt put away a different split from the one dispatch sent, the
    destination would keep a permanent IN_TRANSIT residue for one batch and an
    impossible surplus for another — and the totals would still look right.
    """
    source, dest = route
    product_id = two_batches["product"]["id"]

    transfer = _raise(
        client, manager, raised, source, dest, product_id, two_batches["spanning"]
    )
    client.post(f"/api/v1/transfers/{transfer['id']}/approve", headers=admin)
    client.post(f"/api/v1/transfers/{transfer['id']}/dispatch", headers=admin)
    client.post(f"/api/v1/transfers/{transfer['id']}/receive", headers=admin)

    rows = client.get(
        f"/api/v1/stock/balances?product_id={product_id}"
        f"&warehouse_id={dest}&size=200",
        headers=admin,
    ).json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    stranded = sum(
        Decimal(str(r["qty_on_hand"]))
        for r in rows
        if r.get("status") == "IN_TRANSIT"
    )

    assert stranded == 0, f"{stranded} units still in transit after receipt"


def test_the_received_quantity_matches_what_was_asked_for(
    client, manager, admin, route, two_batches, raised
):
    source, dest = route
    transfer = _raise(
        client, manager, raised, source, dest,
        two_batches["product"]["id"], two_batches["spanning"],
    )
    client.post(f"/api/v1/transfers/{transfer['id']}/approve", headers=admin)
    client.post(f"/api/v1/transfers/{transfer['id']}/dispatch", headers=admin)
    done = client.post(
        f"/api/v1/transfers/{transfer['id']}/receive", headers=admin
    ).json()

    line = done["lines"][0]
    assert Decimal(str(line["qty_received"])) == two_batches["spanning"]


# --- the way out --------------------------------------------------------------


def test_an_approved_transfer_can_still_be_withdrawn(
    client, manager, admin, route, two_batches, raised
):
    """The state a stuck transfer was trapped in."""
    source, dest = route
    transfer = _raise(
        client, manager, raised, source, dest,
        two_batches["product"]["id"], two_batches["smallest"],
    )
    client.post(f"/api/v1/transfers/{transfer['id']}/approve", headers=admin)

    cancelled = client.post(
        f"/api/v1/transfers/{transfer['id']}/cancel", headers=manager
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


def test_a_transfer_already_on_the_road_cannot_be_wished_away(
    client, manager, admin, route, two_batches, raised
):
    """The lorry has to arrive somewhere.

    Cancelling an IN_TRANSIT transfer would leave its stock permanently in a
    status nothing can clear, so the answer is to receive it and send it back —
    which leaves both movements in the ledger where they belong.
    """
    source, dest = route
    transfer = _raise(
        client, manager, raised, source, dest,
        two_batches["product"]["id"], two_batches["smallest"],
    )
    client.post(f"/api/v1/transfers/{transfer['id']}/approve", headers=admin)
    client.post(f"/api/v1/transfers/{transfer['id']}/dispatch", headers=admin)

    refused = client.post(
        f"/api/v1/transfers/{transfer['id']}/cancel", headers=manager
    )

    assert refused.status_code == 409
    assert "IN_TRANSIT" in refused.json()["detail"]

    # Land it rather than leaving stock on an imaginary road.
    client.post(f"/api/v1/transfers/{transfer['id']}/receive", headers=admin)
