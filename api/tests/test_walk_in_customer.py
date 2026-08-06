"""Naming the person at the counter, from inside the sales order.

Run like the other suites, against a live API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

Three things are being defended here, and each of them charges the wrong tax
or refuses a legal sale if it slips.

The state is filled in by the server, not the caller, because it decides
CGST + SGST against IGST and the caller is a form that may not know which
branch it is standing in. The GSTIN, when there is one, has to belong to that
state — GST registers per state, so a Gujarat registration on a Maharashtra
supply is a contradiction the invoice cannot carry. And a blank GSTIN has to
be accepted outright: a supply to an unregistered person is an ordinary B2C
sale, and refusing it would refuse the most common sale in a pharmacy.
"""

import os
import uuid

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: Two real GSTINs — the check digit is computed, not invented, because the
#: endpoint verifies it and a made-up number would fail for the wrong reason.
MH_GSTIN = "27AABCC1234D1ZB"
GJ_GSTIN = "24AABCG9999F1Z5"


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


@pytest.fixture(scope="session")
def made(client, manager) -> list[int]:
    """Every walk-in this file creates, retired again when it finishes.

    Without this the suite leaves five new customers in the picker on every
    run, and the demo database grows a column of `Counter 4f3a91c2` rows that
    nobody raised. Retiring rather than deleting because that is the only
    verb the API offers, and it is the right one: `is_active=false` takes them
    out of the picker while leaving anything that referred to them resolvable.
    """
    ids: list[int] = []
    yield ids
    for customer_id in ids:
        client.delete(f"/api/v1/customers/{customer_id}", headers=manager)


def _name() -> str:
    return f"Counter {uuid.uuid4().hex[:8]}"


def walk_in(client, headers, made, **fields) -> httpx.Response:
    """Create one, and put it on the list to be tidied away afterwards."""
    response = client.post(
        "/api/v1/customers/walk-in",
        headers=headers,
        json={"name": _name(), **fields},
    )
    if response.status_code == 201:
        made.append(response.json()["id"])
    return response


def test_a_walk_in_needs_only_a_name(client, manager, made):
    created = walk_in(client, manager, made)
    assert created.status_code == 201, created.text
    body = created.json()

    assert body["gstin"] is None, "an unregistered buyer is the ordinary case"
    assert body["state_code"], "the server has to supply a state, not leave it blank"
    assert body["is_institutional"] is False
    assert body["credit_limit"] in ("0", "0.00"), "a walk-in settles at the counter"


def test_the_code_is_allocated_rather_than_asked_for(client, manager, made):
    first = walk_in(client, manager, made).json()
    second = walk_in(client, manager, made).json()

    assert first["code"].startswith("WI-")
    assert first["code"] != second["code"], (
        "two counters serving at once must not collide on a code"
    )


def test_a_registration_from_another_state_is_refused(client, manager, made):
    """The branch is in Maharashtra, so a Gujarat GSTIN cannot be the buyer's.

    Not pedantry: the state on the record is what picks the tax split, so
    accepting this pair would put a number on the invoice that disagrees with
    the tax printed beside it.
    """
    refused = walk_in(client, manager, made, gstin=GJ_GSTIN)
    assert refused.status_code == 422
    assert "state" in refused.json()["detail"].lower()


def test_a_visitor_from_another_state_is_allowed_to_say_so(client, manager, made):
    """The same GSTIN, with the state named, is a perfectly ordinary sale."""
    created = walk_in(client, manager, made, gstin=GJ_GSTIN, state_code="GJ")
    assert created.status_code == 201, created.text
    assert created.json()["state_code"] == "GJ"


def test_a_mistyped_gstin_is_caught_by_its_own_check_digit(client, manager, made):
    refused = walk_in(client, manager, made, gstin=MH_GSTIN[:-1] + "9")
    assert refused.status_code == 422
    assert "check digit" in refused.json()["detail"]


def test_whoever_cannot_raise_an_order_cannot_invent_a_buyer(client, staff, made):
    """Staff have no `so.create`, so this is not theirs to do.

    The guard is `so.create` rather than `master.manage` on purpose — naming
    the buyer is part of ringing up the sale — but that is a reason to widen
    it to order-takers, not to everyone.
    """
    refused = walk_in(client, staff, made)
    assert refused.status_code == 403


def test_the_new_buyer_can_be_ordered_for_immediately(client, manager, made):
    """The point of the whole feature: the sale continues without a detour."""
    buyer = walk_in(client, manager, made).json()

    products = client.get("/api/v1/products?size=200", headers=manager).json()["items"]
    para = next(p for p in products if p["sku"] == "PAR-650")

    plan = client.post(
        "/api/v1/sales-orders/plan",
        headers=manager,
        json={
            "customer_id": buyer["id"],
            "lines": [{"product_id": para["id"], "qty_ordered": 1}],
        },
    )
    assert plan.status_code == 200, plan.text
    orders = plan.json()["orders"]
    assert orders, "a brand-new buyer with no history must still be plannable"
