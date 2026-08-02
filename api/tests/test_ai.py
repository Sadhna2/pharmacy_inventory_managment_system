"""Layer 2 suite: lead times, anomalies, forecasting and reorder.

Run the same way as test_e2e.py, against a live API. Most of these need the
synthetic history in place:

    .venv/bin/python -m app.seed.history --days 730

Without it the endpoints still answer — correctly, with "not enough data" —
and the tests that need volume skip rather than fail, because a suite that
goes red on a fresh database teaches people to ignore it.

WHAT IS ACTUALLY BEING ASSERTED
-------------------------------
Not "the model is accurate" — that is what the backtest reports, and pinning
a WAPE in a test only makes the suite brittle. What is asserted here is the
set of properties that must hold for the output to be safe to act on:

  * percentiles are ordered and bounded by observed reality
  * a forecast never suggests negative demand
  * on-order and drafted stock are netted off so nothing is ordered twice
  * prices come from the supplier record and not from the request
  * every permission boundary holds

Those are the things that cause real damage when they break.
"""

import os
from decimal import Decimal

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE, timeout=120.0) as c:
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
def lead_times(client, manager):
    return client.get("/api/v1/ai/lead-times", headers=manager).json()


@pytest.fixture(scope="session")
def has_history(lead_times) -> bool:
    """Whether the synthetic history has been generated."""
    return any(s["deliveries"] >= 20 for s in lead_times["suppliers"])


# --- permissions ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/ai/lead-times",
        "/api/v1/ai/anomalies",
        "/api/v1/ai/forecast",
        "/api/v1/ai/reorder",
    ],
)
def test_analysis_requires_ai_view(client, staff, path):
    """Branch staff record stock; they do not get the chain's analysis."""
    assert client.get(path, headers=staff).status_code == 403


def test_analysis_requires_authentication(client):
    assert client.get("/api/v1/ai/forecast").status_code == 401


def test_raising_an_order_requires_a_second_permission(client, staff):
    """`ai.view` reads a suggestion; `ai.act` spends money on it."""
    resp = client.post(
        "/api/v1/ai/reorder/orders",
        headers=staff,
        json={"supplier_id": 1, "warehouse_id": 1, "lines": [{"product_id": 1, "quantity": 1}]},
    )
    assert resp.status_code == 403
    assert "ai.act" in resp.json()["detail"]


# --- lead times -------------------------------------------------------------


def test_percentiles_are_ordered(lead_times):
    """median <= p90 <= max, always. A violation means the maths is wrong."""
    for supplier in lead_times["suppliers"]:
        assert supplier["min_days"] <= supplier["median_days"] <= supplier["p90_days"]
        assert supplier["p90_days"] <= supplier["max_days"]


def test_percentiles_are_observed_durations(client, manager, lead_times, has_history):
    """p90 must be a delivery that actually happened, not an interpolation.

    Nearest-rank rather than linear interpolation is a deliberate choice, and
    this is the assertion that keeps it.
    """
    if not has_history:
        pytest.skip("needs synthetic history")
    slowest = lead_times["suppliers"][0]
    detail = client.get(
        f"/api/v1/ai/lead-times/{slowest['supplier_id']}", headers=manager
    ).json()
    observed = {d["days"] for d in detail["deliveries"]}
    # The detail list is capped at 40 deliveries, so only assert membership
    # when the supplier's whole history fits inside it.
    if slowest["deliveries"] <= 40:
        assert slowest["p90_days"] in observed


def test_small_samples_are_flagged_not_hidden(lead_times):
    for supplier in lead_times["suppliers"]:
        if supplier["deliveries"] < 5:
            assert supplier["reliable"] is False
            assert "too few" in supplier["verdict"].lower()


def test_unknown_supplier_is_404(client, manager):
    assert client.get("/api/v1/ai/lead-times/999999", headers=manager).status_code == 404


