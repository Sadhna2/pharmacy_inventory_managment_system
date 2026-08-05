"""Indian GST computation (ARCHITECTURE.md §6.8).

The rule is simple and statutory:
    same state  -> CGST + SGST, half the rate each
    other state -> IGST at the full rate

All money is Decimal. Never float — a rounding drift of a paisa per line
becomes a reconciliation problem at scale.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")

#: 2 digits of state, the holder's PAN, entity number, a literal Z, check char.
GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class TaxBreakdown:
    taxable_value: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal

    @property
    def tax_total(self) -> Decimal:
        return self.cgst_amount + self.sgst_amount + self.igst_amount


def is_interstate(supply_state: str, recipient_state: str) -> bool:
    """Place of supply decides the split. Comparison is case-insensitive."""
    return supply_state.strip().upper() != recipient_state.strip().upper()


#: The statutory GST state codes, keyed by the two-letter code this system
#: stores on a warehouse, supplier or customer.
#:
#: The two live side by side because they answer to different masters. Every
#: `state_code` column here holds the postal abbreviation, which is what people
#: type and read; a GSTIN's first two characters are a *numeric* code from the
#: same statute. Nothing converts between them until a GSTIN arrives from
#: outside — which happens in two places now: `ai.intake.service`, when a
#: photographed invoice is read, and a warehouse's own registration.
#:
#: Several rows are duplicates by design, because a state can answer to more
#: than one abbreviation and a record may hold either — `UT`/`UK`, `TG`/`TS`,
#: `OR`/`OD`, `CT`/`CG`, and `DD`/`DN`/`DH` for the territory those two were
#: merged into. `DH` is the one the web picker offers, and it was missing.
STATE_CODES: dict[str, str] = {
    "JK": "01", "HP": "02", "PB": "03", "CH": "04", "UT": "05", "UK": "05",
    "HR": "06", "DL": "07", "RJ": "08", "UP": "09", "BR": "10", "SK": "11",
    "AR": "12", "NL": "13", "MN": "14", "MZ": "15", "TR": "16", "ML": "17",
    "AS": "18", "WB": "19", "JH": "20", "OR": "21", "OD": "21", "CT": "22",
    "CG": "22", "MP": "23", "GJ": "24", "DD": "26", "DN": "26", "DH": "26",
    "MH": "27", "KA": "29", "GA": "30", "LD": "31", "KL": "32", "TN": "33",
    "PY": "34", "AN": "35", "TG": "36", "TS": "36", "AP": "37", "LA": "38",
}


def gstin_prefix_for_state(state_code: str) -> str | None:
    """The numeric GSTIN prefix for a two-letter state code, or None.

    This direction, not the reverse, because several states answer to two
    abbreviations — `UT` and `UK` are both 05, `TG` and `TS` both 36 — so
    translating a number back to letters would have to pick one and would
    then disagree with a record that happened to store the other.

    None means the code is not a state this table knows. The caller must treat
    that as "unknown" and check nothing, never as "a different state":
    inventing a tax finding out of an unrecognised code is how a validator
    starts lying.
    """
    return STATE_CODES.get(state_code.strip().upper())


def gstin_check_digit(first_fourteen: str) -> str | None:
    """The fifteenth character of a GSTIN, computed from the first fourteen.

    GSTIN carries a mod-36 checksum, which makes it one of the few identifiers
    in the system that can be verified outright rather than merely looked
    plausible. Weights alternate 1, 2 across the payload; each product is
    folded by quotient plus remainder over 36.
    """
    payload = first_fourteen.upper()
    if len(payload) != 14 or any(c not in _GSTIN_ALPHABET for c in payload):
        return None
    total = 0
    for i, ch in enumerate(payload):
        product = _GSTIN_ALPHABET.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _GSTIN_ALPHABET[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str) -> bool:
    code = (gstin or "").strip().upper()
    if not GSTIN_SHAPE.match(code):
        return False
    return gstin_check_digit(code[:14]) == code[14]


def gstin_state_matches(gstin: str, state_code: str) -> bool | None:
    """Do a GSTIN's opening two digits agree with a two-letter state code?

    None when there is nothing to compare — a blank GSTIN, or a state this
    system does not recognise. The caller must read that as "unknown" and not
    as disagreement: refusing a record because a lookup table is incomplete
    would be the validator inventing a finding.
    """
    code = (gstin or "").strip().upper()
    expected = gstin_prefix_for_state(state_code or "")
    if not code or expected is None:
        return None
    return code[:2] == expected


def compute_line_tax(
    *,
    quantity: Decimal,
    unit_price: Decimal,
    gst_rate: Decimal,
    interstate: bool,
    discount: Decimal = Decimal("0"),
) -> TaxBreakdown:
    taxable = _round(Decimal(quantity) * Decimal(unit_price) - Decimal(discount))
    if taxable < 0:
        taxable = Decimal("0.00")

    rate = Decimal(gst_rate)
    total_tax = _round(taxable * rate / Decimal("100"))

    if interstate:
        cgst = sgst = Decimal("0.00")
        igst = total_tax
    else:
        # Half each. Split so the two halves always sum to total_tax exactly,
        # even when total_tax has an odd final paisa.
        cgst = _round(total_tax / Decimal("2"))
        sgst = total_tax - cgst
        igst = Decimal("0.00")

    return TaxBreakdown(
        taxable_value=taxable,
        gst_rate=rate,
        cgst_amount=cgst,
        sgst_amount=sgst,
        igst_amount=igst,
        line_total=taxable + total_tax,
    )


@dataclass
class DocumentTotals:
    subtotal: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal


def compute_document_totals(lines: list[TaxBreakdown]) -> DocumentTotals:
    """Sum lines, then round ONCE at the document level.

    Rounding per line and again at the total is how invoices end up a rupee
    off from the customer's own arithmetic.
    """
    subtotal = sum((line.taxable_value for line in lines), Decimal("0.00"))
    tax_total = sum((line.tax_total for line in lines), Decimal("0.00"))
    exact = subtotal + tax_total

    grand_total = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    round_off = _round(grand_total - exact)

    return DocumentTotals(
        subtotal=_round(subtotal),
        tax_total=_round(tax_total),
        round_off=round_off,
        grand_total=grand_total,
    )
