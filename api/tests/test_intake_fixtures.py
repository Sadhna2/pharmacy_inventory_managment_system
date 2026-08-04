"""The recorded readings that let the demo run without a network.

`api/fixtures/intake/` holds four invoice images and what the model read from
each. A fixture is keyed by the SHA-256 of its image, so the two are bound
together — and so they can come apart. Re-render the benchmark images without
re-recording and every digest changes: the endpoint stops finding a fixture and
starts answering 502, which is a thing you want to discover in CI rather than
in front of an audience.

These tests are the tripwire for that. They need no database, no key and no
network, which is what CI has.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.ai.intake.service import _fixture_for
from app.ai.intake.validate import gstin_is_valid, validate_invoice

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "intake"
IMAGES = FIXTURES / "images"


def images() -> list[Path]:
    return sorted(IMAGES.glob("*.png"))


def test_the_demo_images_are_present():
    """Six: five read perfectly, and one that is not — see the README."""
    assert len(images()) == 6


@pytest.mark.parametrize("image", images(), ids=lambda p: p.stem)
def test_every_image_still_finds_its_reading(image: Path):
    """The one failure this file exists to catch.

    `_fixture_for` is the function the endpoint calls, so this exercises the
    real lookup rather than reimplementing the digest and agreeing with itself.
    """
    doc = _fixture_for(image.read_bytes(), FIXTURES)
    assert doc["lines"], f"{image.name} replays an invoice with no lines"


@pytest.mark.parametrize("image", images(), ids=lambda p: p.stem)
def test_no_reading_is_orphaned(image: Path):
    """Every shipped image has a fixture named after it, and vice versa."""
    digest = hashlib.sha256(image.read_bytes()).hexdigest()[:16]
    assert (FIXTURES / f"{digest}.json").is_file()


def test_there_are_no_fixtures_without_an_image():
    """A stale recording is dead weight nothing can ever replay."""
    expected = {
        hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in images()
    }
    stored = {p.stem for p in FIXTURES.glob("*.json")}
    assert stored == expected


def test_the_replayed_readings_still_go_through_the_validator():
    """Replay is not a bypass.

    The whole claim is that only the transcription is recorded and every check
    still runs. If a stored reading could not survive `validate_invoice`, the
    demo would be showing something the live path never produces.
    """
    for image in images():
        doc = _fixture_for(image.read_bytes(), FIXTURES)
        validate_invoice(doc)  # must not raise on any recorded document


def test_one_recording_carries_a_misread_the_checksum_catches():
    """The demo's honest moment, pinned so it cannot quietly disappear.

    One of the four invoices prints two GSTINs — the supplier's and ours — and
    the reading blends them into a plausible, wrong number. That finding is the
    point of showing the feature at all, so it is asserted rather than hoped
    for. If a re-recording ever reads it correctly, this test fails and the
    README needs rewriting; that is the correct outcome, not a broken test.
    """
    caught = []
    for image in images():
        doc = _fixture_for(image.read_bytes(), FIXTURES)
        gstin = doc.get("supplier_gstin") or ""
        if gstin and not gstin_is_valid(gstin):
            flags = [f for f in validate_invoice(doc)
                     if f.field == "supplier_gstin"]
            assert flags, f"{image.name} has an invalid GSTIN and no finding"
            # The finding says a character is wrong, not which one. This
            # invoice is why: the misread is in the middle of the number, so
            # any "did you mean" derived from the last character would be a
            # second wrong answer wearing a valid checksum.
            assert flags[0].suggestion is None
            caught.append(image.stem)

    assert len(caught) == 1, (
        f"expected exactly one misread GSTIN across the demo set, got {caught}"
    )


def test_a_stored_reading_is_json_the_endpoint_can_use():
    for path in FIXTURES.glob("*.json"):
        doc = json.loads(path.read_text())
        assert isinstance(doc.get("lines"), list)
        assert doc.get("invoice_number")
