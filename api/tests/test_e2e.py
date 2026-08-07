"""End-to-end integration suite against a running API + seeded database.

    ./scripts/db.sh start
    cd api && .venv/bin/alembic upgrade head && .venv/bin/python -m app.seed.bootstrap
    .venv/bin/uvicorn app.main:app --port 8000 &
    .venv/bin/pytest tests/test_e2e.py -v

Covers the guarantees that actually matter: append-only ledger, RBAC,
cost scoping, FEFO with shelf-life floor, GST split, separation of duties,
IN_TRANSIT visibility, batch recall, and ledger/projection agreement.
"""

import os
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"
ROOT = Path(__file__).resolve().parents[2]


def _sql(query: str) -> list[str]:
    """Read straight from the database for things the API deliberately hides.

    The audit log has no endpoint yet, so assertions about what was recorded
    have to go around the API rather than through it.
    """
    out = subprocess.run(
        [str(ROOT / "scripts" / "db.sh"), "psql", "-qtAc", query],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.strip().splitlines() if line]


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
def admin(client):
    return {"Authorization": f"Bearer {_token(client, 'admin@pharmacy.co.in')}"}


@pytest.fixture(scope="session")
def manager(client):
    return {"Authorization": f"Bearer {_token(client, 'manager@pharmacy.co.in')}"}


@pytest.fixture(scope="session")
def staff(client):
    return {"Authorization": f"Bearer {_token(client, 'staff@pharmacy.co.in')}"}


@pytest.fixture(scope="session")
def ids(client, manager):
    """Look up the seeded entities every test needs."""
    products = client.get("/api/v1/products?size=200", headers=manager).json()["items"]
    warehouses = client.get("/api/v1/warehouses", headers=manager).json()
    customers = client.get(
        "/api/v1/customers?is_institutional=true", headers=manager
    ).json()
    suppliers = client.get("/api/v1/suppliers", headers=manager).json()

    by_sku = {p["sku"]: p for p in products}
    return {
        "para": by_sku["PAR-650"]["id"],
        "insulin": by_sku["INS-GLA"]["id"],
        "syringe": by_sku["SYR-5ML"]["id"],
        "central": next(w["id"] for w in warehouses if w["is_central"]),
        "branch": next(w["id"] for w in warehouses if not w["is_central"]),
        "customer": customers[0]["id"],
        "supplier": suppliers[0]["id"],
    }


def qty_at(client, headers, product_id, warehouse_id, status="AVAILABLE") -> Decimal:
    rows = client.get(
        f"/api/v1/stock/balances?product_id={product_id}"
        f"&warehouse_id={warehouse_id}&status={status}&size=200",
        headers=headers,
    ).json()["items"]
    return sum((Decimal(r["qty_on_hand"]) for r in rows), Decimal("0"))


# --- health & auth ----------------------------------------------------------


def test_health(client):
    assert client.get("/health/ready").json()["status"] == "ready"


def test_login_returns_role_and_permissions(client):
    body = client.post(
        "/api/v1/auth/login",
        json={"email": "staff@pharmacy.co.in", "password": PASSWORD},
    ).json()
    assert body["user"]["role"] == "STAFF"
    assert len(body["user"]["permissions"]) > 0
    assert "stock.view_cost" not in body["user"]["permissions"]


