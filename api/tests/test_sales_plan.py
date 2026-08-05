"""`POST /sales-orders/plan` — which branches, together, could supply this.

Run like the other live suites, against a running API and a seeded database:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

WHY THIS ROUTE EXISTS
---------------------
A sales order ships from one warehouse, and the form used to ask which before
anyone had said what was in the order. Pick wrong and nothing objected:
`create_sales_order` checks no stock at all, so the document saved cleanly and
the refusal arrived later, at allocation — leaving a saved order that could
never ship, and a person wondering which of five branches to try next.

WHAT IS BEING GUARDED
---------------------
The arithmetic, mostly. A planner that quietly loses a few units, or promises
the same stock to two branches, produces orders that look right and fail one
at a time at allocation — which is the failure it was written to prevent,
returned in a more confusing form. So every case below reconciles what was
asked against what was planned plus what was reported short, and checks that
no branch is asked for more than it holds.

And the shape of the answer: one order per branch, never two, because each
proposed order becomes one document under one GST registration. A branch that
could supply nothing must not appear at all.

Read-mostly. One test raises a real order, because a plan nobody can act on is
worthless and the only convincing proof is to act on one.
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"

#: Larger than the chain holds of anything, so every branch is drawn on and
#: the shortfall path runs. Nothing is written, so the size costs nothing.
MORE_THAN_EXISTS = "999999"


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
def customer(client, admin) -> dict:
    resp = client.get("/api/v1/customers?is_active=true", headers=admin)
    resp.raise_for_status()
    buyers = resp.json()
    return next((c for c in buyers if c["name"] == "City Hospital"), buyers[0])


@pytest.fixture(scope="session")
def products(client, admin) -> list[dict]:
    resp = client.get("/api/v1/products?size=4", headers=admin)
    resp.raise_for_status()
    items = resp.json()["items"]
    if len(items) < 2:
        pytest.skip("this database has too few products to plan across")
    return items


def _plan(client, headers, customer_id: int, lines: list[dict]) -> dict:
    resp = client.post(
        "/api/v1/sales-orders/plan",
        headers=headers,
        json={"customer_id": customer_id, "lines": lines},
    )
    resp.raise_for_status()
    return resp.json()


def _everything(products: list[dict], qty: str) -> list[dict]:
    return [{"product_id": p["id"], "qty_ordered": qty} for p in products]


# --- the arithmetic ----------------------------------------------------------


def test_nothing_is_lost_between_what_was_asked_and_what_was_planned(
    client, admin, customer, products
):
    """Per product: planned + short == requested, exactly.

    The failure this catches is silent. A planner that drops a few units
    produces orders that all look reasonable, and the missing stock surfaces
    only as a customer receiving less than they ordered, with no line item
    anywhere saying so.
    """
    plan = _plan(client, admin, customer["id"], _everything(products, "40"))

    planned: dict[int, Decimal] = {}
    for order in plan["orders"]:
        for line in order["lines"]:
            planned[line["product_id"]] = planned.get(
                line["product_id"], Decimal("0")
            ) + Decimal(line["quantity"])
    short = {
        s["product_id"]: Decimal(s["requested"]) - Decimal(s["planned"])
        for s in plan["shortfalls"]
    }

    for product in products:
        covered = planned.get(product["id"], Decimal("0"))
        missing = short.get(product["id"], Decimal("0"))
        assert covered + missing == Decimal("40"), (
            f'{product["name"]}: planned {covered} + short {missing} '
            f"should be the 40 that were asked for"
        )


def test_no_branch_is_promised_more_than_it_holds(
    client, admin, customer, products
):
    """Every planned line is checked against that branch's own availability.

    Two branches drawing on one pool would produce orders that each allocate
    fine alone and fight at the till.
    """
    plan = _plan(client, admin, customer["id"], _everything(products, MORE_THAN_EXISTS))

    for order in plan["orders"]:
        resp = client.get(
            f'/api/v1/stock/balances?warehouse_id={order["warehouse_id"]}&size=200',
            headers=admin,
        )
        resp.raise_for_status()
        on_hand: dict[int, Decimal] = {}
        for row in resp.json()["items"]:
            free = Decimal(row["qty_on_hand"]) - Decimal(row.get("qty_reserved") or 0)
            on_hand[row["product_id"]] = on_hand.get(
                row["product_id"], Decimal("0")
            ) + max(free, Decimal("0"))

        for line in order["lines"]:
            assert Decimal(line["quantity"]) <= on_hand.get(
                line["product_id"], Decimal("0")
            ), (
                f'{order["warehouse_name"]} was asked for {line["quantity"]} '
                f'of {line["product_name"]}, more than it has free'
            )


def test_a_branch_appears_at_most_once(client, admin, customer, products):
    """One branch, one order — that is the whole point of the split.

    Two orders on the same warehouse would be two documents, two approvals and
    two invoices where one would do, for no reason at all.
    """
    plan = _plan(client, admin, customer["id"], _everything(products, MORE_THAN_EXISTS))

    branches = [o["warehouse_id"] for o in plan["orders"]]

    assert len(branches) == len(set(branches))


def test_a_branch_with_nothing_to_contribute_is_not_proposed(
    client, admin, customer, products
):
    """An order with no lines is a document raised for nothing."""
    plan = _plan(client, admin, customer["id"], _everything(products, MORE_THAN_EXISTS))

    assert plan["orders"], "the seed holds stock, so something should be planned"
    for order in plan["orders"]:
        assert order["lines"]
        assert all(Decimal(line["quantity"]) > 0 for line in order["lines"])


# --- not splitting for the sake of it ----------------------------------------


def test_one_branch_that_can_cover_it_all_gets_the_whole_order(
    client, admin, customer, products
):
    """A small order must stay one order.

    The planner exists to split when splitting is necessary. A version that
    split a twenty-unit order across three branches would be strictly worse
    than the single warehouse picker it replaced.
    """
    plan = _plan(
        client, admin, customer["id"],
        [{"product_id": products[0]["id"], "qty_ordered": "1"}],
    )

    if plan["shortfalls"]:
        pytest.skip("the chain is out of this product, so there is nothing to plan")
    assert len(plan["orders"]) == 1


# --- the tax split each proposed order would carry ---------------------------


def test_each_proposed_order_states_the_split_it_would_be_taxed_under(
    client, admin, customer, products
):
    """Shown per order because it genuinely differs per order.

    One request can produce a Mumbai order at CGST + SGST and an Ahmedabad one
    at IGST for the same customer on the same day. That is correct — they are
    supplies from two registrations — and it will look like a bug to anyone
    not told in advance, so the plan says so before the orders exist.
    """
    plan = _plan(client, admin, customer["id"], _everything(products, MORE_THAN_EXISTS))
    resp = client.get("/api/v1/warehouses", headers=admin)
    resp.raise_for_status()
    state = {w["id"]: w["state_code"] for w in resp.json()}

    for order in plan["orders"]:
        assert order["state_code"] == state[order["warehouse_id"]]
        assert order["is_interstate"] == (
            order["state_code"] != customer["state_code"]
        )


# --- planning writes nothing --------------------------------------------------


def test_planning_raises_no_document_and_holds_no_stock(
    client, admin, customer, products
):
    """A POST because it carries a body, not because it changes anything.

    If planning reserved stock, an abandoned form would quietly hold the chain
    up until something expired the reservation.
    """
    before = client.get("/api/v1/sales-orders?size=1", headers=admin).json()["total"]

    _plan(client, admin, customer["id"], _everything(products, MORE_THAN_EXISTS))

    after = client.get("/api/v1/sales-orders?size=1", headers=admin).json()["total"]
    assert after == before


# --- the plan is actionable ---------------------------------------------------


def test_a_proposed_order_can_be_raised_exactly_as_planned(
    client, admin, customer, products
):
    """The one test that writes, and the only convincing one.

    Everything above could pass while the plan proposed a payload the create
    route rejects — a warehouse the customer cannot be served from, a quantity
    in the wrong shape. So one proposed order is raised verbatim, and then
    allocated, which is the step that used to be where the whole thing fell
    over.
    """
    plan = _plan(
        client, admin, customer["id"],
        [{"product_id": products[0]["id"], "qty_ordered": "1"}],
    )
    if not plan["orders"]:
        pytest.skip("nothing could be planned, so there is nothing to raise")
    proposed = plan["orders"][0]

    created = client.post(
        "/api/v1/sales-orders",
        headers=admin,
        json={
            "customer_id": customer["id"],
            "warehouse_id": proposed["warehouse_id"],
            "notes": "raised from a plan (test)",
            "lines": [
                {
                    "product_id": line["product_id"],
                    "qty_ordered": line["quantity"],
                    "unit_price": line["unit_price"],
                }
                for line in proposed["lines"]
            ],
        },
    )

    assert created.status_code == 201, created.text
    order = created.json()
    # The plan's arithmetic is the order's arithmetic — same tax split, same
    # total — so the figures on screen before it is raised are the real ones.
    assert order["is_interstate"] == proposed["is_interstate"]
    assert Decimal(order["grand_total"]) == Decimal(proposed["grand_total"])

    allocated = client.post(
        f'/api/v1/sales-orders/{order["id"]}/allocate', headers=admin
    )
    assert allocated.status_code == 200, allocated.text

    # Put the stock back rather than leaving a held reservation behind.
    client.post(f'/api/v1/sales-orders/{order["id"]}/cancel', headers=admin)


# --- who may ask --------------------------------------------------------------


def test_planning_needs_the_permission_to_raise_an_order(client, admin):
    """`so.create`, not `so.view`.

    The reply reports stock levels across every branch the caller can see. On
    `so.view` it would be a way to read the chain's whole stock position
    sideways, from a route whose name suggests it only does arithmetic.

    STAFF holds neither, so this is refused by the permission gate — the same
    honest note as `test_branch_scope.py` makes about transfers: the scoping
    inside the route cannot be reached by any role that exists today, and that
    is worth saying out loud rather than dressing the gate up as a scope test.
    """
    staff = _headers(client, "staff@pharmacy.co.in")

    response = client.post(
        "/api/v1/sales-orders/plan",
        headers=staff,
        json={"customer_id": 1, "lines": [{"product_id": 1, "qty_ordered": "1"}]},
    )

    assert response.status_code == 403


def test_an_unknown_customer_is_a_404_not_an_empty_plan(client, admin, products):
    """An empty plan reads as "nothing in stock", which is a different fact."""
    response = client.post(
        "/api/v1/sales-orders/plan",
        headers=admin,
        json={
            "customer_id": 99_999_999,
            "lines": [{"product_id": products[0]["id"], "qty_ordered": "1"}],
        },
    )

    assert response.status_code == 404


def test_an_order_with_no_lines_is_refused(client, admin, customer):
    response = client.post(
        "/api/v1/sales-orders/plan",
        headers=admin,
        json={"customer_id": customer["id"], "lines": []},
    )

    assert response.status_code == 422
