"""Rubbish in must come back as a refusal, never as a 500.

Run like the other live suites, against a running API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

WHY THE DIFFERENCE MATTERS
--------------------------
A 500 is a promise the API makes about itself: *we are broken, this is not
your fault, quote the request id*. Spending that on a caller who simply typed
a bad filter costs twice. The caller is told nothing they can act on — the
handler deliberately withholds detail, because a 500 might be a stack trace
with a connection string in it. And whoever reads the logs finds real faults
buried under noise that was never a fault at all.

There is a second, sharper reason for the id case. Somebody walking the id
space to see what exists reads a 500 as "found one, and broke it". It is the
most interesting possible answer to give an attacker, and it was being given
for free by every `{id}` route in the system.

Nothing here writes.
"""

import os

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: One past the largest value a Postgres `integer` column holds. Every path
#: parameter here is a Python int, which has no such limit, so this reaches
#: the database and is rejected there rather than at the edge.
BEYOND_INT4 = 2_147_483_648


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        yield c


@pytest.fixture(scope="session")
def admin(client) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@pharmacy.co.in", "password": PASSWORD},
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# --- ids past the width of the column ----------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/products/{}",
        "/api/v1/purchase-orders/{}",
        "/api/v1/sales-orders/{}",
        "/api/v1/sales-orders/{}/invoice",
        "/api/v1/warehouses/{}/bins",
    ],
)
def test_an_id_too_large_for_the_column_is_a_refusal_not_a_crash(
    client, admin, path
):
    response = client.get(path.format(BEYOND_INT4), headers=admin)

    assert response.status_code != 500, response.text
    assert response.status_code in (404, 422)


def test_the_refusal_says_something_the_caller_can_act_on(client, admin):
    """"Something went wrong" is what a 500 says, and it is not true here."""
    response = client.get(f"/api/v1/products/{BEYOND_INT4}", headers=admin)

    detail = response.json()["detail"]
    assert "went wrong" not in detail.lower()
    assert "range" in detail.lower()


def test_an_id_that_fits_but_does_not_exist_is_still_a_plain_404(client, admin):
    """The new handler must not swallow the ordinary missing-row case."""
    response = client.get("/api/v1/products/2147483647", headers=admin)

    assert response.status_code == 404


# --- filters that name something that does not exist -------------------------


def test_an_unknown_tracking_mode_names_the_field_it_rejected(client, admin):
    response = client.get("/api/v1/products?tracking_mode=bogus", headers=admin)

    assert response.status_code == 422
    fields = {e["field"] for e in response.json()["errors"]}
    assert "tracking_mode" in fields


@pytest.mark.parametrize("mode", ["NONE", "LOT", "LOT_EXPIRY", "SERIAL"])
def test_every_real_tracking_mode_still_filters(client, admin, mode):
    """Guards the guard: a parameter that refuses everything is not a filter."""
    response = client.get(f"/api/v1/products?tracking_mode={mode}", headers=admin)

    assert response.status_code == 200
    for row in response.json()["items"]:
        assert row["tracking_mode"] == mode


# --- pagination past the end --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/audit",
        "/api/v1/users",
        "/api/v1/products",
        "/api/v1/purchase-orders",
        "/api/v1/sales-orders",
        "/api/v1/transfers",
        "/api/v1/adjustments",
    ],
)
def test_a_page_past_the_end_is_an_empty_page_not_an_error(client, admin, path):
    """Asking for page 99999 of a short list is a normal thing a client does."""
    response = client.get(f"{path}?page=99999&size=25", headers=admin)

    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize(("query", "field"), [("page=0", "page"), ("size=0", "size")])
def test_pagination_below_the_floor_names_the_parameter(client, admin, query, field):
    response = client.get(f"/api/v1/products?{query}", headers=admin)

    assert response.status_code == 422
    assert field in {e["field"] for e in response.json()["errors"]}