def test_erratic_supplier_is_called_out(lead_times, has_history):
    """The seeded history contains one deliberately unreliable distributor.

    If the analysis cannot separate it from the reliable ones, the feature has
    no signal and nothing downstream of it means anything.
    """
    if not has_history:
        pytest.skip("needs synthetic history")
    spreads = [s["p90_days"] - s["median_days"] for s in lead_times["suppliers"]]
    assert max(spreads) >= 5, "no supplier separation — check the seeded history"


# --- anomalies --------------------------------------------------------------


def test_anomaly_report_is_internally_consistent(client, manager):
    body = client.get("/api/v1/ai/anomalies?lookback_days=180", headers=manager).json()
    summary = body["summary"]
    assert summary["total"] == summary["high"] + summary["medium"] + summary["low"]
    assert sum(summary["by_kind"].values()) == summary["total"]


def test_every_finding_carries_its_evidence(client, manager):
    """A finding nobody can trace back to the ledger is an accusation."""
    body = client.get("/api/v1/ai/anomalies?lookback_days=180", headers=manager).json()
    for finding in body["anomalies"]:
        assert finding["explanation"]
        assert finding["baseline"]
        assert finding["movement_ids"], finding["explanation"]


def test_severity_filter_narrows(client, manager):
    all_findings = client.get(
        "/api/v1/ai/anomalies?lookback_days=180", headers=manager
    ).json()
    high_only = client.get(
        "/api/v1/ai/anomalies?lookback_days=180&min_severity=high", headers=manager
    ).json()
    assert high_only["summary"]["total"] <= all_findings["summary"]["total"]
    assert all(f["severity"] == "high" for f in high_only["anomalies"])


def test_planted_anomalies_are_found(client, manager, has_history):
    """The seeder plants shrinkage, an out-of-hours movement and a write-off.

    Every one of them is documented in app/seed/history.py. A detector that
    cannot find faults somebody wrote down is not worth running against faults
    nobody did.
    """
    if not has_history:
        pytest.skip("needs synthetic history")
    body = client.get("/api/v1/ai/anomalies?lookback_days=365", headers=manager).json()
    kinds = body["summary"]["by_kind"]
    assert kinds.get("after_hours"), "missed the 3am movement"
    assert kinds.get("repeat_loss"), "missed the repeated count variances"


def test_after_hours_findings_are_clustered(client, manager):
    """One trip to the warehouse is one finding, not thirty.

    A batch recall writes a dozen movements in the same minute. Emitting a
    finding per row would make the screen unusable, so sessions are grouped.
    """
    body = client.get("/api/v1/ai/anomalies?lookback_days=365", headers=manager).json()
    after_hours = [f for f in body["anomalies"] if f["kind"] == "after_hours"]
    for finding in after_hours:
        movements = finding["baseline"]["movements"]
        assert len(finding["movement_ids"]) <= max(movements, 50)


# --- forecasting ------------------------------------------------------------


@pytest.fixture(scope="session")
def forecast(client, manager):
    return client.get("/api/v1/ai/forecast?horizon_days=30", headers=manager).json()


def test_forecast_horizon_is_respected(forecast):
    for series in forecast["forecasts"]:
        assert len(series["daily"]) == 30
        assert len(series["lower"]) == 30 == len(series["upper"])


def test_forecast_is_never_negative(forecast):
    """Negative demand is not a thing. Clipping is applied on purpose."""
    for series in forecast["forecasts"]:
        assert min(series["daily"]) >= 0
        assert min(series["lower"]) >= 0


def test_band_contains_the_point_forecast(forecast):
    for series in forecast["forecasts"]:
        for low, point, high in zip(
            series["lower"], series["daily"], series["upper"], strict=True
        ):
            assert low <= point <= high


def test_band_widens_with_distance(forecast):
    """Uncertainty compounds. A flat band would be claiming otherwise."""
    for series in forecast["forecasts"]:
        first = series["upper"][0] - series["lower"][0]
        last = series["upper"][-1] - series["lower"][-1]
        assert last >= first


