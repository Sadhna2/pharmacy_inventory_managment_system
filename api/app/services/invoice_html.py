"""A GST tax invoice, rendered as a print-ready HTML document.

HTML rather than a PDF, deliberately. `reportlab` and `weasyprint` are the two
obvious answers and neither is a dependency here: this deploys to a 2 GB ARM64
box, weasyprint alone drags in Pango and Cairo, and every browser already ships
a print-to-PDF engine that paginates a long table better than we would. The
pharmacist presses Ctrl+P and the file that comes out is the file the auditor
asked for.

The renderer is a pure function of what it is handed — no session, no network,
no clock — so it can be tested against stubs, and so a wrong invoice is always
reproducible from the rows it was made from.

Every value that arrives from the database goes through `_esc()`. "Vitamin D3
<60K> & Calcium" is an ordinary product name, and it must not be able to close
a tag.
"""

import html
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Protocol

from app.services import gst

# Type-only. The renderer reads attributes and never touches the ORM, so
# keeping the import out of the runtime path means this module — and its
# tests — load without the mapper registry being configured at all.
if TYPE_CHECKING:
    from app.models.documents import SalesOrder, SalesOrderLine

TWO_PLACES = Decimal("0.01")


class Party(Protocol):
    """The two ends of a supply, as an invoice has to print them.

    `Customer` already satisfies this. The seller does not exist as a table —
    the firm's own GSTIN belongs to the installation, not to a warehouse — so
    the caller composes the seller and this states what it must carry.
    """

    name: str
    address: str | None
    gstin: str | None
    state_code: str

    #: How to reach this party about the document. Not required by rule 46 —
    #: which asks for the name, address and GSTIN and stops there — but an
    #: invoice is also the thing somebody rings about when a carton is short,
    #: and a document that names no way to do that gets a phone call to
    #: whoever answered last time. `Customer` already carries both.
    phone: str | None
    email: str | None


#: Print styling only. Black on white, no fills, no webfonts: this is a
#: document that gets photocopied and faxed, not a screen.
_STYLE = """\
@page { size: A4; margin: 12mm; }
body {
  font: 10pt/1.45 "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: #000; background: #fff; margin: 12mm; max-width: 186mm;
}
h1.doc-title {
  font-size: 15pt; letter-spacing: 0.12em; text-align: center;
  margin: 0 0 8px;
}
table { width: 100%; border-collapse: collapse; margin-bottom: 10px; }
th, td {
  border: 1px solid #000; padding: 4px 6px; text-align: left;
  vertical-align: top;
}
th { font-weight: 700; }
td.num, th.num { text-align: right; white-space: nowrap; }
table.meta th { width: 15%; }
table.parties td { width: 50%; }
/* Ten columns have to sit inside 186mm of A4, and the widest of them is a
   money value that must not wrap. The line table therefore prints a size
   smaller than the rest of the document. */
table.lines, table.hsn { font-size: 9pt; }
table.lines th, table.lines td { padding: 3px 5px; }
table.lines td:nth-child(2) { width: 26%; }
span.sub { display: block; font-size: 8.5pt; color: #333; }
p.caption { font-weight: 700; margin: 14px 0 4px; }
table.totals { width: 62%; margin-left: auto; }
table.totals th { width: 60%; }
tr.grand th, tr.grand td { font-weight: 700; }
p.words { margin: 0 0 14px; }
div.sign { margin-top: 24px; text-align: right; }
div.sign span { display: block; }
p.note { font-size: 8.5pt; color: #333; text-align: center; margin: 18px 0 0; }
@media print {
  body { margin: 0; }
  thead { display: table-header-group; }
  tr { page-break-inside: avoid; }
  div.sign { page-break-inside: avoid; }
}
"""


