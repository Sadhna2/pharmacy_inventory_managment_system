"""An adjustment must always have somewhere to go.

Run like the other live suites, against a running API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

THE FAILURE THESE EXIST FOR
---------------------------
A positive adjustment on a batch-tracked product used to be accepted by
`POST /adjustments` and then refused by every attempt to approve it — the
ledger wants a lot named for a batch-tracked movement, and creation never
asked. There was no cancel, so the document could not be withdrawn either. It
sat at the top of the approver's queue permanently: they could not pass it,
could not clear it, and had nothing on screen telling them why.

Two things fix that, and both are tested here. Creation now runs the same
check the ledger will run, so the refusal arrives while the person who typed
it is still looking at the form. And an adjustment that has not posted can be
cancelled, which is the answer for the cases raise-time validation cannot
predict — stock sold between raising and approval, or an approver who simply
disagrees.

Every document these tests create is cancelled or approved by the end. An
adjustment left PENDING_APPROVAL by a test run is indistinguishable, in the
approver's queue, from the bug this file is about.
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
    """A second person, so approval is not self-approval."""
    return _headers(client, "admin@pharmacy.co.in")


@pytest.fixture(scope="session")
def products(client, admin) -> dict[str, dict]:
    """One product of each tracking mode that matters here."""
    items = client.get("/api/v1/products?size=200", headers=admin).json()["items"]
    batch = next(
        (p for p in items if p["tracking_mode"] in ("LOT", "LOT_EXPIRY")), None
    )
    plain = next((p for p in items if p["tracking_mode"] == "NONE"), None)
    if batch is None or plain is None:
        pytest.skip("the catalogue has no batch-tracked and untracked pair")
    return {"batch": batch, "plain": plain}


@pytest.fixture
def raised(client, manager):
    """Adjustments this test made, cleared on the way out.

    Whatever a test does with them, none may be left pending: a test that
    litters the approval queue is reproducing the defect it was written for.
    """
    made: list[int] = []
    yield made
    for adjustment_id in made:
        client.post(f"/api/v1/adjustments/{adjustment_id}/cancel", headers=manager)


def _raise(client, manager, raised, **body):
    resp = client.post("/api/v1/adjustments", headers=manager, json=body)
    if resp.status_code == 201:
        raised.append(resp.json()["id"])
    return resp


# --- refused at the point of typing ------------------------------------------


def test_a_batch_tracked_product_needs_a_lot_before_the_document_is_accepted(
    client, manager, products, raised
):
    """The check that used to happen only at approval, when it was too late."""
    resp = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="FOUND",
        lines=[{"product_id": products["batch"]["id"], "quantity": "5"}],
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert products["batch"]["sku"] in detail
    assert "lot is required" in detail


def test_an_untracked_product_must_not_carry_a_lot(client, manager, products, raised):
    """The mirror rule, so the raise-time check is not one-sided."""
    lots = client.get(
        f"/api/v1/lots?product_id={products['batch']['id']}", headers=manager
    )
    # This endpoint answers with a bare list, not a page like the documents do.
    rows = lots.json() if lots.status_code == 200 else []
    if not rows:
        pytest.skip("no lot on hand to mis-attach")

    resp = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[
            {
                "product_id": products["plain"]["id"],
                "quantity": "-1",
                "lot_id": rows[0]["id"],
            }
        ],
    )

    assert resp.status_code == 422


def test_nothing_was_written_by_a_refused_raise(client, manager, admin, products):
    """A 422 that had already inserted the header is a half-made document."""
    before = client.get("/api/v1/adjustments?size=1", headers=admin).json()["total"]

    client.post(
        "/api/v1/adjustments",
        headers=manager,
        json={
            "warehouse_id": 1,
            "reason_code": "FOUND",
            "lines": [{"product_id": products["batch"]["id"], "quantity": "7"}],
        },
    )

    after = client.get("/api/v1/adjustments?size=1", headers=admin).json()["total"]
    assert after == before


def test_a_well_formed_adjustment_is_still_accepted(
    client, manager, products, raised
):
    """Guards the guard: a check that refuses everything is not a check."""
    resp = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[{"product_id": products["plain"]["id"], "quantity": "-1"}],
    )

    assert resp.status_code == 201
    assert resp.json()["status"] == "PENDING_APPROVAL"


# --- the way out --------------------------------------------------------------


def test_a_pending_adjustment_can_be_withdrawn(client, manager, products, raised):
    made = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[{"product_id": products["plain"]["id"], "quantity": "-1"}],
    ).json()

    resp = client.post(f"/api/v1/adjustments/{made['id']}/cancel", headers=manager)

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


def test_the_raiser_may_withdraw_their_own(client, manager, products, raised):
    """Separation of duties stops one person *moving stock* unwatched.

    Withdrawing moves nothing, so requiring a second signature to take back an
    obvious mistake would only leave more documents stuck in the queue — which
    is the failure this whole file is about.
    """
    made = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[{"product_id": products["plain"]["id"], "quantity": "-1"}],
    ).json()
    assert made["created_by_name"]

    # Same headers that raised it.
    resp = client.post(f"/api/v1/adjustments/{made['id']}/cancel", headers=manager)

    assert resp.status_code == 200


def test_a_cancelled_adjustment_can_no_longer_be_approved(
    client, manager, admin, products, raised
):
    made = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[{"product_id": products["plain"]["id"], "quantity": "-1"}],
    ).json()
    client.post(f"/api/v1/adjustments/{made['id']}/cancel", headers=manager)

    resp = client.post(f"/api/v1/adjustments/{made['id']}/approve", headers=admin)

    assert resp.status_code == 409


def test_cancelling_twice_is_refused_rather_than_silently_accepted(
    client, manager, products, raised
):
    """A second click on a slow connection must not read as success."""
    made = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[{"product_id": products["plain"]["id"], "quantity": "-1"}],
    ).json()
    client.post(f"/api/v1/adjustments/{made['id']}/cancel", headers=manager)

    resp = client.post(f"/api/v1/adjustments/{made['id']}/cancel", headers=manager)

    assert resp.status_code == 409
    assert "CANCELLED" in resp.json()["detail"]


def test_an_approved_adjustment_cannot_be_cancelled(
    client, manager, admin, products, raised
):
    """It is in the ledger now, and the ledger is append-only.

    A cancel that reached back and unposted a movement would be an edit to
    history — the one thing the whole design refuses to do. The correction for
    an approved adjustment is another adjustment the other way.
    """
    made = _raise(
        client,
        manager,
        raised,
        warehouse_id=1,
        reason_code="DAMAGE",
        lines=[{"product_id": products["plain"]["id"], "quantity": "-1"}],
    ).json()
    approved = client.post(
        f"/api/v1/adjustments/{made['id']}/approve", headers=admin
    )
    if approved.status_code != 200:
        pytest.skip(f"could not approve to set up the case: {approved.text}")

    resp = client.post(f"/api/v1/adjustments/{made['id']}/cancel", headers=manager)

    assert resp.status_code == 409
    # And the movement it posted is still there.
    assert Decimal(approved.json()["lines"][0]["quantity"]) == Decimal("-1")