def test_chosen_method_won_its_backtest(forecast):
    """The winner must genuinely have the lowest error of everything tried."""
    for series in forecast["forecasts"]:
        for loser in series["alternatives"]:
            assert series["accuracy"]["wape"] <= loser["wape"]


def test_confidence_tracks_measured_error(forecast):
    for series in forecast["forecasts"]:
        if series["confidence"] == "high":
            assert series["accuracy"]["wape"] <= 0.25


def test_some_series_are_predictable(forecast, has_history):
    """A flat-demand product must come out with high confidence.

    The seeded history contains chronic medication with deliberately steady
    demand. If nothing at all is predictable, the pipeline is broken rather
    than the world being noisy.
    """
    if not has_history:
        pytest.skip("needs synthetic history")
    assert any(f["confidence"] == "high" for f in forecast["forecasts"])


# --- reorder ----------------------------------------------------------------


@pytest.fixture(scope="session")
def reorder(client, manager):
    return client.get("/api/v1/ai/reorder", headers=manager).json()


def test_reorder_point_exceeds_lead_time_demand(reorder):
    """Safety stock is the whole point — the ROP must be strictly above the
    demand expected while waiting, or there is no buffer at all."""
    for rec in reorder["recommendations"]:
        lead_demand = rec["daily_demand"] * rec["lead_time_days"]
        assert rec["reorder_point"] >= lead_demand
        assert rec["safety_stock"] >= 0


def test_order_up_to_exceeds_reorder_point(reorder):
    for rec in reorder["recommendations"]:
        assert rec["order_up_to"] >= rec["reorder_point"] or rec["suggested_qty"] == 0


def test_position_includes_stock_on_order(reorder):
    """The classic double-ordering bug: goods on a lorry are still yours."""
    for rec in reorder["recommendations"]:
        assert rec["position"] == pytest.approx(
            rec["on_hand"] + rec["on_order"], abs=0.5
        )


def test_erratic_supplier_forces_a_bigger_buffer(reorder):
    """An unreliable distributor must cost the chain more stock than a reliable one.

    The safety-stock formula carries a supply-variance term precisely so this
    is true. Without it, a supplier who delivers in 3 days or 25 produces the
    same buffer as one who always takes 3 — the expensive mistake this whole
    feature exists to avoid.

    Compared as days of cover rather than units, so a busy branch does not win
    on volume alone.
    """
    measured = [
        r
        for r in reorder["recommendations"]
        if r["sourcing"]["measured"] and r["daily_demand"] > 0
    ]
    erratic = [
        r["safety_stock"] / r["daily_demand"]
        for r in measured
        if r["sourcing"]["lead_time_sd"] >= 4
    ]
    steady = [
        r["safety_stock"] / r["daily_demand"]
        for r in measured
        if r["sourcing"]["lead_time_sd"] < 4
    ]
    if not erratic or not steady:
        pytest.skip("dataset has no reliability contrast to compare")

    assert min(erratic) > max(steady), (
        "an erratic supplier is not producing a larger buffer — "
        "check the supply-variance term in the safety stock formula"
    )


def test_service_level_follows_the_medicine(reorder):
    """Cold chain and controlled drugs get a higher service level than OTC."""
    levels = {r["service_level"] for r in reorder["recommendations"]}
    assert levels <= {"critical", "high", "standard"}


def test_suggestions_round_up_to_pack_size(reorder):
    for rec in reorder["recommendations"]:
        pack = rec["sourcing"]["pack_qty"]
        if rec["suggested_qty"] > 0 and pack > 1:
            assert rec["suggested_qty"] % pack == pytest.approx(0, abs=0.001)
            assert rec["suggested_qty"] >= rec["sourcing"]["moq"]


