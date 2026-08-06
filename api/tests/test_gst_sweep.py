"""The tax arithmetic over its input space, not over the rows that happen to exist.

`test_gst.py` pins the worked examples — a known order, a known answer. This is
the other half: every statutory rate against quantities and prices chosen to sit
on the boundaries where paisa go missing, asserting the rules rather than the
implementation. A wrong implementation and a wrong assertion would have to agree
by coincidence for this to pass.

It exists because of what an audit of the demo database showed. There are 43,388
sale postings in two years of generated history and 17 sales orders, because the
history is written straight to the ledger — so the stored data exercises this
arithmetic on 31 lines. Thirty-one lines is not evidence about a tax engine. The
input space is.

No database, no server: it is pure Decimal arithmetic and runs in well under a
second.
"""

from __future__ import annotations

import itertools
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.services import gst

P = Decimal("0.01")
RUPEE = Decimal("1")

#: The statutory slabs, nil-rating, and the 0.25 and 3 percent special rates.
RATES = [Decimal(x) for x in ("0", "0.25", "3", "5", "12", "18", "28")]

#: Quantities and prices chosen to land on halves and half-paisa: odd totals,
#: x.x05 boundaries, fractional quantities, and figures large enough that a
#: float slipping in anywhere would show.
QTYS = [Decimal(x) for x in ("1", "2", "3", "7", "13", "99", "1000", "2.5", "0.5")]
PRICES = [
    Decimal(x)
    for x in (
        "0.01", "0.05", "1.00", "1.01", "3.33",
        "12.345", "99.99", "123.45", "1999.99", "45678.90",
    )
]

CASES = list(itertools.product(RATES, QTYS, PRICES, (False, True)))


@pytest.mark.parametrize("rate,qty,price,interstate", CASES)
def test_a_line_is_taxed_by_the_rules(rate, qty, price, interstate):
    t = gst.compute_line_tax(
        quantity=qty, unit_price=price, gst_rate=rate, interstate=interstate
    )

    # Taxable value is the product, rounded half-up to the paisa, once.
    taxable = (qty * price).quantize(P, rounding=ROUND_HALF_UP)
    assert t.taxable_value == taxable

    # Tax is the rate applied to that, rounded the same way.
    want = (taxable * rate / 100).quantize(P, rounding=ROUND_HALF_UP)
    assert t.cgst_amount + t.sgst_amount + t.igst_amount == want

    if interstate:
        # One supply, one government. A line carrying both regimes is a line
        # taxed twice.
        assert t.cgst_amount == 0 and t.sgst_amount == 0
        assert t.igst_amount == want
    else:
        assert t.igst_amount == 0
        # The halves sum EXACTLY to the whole. Rounding each independently is
        # how an invoice ends up a paisa short of itself.
        assert t.cgst_amount + t.sgst_amount == want
        # And they differ by at most the odd paisa an odd total forces.
        assert abs(t.cgst_amount - t.sgst_amount) <= P

    # Whole paisa throughout — no trailing precision to print oddly or to
    # accumulate into a discrepancy three hundred lines later.
    for value in (t.taxable_value, t.cgst_amount, t.sgst_amount, t.igst_amount):
        assert value == value.quantize(P)

    assert t.line_total == t.taxable_value + want

    if rate == 0:
        # Nil-rated means nil, not a rounding artefact.
        assert t.cgst_amount + t.sgst_amount + t.igst_amount == 0


@pytest.mark.parametrize(
    "rate,interstate,size",
    [(r, i, n) for r, i in itertools.product(RATES, (False, True))
     for n in (1, 2, 3, 17)],
)
def test_a_document_totals_by_the_rules(rate, interstate, size):
    lines = [
        gst.compute_line_tax(
            quantity=Decimal(k + 1),
            unit_price=Decimal("3.33") + k,
            gst_rate=rate,
            interstate=interstate,
        )
        for k in range(size)
    ]
    d = gst.compute_document_totals(lines)
    exact = sum((x.taxable_value for x in lines), Decimal("0")) + sum(
        (x.tax_total for x in lines), Decimal("0")
    )

    # The grand total is whole rupees. That is what round_off is for.
    assert d.grand_total == d.grand_total.quantize(RUPEE)

    # Bounded by half a rupee, and asymmetrically: under ROUND_HALF_UP a total
    # of exactly x.50 goes up, so +0.50 is reachable and -0.50 is not. A
    # symmetric bound here fails on a single 5% line of 3.33 — total 3.50,
    # rounded to 4, round_off exactly 0.50.
    assert Decimal("-0.50") < d.round_off <= Decimal("0.50")

    # The identity a customer checks by hand.
    assert d.subtotal + d.tax_total + d.round_off == d.grand_total

    # And round_off closes exactly the gap it claims to, so it cannot quietly
    # absorb an error from somewhere else.
    assert d.round_off == (d.grand_total - exact).quantize(P, rounding=ROUND_HALF_UP)
