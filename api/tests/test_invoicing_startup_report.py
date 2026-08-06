"""The server says at boot whether it can issue a tax invoice at all.

This exists because of a bug that survived every deploy the project has had.
`SELLER_LEGAL_NAME` and `SELLER_GSTIN` were in the server's `.env` and were
never listed in the compose service's `environment:`, so they stopped at the
container boundary. The API had no seller, and the only symptom was Print
invoice answering 409 — the same 409 it gives when a *branch* has no
registration recorded. Two unrelated problems, one message, and nothing
anywhere that could tell them apart.

A misconfiguration only discoverable by a user clicking a button is one that
gets discovered by a user clicking a button, in front of whoever they were
demonstrating to. So the state is announced once per boot, where a deploy log
shows it.

No database and no server: the function is called directly.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import settings
from app.main import _report_invoicing_configuration


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "seller_legal_name", "Sadhna Pharma Pvt Ltd")
    monkeypatch.setattr(settings, "seller_gstin", "27AABCS9876P1ZA")


def test_a_configured_server_says_so(configured, caplog):
    with caplog.at_level(logging.INFO):
        _report_invoicing_configuration()

    assert "tax invoicing enabled" in caplog.text
    # The registration itself, so a deploy log answers "which GSTIN is this
    # box printing?" without anyone opening a shell on it.
    assert "27AABCS9876P1ZA" in caplog.text


def test_an_unconfigured_server_warns_and_names_what_is_missing(
    configured, monkeypatch, caplog
):
    """The whole point. "Something is wrong" would not have shortened this by
    a single day; "SELLER_GSTIN is missing" would have ended it in a minute."""
    monkeypatch.setattr(settings, "seller_gstin", "")

    with caplog.at_level(logging.WARNING):
        _report_invoicing_configuration()

    assert "DISABLED" in caplog.text
    assert "SELLER_GSTIN" in caplog.text
    # And not the one that is present, or the message sends someone looking in
    # the wrong place.
    assert "SELLER_LEGAL_NAME" not in caplog.text


def test_both_missing_are_both_named(configured, monkeypatch, caplog):
    monkeypatch.setattr(settings, "seller_legal_name", "")
    monkeypatch.setattr(settings, "seller_gstin", "")

    with caplog.at_level(logging.WARNING):
        _report_invoicing_configuration()

    assert "SELLER_LEGAL_NAME" in caplog.text
    assert "SELLER_GSTIN" in caplog.text


def test_the_warning_does_not_stop_the_server(configured, monkeypatch, caplog):
    """Degraded, not broken. Every other screen works without a seller GSTIN,
    and refusing to boot over it would take down stock control to protect a
    document nobody had asked for yet."""
    monkeypatch.setattr(settings, "seller_gstin", "")

    with caplog.at_level(logging.WARNING):
        _report_invoicing_configuration()  # returns rather than raising