def test_wrong_password_rejected(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "staff@pharmacy.co.in", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_unauthenticated_rejected(client):
    assert client.get("/api/v1/stock/summary").status_code == 401


def test_refresh_token_rotates(client):
    with httpx.Client(base_url=BASE, timeout=30.0) as fresh:
        fresh.post(
            "/api/v1/auth/login",
            json={"email": "manager@pharmacy.co.in", "password": PASSWORD},
        )
        first = fresh.post("/api/v1/auth/refresh")
        assert first.status_code == 200
        # Second refresh must also work — the cookie was rotated, not consumed.
        assert fresh.post("/api/v1/auth/refresh").status_code == 200


# --- RBAC -------------------------------------------------------------------


def test_staff_cannot_create_product(client, staff):
    resp = client.post(
        "/api/v1/products",
        headers=staff,
        json={"sku": "DENIED-1", "name": "Denied", "uom_id": 1},
    )
    assert resp.status_code == 403
    assert "product.manage" in resp.json()["detail"]


def test_manager_can_create_product(client, manager):
    import uuid

    resp = client.post(
        "/api/v1/products",
        headers=manager,
        json={"sku": f"PROBE-{uuid.uuid4().hex[:8]}", "name": "Probe", "uom_id": 1},
    )
    assert resp.status_code == 201


def test_cost_visibility_is_a_permission(client, staff, manager):
    """Staff record stock all day but must never see margins.

    Only some movement types carry a cost at all — a status change (recall, QC
    release) moves quantity between buckets and has none, and neither does a
    transfer, which moves stock the firm already owns. So the window is one
    type, and a type that always has a cost: a purchase receipt is the moment
    money was paid, and the price is the point of the record.

    Not "the newest hundred rows of any type". This suite writes real
    movements into a database it shares with every other suite, and almost all
    of them are transfers and issues with no cost — so that window filled up
    with costless rows as the database aged, and the test began failing on a
    permission that had not changed. What it asserts has to be true of the
    seed, not of whatever ran in the last ten minutes.

    Manager first, so a seed holding no purchase receipts at all fails loudly
    here rather than passing the staff half vacuously.
    """
    q = "/api/v1/stock/movements?movement_type=PURCHASE_RECEIPT&size=100"
    mgr_rows = client.get(q, headers=manager).json()["items"]
    staff_rows = client.get(q, headers=staff).json()["items"]

    assert mgr_rows, "no purchase receipts to compare the two roles over"
    assert all(r["unit_cost"] is not None for r in mgr_rows)
    assert all(r["unit_cost"] is None for r in staff_rows)


def test_staff_scoped_to_own_branch(client, staff):
    rows = client.get("/api/v1/stock/balances?size=200", headers=staff).json()["items"]
    assert len({r["warehouse_id"] for r in rows}) == 1


# --- ledger integrity -------------------------------------------------------


def test_over_issue_is_blocked(client, manager, ids):
    lots = client.get(
        f"/api/v1/lots?product_id={ids['para']}", headers=manager
    ).json()
    resp = client.post(
        "/api/v1/stock/movements",
        headers=manager,
        json={
            "product_id": ids["para"],
            "warehouse_id": ids["central"],
            "quantity": -999999,
            "movement_type": "SALE_ISSUE",
            "lot_id": lots[0]["id"],
        },
    )
    assert resp.status_code == 409
    assert "Not enough stock" in resp.json()["detail"]


def test_cold_chain_bin_rule(client, manager, ids):
    """Insulin cannot be placed on an ambient shelf."""
    bins = client.get(
        f"/api/v1/warehouses/{ids['central']}/bins", headers=manager
    ).json()
    ambient = next(b for b in bins if not b["is_cold_chain"] and not b["is_quarantine"])
    lots = client.get(
        f"/api/v1/lots?product_id={ids['insulin']}", headers=manager
    ).json()

    resp = client.post(
        "/api/v1/stock/movements",
        headers=manager,
        json={
            "product_id": ids["insulin"],
            "warehouse_id": ids["central"],
            "quantity": 1,
            "movement_type": "ADJUSTMENT",
            "bin_id": ambient["id"],
            "lot_id": lots[0]["id"],
        },
    )
    assert resp.status_code == 422
    assert "cold-chain" in resp.json()["detail"]


def test_batch_tracked_product_requires_lot(client, manager, ids):
    resp = client.post(
        "/api/v1/stock/movements",
        headers=manager,
        json={
            "product_id": ids["para"],
            "warehouse_id": ids["central"],
            "quantity": 5,
            "movement_type": "ADJUSTMENT",
        },
    )
    assert resp.status_code == 422
    assert "batch-tracked" in resp.json()["detail"]


def test_movement_reversal_leaves_original(client, manager, ids):
    """Corrections are reversing entries — the original never disappears."""
    before = qty_at(client, manager, ids["syringe"], ids["central"])

    created = client.post(
        "/api/v1/stock/movements",
        headers=manager,
        json={
            "product_id": ids["syringe"],
            "warehouse_id": ids["central"],
            "quantity": 7,
            "movement_type": "ADJUSTMENT",
        },
    )
    assert created.status_code == 201
    mid = created.json()["id"]
    assert qty_at(client, manager, ids["syringe"], ids["central"]) == before + 7

    reversal = client.post(
        f"/api/v1/stock/movements/{mid}/reverse",
        headers=manager,
        json={"reason": "keyed in error"},
    )
    assert reversal.status_code == 201
    assert qty_at(client, manager, ids["syringe"], ids["central"]) == before

    # Both rows still exist in the ledger.
    movements = client.get(
        f"/api/v1/stock/movements?product_id={ids['syringe']}&size=200",
        headers=manager,
    ).json()["items"]
    assert any(m["id"] == mid for m in movements)
    assert any(m["reference_type"] == "REVERSAL" for m in movements)

    # The original now points at its correction, so a list can show "Reversed"
    # instead of offering a button that would be refused.
    original = next(m for m in movements if m["id"] == mid)
    assert original["reversed_by_id"] == reversal.json()["id"]

    # Reversing twice would overshoot the correction into a fresh error.
    again = client.post(
        f"/api/v1/stock/movements/{mid}/reverse",
        headers=manager,
        json={"reason": "second attempt"},
    )
    assert again.status_code == 409
    assert qty_at(client, manager, ids["syringe"], ids["central"]) == before

    # Nor can a reversal itself be reversed.
    back = client.post(
        f"/api/v1/stock/movements/{reversal.json()['id']}/reverse",
        headers=manager,
        json={"reason": "undo the undo"},
    )
    assert back.status_code == 409
    assert qty_at(client, manager, ids["syringe"], ids["central"]) == before


# --- purchasing -------------------------------------------------------------


@pytest.fixture(scope="session")
def approved_po(client, manager, admin, ids):
    po = client.post(
        "/api/v1/purchase-orders",
        headers=manager,
        json={
            "supplier_id": ids["supplier"],
            "warehouse_id": ids["central"],
            "lines": [
                {"product_id": ids["para"], "qty_ordered": 100, "unit_price": 21}
            ],
        },
    ).json()
    # Approved by admin, not the manager who raised it.
    client.post(f"/api/v1/purchase-orders/{po['id']}/approve", headers=admin)
    return po


def approved_po_for(client, manager, admin, ids, quantity: int) -> int:
    """A fresh approved order, raised by the manager and approved by the admin.

    A helper rather than a fixture because these tests want more than one order
    each, at quantities they choose.
    """
    po = client.post(
        "/api/v1/purchase-orders",
        headers=manager,
        json={
            "supplier_id": ids["supplier"],
            "warehouse_id": ids["central"],
            "lines": [
                {
                    "product_id": ids["para"],
                    "qty_ordered": quantity,
                    "unit_price": 21,
                }
            ],
        },
    ).json()
    client.post(f"/api/v1/purchase-orders/{po['id']}/approve", headers=admin)
    return po["id"]


def test_po_computes_gst_intrastate(client, manager, ids):
    po = client.post(
        "/api/v1/purchase-orders",
        headers=manager,
        json={
            "supplier_id": ids["supplier"],
            "warehouse_id": ids["central"],
            "lines": [
                {"product_id": ids["para"], "qty_ordered": 100, "unit_price": 21}
            ],
        },
    ).json()

    line = po["lines"][0]
    assert Decimal(line["taxable_value"]) == Decimal("2100.00")
    # 12% GST split evenly for an intra-state supply.
    assert Decimal(line["cgst_amount"]) == Decimal("126.00")
    assert Decimal(line["sgst_amount"]) == Decimal("126.00")
    assert Decimal(line["igst_amount"]) == Decimal("0.00")
    assert po["is_interstate"] is False


def test_creator_cannot_approve_own_po(client, manager, ids):
    po = client.post(
        "/api/v1/purchase-orders",
        headers=manager,
        json={
            "supplier_id": ids["supplier"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["para"], "qty_ordered": 10, "unit_price": 21}],
        },
    ).json()
    resp = client.post(f"/api/v1/purchase-orders/{po['id']}/approve", headers=manager)
    assert resp.status_code == 422
    assert "other than its creator" in resp.json()["detail"]