def render_tax_invoice(
    order: "SalesOrder",
    *,
    seller: Party,
    place_of_supply_name: str,
) -> str:
    """One sales order as a complete, self-contained HTML tax invoice.

    `place_of_supply_name` is passed in rather than looked up because the only
    state table this system has maps a two-letter code to a GSTIN prefix and
    refuses to go back the other way (see `services/gst.py`) — several states
    answer to two abbreviations, so a reverse lookup would have to guess.
    """
    # The column set is decided once, from the document's own flag, and never
    # per line. Reading it off the amounts instead would break on a zero-rated
    # line — CGST, SGST and IGST are all zero there, so the columns would flip
    # halfway down the table — and an invoice showing both splits at once is
    # not a valid tax invoice under any reading of the statute.
    interstate = bool(order.is_interstate)
    state_tax = _state_tax_name(seller.state_code)

    body = "\n".join(
        [
            _heading(
                order,
                place_of_supply_name=place_of_supply_name,
                interstate=interstate,
                state_tax=state_tax,
            ),
            _parties(order, seller=seller),
            _line_table(order.lines, interstate=interstate, state_tax=state_tax),
            _hsn_summary(order.lines, interstate=interstate, state_tax=state_tax),
            _totals(order, interstate=interstate, state_tax=state_tax),
            _signature(seller),
        ]
    )
    title = f"Tax Invoice {_esc(order.so_number)}"
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        f"<style>\n{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


#: The union territories that levy UTGST rather than SGST — the ones with no
#: legislature of their own. Delhi, Puducherry and Jammu & Kashmir are absent
#: on purpose: each has a legislature, so each levies SGST like any state.
#:
#: This changes the caption and nothing else. The rate, the amount and the
#: column it is stored in are all identical, which is exactly why it is easy to
#: get wrong and why it is worth getting right: an invoice raised in Chandigarh
#: that says SGST names a tax that was not levied on it.
_UTGST_TERRITORIES = frozenset({"CH", "LD", "AN", "DD", "DN", "LA"})


def _state_tax_name(state_code: str | None) -> str:
    """"SGST" or "UTGST" — whichever the supplier's own place levies."""
    if state_code and state_code.strip().upper() in _UTGST_TERRITORIES:
        return "UTGST"
    return "SGST"


def amount_in_words(amount: Decimal) -> str:
    """"1,23,456.78" spoken the way an Indian invoice says it.

    Lakh and crore, never million: an invoice that reads "One Million" is one a
    buyer's accounts department will send back. Written here rather than taken
    from a package because the whole of it is forty lines and the packages that
    do it either speak the western scale or arrive with their own dependencies.
    """
    value = Decimal(amount).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if value < 0:
        # Nothing in this system should reach here — a grand total is never
        # negative — but printing "Minus" beats printing nonsense if it does.
        return f"Minus {amount_in_words(-value)}"

    rupees = int(value)
    paise = int((value - rupees) * 100)
    words = _rupees_in_words(rupees)
    if paise:
        # "One Paisa", not "One Paise" — the singular is a different word, and
        # this line is read aloud across a counter often enough to be noticed.
        unit = "Paisa" if paise == 1 else "Paise"
        return f"Rupees {words} and {_below_hundred(paise)} {unit} Only"
    return f"Rupees {words} Only"


# --- Sections ---------------------------------------------------------------


def _heading(
    order: "SalesOrder",
    *,
    place_of_supply_name: str,
    interstate: bool,
    state_tax: str,
) -> str:
    supply_type = (
        "Inter-state supply — IGST"
        if interstate
        else f"Intra-state supply — CGST + {state_tax}"
    )
    return (
        # The heading is spelled out in the markup rather than uppercased in
        # CSS: it is a statutory caption, and it has to survive a copy-paste
        # and a stylesheet that failed to load.
        '<h1 class="doc-title">TAX INVOICE</h1>\n'
        '<table class="meta" id="invoice-meta">\n'
        "<tr>"
        f"<th>Invoice No.</th><td>{_esc(order.so_number)}</td>"
        f"<th>Invoice Date</th><td>{_date(order.order_date)}</td>"
        "</tr>\n"
        "<tr>"
        "<th>Place of Supply</th>"
        f"<td>{_place_of_supply(order, place_of_supply_name)}</td>"
        f"<th>Supply Type</th><td>{_esc(supply_type)}</td>"
        "</tr>\n"
        "</table>"
    )


def _parties(order: "SalesOrder", *, seller: Party) -> str:
    return (
        '<table class="parties" id="parties">\n'
        "<tr><th>Seller</th><th>Buyer</th></tr>\n"
        f"<tr><td>{_party_block(seller)}</td>"
        f"<td>{_party_block(order.customer)}</td></tr>\n"
        "</table>"
    )


