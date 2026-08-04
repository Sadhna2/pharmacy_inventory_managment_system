"""The extraction service (app/ai/intake/service.py).

Nothing here makes a network call or needs an API key, and that is a
requirement rather than a convenience: CI runs on a GitHub runner with no
Gemini credentials, and a test suite that quietly skips itself when a secret is
missing is how a feature ends up unverified in the branch that ships it.

The upload guards and the fixture path are covered directly. The single
outbound call is covered by pointing the service at a fixture directory, which
is the same mechanism the demo uses.
"""

import json
import struct
import zlib

import pytest

from app.ai.intake import service
from app.ai.intake.service import (
    IntakeRejected,
    IntakeUnavailable,
    derive_context,
    extract_invoice,
)


def png_bytes() -> bytes:
    """A real one-pixel PNG, so the magic-byte check sees genuine content."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
            + chunk(b"IEND", b""))


@pytest.fixture
def fixtures(tmp_path, monkeypatch):
    """Point the service at replayed extractions instead of the API."""
    monkeypatch.setattr(service.settings, "intake_fixture_dir", str(tmp_path))
    monkeypatch.setattr(service.settings, "gemini_api_key", "")
    return tmp_path


# ------------------------------------------------------------- upload guards


def test_an_empty_upload_is_rejected(fixtures):
    with pytest.raises(IntakeRejected, match="empty"):
        extract_invoice(b"")


def test_an_oversized_upload_is_rejected_before_it_is_sent(fixtures):
    huge = png_bytes() + b"\x00" * service.MAX_IMAGE_BYTES
    with pytest.raises(IntakeRejected, match="limit"):
        extract_invoice(huge)


def test_an_unsupported_declared_type_is_rejected(fixtures):
    with pytest.raises(IntakeRejected, match="not a supported format"):
        extract_invoice(png_bytes(), mime_type="text/html")


def test_a_file_that_is_not_an_image_is_rejected(fixtures):
    """The declared type is a client hint; this content leaves our network."""
    with pytest.raises(IntakeRejected, match="not a PNG"):
        extract_invoice(b"MZ\x90\x00 this is an executable", mime_type="image/png")


def test_a_mislabelled_but_valid_image_is_accepted(fixtures):
    """A phone that labels a PNG as JPEG should not cost the user a retry."""
    (fixtures / "default.json").write_text(json.dumps({"lines": []}))
    assert extract_invoice(png_bytes(), mime_type="image/jpeg") == {"lines": []}


@pytest.mark.parametrize("magic,mime", [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
    (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
])
def test_every_accepted_format_is_recognised(magic, mime):
    assert service._sniff(magic + b"\x00" * 32) == mime


# ------------------------------------------------------------------ fixtures


def test_a_fixture_is_replayed_by_content_hash(fixtures):
    import hashlib

    image = png_bytes()
    digest = hashlib.sha256(image).hexdigest()[:16]
    (fixtures / f"{digest}.json").write_text(json.dumps({"invoice_number": "A/1"}))
    assert extract_invoice(image)["invoice_number"] == "A/1"


def test_a_missing_fixture_fails_loudly(fixtures):
    with pytest.raises(service.ExtractionFailed, match="no fixture"):
        extract_invoice(png_bytes())


def test_without_a_key_or_fixtures_the_feature_reports_itself_off(monkeypatch):
    """Unset is a supported state — 503, not a crash, and not a blank draft."""
    monkeypatch.setattr(service.settings, "intake_fixture_dir", "")
    monkeypatch.setattr(service.settings, "gemini_api_key", "")
    with pytest.raises(IntakeUnavailable, match="not configured"):
        extract_invoice(png_bytes())


# ------------------------------------------------------------------- parsing


def test_a_blocked_response_names_the_reason():
    payload = {"candidates": [{"finishReason": "SAFETY"}]}
    with pytest.raises(service.ExtractionFailed, match="SAFETY"):
        service._parse(payload)


def test_malformed_json_is_reported_as_such():
    payload = {"candidates": [{"content": {"parts": [{"text": "{not json"}]}}]}
    with pytest.raises(service.ExtractionFailed, match="malformed"):
        service._parse(payload)


def test_a_non_object_response_is_rejected():
    payload = {"candidates": [{"content": {"parts": [{"text": "[1, 2, 3]"}]}}]}
    with pytest.raises(service.ExtractionFailed, match="other than an invoice"):
        service._parse(payload)


# ------------------------------------------------------------------- context


def test_intra_state_is_derived_from_our_own_state_not_the_model():
    """`MH`, not `27`, because `MH` is what a warehouse row actually holds.

    These tests used to pass `our_state_code="27"` — the same coding the GSTIN
    uses — so the comparison always lined up and the function looked correct.
    No caller ever supplies that. `Warehouse.state_code` is the postal code,
    and against a real row every invoice came out inter-state, which flagged
    the CGST+SGST on every genuinely local delivery. The test agreed with the
    bug because it was written in the bug's units.
    """
    doc = derive_context({"supplier_gstin": "27AAPFU0939F1ZV"}, our_state_code="MH")
    assert doc["is_intra_state"] is True
    assert doc["supplier_state_code"] == "27"


def test_inter_state_is_derived_the_same_way():
    doc = derive_context({"supplier_gstin": "27AAPFU0939F1ZV"}, our_state_code="GJ")
    assert doc["is_intra_state"] is False


@pytest.mark.parametrize("ours,gstin,intra", [
    ("mh", "27AAPFU0939F1ZV", True),     # case is not the caller's problem
    ("TS", "36AACCU5699S1Z9", True),     # Telangana, spelled TS
    ("TG", "36AACCU5699S1Z9", True),     # ...and spelled TG
    ("UK", "05AAACP1234A1Z5", True),     # Uttarakhand, both abbreviations
    ("UT", "05AAACP1234A1Z5", True),
    ("KA", "29AAACP1234A1Z5", True),
    ("KA", "27AAACP1234A1Z5", False),
])
def test_the_two_codings_line_up_across_the_states(ours, gstin, intra):
    """Several states answer to two abbreviations. Both have to work, or a
    branch recorded as `TS` silently disagrees with one recorded as `TG`."""
    assert derive_context({"supplier_gstin": gstin},
                          our_state_code=ours)["is_intra_state"] is intra


def test_a_state_code_we_cannot_place_asks_nothing():
    """Unknown is not "different". Inventing inter-state from a code this
    table has never heard of would manufacture a tax finding out of nothing."""
    doc = derive_context({"supplier_gstin": "27AAPFU0939F1ZV"}, our_state_code="ZZ")
    assert "is_intra_state" not in doc
    assert doc["supplier_state_code"] == "27"


def test_without_our_state_no_context_is_invented():
    """A guessed context produces confident nonsense downstream."""
    doc = derive_context({"supplier_gstin": "27AAPFU0939F1ZV"}, our_state_code=None)
    assert "is_intra_state" not in doc


def test_an_unreadable_gstin_yields_no_context():
    doc = derive_context({"supplier_gstin": "rubbish"}, our_state_code="MH")
    assert "is_intra_state" not in doc


def test_derive_context_does_not_mutate_the_original():
    original = {"supplier_gstin": "27AAPFU0939F1ZV"}
    derive_context(original, our_state_code="27")
    assert original == {"supplier_gstin": "27AAPFU0939F1ZV"}


# -------------------------------------------------------------------- schema


def test_the_schema_demands_the_fields_the_validator_needs():
    """Regression: the benchmark prompt omitted these and the checks went dark.

    Seven of the validator's rules depend on fields a goods receipt never
    displays. Dropping them from the schema costs nothing visible and silently
    removes most of the error detection, which is exactly the kind of change
    that gets made by accident.
    """
    required = set(service.LINE_SCHEMA["required"])
    assert {"hsn", "gst_rate", "taxable_amount", "tax_amount"} <= required
    assert "round_off" in service.SCHEMA["properties"]["totals"]["required"]
