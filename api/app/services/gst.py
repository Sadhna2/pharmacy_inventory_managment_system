"""Indian GST computation (ARCHITECTURE.md §6.8).

The rule is simple and statutory:
    same state  -> CGST + SGST, half the rate each
    other state -> IGST at the full rate

All money is Decimal. Never float — a rounding drift of a paisa per line
becomes a reconciliation problem at scale.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


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