def test_goods_receipt_increases_stock(client, manager, ids, approved_po):
    before = qty_at(client, manager, ids["para"], ids["central"])

    resp = client.post(
        "/api/v1/goods-receipts",
        headers=manager,
        json={
            "warehouse_id": ids["central"],
            "purchase_order_id": approved_po["id"],
            "supplier_invoice_no": "INV-9931",
            "lines": [
                {
                    "product_id": ids["para"],
                    "quantity": 100,
                    "unit_cost": 21,
                    "lot_code": "PARA-GRN-1",
                    "expiry_date": "2028-06-30",
                }
            ],
        },
    )
    assert resp.status_code == 201
    assert qty_at(client, manager, ids["para"], ids["central"]) == before + 100

    po = client.get(
        f"/api/v1/purchase-orders/{approved_po['id']}", headers=manager
    ).json()
    assert po["status"] == "RECEIVED"


def test_receipt_refuses_a_warehouse_the_order_was_not_for(
    client, manager, admin, ids
):
    """An order names where it is being delivered; receiving it elsewhere is a
    mis-picked order, not a decision.

    Left unchecked this books stock to a branch that never saw it *and* closes
    an order that was never delivered — two wrong balances from one mis-click.
    """
    elsewhere = next(
        w["id"]
        for w in client.get("/api/v1/warehouses", headers=manager).json()
        if w["id"] != ids["central"]
    )
    resp = client.post(
        "/api/v1/goods-receipts",
        headers=manager,
        json={
            "warehouse_id": elsewhere,
            "purchase_order_id": approved_po_for(client, manager, admin, ids, 10),
            "lines": [
                {
                    "product_id": ids["para"],
                    "quantity": 10,
                    "unit_cost": 21,
                    "lot_code": f"WH-{uuid.uuid4().hex[:6]}",
                    "expiry_date": "2028-06-30",
                }
            ],
        },
    )
    assert resp.status_code == 422
    assert "delivery to" in resp.json()["detail"]


