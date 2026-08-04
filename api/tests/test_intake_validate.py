"""The invoice validator (app/ai/intake/validate.py).

These are the tests the accuracy claim rests on, so they are written against
behaviour a pharmacist would recognise rather than against internals: a
transposed price column, a batch code that does not look like this supplier's,
an expiry read as the first of the month instead of the last.

`bench/ocr/validate_bench.py` measures the catch rate against real extractions.
This file proves each individual rule fires when it should and — the half that
is easy to forget — stays quiet when it should.
"""

from datetime import date

import pytest

from app.ai.intake.validate import (
    Flag,
    Severity,
    batch_shape,
    blocking,
    gstin_check_digit,
    gstin_is_valid,
    learn_batch_shapes,
    validate_invoice,
)

# Shapes a supplier has shipped before, as `learn_batch_shapes` would return
# from the lot history.
KNOWN = {"AA99-99", "AA9999", "9999AA"}


def clean_invoice() -> dict:
    """An invoice where every identity closes.

    Deliberately built by hand rather than copied from the benchmark: if a rule
    is wrong, this fixture should be what disagrees with it.
    """
    return {
        "invoice_number": "INV/26-27/5194",
        "invoice_date": "2026-05-14",
        "supplier_name": "SHREE PHARMA DISTRIBUTORS",
        "supplier_gstin": "27AAPFU0939F1ZV",   # a real, checksum-valid GSTIN
        "supplier_state_code": "27",
        "is_intra_state": True,
        "lines": [
            {
                "product_name": "AMOXY-500 CAP",
                "hsn": "3004",
                "batch_no": "ZP01-73",
                "expiry_date": "2027-08-31",
                "quantity": 10,
                "rate": 88.06,
                "mrp": 116.00,
                "discount_pct": 2.5,
                "gst_rate": 12,
                "taxable_amount": 858.59,
                "tax_amount": 103.03,
            }
        ],
        "totals": {
            "taxable_amount": 858.59,
            "cgst": 51.52, "sgst": 51.52, "igst": 0.0,
            "round_off": -0.03,
            "grand_total": 961.60,
        },
    }


def fields(flags: list[Flag]) -> set[str]:
    return {f.field for f in flags}


# ------------------------------------------------------------------ the quiet case


def test_a_consistent_invoice_raises_nothing():
    assert validate_invoice(clean_invoice(), known_batch_shapes=KNOWN) == []


def test_absent_mrp_does_not_trip_the_price_ceiling():
    """Regression: a layout that does not print MRP is not a violation.

    Extractors emit 0 for a column that is not on the page. Reading that as a
    real ceiling made `rate <= MRP` fire on every line of every invoice without
    an MRP column — which was most of them.
    """
    doc = clean_invoice()
    doc["lines"][0]["mrp"] = 0
    assert "rate" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_missing_round_off_widens_the_total_tolerance():
    """An omitted round-off cell is unknown, not zero."""
    doc = clean_invoice()
    del doc["totals"]["round_off"]
    doc["totals"]["grand_total"] = 961.63     # the unrounded sum
    assert "totals.grand_total" not in fields(
        validate_invoice(doc, known_batch_shapes=KNOWN)
    )


def test_unknown_supplier_skips_the_batch_check():
    """With no lot history there is no vocabulary, so inventing one is wrong."""
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = "WHATEVER-9"
    assert "batch_no" not in fields(validate_invoice(doc, known_batch_shapes=None))


def _lines(doc: dict, batches: list[str]) -> dict:
    """Repeat the fixture's one line, varying only the batch code."""
    template = doc["lines"][0]
    doc["lines"] = [dict(template, batch_no=b) for b in batches]
    doc["totals"] = dict(
        doc["totals"],
        taxable_amount=round(858.59 * len(batches), 2),
        cgst=round(51.52 * len(batches), 2),
        sgst=round(51.52 * len(batches), 2),
        grand_total=round(961.60 * len(batches), 2),
        round_off=0.0,
    )
    return doc


def test_an_unfamiliar_batch_format_does_not_block_a_delivery():
    """Regression, found by receiving a real invoice against real history.

    The vocabulary is gathered per distributor, and a distributor carries many
    manufacturers, so shapes it has never seen turn up constantly — a first
    delivery of any product produces one. These held five of eight lines on a
    genuine invoice, every one of them read correctly, and goods standing on
    the floor could not be received at all.

    They are still worth a glance. They are not worth stopping a delivery.
    """
    doc = _lines(clean_invoice(), ["X48659", "U78775", "T44771"])
    flags = [f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
             if f.field == "batch_no"]
    # Said once, about the page, rather than three times about three rows —
    # what is thin here is the history, not any one of these codes.
    assert len(flags) == 1
    assert flags[0].severity is Severity.REVIEW
    assert flags[0].line_no is None
    assert "3 of 3" in flags[0].message