def _party_block(party: Party) -> str:
    # Contact lines are omitted rather than printed empty. An invoice showing
    # "Phone: —" tells the reader the number is unknown to the system, which is
    # information nobody needs on a tax document; no line at all reads as the
    # firm simply not putting one there.
    contact = "".join(
        f'<span class="sub">{label}: {_esc(value)}</span>'
        for label, value in (
            ("Phone", (getattr(party, "phone", None) or "").strip()),
            ("Email", (getattr(party, "email", None) or "").strip()),
        )
        if value
    )
    return (
        f"<strong>{_esc(party.name)}</strong>"
        f'<span class="sub">{_address(party.address)}</span>'
        f'<span class="sub">GSTIN: {_esc(party.gstin)}</span>'
        f'<span class="sub">State: {_state(party.state_code)}</span>'
        f"{contact}"
    )


def _line_table(
    lines: Iterable["SalesOrderLine"], *, interstate: bool, state_tax: str
) -> str:
    tax_headers = (
        '<th class="num">IGST</th>'
        if interstate
        else f'<th class="num">CGST</th><th class="num">{state_tax}</th>'
    )
    rows = []
    for number, line in enumerate(lines, start=1):
        tax_cells = (
            f'<td class="num">{_money(line.igst_amount)}</td>'
            if interstate
            else f'<td class="num">{_money(line.cgst_amount)}</td>'
            f'<td class="num">{_money(line.sgst_amount)}</td>'
        )
        rows.append(
            "<tr>"
            f'<td class="num">{number}</td>'
            f"<td>{_description(line)}</td>"
            f"<td>{_esc(line.hsn_code)}</td>"
            f'<td class="num">{_qty_with_unit(line)}</td>'
            f'<td class="num">{_unit_rate(line.unit_price)}</td>'
            f'<td class="num">{_money(line.taxable_value)}</td>'
            f'<td class="num">{_percent(line.gst_rate)}</td>'
            f"{tax_cells}"
            f'<td class="num">{_money(line.line_total)}</td>'
            "</tr>"
        )
    return (
        '<table class="lines" id="lines">\n'
        "<thead><tr>"
        '<th class="num">#</th><th>Description</th><th>HSN</th>'
        '<th class="num">Qty</th><th class="num">Rate</th>'
        '<th class="num">Taxable Value</th><th class="num">GST %</th>'
        f"{tax_headers}"
        '<th class="num">Amount</th>'
        "</tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody>\n"
        "</table>"
    )


def _hsn_summary(
    lines: Iterable["SalesOrderLine"], *, interstate: bool, state_tax: str
) -> str:
    """The HSN-wise summary the statute wants at the foot of the invoice."""
    tax_headers = (
        '<th class="num">IGST</th>'
        if interstate
        else f'<th class="num">CGST</th><th class="num">{state_tax}</th>'
    )
    rows = []
    for group in _group_by_hsn(lines):
        tax_cells = (
            f'<td class="num">{_money(group.igst)}</td>'
            if interstate
            else f'<td class="num">{_money(group.cgst)}</td>'
            f'<td class="num">{_money(group.sgst)}</td>'
        )
        rows.append(
            "<tr>"
            f"<td>{_esc(group.hsn)}</td>"
            # The rate is printed because the row is keyed on it: without the
            # column, two rows under one HSN would look like a duplicate.
            f'<td class="num">{_percent(group.gst_rate)}</td>'
            f'<td class="num">{_money(group.taxable_value)}</td>'
            f"{tax_cells}"
            f'<td class="num">{_money(group.tax_total)}</td>'
            "</tr>"
        )
    return (
        '<p class="caption">HSN-wise summary</p>\n'
        '<table class="hsn" id="hsn-summary">\n'
        "<thead><tr>"
        '<th>HSN</th><th class="num">Rate</th>'
        '<th class="num">Taxable Value</th>'
        f"{tax_headers}"
        '<th class="num">Total Tax</th>'
        "</tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody>\n"
        "</table>"
    )