def test_partial_receipt_leaves_the_rest_outstanding(client, manager, admin, ids):
    """Distributors short you, so a receipt settles part of a line, not all.

    The order has to stay open with the arithmetic intact — quantity received
    against quantity ordered — because that difference is the only record that
    anything is still owed.
    """
    po_id = approved_po_for(client, manager, admin, ids, 100)

    def receive(quantity: int) -> int:
        return client.post(
            "/api/v1/goods-receipts",
            headers=manager,
            json={
                "warehouse_id": ids["central"],
                "purchase_order_id": po_id,
                "supplier_invoice_no": f"INV-{uuid.uuid4().hex[:6]}",
                "lines": [
                    {
                        "product_id": ids["para"],
                        "quantity": quantity,
                        "unit_cost": 21,
                        "lot_code": f"PART-{uuid.uuid4().hex[:6]}",
                        "expiry_date": "2028-06-30",
                    }
                ],
            },
        ).status_code

    def order() -> dict:
        return client.get(
            f"/api/v1/purchase-orders/{po_id}", headers=manager
        ).json()

    assert receive(40) == 201
    after_first = order()
    assert after_first["status"] == "PARTIALLY_RECEIVED"
    assert Decimal(after_first["lines"][0]["qty_received"]) == Decimal(40)
    assert Decimal(after_first["lines"][0]["qty_ordered"]) == Decimal(100)

    assert receive(60) == 201
    after_second = order()
    assert after_second["status"] == "RECEIVED"
    assert Decimal(after_second["lines"][0]["qty_received"]) == Decimal(100)


def test_po_line_reports_tracking_mode_for_the_receiving_screen(
    client, manager, admin, ids
):
    """The receiving form pre-fills its rows from the order, and has to know
    which of them will demand a batch number before it will save."""
    po_id = approved_po_for(client, manager, admin, ids, 5)
    po = client.get(f"/api/v1/purchase-orders/{po_id}", headers=manager).json()
    assert po["lines"][0]["tracking_mode"] == "LOT_EXPIRY"


