"""Administrator settings: the registry, the guards, and the blast radius.

Run the same way as the other suites, against a live API:

    SEED_PASSWORD=... API_BASE=http://127.0.0.1:8000 .venv/bin/pytest tests/

WHAT IS ACTUALLY BEING ASSERTED
-------------------------------
A settings screen is a remote control for arithmetic that people order stock
against, so the interesting tests are not "can I save a number". They are:

  * a bad value is refused at the edge and **nothing** in the batch is written,
    because a half-applied batch leaves an admin unsure what is in effect
  * combinations that are individually legal and jointly meaningless are
    refused too — those are the ones that surface later as an empty screen
  * switching a capability off closes its API, not just its menu item
  * only an administrator can change any of it, while everyone signed in can
    still ask which capabilities are live (the navigation needs that)
  * the numbers genuinely reach the calculation — a settings screen wired to
    nothing is worse than no settings screen

Every test restores what it changed. The session-scoped `restore` fixture is
the backstop: it snapshots the overrides and the switches before anything runs
and puts them back afterwards, so a failure halfway through cannot leave the
development database with the anomaly detector switched off.
"""

import os
import subprocess
from pathlib import Path

import httpx
import pytest

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PASSWORD = os.environ.get("SEED_PASSWORD") or "ChangeMe@123"
ROOT = Path(__file__).resolve().parents[2]


def _sql(query: str) -> list[str]:
    """Read the audit log directly — it has no endpoint of its own."""
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


@pytest.fixture(scope="session", autouse=True)
def restore(client, admin):
    """Put the database back exactly as it was found, whatever the tests did."""
    before = client.get("/api/v1/settings", headers=admin).json()
    overrides = {
        t["key"]: t["value"]
        for group in before["groups"]
        for t in group["tunables"]
        if t["is_overridden"]
    }
    switches = {f["key"]: f["is_enabled"] for f in before["features"]}

    yield

    client.post("/api/v1/settings/reset", headers=admin)
    if overrides:
        client.patch("/api/v1/settings", headers=admin, json={"values": overrides})
    client.patch("/api/v1/settings", headers=admin, json={"features": switches})


@pytest.fixture
def snapshot(client, admin):
    """The current settings payload, flattened by key."""

    def read():
        body = client.get("/api/v1/settings", headers=admin).json()
        return {
            t["key"]: t for group in body["groups"] for t in group["tunables"]
        }

    return read


def _fields(resp: httpx.Response) -> dict[str, str]:
    return {e["field"]: e["message"] for e in resp.json().get("errors", [])}


# --- the registry -----------------------------------------------------------


def test_every_tunable_describes_itself(snapshot):
    """The screen renders from this, so a missing label is a broken field."""
    rows = snapshot()
    assert len(rows) >= 20
    for key, row in rows.items():
        assert row["label"] and row["help"], f"{key} has no label or help text"
        assert row["kind"] in ("bool", "int", "float", "time")
        assert row["default"] is not None
        assert "value" in row and "is_overridden" in row
        if row["kind"] in ("int", "float") and row["minimum"] is not None:
            assert row["minimum"] <= float(row["value"]) <= row["maximum"]


def test_groups_are_ordered_and_populated(client, admin):
    body = client.get("/api/v1/settings", headers=admin).json()
    keys = [g["key"] for g in body["groups"]]
    assert keys == ["business", "reorder", "forecast", "anomaly", "leadtime"]
    assert all(g["tunables"] for g in body["groups"])


def test_features_are_listed_with_their_build_state(client, admin):
    body = client.get("/api/v1/settings", headers=admin).json()
    by_key = {f["key"]: f for f in body["features"]}
    assert "features.anomaly" in by_key
    assert by_key["features.anomaly"]["is_implemented"] is True
    # The roadmap rows are the point of `is_implemented` — an honest "designed,
    # not built" beats a menu item that 500s.
    assert any(not f["is_implemented"] for f in body["features"])


# --- permission boundary ----------------------------------------------------


def test_manager_cannot_read_or_write_settings(client, manager):
    assert client.get("/api/v1/settings", headers=manager).status_code == 403
    refused = client.patch(
        "/api/v1/settings",
        headers=manager,
        json={"values": {"anomaly.z_threshold": 2.0}},
    )
    assert refused.status_code == 403
    assert client.post("/api/v1/settings/reset", headers=manager).status_code == 403