def test_one_unfamiliar_batch_among_familiar_ones_is_named():
    """A minority is the case worth reporting per line: the contrast is the
    information, and the reader needs to know which row to look at."""
    doc = _lines(clean_invoice(), ["ZP01-73", "ZP01-74", "ZP01-75", "X48659"])
    flags = [f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
             if f.field == "batch_no"]
    assert [f.line_no for f in flags] == [4]
    assert flags[0].severity is Severity.REVIEW


def test_a_batch_one_confusable_character_from_a_known_shape_is_reported():
    """The case the check exists for, and the only one it still makes.

    `ZPO1-73` is `ZP01-73` with the digit zero read as the letter O — the
    classic OCR failure, and a specific enough claim to come with the
    correction attached.
    """
    doc = _lines(clean_invoice(), ["ZP01-73", "ZP01-74", "ZP01-75", "ZPO1-73"])
    flags = [f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
             if f.field == "batch_no"]
    assert [f.line_no for f in flags] == [4]
    assert flags[0].suggestion == "ZP01-73"
    assert flags[0].severity is Severity.BLOCK


def test_a_batch_absorbing_a_neighbouring_character_is_reported():
    """`0423KA` arrived as `0423KAL` — a column bleeding into the one beside
    it, which a trailing deletion recovers."""
    doc = _lines(clean_invoice(), ["9999AAL"])
    flags = [f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
             if f.field == "batch_no"]
    assert [f.suggestion for f in flags] == ["9999AA"]


# --------------------------------------------------------------------- arithmetic


def test_line_arithmetic_catches_a_wrong_amount():
    doc = clean_invoice()
    doc["lines"][0]["taxable_amount"] = 885.59        # 858.59 transposed
    flags = validate_invoice(doc, known_batch_shapes=KNOWN)
    assert "taxable_amount" in fields(flags)
    assert all(f.severity is Severity.BLOCK for f in flags if f.field == "taxable_amount")


def test_an_unprinted_discount_is_not_an_error():
    """Regression: most layouts do not print a discount column.

    On 178 of the benchmark's 462 lines the discount is real but never shown —
    the amount simply arrives lower than quantity x rate. A strict identity
    called every one of those an error, which is worse than having no check.
    """
    doc = clean_invoice()
    doc["lines"][0]["discount_pct"] = 0          # column absent from the page
    assert "taxable_amount" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_an_amount_above_quantity_times_rate_is_blocked():
    """A discount only ever reduces, so this direction needs no discount known."""
    doc = clean_invoice()
    doc["lines"][0] |= {"discount_pct": 0, "taxable_amount": 900.00}   # > 10 x 88.06
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "taxable_amount")
    assert flag.severity is Severity.BLOCK


def test_an_implausible_implied_discount_is_blocked():
    """A quantity read ten-fold high shows up as a ~90% discount."""
    doc = clean_invoice()
    doc["lines"][0] |= {"quantity": 100, "discount_pct": 0}
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "taxable_amount")
    assert flag.severity is Severity.BLOCK
    assert "misread" in flag.message


def test_tax_identity_catches_a_wrong_gst_rate():
    doc = clean_invoice()
    doc["lines"][0]["gst_rate"] = 18                  # HSN 3004 is never 18%
    flags = validate_invoice(doc, known_batch_shapes=KNOWN)
    assert {"tax_amount", "gst_rate"} <= fields(flags)


def test_footer_catches_a_dropped_line():
    """The one check that sees a *missing* row rather than a wrong one."""
    doc = clean_invoice()
    doc["totals"]["taxable_amount"] = 1717.18         # footer counted two lines
    doc["totals"]["grand_total"] = 1923.23
    assert "totals.taxable_amount" in fields(
        validate_invoice(doc, known_batch_shapes=KNOWN)
    )


