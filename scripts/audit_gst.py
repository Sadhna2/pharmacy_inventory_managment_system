"""Recompute every tax figure in the database and compare it to what is stored.

Read-only. Nothing here writes, so it is safe against the demo database.

The point is not to re-run the same function and get the same answer — that
proves nothing. Each check below is an independent statement about what the
number ought to be, derived from the GST rules rather than from the code that
produced it, so a wrong implementation and a wrong check would have to agree
by coincidence.
"""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.session import SessionLocal
from app.models.documents import SalesOrder, SalesOrderLine
from app.services import gst

P = Decimal("0.01")


def r(x) -> Decimal:
    return Decimal(x).quantize(P, rounding=ROUND_HALF_UP)


findings: list[str] = []
counts: dict[str, int] = defaultdict(int)


def check(ok: bool, key: str, detail: str) -> None:
    counts[key] += 1
    if not ok:
        findings.append(f"[{key}] {detail}")


with SessionLocal() as db:
    orders = db.scalars(
        select(SalesOrder).options(
            selectinload(SalesOrder.lines).selectinload(SalesOrderLine.product),
            selectinload(SalesOrder.customer),
            selectinload(SalesOrder.warehouse),
        )
    ).all()
    print(f"sales orders: {len(orders):,}")

    for so in orders:
        tag = so.so_number

        # 1. The split is decided by where the goods are and where the buyer
        #    is, and by nothing else. Recomputed from the two state codes
        #    rather than trusting the stored flag.
        expect_inter = gst.is_interstate(
            so.warehouse.state_code, so.customer.state_code
        )
        check(
            expect_inter == so.is_interstate,
            "interstate-flag",
            f"{tag}: {so.warehouse.state_code}->{so.customer.state_code} "
            f"should be interstate={expect_inter}, stored {so.is_interstate}",
        )

        # 2. Place of supply is the buyer's state. Getting this wrong moves
        #    the tax to the wrong government.
        check(
            so.place_of_supply == so.customer.state_code,
            "place-of-supply",
            f"{tag}: place_of_supply={so.place_of_supply} but the buyer is in "
            f"{so.customer.state_code}",
        )

        sub = Decimal("0.00")
        tax = Decimal("0.00")

        for ln in so.lines:
            lt = f"{tag} line {ln.id}"
            # 3. Taxable value is qty x price, rounded once. There is no
            #    discount column on a sales order line, so nothing to net
            #    off — see the short-shipment check below for the case
            #    where the figure could legitimately have moved.
            taxable = r(Decimal(ln.qty_ordered) * Decimal(ln.unit_price))
            check(
                r(ln.taxable_value) == taxable,
                "taxable-value",
                f"{lt}: stored {ln.taxable_value}, recomputed {taxable}",
            )

            total_tax = r(taxable * Decimal(ln.gst_rate) / 100)
            c, s, i = (
                Decimal(ln.cgst_amount),
                Decimal(ln.sgst_amount),
                Decimal(ln.igst_amount),
            )

            # 4. The three components sum to the tax the rate implies.
            check(
                r(c + s + i) == total_tax,
                "tax-total-per-line",
                f"{lt}: cgst+sgst+igst={r(c + s + i)}, rate implies {total_tax}",
            )

            # 5. Exactly one regime applies. A line carrying both is a line
            #    that would be taxed twice by two different governments.
            if so.is_interstate:
                check(
                    c == 0 and s == 0 and i == total_tax,
                    "regime",
                    f"{lt}: interstate but cgst={c} sgst={s} igst={i}",
                )
            else:
                check(
                    i == 0 and r(c + s) == total_tax,
                    "regime",
                    f"{lt}: intrastate but igst={i} (cgst={c} sgst={s})",
                )
                # 6. Central and state halves must not differ by more than the
                #    odd paisa an odd total forces.
                check(
                    abs(c - s) <= P,
                    "half-split",
                    f"{lt}: cgst={c} and sgst={s} differ by more than a paisa",
                )

            # 7. Line total is taxable plus its own tax.
            check(
                r(ln.line_total) == r(taxable + total_tax),
                "line-total",
                f"{lt}: stored {ln.line_total}, recomputed {taxable + total_tax}",
            )

            # 8. The HSN on the line is frozen at pricing time, but it still
            #    has to be a plausible code — 4, 6 or 8 digits.
            if ln.hsn_code:
                check(
                    ln.hsn_code.isdigit() and len(ln.hsn_code) in (4, 6, 8),
                    "hsn-shape",
                    f"{lt}: hsn_code={ln.hsn_code!r}",
                )

            # 9. Short shipment. Tax was computed on qty_ordered at raise
            #    time; if less was actually shipped and the figures were never
            #    revised, the buyer is invoiced for goods that never arrived.
            if ln.qty_shipped is not None and Decimal(ln.qty_shipped) not in (
                Decimal(0),
                Decimal(ln.qty_ordered),
            ):
                check(
                    False,
                    "short-shipment-not-repriced",
                    f"{lt}: ordered {ln.qty_ordered}, shipped {ln.qty_shipped}, "
                    f"still taxed on {ln.taxable_value}",
                )

            sub += taxable
            tax += total_tax

        if not so.lines:
            continue

        # 9. Document totals: summed from the lines, rounded once at the end.
        check(
            r(so.subtotal) == r(sub),
            "doc-subtotal",
            f"{tag}: stored {so.subtotal}, lines sum to {r(sub)}",
        )
        check(
            r(so.tax_total) == r(tax),
            "doc-tax-total",
            f"{tag}: stored {so.tax_total}, lines sum to {r(tax)}",
        )

        exact = sub + tax
        grand = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        check(
            Decimal(so.grand_total) == grand,
            "doc-grand-total",
            f"{tag}: stored {so.grand_total}, recomputed {grand}",
        )
        check(
            r(so.round_off) == r(grand - exact),
            "doc-round-off",
            f"{tag}: stored {so.round_off}, recomputed {r(grand - exact)}",
        )

        # 10. The identity the customer checks with a calculator.
        check(
            r(Decimal(so.subtotal) + Decimal(so.tax_total) + Decimal(so.round_off))
            == Decimal(so.grand_total),
            "doc-identity",
            f"{tag}: {so.subtotal} + {so.tax_total} + {so.round_off} "
            f"!= {so.grand_total}",
        )

        # 11. Rounding to the rupee can never move the total by half a rupee
        #     or more. A bigger round_off means something else is hiding in it.
        check(
            Decimal("-0.50") < Decimal(so.round_off) <= Decimal("0.50"),
            "round-off-magnitude",
            f"{tag}: round_off={so.round_off}",
        )

print()
for key in sorted(counts):
    bad = sum(1 for f in findings if f.startswith(f"[{key}]"))
    mark = "FAIL" if bad else "ok  "
    print(f"  {mark}  {key:24} {counts[key]:>6,} checked, {bad} bad")

print(f"\n{len(findings)} findings")
for f in findings[:25]:
    print("   ", f)
if len(findings) > 25:
    print(f"    ... and {len(findings) - 25} more")