def test_anyone_signed_in_can_ask_which_features_are_live(client, manager):
    """The navigation needs this. It is not a boundary — each endpoint checks."""
    resp = client.get("/api/v1/settings/features", headers=manager)
    assert resp.status_code == 200
    assert isinstance(resp.json()["enabled"], dict)
    assert client.get("/api/v1/settings/features").status_code == 401


# --- validation -------------------------------------------------------------


def test_out_of_bounds_value_is_refused_with_a_field_error(client, admin, snapshot):
    before = snapshot()["anomaly.z_threshold"]["value"]
    resp = client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"anomaly.z_threshold": 99}},
    )
    assert resp.status_code == 422
    assert "anomaly.z_threshold" in _fields(resp)
    assert snapshot()["anomaly.z_threshold"]["value"] == before


def test_unknown_key_is_refused(client, admin):
    resp = client.patch(
        "/api/v1/settings", headers=admin, json={"values": {"anomaly.banana": 1}}
    )
    assert resp.status_code == 422
    assert "anomaly.banana" in resp.json()["detail"]


def test_a_bad_field_rejects_the_whole_batch(client, admin, snapshot):
    """Half-saved settings are worse than none — nobody knows what is in effect."""
    before = snapshot()
    resp = client.patch(
        "/api/v1/settings",
        headers=admin,
        json={
            "values": {
                "anomaly.baseline_days": 35,  # perfectly fine on its own
                "anomaly.z_threshold": 0.1,  # below the floor
            }
        },
    )
    assert resp.status_code == 422
    after = snapshot()
    assert after["anomaly.baseline_days"]["value"] == before["anomaly.baseline_days"]["value"]
    assert after["anomaly.z_threshold"]["value"] == before["anomaly.z_threshold"]["value"]


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"business.opens": "22:00", "business.closes": "07:00"}, "business.closes"),
        (
            {"forecast.backtest_days": 90, "forecast.lookback_days": 60},
            "forecast.backtest_days",
        ),
        (
            {"anomaly.min_value": 9000, "anomaly.material_value": 100},
            "anomaly.material_value",
        ),
        (
            {"reorder.service_standard": 3.0, "reorder.service_critical": 1.2},
            "reorder.service_critical",
        ),
    ],
)
def test_incoherent_combinations_are_refused(client, admin, values, field):
    """Each value is legal alone; together they describe nothing."""
    resp = client.patch("/api/v1/settings", headers=admin, json={"values": values})
    assert resp.status_code == 422
    assert field in _fields(resp)


def test_a_time_must_be_a_time(client, admin):
    resp = client.patch(
        "/api/v1/settings", headers=admin, json={"values": {"business.opens": "half six"}}
    )
    assert resp.status_code == 422
    assert "business.opens" in _fields(resp)


# --- write path -------------------------------------------------------------


def test_saving_marks_the_setting_as_changed(client, admin, snapshot):
    resp = client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"anomaly.baseline_days": 35}},
    )
    assert resp.status_code == 200
    row = snapshot()["anomaly.baseline_days"]
    assert row["value"] == 35
    assert row["is_overridden"] is True


def test_saving_the_default_back_removes_the_override(client, admin, snapshot):
    """Otherwise "never touched" and "set to today's default" quietly diverge."""
    default = snapshot()["anomaly.baseline_days"]["default"]
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"anomaly.baseline_days": 35}},
    )
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"anomaly.baseline_days": default}},
    )
    row = snapshot()["anomaly.baseline_days"]
    assert row["value"] == default
    assert row["is_overridden"] is False


def test_reset_clears_numbers_but_leaves_switches_alone(client, admin, snapshot):
    """Resetting the maths must not silently turn a capability back on."""
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"features": {"features.leadtime": False}},
    )
    client.patch(
        "/api/v1/settings", headers=admin, json={"values": {"anomaly.min_units": 9}}
    )

    body = client.post("/api/v1/settings/reset", headers=admin).json()
    assert not any(
        t["is_overridden"] for g in body["groups"] for t in g["tunables"]
    )
    switches = {f["key"]: f["is_enabled"] for f in body["features"]}
    assert switches["features.leadtime"] is False

    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"features": {"features.leadtime": True}},
    )


# --- feature switches -------------------------------------------------------


def test_an_unbuilt_capability_cannot_be_switched_on(client, admin):
    body = client.get("/api/v1/settings", headers=admin).json()
    unbuilt = next(f for f in body["features"] if not f["is_implemented"])
    resp = client.patch(
        "/api/v1/settings", headers=admin, json={"features": {unbuilt["key"]: True}}
    )
    assert resp.status_code == 422
    assert "not built" in resp.json()["detail"].lower()