def test_an_unprinted_gst_rate_is_derived_and_accepted():
    """Split-column layouts print CGST and SGST as rupees and no percentage.

    Asking the model for a rate that is not on the page invited a calculation,
    and a calculated rate agrees with whatever the model already believed — on
    the benchmark it came back doubled on 37 lines. The rate is now derived
    from the two figures that *were* printed.
    """
    doc = clean_invoice()
    doc["lines"][0]["gst_rate"] = 0            # not printed anywhere on the line
    assert "tax_amount" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_an_unprinted_tax_amount_is_not_checked_against_a_printed_rate():
    """The mirror case: some layouts print a rate but no per-line tax column.

    Zero means "not on the page" for the tax figure too. Missing that fired on
    130 correct lines of a 50-invoice run.
    """
    doc = clean_invoice()
    doc["lines"][0]["tax_amount"] = 0          # only an HSN summary at the foot
    assert "tax_amount" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_a_derived_rate_on_no_legal_slab_is_flagged():
    doc = clean_invoice()
    doc["lines"][0] |= {"gst_rate": 0, "tax_amount": 75.00}   # ~8.7% of 858.59
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "tax_amount")
    assert "not a GST rate" in flag.message


def test_tax_findings_do_not_block_a_receipt():
    """No part of a tax figure reaches a stock movement.

    Blocking on them held 34% of correct invoices in an earlier version, which
    is how a receiver learns to click through every warning including the real
    ones.
    """
    doc = clean_invoice()
    doc["lines"][0] |= {"gst_rate": 0, "tax_amount": 75.00}
    assert blocking(validate_invoice(doc, known_batch_shapes=KNOWN)) == []


def test_footer_tax_catches_a_self_consistent_gst_misread():
    """The case per-line checks structurally cannot see.

    A model that reads 5% as 12% and then *recalculates* the tax to match
    produces a line that agrees with itself, so every per-line rule passes.
    Observed on a real extraction. The footer is the supplier's arithmetic
    rather than the reader's, so it breaks the tie.
    """
    doc = clean_invoice()
    line = doc["lines"][0]
    line["gst_rate"] = 18
    line["tax_amount"] = round(line["taxable_amount"] * 0.18, 2)   # recomputed
    flags = validate_invoice(doc, known_batch_shapes=KNOWN)
    assert "tax_amount" not in fields(flags)          # the line is self-consistent
    assert "totals.cgst" in fields(flags)             # the footer is not fooled


def test_grand_total_must_close():
    doc = clean_invoice()
    doc["totals"]["grand_total"] = 1961.60
    assert "totals.grand_total" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


# ------------------------------------------------------------------------ pricing


def test_rate_above_mrp_is_blocked_and_suggests_the_swap():
    doc = clean_invoice()
    doc["lines"][0]["rate"], doc["lines"][0]["mrp"] = 116.00, 88.06
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "rate")
    assert flag.severity is Severity.BLOCK
    assert "88.06" in flag.suggestion


# ------------------------------------------------------------------------- expiry


def test_expiry_must_be_a_month_end():
    doc = clean_invoice()
    doc["lines"][0]["expiry_date"] = "2027-08-01"     # read as the 1st
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "expiry_date")
    assert flag.suggestion == "2027-08-31"


def test_expiry_before_the_invoice_date_is_blocked():
    doc = clean_invoice()
    doc["lines"][0]["expiry_date"] = "2026-01-31"
    flags = [f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
             if f.field == "expiry_date"]
    assert any(f.severity is Severity.BLOCK for f in flags)


# -------------------------------------------------------------------------- batch


@pytest.mark.parametrize("code,shape", [
    ("PX8602", "AA9999"),
    ("AM26-21", "AA99-99"),
    ("0423KA", "9999AA"),
])
def test_batch_shape_skeleton(code, shape):
    assert batch_shape(code) == shape


def test_learn_batch_shapes_builds_the_vocabulary():
    assert learn_batch_shapes(["PX8602", "AM26-21", "", "NV7025"]) == {"AA9999", "AA99-99"}


@pytest.mark.parametrize("bad", ["0423KAL", "AM26-0MD", "EX73-0MD"])
def test_real_ocr_garbles_are_reported(bad):
    """The three batch errors Gemini actually made on the benchmark.

    All three still surface. Only `0423KAL` blocks — a trailing character
    carried in from the next column, which a single deletion undoes and names.
    The other two are reported for a glance: they are unfamiliar, and nothing
    stronger than that can honestly be said about them.
    """
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = bad
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "batch_no")
    assert flag.severity in (Severity.BLOCK, Severity.REVIEW)


def test_a_confusable_substitution_is_suggested():
    """`ZPO1-73` is `ZP01-73` with the zero read as a letter O."""
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = "ZPO1-73"
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "batch_no")
    assert flag.suggestion == "ZP01-73"