def _totals(order: "SalesOrder", *, interstate: bool, state_tax: str) -> str:
    if interstate:
        igst = _sum(order.lines, "igst_amount")
        tax_rows = f'<tr><th>IGST</th><td class="num">{_money(igst)}</td></tr>\n'
    else:
        cgst = _sum(order.lines, "cgst_amount")
        sgst = _sum(order.lines, "sgst_amount")
        tax_rows = (
            f'<tr><th>CGST</th><td class="num">{_money(cgst)}</td></tr>\n'
            f'<tr><th>{state_tax}</th><td class="num">{_money(sgst)}</td></tr>\n'
        )
    return (
        '<table class="totals" id="totals">\n'
        f'<tr><th>Taxable Value</th><td class="num">{_money(order.subtotal)}</td></tr>\n'
        f"{tax_rows}"
        f'<tr><th>Total Tax</th><td class="num">{_money(order.tax_total)}</td></tr>\n'
        f'<tr><th>Round Off</th><td class="num">{_money(order.round_off)}</td></tr>\n'
        '<tr class="grand"><th>Grand Total</th>'
        f'<td class="num">{_money(order.grand_total)}</td></tr>\n'
        "</table>\n"
        '<p class="words" id="amount-in-words">Amount chargeable (in words): '
        f"{_esc(amount_in_words(order.grand_total))}</p>"
    )


def _signature(seller: Party) -> str:
    return (
        # Rule 46(p) wants the reverse-charge position stated, and stated
        # either way — "No" is as much a required particular as "Yes", because
        # its absence leaves the buyer's accounts department to guess who owes
        # the tax. It is always No here: nothing this system sells is a
        # notified reverse-charge supply, and it has no way to mark one, so
        # printing the answer it can actually stand behind beats a blank.
        '<p class="note" id="reverse-charge">'
        "Tax payable on reverse charge basis: No</p>\n"
        '<div class="sign">\n'
        f"<span>For {_esc(seller.name)}</span>\n"
        "<span>Authorised Signatory</span>\n"
        "</div>\n"
        '<p class="note">This is a computer-generated tax invoice.</p>'
    )


# --- HSN grouping -----------------------------------------------------------