def test_each_batch_keeps_its_own_printed_mrp(client, manager, ids):
    """Two batches, two printed prices, both on the shelf at once.

    MRP is a legal ceiling per pack, so a price rise on the newer carton must
    not reprice the older stock still sitting in the warehouse.
    """
    old_price, new_price = "31.50", "36.00"
    old_batch = f"MRP-OLD-{uuid.uuid4().hex[:6]}"
    new_batch = f"MRP-NEW-{uuid.uuid4().hex[:6]}"

    for batch, price in ((old_batch, old_price), (new_batch, new_price)):
        resp = client.post(
            "/api/v1/goods-receipts",
            headers=manager,
            json={
                "warehouse_id": ids["central"],
                "lines": [
                    {
                        "product_id": ids["para"],
                        "quantity": 40,
                        "unit_cost": 21,
                        "lot_code": batch,
                        "expiry_date": "2028-06-30",
                        "mrp": price,
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text

    rows = client.get(
        f"/api/v1/stock/balances?product_id={ids['para']}"
        f"&warehouse_id={ids['central']}&size=200",
        headers=manager,
    ).json()["items"]
    prices = {r["lot_code"]: Decimal(r["mrp"]) for r in rows if r["mrp"] is not None}

    assert prices[old_batch] == Decimal(old_price)
    assert prices[new_batch] == Decimal(new_price)

    # The product-level MRP tracks the latest carton — it is only the default
    # offered for the next receipt, never what the old batch sells at.
    product = client.get(f"/api/v1/products/{ids['para']}", headers=manager).json()
    assert Decimal(product["mrp"]) == Decimal(new_price)


def test_expired_batch_cannot_be_received(client, manager, ids):
    resp = client.post(
        "/api/v1/goods-receipts",
        headers=manager,
        json={
            "warehouse_id": ids["central"],
            "lines": [
                {
                    "product_id": ids["para"],
                    "quantity": 5,
                    "unit_cost": 21,
                    "lot_code": "EXPIRED-1",
                    "expiry_date": "2020-01-01",
                }
            ],
        },
    )
    assert resp.status_code == 422
    assert "expired" in resp.json()["detail"].lower()


# --- sales & FEFO -----------------------------------------------------------


def test_fefo_respects_shelf_life_floor(client, manager, ids):
    """The ~25-day batch is below the 30-day floor, so FEFO must skip it."""
    so = client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": ids["customer"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["para"], "qty_ordered": 50}],
        },
    ).json()

    allocations = client.post(
        f"/api/v1/sales-orders/{so['id']}/allocate", headers=manager
    ).json()

    assert allocations, "expected at least one allocation"
    # PAR-650-B2 is the near-expiry batch seeded at ~25 days out.
    assert all(a["lot_code"] != "PAR-650-B2" for a in allocations)

    client.post(f"/api/v1/sales-orders/{so['id']}/cancel", headers=manager)


def test_ship_reduces_stock_and_records_lot(client, manager, ids):
    before = qty_at(client, manager, ids["para"], ids["central"])

    so = client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": ids["customer"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["para"], "qty_ordered": 30}],
        },
    ).json()
    client.post(f"/api/v1/sales-orders/{so['id']}/allocate", headers=manager)
    shipment = client.post(
        f"/api/v1/sales-orders/{so['id']}/ship", headers=manager
    ).json()

    assert shipment["shipment_number"].startswith("SHP-")
    # Lot recorded on the shipment line — this is what recall tracing walks back.
    assert shipment["lines"][0]["lot_id"] is not None
    assert qty_at(client, manager, ids["para"], ids["central"]) == before - 30


def test_cancel_releases_reservation(client, manager, ids):
    so = client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": ids["customer"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["para"], "qty_ordered": 10}],
        },
    ).json()
    client.post(f"/api/v1/sales-orders/{so['id']}/allocate", headers=manager)

    reserved = client.get(
        f"/api/v1/stock/balances?product_id={ids['para']}"
        f"&warehouse_id={ids['central']}&size=200",
        headers=manager,
    ).json()["items"]
    assert sum(Decimal(r["qty_reserved"]) for r in reserved) >= 10

    client.post(f"/api/v1/sales-orders/{so['id']}/cancel", headers=manager)

    after = client.get(
        f"/api/v1/stock/balances?product_id={ids['para']}"
        f"&warehouse_id={ids['central']}&size=200",
        headers=manager,
    ).json()["items"]
    assert sum(Decimal(r["qty_reserved"]) for r in after) == 0


# --- transfers --------------------------------------------------------------