def test_switching_a_capability_off_closes_its_api(client, admin, manager):
    """Hiding the menu item is presentation. This is the part that matters."""
    assert client.get("/api/v1/ai/anomalies", headers=manager).status_code == 200

    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"features": {"features.anomaly": False}},
    )
    try:
        blocked = client.get("/api/v1/ai/anomalies", headers=manager)
        assert blocked.status_code == 404
        assert "administrator" in blocked.json()["detail"].lower()

        # ...and only that one.
        assert client.get("/api/v1/ai/lead-times", headers=manager).status_code == 200

        live = client.get("/api/v1/settings/features", headers=manager).json()
        assert live["enabled"]["features.anomaly"] is False
    finally:
        client.patch(
            "/api/v1/settings",
            headers=admin,
            json={"features": {"features.anomaly": True}},
        )

    assert client.get("/api/v1/ai/anomalies", headers=manager).status_code == 200


# --- the numbers actually reach the calculation -----------------------------


def test_sensitivity_changes_what_the_detector_reports(client, admin, manager):
    """A settings screen wired to nothing is worse than no settings screen."""

    def findings() -> int:
        resp = client.get(
            "/api/v1/ai/anomalies", headers=manager, params={"lookback_days": 90}
        )
        resp.raise_for_status()
        return resp.json()["summary"]["total"]

    client.post("/api/v1/settings/reset", headers=admin)
    strict = findings()

    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"anomaly.z_threshold": 2.5}},
    )
    loose = findings()

    client.post("/api/v1/settings/reset", headers=admin)
    assert findings() == strict
    if strict == 0:
        pytest.skip("no anomaly history seeded to compare against")
    assert loose > strict, "lowering the threshold found no extra days"


def test_forecast_horizon_default_follows_the_setting(client, admin, manager):
    """A literal default in the route signature would outrank the setting."""
    client.patch(
        "/api/v1/settings", headers=admin, json={"values": {"forecast.horizon_days": 45}}
    )
    try:
        body = client.get("/api/v1/ai/forecast", headers=manager).json()
        assert body["horizon_days"] == 45
        if not body["forecasts"]:
            pytest.skip("no sales history seeded")
        assert len(body["forecasts"][0]["daily"]) == 45

        # Replenishment plans over the same horizon...
        plan = client.get("/api/v1/ai/reorder", headers=manager).json()
        assert plan["horizon_days"] == 45

        # ...and an explicit request still wins over the setting.
        asked = client.get(
            "/api/v1/ai/forecast", headers=manager, params={"horizon_days": 14}
        ).json()
        assert asked["horizon_days"] == 14
    finally:
        client.post("/api/v1/settings/reset", headers=admin)


def test_lead_time_window_default_follows_the_setting(client, admin, manager):
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"leadtime.lookback_days": 365}},
    )
    try:
        body = client.get("/api/v1/ai/lead-times", headers=manager).json()
        assert body["lookback_days"] == 365
    finally:
        client.post("/api/v1/settings/reset", headers=admin)


# --- audit ------------------------------------------------------------------


def test_every_change_is_audited_with_before_and_after(client, admin):
    """"It started recommending twice as much last Tuesday" has to be answerable."""
    client.post("/api/v1/settings/reset", headers=admin)
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"values": {"anomaly.min_units": 7}},
    )
    rows = _sql(
        "SELECT before_json::text, after_json::text FROM audit_logs "
        "WHERE action = 'settings.update' ORDER BY id DESC LIMIT 1"
    )
    assert rows, "no audit row written for the settings change"
    assert "anomaly.min_units" in rows[0]
    assert '"7"' in rows[0] or ": 7" in rows[0]

    client.post("/api/v1/settings/reset", headers=admin)
    assert _sql(
        "SELECT 1 FROM audit_logs WHERE action = 'settings.reset' "
        "ORDER BY id DESC LIMIT 1"
    )


def test_feature_toggles_are_audited(client, admin):
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"features": {"features.leadtime": False}},
    )
    client.patch(
        "/api/v1/settings",
        headers=admin,
        json={"features": {"features.leadtime": True}},
    )
    rows = _sql(
        "SELECT after_json::text FROM audit_logs "
        "WHERE action = 'settings.feature_toggle' ORDER BY id DESC LIMIT 1"
    )
    assert rows and "features.leadtime" in rows[0]
    assert "true" in rows[0].lower()