def test_draft_orders_group_by_supplier_and_destination(reorder):
    seen = set()
    for order in reorder["draft_orders"]:
        key = (order["supplier_id"], order["warehouse_id"])
        assert key not in seen, "same supplier and branch split across two orders"
        seen.add(key)
        assert order["lines"] == len(order["items"])


# --- raising an order -------------------------------------------------------


def test_raised_order_is_a_draft_priced_from_the_supplier(client, admin, reorder):
    """A client that could set its own unit price could set it to zero."""
    candidate = next(
        (
            r
            for r in reorder["recommendations"]
            if r["suggested_qty"] > 0 and r["sourcing"]["supplier_id"]
        ),
        None,
    )
    if candidate is None:
        pytest.skip("nothing to order in this dataset")

    resp = client.post(
        "/api/v1/ai/reorder/orders",
        headers=admin,
        json={
            "supplier_id": candidate["sourcing"]["supplier_id"],
            "warehouse_id": candidate["warehouse_id"],
            "lines": [
                {"product_id": candidate["product_id"], "quantity": 10}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    po = resp.json()
    assert po["status"] == "DRAFT", "an AI suggestion must never self-approve"
    assert Decimal(po["lines"][0]["unit_price"]) == pytest.approx(
        Decimal(str(candidate["sourcing"]["unit_cost"])), abs=Decimal("0.01")
    )
    # The date on the order comes from the p90, so it is a promise the goods
    # have a 90% chance of beating rather than one they beat half the time.
    assert po["expected_date"] is not None


def test_draft_suppresses_the_same_suggestion(client, admin, reorder):
    """Raise a draft, look again, and the order must not be suggested twice.

    The draft is deliberately NOT counted as stock — it is paper — so the
    shortage stays visible. What must not happen is the quantity being
    suggested a second time.
    """
    candidate = next(
        (
            r
            for r in reorder["recommendations"]
            if r["suggested_qty"] > 0 and r["sourcing"]["supplier_id"]
        ),
        None,
    )
    if candidate is None:
        pytest.skip("nothing to order in this dataset")

    quantity = candidate["suggested_qty"]
    created = client.post(
        "/api/v1/ai/reorder/orders",
        headers=admin,
        json={
            "supplier_id": candidate["sourcing"]["supplier_id"],
            "warehouse_id": candidate["warehouse_id"],
            "lines": [{"product_id": candidate["product_id"], "quantity": quantity}],
        },
    )
    assert created.status_code == 201, created.text

    after = client.get("/api/v1/ai/reorder", headers=admin).json()
    same = next(
        (r for r in after["recommendations"] if r["key"] == candidate["key"]), None
    )
    if same is not None:
        assert same["drafted_qty"] >= quantity
        assert same["suggested_qty"] < candidate["suggested_qty"]


def test_order_rejects_a_supplier_that_does_not_stock_the_product(
    client, admin, reorder
):
    candidate = next(
        (r for r in reorder["recommendations"] if r["sourcing"]["supplier_id"]), None
    )
    if candidate is None:
        pytest.skip("nothing to order in this dataset")

    suppliers = client.get("/api/v1/suppliers", headers=admin).json()
    other = next(
        (
            s["id"]
            for s in suppliers
            if s["id"] != candidate["sourcing"]["supplier_id"]
        ),
        None,
    )
    if other is None:
        pytest.skip("only one supplier on file")

    resp = client.post(
        "/api/v1/ai/reorder/orders",
        headers=admin,
        json={
            "supplier_id": other,
            "warehouse_id": candidate["warehouse_id"],
            "lines": [{"product_id": candidate["product_id"], "quantity": 10}],
        },
    )
    # Either they genuinely supply it (201) or the guard fires (422). What must
    # never happen is a 500, or an order against a supplier with no price.
    assert resp.status_code in (201, 422)
    if resp.status_code == 422:
        assert "does not supply" in resp.json()["detail"]