def test_a_character_carried_in_from_the_next_column_is_suggested_away():
    """The real one: `0423KA` arrived as `0423KAL` on benchmark invoice 14."""
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = "0423KAL"
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "batch_no")
    assert flag.suggestion == "0423KA"


def test_an_ambiguous_garble_gets_no_suggestion():
    """Nudging towards one of several equally likely readings is worse than none.

    And with nothing specific to say, the finding does not block: a delivery
    is not held on the strength of an unfamiliar-looking code.
    """
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = "AM26-0MD"
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "batch_no")
    assert flag.severity is Severity.REVIEW
    assert flag.suggestion is None


def test_a_missing_batch_is_blocked():
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = ""
    assert blocking(validate_invoice(doc, known_batch_shapes=KNOWN))


# --------------------------------------------------------------------------- GST


@pytest.mark.parametrize("gstin", [
    "27AAPFU0939F1ZV",
    "29AAGCB7383J1Z4",
    "09AAACH7409R1ZZ",
])
def test_real_gstins_validate(gstin):
    assert gstin_is_valid(gstin)


@pytest.mark.parametrize("gstin", [
    "27AAPFU0939F1ZW",   # check digit bumped
    "27AAPFU0930F1ZV",   # an inner digit changed
    "27AAPFU0939F1Z",    # too short
    "27AAPFU0939F1AV",   # the mandatory Z is not a Z
    "",
])
def test_corrupted_gstins_are_rejected(gstin):
    assert not gstin_is_valid(gstin)


def test_check_digit_is_recoverable():
    assert gstin_check_digit("27AAPFU0939F1Z") == "V"


def test_igst_on_an_intra_state_invoice_is_blocked():
    doc = clean_invoice()
    doc["totals"] |= {"cgst": 0.0, "sgst": 0.0, "igst": 103.03}
    assert "totals.igst" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_cgst_on_an_inter_state_invoice_is_blocked():
    doc = clean_invoice()
    doc["is_intra_state"] = False
    assert "totals.cgst" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_cgst_and_sgst_must_be_equal():
    doc = clean_invoice()
    doc["totals"]["sgst"] = 41.52
    assert "totals.sgst" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_state_code_must_match_the_gstin_prefix():
    doc = clean_invoice()
    doc["supplier_state_code"] = "24"
    assert "supplier_state_code" in fields(
        validate_invoice(doc, known_batch_shapes=KNOWN)
    )


def test_hsn_outside_pharma_is_noted_but_not_blocked():
    doc = clean_invoice()
    doc["lines"][0]["hsn"] = "8471"                   # computers
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "hsn")
    assert flag.severity is Severity.INFO


# -------------------------------------------------------------------------- misc


def test_a_future_invoice_date_is_reviewed():
    doc = clean_invoice()
    flags = validate_invoice(doc, known_batch_shapes=KNOWN, today=date(2026, 1, 1))
    assert "invoice_date" in fields(flags)


def test_messy_string_numbers_are_still_checked():
    """Models return "1,250.00" and "12%" as readily as they return floats."""
    doc = clean_invoice()
    doc["lines"][0] |= {"quantity": "10", "rate": "88.06", "taxable_amount": "1,858.59"}
    assert "taxable_amount" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_blocking_filters_to_the_ones_that_stop_a_post():
    doc = clean_invoice()
    doc["lines"][0]["hsn"] = "8471"                   # INFO only
    assert blocking(validate_invoice(doc, known_batch_shapes=KNOWN)) == []


# --------------------------------------------------------------------- edges
#
# Everything below is a way the checks could pass on a document nobody should
# accept. They are worth more than the happy paths: a validator that stays
# silent on a page it failed to read is worse than no validator, because the
# silence is indistinguishable from approval.


def test_an_extraction_with_no_lines_is_blocked():
    """The dangerous one: every other rule is about a line or a footer.

    With no lines there is nothing to disagree with, so the document passed
    silently — which reads as "this invoice is fine" when what happened is that
    the page was never read.
    """
    doc = clean_invoice()
    doc["lines"] = []
    flags = validate_invoice(doc, known_batch_shapes=KNOWN)
    assert "lines" in fields(flags)
    assert blocking(flags)


def test_a_document_that_is_not_an_invoice_at_all_is_blocked():
    assert blocking(validate_invoice({}, known_batch_shapes=KNOWN))