def test_transfer_shows_in_transit_then_lands(client, manager, ids):
    """Stock on a truck must stay visible, not vanish.

    Measured as a delta, not as an absolute. This asserted `== 20` and passed
    for months, because no seeded transfer happened to be mid-flight for this
    product at this branch. The seeded history generates two years of trading
    up to *today*, so it re-rolls every day — and on a day when it left 968
    syringes on the road, a test about a transfer this test made itself failed
    on stock it had nothing to do with. What the test is actually about is the
    twenty units it dispatched: that they appear at the destination while in
    flight, and convert to available on arrival.
    """
    on_the_road = qty_at(
        client, manager, ids["syringe"], ids["branch"], status="IN_TRANSIT"
    )

    transfer = client.post(
        "/api/v1/transfers",
        headers=manager,
        json={
            "from_warehouse_id": ids["central"],
            "to_warehouse_id": ids["branch"],
            "lines": [{"product_id": ids["syringe"], "quantity": 20}],
        },
    ).json()
    tid = transfer["id"]

    client.post(f"/api/v1/transfers/{tid}/approve", headers=manager)
    client.post(f"/api/v1/transfers/{tid}/dispatch", headers=manager)

    in_transit = qty_at(
        client, manager, ids["syringe"], ids["branch"], status="IN_TRANSIT"
    )
    assert in_transit == on_the_road + 20, (
        "dispatched stock must be visible as IN_TRANSIT"
    )

    available_before = qty_at(client, manager, ids["syringe"], ids["branch"])
    received = client.post(f"/api/v1/transfers/{tid}/receive", headers=manager)
    assert received.status_code == 200
    assert received.json()["status"] == "COMPLETED"

    assert (
        qty_at(client, manager, ids["syringe"], ids["branch"], "IN_TRANSIT")
        == on_the_road
    ), "arriving must clear this transfer's in-transit rows and no others"
    assert qty_at(client, manager, ids["syringe"], ids["branch"]) == (
        available_before + 20
    )


# --- adjustments ------------------------------------------------------------


def test_adjustment_needs_second_approver(client, manager, admin, ids):
    adjustment = client.post(
        "/api/v1/adjustments",
        headers=manager,
        json={
            "warehouse_id": ids["central"],
            "reason_code": "CYCLE_COUNT",
            "lines": [{"product_id": ids["syringe"], "quantity": -3}],
        },
    ).json()

    same_person = client.post(
        f"/api/v1/adjustments/{adjustment['id']}/approve", headers=manager
    )
    assert same_person.status_code == 422

    before = qty_at(client, manager, ids["syringe"], ids["central"])
    other = client.post(
        f"/api/v1/adjustments/{adjustment['id']}/approve", headers=admin
    )
    assert other.status_code == 200
    # Nothing posts to the ledger until approval.
    assert qty_at(client, manager, ids["syringe"], ids["central"]) == before - 3


# --- recall -----------------------------------------------------------------


def test_batch_recall_freezes_and_traces(client, manager, ids):
    # Ship a batch to a customer first, so there is a downstream trail.
    so = client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": ids["customer"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["para"], "qty_ordered": 15}],
        },
    ).json()
    client.post(f"/api/v1/sales-orders/{so['id']}/allocate", headers=manager)
    shipment = client.post(
        f"/api/v1/sales-orders/{so['id']}/ship", headers=manager
    ).json()
    recalled_lot = shipment["lines"][0]["lot_id"]

    # A lot may only be under one open recall at a time. On a database that has
    # already been exercised (a demo run, a previous pass of this suite) FEFO
    # can hand back a lot that is still open, so clear it before re-testing.
    for existing in client.get("/api/v1/recalls", headers=manager).json():
        if existing["lot_id"] == recalled_lot and existing["status"] != "CLOSED":
            client.post(f"/api/v1/recalls/{existing['id']}/close", headers=manager)

    impact = client.post(
        "/api/v1/recalls",
        headers=manager,
        json={
            "lot_id": recalled_lot,
            "reason": "Contamination detected in QC",
            "regulator_ref": "CDSCO-2026-114",
        },
    )
    assert impact.status_code == 201
    body = impact.json()

    assert Decimal(body["total_quarantined"]) > 0, "stock should be frozen"
    assert body["locations"], "at least one location affected"
    assert body["customers"], "downstream customer must be traced"

    # Recalled stock must no longer be allocatable.
    so2 = client.post(
        "/api/v1/sales-orders",
        headers=manager,
        json={
            "customer_id": ids["customer"],
            "warehouse_id": ids["central"],
            "lines": [{"product_id": ids["para"], "qty_ordered": 1}],
        },
    ).json()
    allocations = client.post(
        f"/api/v1/sales-orders/{so2['id']}/allocate", headers=manager
    ).json()
    assert all(a["lot_id"] != recalled_lot for a in allocations), (
        "recalled batch leaked into a new allocation"
    )
    client.post(f"/api/v1/sales-orders/{so2['id']}/cancel", headers=manager)


# --- the invariant that proves the whole design -----------------------------


