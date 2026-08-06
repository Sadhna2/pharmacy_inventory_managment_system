"""A branch's own GST registration, and the two ways of getting it wrong.

The column exists because GST registers per state: a Gujarat branch of a Mumbai
firm is a separately registered person with its own GSTIN, and the first two
characters of a GSTIN *are* that state's numeric code. What the invoice does
with the column is pinned down in `test_invoice_route.py`; what is at stake
here is refusing a bad one at the door, because a wrong registration on a
warehouse is not visible until somebody prints an invoice under it.

Two independent checks, catching two different mistakes.

The checksum catches a typo. GSTIN carries a mod-36 check digit, so a mistyped
character is provably wrong rather than merely unfamiliar — one of the few
identifiers in this system that can be verified outright.

The prefix catches the likelier mistake, and the one the column exists to end:
pasting head office's registration into a branch in another state. That number
is perfectly valid. It is just not this branch's, and no checksum will say so.

No database and no network — these are the schema's own rules.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.masters import WarehouseIn, WarehouseUpdate
from app.services import gst

MAHARASHTRA = "27AABCS9876P1ZA"
GUJARAT = "24AABCS9876P1ZG"


def _warehouse(**overrides):
    fields = {"code": "BR-AHM", "name": "Ahmedabad Branch", "state_code": "GJ"}
    return WarehouseIn(**{**fields, **overrides})


# --- the seeded numbers are real ---------------------------------------------


@pytest.mark.parametrize("gstin", [MAHARASHTRA, GUJARAT])
def test_the_registrations_this_system_ships_with_are_valid(gstin):
    """The seed writes these onto real warehouses.

    A seeded GSTIN that failed the checksum would have the system contradicting
    itself the first time anyone photographed its own invoice: the same
    validator runs on the way in, and would reject the number this application
    printed.
    """
    assert gst.gstin_is_valid(gstin)


def test_the_two_registrations_belong_to_one_firm():
    """Same PAN, different state. That is what a second registration *is* —
    not a second company, the same one registered again somewhere else."""
    assert MAHARASHTRA[2:12] == GUJARAT[2:12]
    assert MAHARASHTRA[:2] != GUJARAT[:2]


# --- refusing a bad one -------------------------------------------------------


def test_a_mistyped_registration_is_refused():
    with pytest.raises(ValidationError) as caught:
        _warehouse(gstin="24AABCS9876P1ZZ")  # correct shape, wrong check digit

    assert "check digit" in str(caught.value)


def test_head_offices_registration_on_a_branch_in_another_state_is_refused():
    """The mistake this column exists to prevent, and the one a checksum
    cannot see: the number is entirely valid, just not this branch's."""
    with pytest.raises(ValidationError) as caught:
        _warehouse(state_code="GJ", gstin=MAHARASHTRA)

    message = str(caught.value)
    assert "starting 24" in message
    assert "one state" in message


def test_something_not_shaped_like_a_gstin_at_all_is_refused():
    with pytest.raises(ValidationError):
        _warehouse(gstin="24AABCS9876")


# --- accepting a good one -----------------------------------------------------


def test_a_branchs_own_registration_is_accepted_and_normalised():
    """Stored upper-case and trimmed, so two spellings of one number cannot
    sit in the table looking like two registrations."""
    assert _warehouse(gstin=f"  {GUJARAT.lower()} ").gstin == GUJARAT


def test_no_registration_is_a_legitimate_answer():
    """Null is what every row held the day the column was added, and what a
    chain trading in one state still holds. It falls back to the firm's."""
    assert _warehouse().gstin is None
    assert _warehouse(gstin=None).gstin is None


def test_a_state_this_table_does_not_know_checks_the_shape_and_stops_there():
    """`STATE_CODES` is a lookup, and an incomplete lookup must not become a
    finding. An unrecognised state means the prefix cannot be judged — not
    that it disagrees."""
    assert gst.gstin_prefix_for_state("ZZ") is None

    accepted = _warehouse(state_code="ZZ", gstin=GUJARAT)

    assert accepted.gstin == GUJARAT


# --- editing an existing one --------------------------------------------------


def test_a_patch_that_names_only_the_registration_still_checks_the_checksum():
    with pytest.raises(ValidationError):
        WarehouseUpdate(gstin="24AABCS9876P1ZZ")


def test_a_patch_that_names_only_the_registration_passes_the_schema():
    """And must not be the end of the story.

    The state is not in this payload, so the schema has nothing to compare
    against, and refusing here would make the field uneditable without also
    resending a state nobody is changing. But "no opinion" is not "allowed":
    sending only the number is exactly how head office's registration gets
    pasted onto a branch, so the route re-checks the pairing against the row
    it already holds. That guard is covered in `test_bad_input.py`, over a
    real database, because it is the persisted state that makes it possible.
    """
    assert WarehouseUpdate(gstin=MAHARASHTRA).gstin == MAHARASHTRA


def test_a_patch_moving_a_branch_to_another_state_must_move_its_registration():
    with pytest.raises(ValidationError):
        WarehouseUpdate(state_code="GJ", gstin=MAHARASHTRA)