@dataclass
class _HsnGroup:
    """One row of the HSN-wise summary."""

    hsn: str | None
    gst_rate: Decimal = Decimal("0")
    taxable_value: Decimal = Decimal("0")
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")

    @property
    def tax_total(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


def _group_by_hsn(lines: Iterable["SalesOrderLine"]) -> list[_HsnGroup]:
    """Sum the lines per HSN *and rate*, in the order they first appear.

    Keyed on the pair, not the code alone. HSN 3004 covers both standard and
    concessional-rate formulations, so one invoice can carry 12% and 5% under
    the same code — and summing those together produces a row whose tax over
    its taxable value is 8.5%, which is not a GST rate and never has been.
    Nobody reading it could reconcile it to anything. The GSTR-1 HSN table is
    keyed on code and rate for exactly this reason.

    First appearance rather than sorted, because the summary is checked by
    reading it against the lines above it and that is easiest when the two run
    in the same order. Lines with no HSN collapse into a single row rather than
    one row each — the table is per HSN, and "none" is one of them.
    """
    groups: dict[tuple[str, Decimal], _HsnGroup] = {}
    for line in lines:
        rate = Decimal(line.gst_rate)
        key = ((line.hsn_code or "").strip(), rate)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _HsnGroup(hsn=key[0] or None, gst_rate=rate)
        group.taxable_value += Decimal(line.taxable_value)
        group.cgst += Decimal(line.cgst_amount)
        group.sgst += Decimal(line.sgst_amount)
        group.igst += Decimal(line.igst_amount)
    return list(groups.values())


# --- Words ------------------------------------------------------------------

_ONES = (
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
)
_TENS = (
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty",
    "Ninety",
)


def _below_hundred(value: int) -> str:
    if value < 20:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    return f"{_TENS[tens]} {_ONES[ones]}" if ones else _TENS[tens]


def _below_thousand(value: int) -> str:
    hundreds, rest = divmod(value, 100)
    if not hundreds:
        return _below_hundred(rest)
    words = f"{_ONES[hundreds]} Hundred"
    return f"{words} {_below_hundred(rest)}" if rest else words


def _rupees_in_words(value: int) -> str:
    if value == 0:
        return "Zero"

    crore, rest = divmod(value, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, rest = divmod(rest, 1_000)

    groups = []
    if crore:
        # Recursive, because above a crore the Indian scale repeats itself:
        # 10^9 is "One Hundred Crore" and 10^12 is "One Lakh Crore".
        groups.append(f"{_rupees_in_words(crore)} Crore")
    if lakh:
        groups.append(f"{_below_hundred(lakh)} Lakh")
    if thousand:
        groups.append(f"{_below_hundred(thousand)} Thousand")
    if rest:
        groups.append(_below_thousand(rest))
    return " ".join(groups)


# --- Value formatting -------------------------------------------------------


def _esc(value: object) -> str:
    """The only way a database value is allowed onto the page.

    An em dash for absent, because a blank cell on a printed invoice reads as
    something that fell off rather than something that was never there.
    """
    if value is None or value == "":
        return "—"
    return html.escape(str(value))


def _address(address: str | None) -> str:
    """A multi-line address, escaped first and only then broken into lines."""
    if not address:
        return "—"
    return "<br>".join(html.escape(line) for line in address.splitlines() if line)


def _state(state_code: str | None) -> str:
    """"MH (27)" — the postal code people read, the numeric one GST wants."""
    if not state_code:
        return "—"
    numeric = gst.gstin_prefix_for_state(state_code)
    text = state_code.strip().upper()
    return _esc(f"{text} ({numeric})" if numeric else text)


def _place_of_supply(order: "SalesOrder", place_of_supply_name: str) -> str:
    numeric = (
        gst.gstin_prefix_for_state(order.place_of_supply)
        if order.place_of_supply
        else None
    )
    if numeric:
        return _esc(f"{place_of_supply_name} ({numeric})")
    return _esc(place_of_supply_name)


def _qty_with_unit(line: "SalesOrderLine") -> str:
    """"10 STRIP" — rule 46(g) asks for the quantity *and* its unit.

    The unit printed is the one this system records against the product, not a
    Unique Quantity Code: the statutory UQC list has no entry for a strip, and
    guessing at PAC or NOS on the invoice would print a classification nobody
    here decided. 46(g) reads "unit or Unique Quantity Code thereof", so the
    unit satisfies it. UQC is separately mandatory on the GSTR-1 return, and
    that mapping belongs with the return, which this system does not file.

    A product with no unit loaded falls back to the bare number rather than
    breaking the row — an invoice missing a unit is a defect; a 500 is worse.
    """
    quantity = _qty(line.qty_ordered)
    uom = getattr(getattr(line.product, "uom", None), "code", None)
    return f"{quantity} {_esc(uom)}" if uom else quantity


def _description(line: "SalesOrderLine") -> str:
    product = line.product
    description = f"<strong>{_esc(product.name)}</strong>"
    details = [detail for detail in (product.sku, product.pack_size) if detail]
    if details:
        joined = " · ".join(str(detail) for detail in details)
        description += f'<span class="sub">{_esc(joined)}</span>'
    return description


def _group_indian(whole: str) -> str:
    """12345678 -> 1,23,45,678. Last three, then twos."""
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    groups.append(tail)
    return ",".join(groups)


def _money(value: Decimal | None) -> str:
    """Two decimals, grouped the Indian way: 12,34,567.89."""
    if value is None:
        return "—"
    amount = Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    return f"{sign}{_group_indian(whole)}.{fraction}"


def _unit_rate(value: Decimal | None) -> str:
    """The rate at the scale it is actually stored, not rounded to two places.

    `unit_price` is NUMERIC(18,4), and rounding it for display breaks the one
    check a person genuinely performs on an invoice line: quantity times rate
    against the taxable value. At a stored 88.0625, ten units are taxed on
    880.63 while ten times a printed 88.06 is 880.60 — three paise, which is
    plenty for a buyer's accounts department to bounce the document.

    Trailing zeros beyond two places are trimmed, so the ordinary case still
    reads as ordinary money and only a genuinely finer rate takes more room.
    """
    if value is None:
        return "—"
    amount = Decimal(value)
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):f}".partition(".")
    # Never round: this value is printed so that quantity times it reconciles
    # to the taxable value beside it. Pad to two places, keep any beyond that
    # which carry information, drop the rest.
    fraction = (fraction.rstrip("0") or "").ljust(2, "0")
    return f"{sign}{_group_indian(whole)}.{fraction}"


def _qty(value: Decimal) -> str:
    """50.0000 prints as 50; 2.5000 as 2.5. Stored scale is not information."""
    text = f"{Decimal(value):f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _percent(value: Decimal) -> str:
    return f"{_qty(value)}%"


def _date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def _sum(lines: Iterable["SalesOrderLine"], attribute: str) -> Decimal:
    return sum(
        (Decimal(getattr(line, attribute)) for line in lines), Decimal("0")
    )