def test_rebuild_balances_matches_ledger():
    """THE test: recompute the projection from the ledger and assert zero drift.

    If this passes after every operation above, the balance table has never
    disagreed with the append-only ledger it is derived from.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [str(root / "scripts" / "db.sh"), "psql", "-qtAc", "SELECT rebuild_balances();"],
        check=True,
        capture_output=True,
    )
    drift = subprocess.run(
        [
            str(root / "scripts" / "db.sh"),
            "psql",
            "-qtAc",
            """
            SELECT COUNT(*) FROM (
              SELECT product_id, warehouse_id, COALESCE(bin_id,0) b,
                     COALESCE(lot_id,0) l, status, SUM(quantity) q
              FROM stock_movements GROUP BY 1,2,3,4,5 HAVING SUM(quantity) <> 0
            ) led
            FULL OUTER JOIN (
              SELECT product_id, warehouse_id, COALESCE(bin_id,0) b,
                     COALESCE(lot_id,0) l, status, qty_on_hand q
              FROM stock_balances WHERE qty_on_hand <> 0
            ) bal USING (product_id, warehouse_id, b, l, status)
            WHERE led.q IS DISTINCT FROM bal.q;
            """,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert drift.stdout.strip() == "0", (
        f"ledger and balance projection disagree on {drift.stdout.strip()} rows"
    )


# --- master data ------------------------------------------------------------


def test_supplier_can_be_edited_and_code_is_immutable(client, manager, ids):
    """A distributor changing its GSTIN is routine; changing its code is not."""
    supplier_id = ids["supplier"]
    original = client.get("/api/v1/suppliers", headers=manager).json()
    before = next(s for s in original if s["id"] == supplier_id)

    updated = client.patch(
        f"/api/v1/suppliers/{supplier_id}",
        headers=manager,
        json={"gstin": "27ZZZZZ9999Z1Z5", "payment_terms_days": 60, "code": "HACKED"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["gstin"] == "27ZZZZZ9999Z1Z5"
    assert body["payment_terms_days"] == 60
    # `code` is not in the update schema, so it is ignored rather than applied.
    assert body["code"] == before["code"]

    # Untouched fields survive a partial update.
    assert body["name"] == before["name"]
    assert body["state_code"] == before["state_code"]


def test_retiring_a_supplier_hides_it_without_deleting(client, manager, ids):
    supplier_id = ids["supplier"]
    assert client.delete(
        f"/api/v1/suppliers/{supplier_id}", headers=manager
    ).status_code == 200

    active = client.get("/api/v1/suppliers?is_active=true", headers=manager).json()
    assert all(s["id"] != supplier_id for s in active)

    # Still there, still readable — retired, not deleted.
    everything = client.get("/api/v1/suppliers", headers=manager).json()
    retired = next(s for s in everything if s["id"] == supplier_id)
    assert retired["is_active"] is False

    restored = client.patch(
        f"/api/v1/suppliers/{supplier_id}", headers=manager, json={"is_active": True}
    )
    assert restored.json()["is_active"] is True


def test_warehouse_holding_stock_cannot_be_retired(client, manager, ids):
    """Retiring a location with stock in it would strand the stock."""
    refused = client.delete(f"/api/v1/warehouses/{ids['central']}", headers=manager)
    assert refused.status_code == 409
    assert "still holds" in refused.json()["detail"]

    still_active = client.get("/api/v1/warehouses", headers=manager).json()
    assert next(w for w in still_active if w["id"] == ids["central"])["is_active"]


def test_empty_warehouse_can_be_retired(client, manager):
    created = client.post(
        "/api/v1/warehouses",
        headers=manager,
        json={
            "code": f"TMP-{uuid.uuid4().hex[:6].upper()}",
            "name": "Temporary Depot",
            "state_code": "MH",
        },
    )
    assert created.status_code == 201
    assert client.delete(
        f"/api/v1/warehouses/{created.json()['id']}", headers=manager
    ).status_code == 200


def test_staff_cannot_edit_master_data(client, staff, ids):
    refused = client.patch(
        f"/api/v1/suppliers/{ids['supplier']}", headers=staff, json={"name": "Nope"}
    )
    assert refused.status_code == 403


def test_master_edits_are_audited(client, manager, ids):
    """The before/after pair is what a per-record history screen reads back."""
    client.patch(
        f"/api/v1/customers/{ids['customer']}",
        headers=manager,
        json={"phone": "+91 22 1111 2222"},
    )
    rows = _sql(
        "SELECT before_json->>'phone', after_json->>'phone' FROM audit_logs "
        "WHERE action = 'customer.update' ORDER BY id DESC LIMIT 1"
    )
    assert rows, "no audit row written for the customer edit"
    assert rows[0].endswith("+91 22 1111 2222")
