"""Documents say *who* raised and approved them, not just a user id.

Purchase orders and adjustments both refuse to let one person raise and
approve the same document. That control only does its job if the approver can
see whose work they are signing off — and the API used to answer `created_by:
2`, which is not an answer a person can act on.

These run against the live API, like the rest of the end-to-end suite.
"""

from __future__ import annotations

import os

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE, timeout=60.0) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client: httpx.Client) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@pharmacy.co.in", "password": PASSWORD},
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _items(client: httpx.Client, path: str, headers: dict[str, str]) -> list[dict]:
    resp = client.get(f"{path}?size=25", headers=headers)
    resp.raise_for_status()
    body = resp.json()
    return body["items"] if isinstance(body, dict) else body


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/v1/purchase-orders", "created_by"),
        ("/api/v1/adjustments", "created_by"),
        ("/api/v1/transfers", "created_by"),
        ("/api/v1/goods-receipts", "received_by"),
    ],
)
def test_the_actor_is_named_not_just_numbered(client, admin, path, field):
    rows = _items(client, path, admin)
    if not rows:
        pytest.skip(f"no rows at {path} to check")
    for row in rows:
        assert row[field] is not None, f"{path} lost {field}"
        name = row[f"{field}_name"]
        assert name, f"{path} row {row.get('id')} has {field} but no name"
        assert not name.isdigit(), "a name, not the id in a string"


def test_an_approver_is_named_once_a_document_is_approved(client, admin):
    """The second signature is as much a fact as the first."""
    approved = [
        row
        for row in _items(client, "/api/v1/purchase-orders", admin)
        if row["approved_by"] is not None
    ]
    if not approved:
        pytest.skip("no approved purchase orders in this dataset")
    for row in approved:
        assert row["approved_by_name"], f"{row['po_number']} approved by nobody named"


def test_an_unapproved_document_names_no_approver(client, admin):
    """The optimistic direction: absence must stay absent.

    A blank here is the screen's cue that the order is still waiting, so a
    stray name would read as already signed off.
    """
    for row in _items(client, "/api/v1/purchase-orders", admin):
        if row["approved_by"] is None:
            assert row["approved_by_name"] is None, (
                f"{row['po_number']} has no approver but names one"
            )


def test_the_names_survive_the_single_document_route(client, admin):
    """The list batches its lookups and the detail route does not.

    Two code paths, so two chances to return an id and no name.
    """
    rows = _items(client, "/api/v1/purchase-orders", admin)
    if not rows:
        pytest.skip("no purchase orders")
    resp = client.get(f"/api/v1/purchase-orders/{rows[0]['id']}", headers=admin)
    resp.raise_for_status()
    one = resp.json()
    assert one["created_by_name"] == rows[0]["created_by_name"]
    assert one["approved_by_name"] == rows[0]["approved_by_name"]