@pytest.mark.parametrize("junk", ["1e400", "-1e400", float("inf"), float("nan")])
def test_infinities_and_nan_cannot_silence_a_check(junk):
    """NaN makes every comparison false, so it would pass every rule at once."""
    doc = clean_invoice()
    doc["lines"][0]["taxable_amount"] = junk
    flags = validate_invoice(doc, known_batch_shapes=KNOWN)
    assert "taxable_amount" in fields(flags)
    assert blocking(flags)


def test_a_free_goods_only_line_is_allowed():
    """A sample or scheme delivered against nothing charged is a real delivery."""
    doc = clean_invoice()
    doc["lines"][0] |= {"quantity": 0, "free_quantity": 5, "taxable_amount": 0,
                        "tax_amount": 0}
    doc["totals"] |= {"taxable_amount": 0, "cgst": 0, "sgst": 0,
                      "round_off": 0, "grand_total": 0}
    assert "quantity" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_a_negative_free_quantity_is_blocked():
    doc = clean_invoice()
    doc["lines"][0]["free_quantity"] = -3
    assert "free_quantity" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


@pytest.mark.parametrize("bad", [-5, 100, 250])
def test_a_discount_outside_zero_to_a_hundred_is_blocked(bad):
    doc = clean_invoice()
    doc["lines"][0]["discount_pct"] = bad
    assert "discount_pct" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_an_unreadable_expiry_is_blocked_not_ignored():
    """Skipping it silently puts a batch on the shelf that FEFO cannot rank."""
    doc = clean_invoice()
    doc["lines"][0]["expiry_date"] = "AUG-27"          # never normalised
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=KNOWN)
                if f.field == "expiry_date")
    assert flag.severity is Severity.BLOCK


def test_a_missing_expiry_is_blocked():
    doc = clean_invoice()
    doc["lines"][0]["expiry_date"] = ""
    assert "expiry_date" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_an_absurdly_distant_expiry_is_reviewed():
    """A misread year parks the batch behind everything else in FEFO."""
    doc = clean_invoice()
    doc["lines"][0]["expiry_date"] = "2077-08-31"
    assert "expiry_date" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_a_duplicated_line_is_caught_by_the_footer():
    """The same printed row read twice — invisible to every per-line rule."""
    doc = clean_invoice()
    doc["lines"].append(dict(doc["lines"][0]))
    assert "totals.taxable_amount" in fields(
        validate_invoice(doc, known_batch_shapes=KNOWN)
    )


def test_a_free_goods_column_with_nothing_under_it_is_flagged():
    """The error class no arithmetic can reach.

    Free goods are free, so they appear in no money identity — three were
    dropped silently on the benchmark. The heading contradicting the rows is
    the only evidence available, and it only exists because the extractor is
    now asked to list the columns it can see.
    """
    doc = clean_invoice()
    doc["columns_seen"] = ["S.N.", "PRODUCT", "BATCH", "EXP", "QTY", "FREE", "RATE"]
    doc["lines"][0]["free_quantity"] = 0
    flags = validate_invoice(doc, known_batch_shapes=KNOWN)
    assert "free_quantity" in fields(flags)
    assert blocking(flags) == []          # a scheme-free invoice looks the same


def test_a_free_goods_column_with_a_value_is_quiet():
    doc = clean_invoice()
    doc["columns_seen"] = ["S.N.", "PRODUCT", "QTY", "FREE", "RATE"]
    doc["lines"][0]["free_quantity"] = 2
    assert "free_quantity" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_no_free_column_means_no_free_goods_flag():
    doc = clean_invoice()
    doc["columns_seen"] = ["S.N.", "PRODUCT", "BATCH", "EXP", "QTY", "RATE"]
    assert "free_quantity" not in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_a_line_with_no_product_name_is_blocked():
    doc = clean_invoice()
    doc["lines"][0]["product_name"] = "  "
    assert "product_name" in fields(validate_invoice(doc, known_batch_shapes=KNOWN))


def test_an_absurdly_long_batch_is_blocked_without_any_history():
    """Holds for a supplier we have never received from, unlike the vocabulary."""
    doc = clean_invoice()
    doc["lines"][0]["batch_no"] = "ZP01-73 AMOXYCILLIN 500MG CAP 10x10 3004"
    flag = next(f for f in validate_invoice(doc, known_batch_shapes=None)
                if f.field == "batch_no")
    assert flag.severity is Severity.BLOCK


def test_lowercase_input_is_normalised():
    doc = clean_invoice()
    doc["supplier_gstin"] = "27aapfu0939f1zv"
    doc["lines"][0]["batch_no"] = "zp01-73"
    assert validate_invoice(doc, known_batch_shapes=KNOWN) == []
