"""The printed tax invoice (app/services/invoice_html.py).

What breaks if these fail is a document, not a screen. An invoice that charges
CGST and IGST at once is not valid under the Act; an HSN summary that does not
add up to the lines above it is the first thing an assessing officer checks;
and a total spelled "One Million" goes back unpaid from a buyer who counts in
lakhs. None of that is visible from the JSON the API tests exercise — the
numbers can all be right and the paper still wrong.

Stub objects rather than the database, deliberately. The renderer takes no
session and does no I/O, and these tests are what holds it to that: the day one
of them needs a live server, the renderer has grown a dependency it should not
have. The line amounts are still computed by `services/gst.py` itself, so the
figures asserted here are the ones the system would really print.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from app.services import gst
from app.services.invoice_html import amount_in_words, render_tax_invoice

# --- Stubs ------------------------------------------------------------------


@dataclass
class StubParty:
    name: str
    address: str | None
    gstin: str | None
    state_code: str


@dataclass
class StubUom:
    code: str


@dataclass
class StubProduct:
    name: str
    sku: str
    pack_size: str | None = None
    #: Present only so a test can prove the renderer ignores it. The line
    #: carries the HSN that was in force when the order was priced; the
    #: product's can be edited afterwards and must not rewrite history.
    hsn_code: str | None = None
    uom: StubUom | None = field(default_factory=lambda: StubUom("STRIP"))


@dataclass
class StubLine:
    product: StubProduct
    hsn_code: str | None
    qty_ordered: Decimal
    unit_price: Decimal
    taxable_value: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal


@dataclass
class StubOrder:
    so_number: str
    order_date: date
    customer: StubParty
    lines: list[StubLine]
    is_interstate: bool
    place_of_supply: str | None
    subtotal: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal


SELLER = StubParty(
    name="Shree Medicos Distributors Pvt Ltd",
    address="14 Linking Road\nBandra West\nMumbai 400050",
    gstin="27AABCS1429B1ZQ",
    state_code="MH",
)


def make_line(
    *,
    name: str = "Paracetamol 500mg",
    hsn: str | None = "30049099",
    qty: str = "50",
    price: str = "12.50",
    rate: str = "12",
    interstate: bool,
    sku: str = "PARA-500",
    product_hsn: str | None = None,
) -> StubLine:
    """A line priced by the real GST service, not by hand-typed figures."""
    tax = gst.compute_line_tax(
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        gst_rate=Decimal(rate),
        interstate=interstate,
    )
    return StubLine(
        product=StubProduct(name=name, sku=sku, hsn_code=product_hsn),
        hsn_code=hsn,
        qty_ordered=Decimal(qty),
        unit_price=Decimal(price),
        taxable_value=tax.taxable_value,
        gst_rate=tax.gst_rate,
        cgst_amount=tax.cgst_amount,
        sgst_amount=tax.sgst_amount,
        igst_amount=tax.igst_amount,
        line_total=tax.line_total,
    )


def make_order(
    lines: list[StubLine],
    *,
    interstate: bool,
    buyer_state: str,
    buyer_name: str = "City General Hospital",
    buyer_gstin: str | None = "27AACCC1234D1Z5",
) -> StubOrder:
    totals = gst.compute_document_totals(
        [
            gst.TaxBreakdown(
                taxable_value=line.taxable_value,
                gst_rate=line.gst_rate,
                cgst_amount=line.cgst_amount,
                sgst_amount=line.sgst_amount,
                igst_amount=line.igst_amount,
                line_total=line.line_total,
            )
            for line in lines
        ]
    )
    return StubOrder(
        so_number="SO-000417",
        order_date=date(2026, 8, 5),
        customer=StubParty(
            name=buyer_name,
            address="Plot 9, Sector 21\nNavi Mumbai 400703",
            gstin=buyer_gstin,
            state_code=buyer_state,
        ),
        lines=lines,
        is_interstate=interstate,
        place_of_supply=buyer_state,
        subtotal=totals.subtotal,
        tax_total=totals.tax_total,
        round_off=totals.round_off,
        grand_total=totals.grand_total,
    )


def intra_state_invoice(lines: list[StubLine] | None = None) -> str:
    order = make_order(
        lines or [make_line(interstate=False)], interstate=False, buyer_state="MH"
    )
    return render_tax_invoice(
        order, seller=SELLER, place_of_supply_name="Maharashtra"
    )


def inter_state_invoice(lines: list[StubLine] | None = None) -> str:
    order = make_order(
        lines or [make_line(interstate=True)], interstate=True, buyer_state="KA"
    )
    return render_tax_invoice(
        order, seller=SELLER, place_of_supply_name="Karnataka"
    )


def table(document: str, element_id: str) -> str:
    """The markup of one table, so an assertion can be about that table alone."""
    start = document.index(f'id="{element_id}"')
    return document[start : document.index("</table>", start)]


def body_rows(document: str, element_id: str) -> int:
    markup = table(document, element_id)
    return markup.partition("<tbody>")[2].count("<tr>")


# --- The tax split ----------------------------------------------------------


def test_an_intra_state_invoice_shows_cgst_and_sgst_and_never_igst() -> None:
    """Same state, so the tax splits in two — and IGST must not appear at all.

    The assertion is against the whole document rather than the line table
    because the split appears in four places: the supply-type caption, the
    line columns, the HSN summary and the totals. Any one of them disagreeing
    with the others produces a document that is internally contradictory, and
    a buyer's accounts department will bounce it before a regulator ever sees
    it.
    """
    document = intra_state_invoice()

    assert "CGST" in document
    assert "SGST" in document
    assert "IGST" not in document
    # 50 x 12.50 at 12% = 75.00 tax, split half and half.
    assert "37.50" in table(document, "lines")


def test_an_inter_state_invoice_shows_igst_and_never_cgst_or_sgst() -> None:
    """Across a state line it is one tax at the full rate, in one column."""
    document = inter_state_invoice()

    assert "IGST" in document
    assert "CGST" not in document
    assert "SGST" not in document
    assert "75.00" in table(document, "lines")


def test_the_supply_type_is_stated_in_words_on_the_face_of_the_invoice() -> None:
    """A reader should not have to infer the split from which columns exist."""
    assert "Intra-state supply" in intra_state_invoice()
    assert "Inter-state supply" in inter_state_invoice()


# --- The statutory fields ---------------------------------------------------


def test_the_invoice_carries_every_particular_the_statute_requires() -> None:
    """Heading, number, date, both parties with GSTIN and state, place of supply.

    Rule 46 lists these. A missing one is not a cosmetic defect: it is what
    makes the buyer unable to claim input credit against the document.
    """
    document = intra_state_invoice()

    assert "TAX INVOICE" in document
    assert "SO-000417" in document
    assert "05 Aug 2026" in document

    parties = table(document, "parties")
    assert "Shree Medicos Distributors Pvt Ltd" in parties
    assert "27AABCS1429B1ZQ" in parties
    assert "Bandra West" in parties
    assert "City General Hospital" in parties
    assert "27AACCC1234D1Z5" in parties
    assert "MH (27)" in parties

    meta = table(document, "invoice-meta")
    assert "Place of Supply" in meta
    assert "Maharashtra (27)" in meta


def test_a_line_prints_the_whole_of_its_arithmetic() -> None:
    """Description, HSN, quantity, rate, taxable value, GST rate and the tax.

    Everything needed to re-derive the line total by hand. An invoice that
    shows only an amount cannot be checked, and being checkable is the point.
    """
    lines = table(intra_state_invoice(), "lines")

    assert "Paracetamol 500mg" in lines
    assert "30049099" in lines
    assert ">50 STRIP<" in lines  # quantity with its unit, not 50.0000
    assert "12.50" in lines  # rate
    assert "625.00" in lines  # taxable value
    assert "12%" in lines  # GST rate, not 12.00%
    assert "700.00" in lines  # line total


def test_an_unregistered_buyer_is_shown_as_absent_not_as_blank() -> None:
    """A customer with no GSTIN is ordinary. A blank cell reads as a mistake."""
    order = make_order(
        [make_line(interstate=False)],
        interstate=False,
        buyer_state="MH",
        buyer_gstin=None,
    )
    document = render_tax_invoice(
        order, seller=SELLER, place_of_supply_name="Maharashtra"
    )
    assert "GSTIN: —" in table(document, "parties")


# --- HSN summary ------------------------------------------------------------


def test_lines_sharing_an_hsn_collapse_into_one_summary_row() -> None:
    """The summary is per HSN, not per line — that is the whole of its job.

    Three lines, two of them the same HSN, must produce two rows, and the
    shared row must carry the sum of both taxable values (625.00 + 250.00).
    """
    document = intra_state_invoice(
        [
            make_line(interstate=False),
            make_line(
                name="Amoxycillin 250mg",
                sku="AMOX-250",
                qty="20",
                price="12.50",
                interstate=False,
            ),
            make_line(
                name="Surgical Gloves",
                sku="GLOVE-M",
                hsn="40151900",
                qty="10",
                price="30",
                rate="18",
                interstate=False,
            ),
        ]
    )

    assert body_rows(document, "lines") == 3
    assert body_rows(document, "hsn-summary") == 2

    summary = table(document, "hsn-summary")
    assert "875.00" in summary  # 625.00 + 250.00 under HSN 30049099
    assert "300.00" in summary  # the gloves, alone under 40151900
    assert "105.00" in summary  # 12% of 875.00, the two lines' tax together


def test_a_line_with_no_hsn_still_appears_in_the_summary() -> None:
    """Grouping on a missing HSN must not quietly drop the value.

    A product whose HSN was never filled in is a data gap, and the summary has
    to show it as one — a row that is short by its taxable value is a summary
    that no longer reconciles to the invoice total.
    """
    document = intra_state_invoice(
        [
            make_line(interstate=False),
            make_line(
                name="Cotton Roll", sku="COT-1", hsn=None, qty="4", price="25",
                interstate=False,
            ),
        ]
    )

    summary = table(document, "hsn-summary")
    assert body_rows(document, "hsn-summary") == 2
    assert "100.00" in summary
    assert "—" in summary


def test_the_hsn_printed_is_the_line_s_own_not_the_product_s() -> None:
    """The line froze the HSN when the order was priced; the product moves on.

    If a pharmacist corrects a product's HSN next month, last month's invoices
    must keep saying what was actually charged. Reading through to the product
    would silently reprint history.
    """
    document = intra_state_invoice(
        [make_line(hsn="30049099", product_hsn="99999999", interstate=False)]
    )

    assert "30049099" in document
    assert "99999999" not in document


# --- Totals and words -------------------------------------------------------


def test_the_foot_of_the_invoice_closes_the_arithmetic() -> None:
    """Taxable value, tax, round off and grand total, all from the document."""
    document = intra_state_invoice()
    totals = table(document, "totals")

    assert "625.00" in totals
    assert "75.00" in totals
    assert "700.00" in totals
    assert "Round Off" in totals


def test_the_grand_total_is_repeated_in_words() -> None:
    document = intra_state_invoice()
    assert "Rupees Seven Hundred Only" in document


def test_amounts_are_grouped_the_indian_way() -> None:
    """12,00,000.00 — not 1,200,000.00. Two-digit groups above the thousand."""
    document = intra_state_invoice(
        [
            make_line(
                name="Saline 500ml", sku="SAL-500", qty="100000", price="12",
                rate="0", interstate=False,
            )
        ]
    )
    assert "12,00,000.00" in document
    assert "1,200,000.00" not in document


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("0"), "Rupees Zero Only"),
        (Decimal("1"), "Rupees One Only"),
        (Decimal("99"), "Rupees Ninety Nine Only"),
        (Decimal("100"), "Rupees One Hundred Only"),
        (Decimal("1000"), "Rupees One Thousand Only"),
        (Decimal("100000"), "Rupees One Lakh Only"),
        (Decimal("10000000"), "Rupees One Crore Only"),
        # Above a crore the Indian scale repeats itself rather than growing a
        # new word — 10^9 is a hundred crore, not a billion.
        (Decimal("1000000000"), "Rupees One Hundred Crore Only"),
        (Decimal("0.50"), "Rupees Zero and Fifty Paise Only"),
        (
            Decimal("1234.56"),
            "Rupees One Thousand Two Hundred Thirty Four and Fifty Six Paise Only",
        ),
        (
            Decimal("99999.99"),
            "Rupees Ninety Nine Thousand Nine Hundred Ninety Nine and "
            "Ninety Nine Paise Only",
        ),
        (
            Decimal("12345678.90"),
            "Rupees One Crore Twenty Three Lakh Forty Five Thousand "
            "Six Hundred Seventy Eight and Ninety Paise Only",
        ),
    ],
)
def test_amount_in_words_speaks_the_indian_scale(
    amount: Decimal, expected: str
) -> None:
    """Every boundary where a scale word appears or disappears.

    These are the values that break a hand-rolled implementation: the empty
    tens place at 100 and 1000, the jump from thousand to lakh at 10^5, the
    first crore, and a paise remainder that must not be swallowed.
    """
    words = amount_in_words(amount)

    assert words == expected
    assert "Million" not in words
    assert "Billion" not in words


def test_the_paise_are_rounded_half_up_like_every_other_amount_here() -> None:
    """Decimal in, ROUND_HALF_UP — the same rule the ledger uses.

    Had this taken a float, 1.005 would have arrived as 1.00499... and printed
    a rupee that the totals table does not agree with.
    """
    assert amount_in_words(Decimal("1.005")) == "Rupees One and One Paisa Only"


def test_a_single_paisa_is_named_in_the_singular() -> None:
    """"One Paise" is not a thing anyone says, and this line is read aloud."""
    assert amount_in_words(Decimal("5.01")) == "Rupees Five and One Paisa Only"
    assert amount_in_words(Decimal("5.02")) == "Rupees Five and Two Paise Only"


# --- Escaping and self-containment -----------------------------------------


def test_a_hostile_product_name_cannot_break_the_document() -> None:
    """Names come from a keyboard, and a keyboard can type a tag.

    "Vitamin D3 <60K> & Calcium" is a plausible product name typed by a
    pharmacist, not an attack — but if it goes onto the page unescaped it
    closes a cell, and the same hole lets a stored script out. Both are the
    one bug, and `html.escape` is the one fix.
    """
    document = intra_state_invoice(
        [
            make_line(
                name='<script>alert("x")</script> Vitamin D3 <60K> & Calcium',
                interstate=False,
            )
        ]
    )

    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "&lt;60K&gt; &amp; Calcium" in document
    # And the table itself survived: the row still has its ten cells.
    assert body_rows(document, "lines") == 1


def test_a_hostile_customer_name_is_escaped_too() -> None:
    """Master data is not more trusted than product data."""
    order = make_order(
        [make_line(interstate=False)],
        interstate=False,
        buyer_state="MH",
        buyer_name='Sharma & Sons <Chemists>',
    )
    document = render_tax_invoice(
        order, seller=SELLER, place_of_supply_name="Maharashtra"
    )

    assert "Sharma &amp; Sons &lt;Chemists&gt;" in document
    assert "<Chemists>" not in document


def test_the_document_stands_alone_and_knows_how_to_print() -> None:
    """One file, no network, and an @media print block that fits A4.

    The pharmacy prints this from a browser on a counter machine that may have
    no internet at all. A stylesheet or font fetched over the wire would print
    as an unstyled column of numbers exactly when it matters.
    """
    document = intra_state_invoice()

    assert document.startswith("<!doctype html>")
    assert "<style>" in document
    assert "@media print" in document
    assert "@page { size: A4;" in document
    assert "http://" not in document
    assert "https://" not in document
    assert "<link" not in document
    assert "<script" not in document


# --- Rule 46 particulars added after the first pass ---------------------------


def test_each_line_prints_its_unit_beside_the_quantity() -> None:
    """Rule 46(g): quantity *and* the unit it is counted in.

    "50" alone is ambiguous on a pharmacy invoice, where the same product is
    ordered by the strip, the box and the bottle.
    """
    assert "50 STRIP" in table(intra_state_invoice(), "lines")


def test_a_product_with_no_unit_still_prints_its_quantity() -> None:
    """A defective row beats a 500 on a document someone is waiting to print."""
    line = make_line(interstate=False)
    line.product.uom = None

    assert "50" in table(intra_state_invoice([line]), "lines")


def test_the_reverse_charge_position_is_stated_either_way() -> None:
    """Rule 46(p). Its absence leaves the buyer guessing who owes the tax."""
    assert "reverse charge basis: No" in intra_state_invoice()


def test_a_union_territory_levies_utgst_and_the_invoice_says_so() -> None:
    """Chandigarh has no legislature, so the second half is UTGST, not SGST.

    Same rate, same amount, same column in the database — only the name of the
    tax differs, which is exactly why it goes unnoticed.
    """
    seller = StubParty(
        name="Shree Medicos Distributors Pvt Ltd",
        address="SCO 42, Sector 17\nChandigarh 160017",
        gstin="04AABCS1429B1ZQ",
        state_code="CH",
    )
    order = make_order(
        [make_line(interstate=False)], interstate=False, buyer_state="CH"
    )

    document = render_tax_invoice(
        order, seller=seller, place_of_supply_name="Chandigarh"
    )

    assert "UTGST" in document
    assert "CGST" in document
    # Not ">SGST<" — "UTGST" contains "SGST" as a substring, so a bare `in`
    # check would pass on the very output this test exists to distinguish.
    assert ">SGST<" not in document
    assert "Intra-state supply — CGST + UTGST" in document


def test_a_state_with_a_legislature_still_levies_sgst() -> None:
    """Delhi and Puducherry are union territories that are not UTGST cases."""
    document = intra_state_invoice()

    assert ">SGST<" in document
    assert "UTGST" not in document
