"""Checking an extracted invoice against itself.

A supplier invoice is an *over-determined* document. The line amounts have to
add up to the footer, the tax has to follow from the taxable value, the rate
can never exceed the printed MRP, and an expiry is always a month end. None of
that is a convention we invented — it is what the document already promises,
and GST law requires most of it.

That redundancy is the whole opportunity. A vision model reading the page can
misread a digit, but it cannot misread a digit *and* have the arithmetic still
close. So the extraction gets checked against the document's own internal
identities before a human ever sees it, and the residual is flagged rather than
silently accepted.

WHY THIS MODULE CONTAINS NO AI
------------------------------
The model's job is transcription and nothing else. Every judgement about
whether a transcription is trustworthy is made here, by ordinary arithmetic and
regular expressions, which are deterministic and unit-testable. A hallucination
cannot survive this layer unless it happens to satisfy seven independent
constraints simultaneously — and if it does, it is arithmetically consistent
with the paper, which is the most anyone can ask of a reading.

The consequence worth stating plainly: this file, not the model, is where the
accuracy claim lives.

MEASURED (bench/ocr, 50 invoices, 462 line items)
-------------------------------------------------
Every identity below holds on 100% of ground truth, so a violation is evidence
about the extraction rather than about the rules:

    line amount        <= qty x rate               462/462
    sum(line taxable)  == header taxable            50/50
    sum(line tax)      == cgst + sgst + igst        50/50
    taxable + cgst + sgst + igst + round_off
                       == grand total               50/50
    rate               <= MRP                      462/462
    expiry is a month end                          462/462
    CGST+SGST vs IGST matches supplier state        50/50

TWO THINGS THE REAL INVOICES TAUGHT THAT THE TRUTH FILES DID NOT
----------------------------------------------------------------
The obvious identity — `qty x rate x (1 - disc) == amount` — is wrong as
stated, because on 178 of the 462 lines the discount is real and never printed
as a column. The amount arrives with it already applied and there is nothing on
the page to reconstruct it from. Hence the weaker pair actually used: the
amount may never exceed `qty x rate`, and the shortfall must be a plausible
trade discount.

Likewise `taxable x gst_rate == tax` only runs when a rate is printed. Many
layouts show CGST and SGST as rupee amounts and no percentage at all; asking
the model for the rate anyway produced a *calculated* one, which agrees with
whatever the model already believed and took the identity down with it. Where
no rate is printed, it is derived from the amounts and checked against the
statutory slabs instead.

Run `python bench/ocr/validate_bench.py` for the catch rate against real
Gemini extractions, and watch the BLOCKED false-alarm line rather than the
total: an advisory note on a good invoice costs a glance, a block costs a
decision, and too many of those teach the receiver to ignore all of them.
"""

from __future__ import annotations

import calendar
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum

# Money is compared to the paise. The generator and every real invoice round
# line values to two places, so anything looser would wave through a genuine
# transposition, and anything tighter would trip on the rounding itself.
LINE_TOLERANCE = 0.02
#: Footers accumulate up to fourteen rounded lines, so the slack has to grow
#: with them or a long invoice fails for being long.
TOTAL_TOLERANCE = 0.05
#: GST invoices round the payable to the nearest rupee, so the round-off cell
#: is bounded by construction. A larger value means a misread, not a rounding.
MAX_ROUND_OFF = 1.0
#: When a line's discount is not printed, how far the amount may fall below
#: quantity x rate before it stops being a trade discount and starts being a
#: misreading. Distributor schemes reach 20-30%; the benchmark tops out at 10%.
#: Set well clear of both, because the errors this needs to catch — a quantity
#: or rate read an order of magnitude out — land near 90%.
MAX_IMPLIED_DISCOUNT = 40.0
#: Pharmaceutical shelf life tops out around five years. Anything much beyond
#: that is a misread year, and the consequence is quiet: the batch sorts to the
#: back of the FEFO queue and is not looked at again until it is a write-off.
MAX_SHELF_LIFE_YEARS = 6
#: Manufacturers' batch codes run to a dozen characters or so. Well past that
#: is a cell that has swallowed the column beside it, and unlike the learned
#: vocabulary this holds for a supplier with no history at all.
MAX_BATCH_LENGTH = 24

#: The weak half of the batch check, named once so the pass that collapses a
#: page full of them can find its own findings without matching on prose.
UNFAMILIAR_SHAPE = "is not a batch format seen from this supplier before"
#: The GST rates a pharmacy invoice can legally carry. Used to check a rate the
#: page never printed, by deriving it from the tax and taxable amounts that it
#: did — a derived rate that lands on no slab means one of the two was misread.
GST_SLABS = (0, 5, 12, 18, 28)
#: Slack on that derivation. Line tax is rounded to the paise before the
#: division, so the implied rate wobbles by a fraction of a point on small
#: lines; anything wider is a different rate, not rounding.
GST_SLAB_TOLERANCE = 0.6
#: Headings a distributor puts over the free-goods column. Matched against the
#: headings the extractor reports seeing, so that a column printed on the page
#: with nothing under it can be told apart from a column that is not there.
FREE_GOODS_HEADINGS = frozenset({
    "FREE", "FREE QTY", "F.QTY", "FQTY", "SCH", "SCHEME", "SCH.", "BONUS",
})

#: Pharma sits in a small, well-known corner of the HSN tree. This is not the
#: full catalogue — it is the set a pharmacy distributor can legitimately
#: invoice, and anything outside it is worth a human glance rather than a
#: rejection, because a distributor may also sell equipment.
PHARMA_HSN = {
    "3002": "vaccines, blood fractions, antisera",
    "3003": "medicaments, not in measured doses",
    "3004": "medicaments, in measured doses or retail packs",
    "3005": "wadding, gauze, bandages",
    "3006": "pharmaceutical goods (sutures, dressings)",
    "3822": "diagnostic reagents",
    "4015": "surgical gloves",
    "6307": "masks and made-up textile articles",
    "9018": "medical instruments",
    "9025": "thermometers",
}

#: GST is levied per HSN, so an HSN pins the rate to a small set. Observed
#: across the benchmark plus the statutory schedule: 3004 legitimately carries
#: both 5% (a short list of life-saving formulations) and 12% (most of the
#: rest), which is why this maps to a set rather than a single value. It
#: cannot, however, be 18% — and catching that is the point.
HSN_GST_RATES: Mapping[str, frozenset[int]] = {
    "3002": frozenset({5, 12}),
    "3003": frozenset({5, 12}),
    "3004": frozenset({5, 12}),
    "3005": frozenset({12}),
    "3006": frozenset({12}),
    "3822": frozenset({12}),
    "4015": frozenset({12}),
    "6307": frozenset({5}),
    "9018": frozenset({12, 18}),
    "9025": frozenset({18}),
}

#: The 36-character alphabet GSTIN check digits are computed over: digits then
#: letters, value equal to index.
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

#: Character pairs an OCR system confuses, in both directions. Printed batch
#: numbers are the worst case for this: no dictionary to fall back on, and a
#: recall traces by exact match, so "close" is worthless.
CONFUSABLE = {
    "0": "O", "O": "0",
    "1": "I", "I": "1",
    "5": "S", "S": "5",
    "8": "B", "B": "8",
    "2": "Z", "Z": "2",
}


class Severity(str, Enum):
    """How much a flag should interrupt, decided by what it can damage.

    The line is drawn at the ledger, not at how confident the check is:

      BLOCK   this value would put wrong stock on a shelf. A garbled batch
              code, an unreadable expiry, a quantity that cannot be right, a
              line count that disagrees with the footer.
      REVIEW  the page shows signs of being misread, but the fields the
              receipt actually uses are intact. Tax and totals live here: they
              are excellent evidence that the reading was sloppy, and no part
              of them reaches a stock movement.
      INFO    worth recording, never worth stopping for.

    Getting this wrong in the obvious direction is expensive. An early version
    blocked on any failed arithmetic, and 34% of clean invoices were held for
    review over a tax figure that no goods receipt ever stores — which trains
    the person receiving stock to click through the warnings.
    """

    BLOCK = "BLOCK"
    REVIEW = "REVIEW"
    INFO = "INFO"


@dataclass(frozen=True)
class Flag:
    """One thing wrong, or worth a second look, with where to look.

    `line_no` is 1-based to match the S.N. column a human is reading off the
    paper; None means the flag is about the invoice header.
    """

    field: str
    severity: Severity
    message: str
    line_no: int | None = None
    suggestion: str | None = None

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        where = f"line {self.line_no}" if self.line_no else "header"
        tail = f"  (did you mean {self.suggestion}?)" if self.suggestion else ""
        return f"[{self.severity.value}] {where} {self.field}: {self.message}{tail}"


# --------------------------------------------------------------------- helpers


def _num(value: object) -> float | None:
    """Coerce whatever the extractor produced into a number, or None.

    Models return "1,250.00" and "12%" as often as they return floats, and a
    check that crashes on a string is a check that does not run.

    Infinity and NaN are rejected rather than passed through. They arrive from
    a value like "1e400", and they are the one input that makes every
    comparison downstream silently false — an invoice full of NaN would raise
    no flags at all, which is the worst possible failure for this module.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        cleaned = re.sub(r"[^0-9.\-]", "", str(value))
        if cleaned in ("", "-", ".", "-."):
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    """A number, but only if it is a real one.

    Extractors emit 0 for a column the layout does not print, and MRP is the
    common case — plenty of distributor formats omit it entirely. Reading that
    zero as a genuine price makes `rate <= MRP` fire on every line of every
    invoice that happens not to print the column, which is the difference
    between a useful check and one nobody looks at.
    """
    number = _num(value)
    return number if number is not None and number > 0 else None


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def batch_shape(code: str) -> str:
    """Collapse a batch code to its character class skeleton.

    `PX8602` -> `AA9999`, `AM26-21` -> `AA99-99`. Punctuation and case survive
    because they are part of how a manufacturer prints its codes.
    """
    return re.sub(r"[0-9]", "9", re.sub(r"[A-Z]", "A", code.upper()))


def learn_batch_shapes(codes: Iterable[str]) -> set[str]:
    """The shape vocabulary already seen in received lots.

    There is no regulated format for a batch number — the Drugs and Cosmetics
    Rules require one on the label and say nothing about its composition, so
    every manufacturer invents their own.

    Read what this is *not*. It is not a whitelist. The vocabulary is gathered
    per distributor, and a distributor carries dozens of manufacturers, so a
    shape absent from it usually means "a product from a manufacturer we have
    not received before" rather than "misread". Treating absence as an error
    held five of eight lines on a real delivery, every one of them correct.

    Its one honest use is as a reference for `_confusable_candidates`: a code
    that becomes a familiar shape by swapping a single `O` for `0` is a claim
    about *that code*, not about the vocabulary's completeness.
    """
    return {batch_shape(c) for c in codes if c}


def _confusable_candidates(code: str, known: set[str]) -> str | None:
    """A one-character edit that would make this code plausible.

    Two kinds, both observed in real extractions:

      substitution  a letter O read as a digit 0, I as 1
      deletion      a character carried in from the column alongside, which is
                    how `0423KA` arrived as `0423KAL`

    Only single edits, and only substitutions across the pairs an OCR system
    genuinely confuses. Anything needing two edits is a guess, and a wrong
    batch number is worse than no batch number.

    If more than one distinct candidate fits, none is offered. The flag still
    fires — the human simply reads the code off the page rather than being
    nudged towards one of several equally likely readings.
    """
    upper = code.upper()
    found: set[str] = set()

    for i, ch in enumerate(upper):
        swap = CONFUSABLE.get(ch)
        if swap is not None:
            candidate = upper[:i] + swap + upper[i + 1:]
            if batch_shape(candidate) in known:
                found.add(candidate)

    # Deletion is considered only at the end. A character absorbed from the
    # column alongside is appended — `0423KA` became `0423KAL` — so dropping
    # the last character is the one deletion with a reason behind it. Allowing
    # interior deletions produced three shape-identical candidates for that
    # same code, none of which could be preferred over the others.
    if len(upper) > 1 and batch_shape(upper[:-1]) in known:
        found.add(upper[:-1])

    return found.pop() if len(found) == 1 else None


def gstin_check_digit(first_fourteen: str) -> str | None:
    """The fifteenth character of a GSTIN, computed from the first fourteen.

    GSTIN carries a mod-36 checksum, which makes it one of the few fields on
    the page that can be verified outright rather than merely looked plausible.
    Weights alternate 1, 2 across the payload; each product is folded by
    quotient plus remainder over 36.
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
    if not _GSTIN_SHAPE.match(code):
        return False
    return gstin_check_digit(code[:14]) == code[14]


# ---------------------------------------------------------------------- header


def _check_header(doc: Mapping, today: date | None) -> list[Flag]:
    flags: list[Flag] = []
    totals = doc.get("totals") or {}

    gstin = str(doc.get("supplier_gstin") or "").strip().upper()
    if gstin:
        if not _GSTIN_SHAPE.match(gstin):
            flags.append(Flag(
                "supplier_gstin", Severity.REVIEW,
                f"{gstin!r} is not shaped like a GSTIN "
                "(2 digits, 5 letters, 4 digits, letter, entity, Z, check)",
            ))
        elif not gstin_is_valid(gstin):
            # No suggestion, deliberately. A failed checksum proves that at
            # least one of the fifteen characters is wrong; it does not say
            # which. Recomputing the last one assumes the other fourteen were
            # read correctly, and that is the less likely case — the check
            # digit is no more misreadable than any other character.
            #
            # It was offered as "did you mean …" until a real misread showed
            # what that costs: an invoice whose page carries both the
            # supplier's GSTIN and ours had the two blended in the middle, and
            # the suggestion confidently proposed a *different* wrong number
            # with a valid check digit. Handing somebody a plausible answer to
            # accept is worse than telling them to look at the paper.
            flags.append(Flag(
                "supplier_gstin", Severity.REVIEW,
                "GSTIN checksum does not match — at least one character was "
                "misread; check it against the invoice",
            ))

    # A GSTIN opens with the supplier's state code, and the tax split follows
    # from whether that is our state: same state is CGST+SGST, different is
    # IGST. Two independent cells that must agree, so a misread of either is
    # visible here.
    cgst, sgst = _num(totals.get("cgst")) or 0.0, _num(totals.get("sgst")) or 0.0
    igst = _num(totals.get("igst")) or 0.0
    intra = doc.get("is_intra_state")
    if intra is not None and (cgst or sgst or igst):
        if intra and igst > 0:
            flags.append(Flag(
                "totals.igst", Severity.REVIEW,
                "IGST charged on an intra-state invoice — expected CGST + SGST",
            ))
        elif not intra and (cgst > 0 or sgst > 0):
            flags.append(Flag(
                "totals.cgst", Severity.REVIEW,
                "CGST/SGST charged on an inter-state invoice — expected IGST",
            ))
    if cgst and sgst and abs(cgst - sgst) > LINE_TOLERANCE:
        flags.append(Flag(
            "totals.sgst", Severity.REVIEW,
            f"CGST {cgst:.2f} and SGST {sgst:.2f} must be equal",
        ))

    if gstin and len(gstin) >= 2 and doc.get("supplier_state_code"):
        if gstin[:2] != str(doc["supplier_state_code"]).strip():
            flags.append(Flag(
                "supplier_state_code", Severity.REVIEW,
                f"state code {doc['supplier_state_code']!r} does not match "
                f"the GSTIN prefix {gstin[:2]!r}",
            ))

    # An absent round-off is not zero — it is unknown. Extractions that do not
    # capture the cell still have to satisfy the identity, but only to within
    # the rupee the cell could have held. Treating missing as 0.0 would raise a
    # false alarm on every invoice that rounded, which is most of them.
    has_round_off = totals.get("round_off") is not None
    round_off = _num(totals.get("round_off")) or 0.0
    if has_round_off and abs(round_off) > MAX_ROUND_OFF:
        flags.append(Flag(
            "totals.round_off", Severity.REVIEW,
            f"round-off {round_off:.2f} exceeds one rupee",
        ))

    taxable = _num(totals.get("taxable_amount"))
    grand = _num(totals.get("grand_total"))
    if taxable is not None and grand is not None:
        computed = taxable + cgst + sgst + igst + round_off
        slack = TOTAL_TOLERANCE if has_round_off else MAX_ROUND_OFF
        if abs(computed - grand) > slack:
            flags.append(Flag(
                "totals.grand_total", Severity.REVIEW,
                f"taxable + tax{' + round-off' if has_round_off else ''} "
                f"= {computed:.2f}, but the invoice says {grand:.2f}",
            ))

    invoice_date = _as_date(doc.get("invoice_date"))
    if invoice_date and today and invoice_date > today:
        flags.append(Flag(
            "invoice_date", Severity.REVIEW,
            f"dated {invoice_date.isoformat()}, which is in the future",
        ))

    return flags


# ----------------------------------------------------------------------- lines


def _check_line(
    line: Mapping,
    line_no: int,
    invoice_date: date | None,
    known_shapes: set[str],
) -> list[Flag]:
    flags: list[Flag] = []

    # A field that is present but unreadable must not be treated as absent.
    # Every numeric rule below skips a None, so a value like "1e400" or a NaN
    # would switch off the checks that were supposed to catch it — silence
    # produced by the very corruption being looked for.
    for name in ("quantity", "rate", "taxable_amount", "tax_amount"):
        raw = line.get(name)
        if raw is not None and str(raw).strip() != "" and _num(raw) is None:
            flags.append(Flag(
                name, Severity.BLOCK,
                f"{str(raw)[:24]!r} is not a number that can be read", line_no,
            ))

    qty = _num(line.get("quantity"))
    rate = _num(line.get("rate"))
    mrp = _positive(line.get("mrp"))
    disc = _num(line.get("discount_pct")) or 0.0
    taxable = _num(line.get("taxable_amount"))
    tax = _num(line.get("tax_amount"))
    gst_rate = _num(line.get("gst_rate"))

    # A line can legitimately be free goods only — a sample or a scheme
    # delivered against nothing charged — and that reads as quantity 0 with a
    # free quantity above it. Blocking it would reject a real delivery.
    free = _num(line.get("free_quantity")) or 0.0
    if qty is not None and qty <= 0 and free <= 0:
        flags.append(Flag("quantity", Severity.BLOCK,
                          f"quantity {qty:g} is not positive", line_no))
    if rate is not None and rate <= 0 and qty:
        flags.append(Flag("rate", Severity.BLOCK,
                          f"rate {rate:g} is not positive", line_no))
    if free < 0:
        flags.append(Flag("free_quantity", Severity.BLOCK,
                          f"free quantity {free:g} is negative", line_no))

    if not 0 <= disc < 100:
        flags.append(Flag("discount_pct", Severity.BLOCK,
                          f"discount {disc:g}% is outside 0-100", line_no))
        disc = 0.0

    # MRP is the maximum *retail* price, printed on the pack and legally
    # binding. A distributor selling to us above it would be selling above the
    # consumer ceiling, which does not happen — so this catches the classic
    # transposition of the two columns without needing to know either value.
    if rate is not None and mrp is not None and rate > mrp + LINE_TOLERANCE:
        flags.append(Flag(
            "rate", Severity.BLOCK,
            f"rate {rate:.2f} exceeds MRP {mrp:.2f} — the columns may be swapped",
            line_no, suggestion=f"rate {mrp:.2f} / mrp {rate:.2f}",
        ))

    # The amount column is the strongest check on the page, but only if the
    # discount is read the same way the invoice applied it — and on 178 of the
    # 462 benchmark lines the discount is real yet never printed as a column.
    # The amount already has it baked in, so a strict qty x rate x (1 - disc)
    # identity fails on every one of them. Two rules that survive that:
    #
    #   the amount can never EXCEED qty x rate    - a discount only reduces
    #   the shortfall must be a plausible discount - not an arithmetic accident
    #
    # A quantity misread ten-fold breaks the first if it read low and the
    # second if it read high, so nothing is given up by not knowing the
    # discount. When the discount IS printed, the exact identity still applies.
    if None not in (qty, rate, taxable) and qty > 0 and rate > 0:
        gross = qty * rate
        if taxable > gross + LINE_TOLERANCE:
            flags.append(Flag(
                "taxable_amount", Severity.BLOCK,
                f"amount {taxable:.2f} is more than {qty:g} x {rate:.2f} "
                f"= {gross:.2f}; a discount cannot increase a line",
                line_no,
            ))
        elif disc > 0:
            computed = gross * (1 - disc / 100)
            if abs(computed - taxable) > LINE_TOLERANCE:
                flags.append(Flag(
                    "taxable_amount", Severity.BLOCK,
                    f"{qty:g} x {rate:.2f} less {disc:g}% = {computed:.2f}, "
                    f"but the line says {taxable:.2f}",
                    line_no,
                ))
        else:
            implied = (1 - taxable / gross) * 100
            if implied > MAX_IMPLIED_DISCOUNT:
                flags.append(Flag(
                    "taxable_amount", Severity.BLOCK,
                    f"amount {taxable:.2f} is {implied:.0f}% below "
                    f"{qty:g} x {rate:.2f} = {gross:.2f} — too large to be a "
                    "trade discount, so a figure has been misread",
                    line_no,
                ))

    # Many layouts print tax as rupee amounts in CGST and SGST columns and show
    # no percentage anywhere on the line, so the rate is genuinely absent
    # rather than missed. Asking the model for it invited a calculation, and a
    # calculated rate agrees with whatever the model already believed — on the
    # benchmark it came back doubled on 37 lines and took the identity with it.
    #
    # So: check the identity only when a rate was actually printed. Otherwise
    # derive the rate from the two figures that *were* printed and require it
    # to be a real GST slab. That catches the same misreads without depending
    # on a number the page does not carry.
    # Zero means "this cell is not on the page", for both the rate and the tax.
    # That distinction bit three separate checks during development, each time
    # firing on a hundred-odd correct lines, so it is stated once here: a tax
    # check runs only over figures the invoice actually printed.
    #
    #   rate and tax printed  ->  the identity
    #   tax printed only      ->  derive the rate, require a statutory slab
    #   no tax printed        ->  nothing to check
    if taxable is not None and taxable > 0 and tax:
        if gst_rate:
            computed = taxable * gst_rate / 100
            if abs(computed - tax) > LINE_TOLERANCE:
                flags.append(Flag(
                    "tax_amount", Severity.REVIEW,
                    f"{taxable:.2f} at {gst_rate:g}% = {computed:.2f}, "
                    f"but the line says {tax:.2f}",
                    line_no,
                ))
        else:
            implied = tax / taxable * 100
            if not any(abs(implied - slab) <= GST_SLAB_TOLERANCE
                       for slab in GST_SLABS):
                flags.append(Flag(
                    "tax_amount", Severity.REVIEW,
                    f"tax {tax:.2f} on {taxable:.2f} works out at {implied:.1f}%, "
                    f"which is not a GST rate "
                    f"({', '.join(f'{s}%' for s in GST_SLABS)})",
                    line_no,
                ))

    hsn = str(line.get("hsn") or "").strip()
    if hsn:
        head = hsn[:4]
        if head not in PHARMA_HSN:
            flags.append(Flag(
                "hsn", Severity.INFO,
                f"HSN {hsn} is outside the usual pharmacy range",
                line_no,
            ))
        # `gst_rate` of 0 means the line printed no percentage, not that the
        # goods are zero-rated — see the tax check above. Comparing that
        # absence against the statutory rates fired on 130 lines of a 50-invoice
        # run, every one of them a correct extraction.
        elif gst_rate and int(gst_rate) not in HSN_GST_RATES[head]:
            allowed = "/".join(f"{r}%" for r in sorted(HSN_GST_RATES[head]))
            flags.append(Flag(
                "gst_rate", Severity.REVIEW,
                f"HSN {hsn} ({PHARMA_HSN[head]}) is taxed at {allowed}, "
                f"not {gst_rate:g}%",
                line_no,
            ))

    # Pharma expiry is a month, never a day: "08/27" means the stock is good
    # through 31 August 2027. Reading it as the 1st loses a month, and that
    # month decides both whether the delivery is accepted and where the batch
    # sits in the FEFO queue.
    raw_expiry = line.get("expiry_date")
    expiry = _as_date(raw_expiry)
    if expiry is None and str(raw_expiry or "").strip():
        # Unreadable is not the same as absent. Skipping it silently would let
        # a batch onto the shelf with no expiry, which FEFO then cannot rank.
        flags.append(Flag(
            "expiry_date", Severity.BLOCK,
            f"{str(raw_expiry)[:24]!r} is not a date that can be read", line_no,
        ))
    elif expiry is None:
        flags.append(Flag("expiry_date", Severity.BLOCK,
                          "no expiry date — required to receive stock", line_no))
    else:
        last = calendar.monthrange(expiry.year, expiry.month)[1]
        if expiry.day != last:
            flags.append(Flag(
                "expiry_date", Severity.REVIEW,
                f"{expiry.isoformat()} is not a month end — pharma expiry is a month",
                line_no,
                suggestion=expiry.replace(day=last).isoformat(),
            ))
        if invoice_date and expiry <= invoice_date:
            flags.append(Flag(
                "expiry_date", Severity.BLOCK,
                f"expires {expiry.isoformat()}, on or before the invoice date",
                line_no,
            ))
        # Shelf lives run to about five years; a decade out is a misread year,
        # and it matters because it parks the batch at the back of the FEFO
        # queue where nobody looks at it again.
        elif invoice_date and expiry.year - invoice_date.year > MAX_SHELF_LIFE_YEARS:
            flags.append(Flag(
                "expiry_date", Severity.REVIEW,
                f"expires {expiry.isoformat()}, over {MAX_SHELF_LIFE_YEARS} years "
                "after the invoice — check the year",
                line_no,
            ))

    if not str(line.get("product_name") or "").strip():
        flags.append(Flag("product_name", Severity.BLOCK,
                          "no product name — the line cannot be matched to stock",
                          line_no))

    batch = str(line.get("batch_no") or "").strip()
    if not batch:
        flags.append(Flag("batch_no", Severity.BLOCK,
                          "no batch number — required to receive stock", line_no))
    elif len(batch) > MAX_BATCH_LENGTH:
        # Independent of any learned vocabulary, so it still holds for a
        # supplier we have never received from. A batch this long is a row of
        # the table swept up into one cell, not a manufacturer's code.
        flags.append(Flag(
            "batch_no", Severity.BLOCK,
            f"{batch[:20]!r}... is {len(batch)} characters; no batch code is "
            "that long, so this has absorbed a neighbouring column",
            line_no,
        ))
    elif known_shapes and batch_shape(batch) not in known_shapes:
        # Two different claims, and they used to be made at the same volume.
        #
        # An unfamiliar shape on its own is weak. `learn_batch_shapes` above
        # explains why without noticing it: batch formats belong to
        # *manufacturers*, and a distributor carries dozens of them, so the
        # vocabulary gathered per distributor is a union that is never
        # finished. A first delivery of any product produces a shape nobody
        # has seen. Blocking on that held five of eight lines on a real
        # delivery, every one of them read correctly — goods on the floor that
        # could not be received because of a hunch.
        #
        # A code one confusable character from a familiar shape is a different
        # claim entirely: `O` read as `0`, `I` as `1`, a character absorbed
        # from the column alongside. It names the specific error and carries
        # the correction.
        #
        # So: the second still blocks, the first is only worth a look. Both
        # stay visible — three of these were genuine misreads on the benchmark
        # and none of them had a confusable candidate — but only one of them
        # stops a delivery.
        near = _confusable_candidates(batch, known_shapes)
        flags.append(Flag(
            "batch_no",
            Severity.BLOCK if near else Severity.REVIEW,
            f"{batch!r} is one character from a batch format this supplier "
            f"ships — check the confusable characters"
            if near else
            f"{batch!r} {UNFAMILIAR_SHAPE} — fine for a new product, worth a "
            f"glance otherwise",
            line_no,
            suggestion=near,
        ))

    return flags


# ------------------------------------------------------------------ public API


def validate_invoice(
    doc: Mapping,
    *,
    known_batch_shapes: set[str] | None = None,
    today: date | None = None,
) -> list[Flag]:
    """Check one extracted invoice against its own arithmetic and the rules.

    `known_batch_shapes` comes from the lot history of the supplier being
    received from — see `learn_batch_shapes`. Passing None skips the batch
    format check entirely, which is the correct behaviour for a supplier we
    have never received from: there is nothing to compare against yet, and
    inventing a rule would be worse than not having one.

    Returns every flag found, ordered header first then by line. An empty list
    means the document is internally consistent — not that it is correct, but
    that nothing in it contradicts anything else in it.
    """
    flags = _check_header(doc, today)

    invoice_date = _as_date(doc.get("invoice_date"))
    lines = doc.get("lines") or []

    # An extraction with no lines passes every other rule in this file, because
    # every other rule is about a line or about a footer that has nothing to
    # disagree with. Silence would read as "this invoice is fine" when what
    # happened is that the page was never read. Say so instead.
    if not lines:
        flags.append(Flag(
            "lines", Severity.BLOCK,
            "no line items were read from this document — "
            "it may not be an invoice, or the image may be unreadable",
        ))
    running_taxable = 0.0
    running_tax = 0.0
    seen_taxable = False
    seen_tax = False

    for index, line in enumerate(lines, start=1):
        flags.extend(_check_line(line, index, invoice_date, known_batch_shapes or set()))
        value = _num(line.get("taxable_amount"))
        if value is not None:
            running_taxable += value
            seen_taxable = True
        levy = _num(line.get("tax_amount"))
        if levy is not None:
            running_tax += levy
            seen_tax = True

    # Eight identical notes say less than one.
    #
    # When most of a page trips the weak half of the batch check, the fact
    # being reported is not about any particular line — it is that there is
    # too little history here to have an opinion. Repeating it per row buries
    # the findings that are about a specific line, and a reader who scrolls
    # past eight amber notes will scroll past the ninth that mattered.
    #
    # A minority still reports per line, because there the contrast is the
    # information: seven familiar codes and one that is not.
    unfamiliar = [f for f in flags
                  if f.field == "batch_no" and UNFAMILIAR_SHAPE in f.message]
    printed = sum(1 for line in lines if str(line.get("batch_no") or "").strip())
    if len(unfamiliar) >= 3 and len(unfamiliar) * 2 > printed:
        flags = [f for f in flags if f not in unfamiliar]
        flags.append(Flag(
            "batch_no", Severity.REVIEW,
            f"{len(unfamiliar)} of {printed} batch codes are in formats not "
            f"seen from this supplier before — normal for a supplier we have "
            f"received from rarely, so no code here is being singled out",
        ))

    # The footer is an independent restatement of the lines. A dropped line is
    # invisible in every per-line check and obvious here, which makes this the
    # only check that catches a *missing* row rather than a wrong one.
    # The one error class that was previously undetectable. Free goods enter no
    # money identity — they are free — so no arithmetic can see them missing,
    # and on the benchmark three were dropped silently. What *can* be seen is
    # the contradiction between the heading and the rows: a FREE column printed
    # on the page with not one free unit under it.
    #
    # REVIEW rather than BLOCK. An invoice genuinely without schemes looks
    # exactly like this, and it is common; the flag says "look at that column",
    # not "this receipt is wrong".
    headings = [str(c).strip().upper() for c in (doc.get("columns_seen") or [])]
    if lines and any(h in FREE_GOODS_HEADINGS for h in headings):
        if not any((_num(line.get("free_quantity")) or 0) > 0 for line in lines):
            flags.append(Flag(
                "free_quantity", Severity.REVIEW,
                "the invoice prints a free-goods column but no line claims any "
                "— check it before posting, free units are received stock",
            ))

    totals = doc.get("totals") or {}
    header_taxable = _num(totals.get("taxable_amount"))
    if seen_taxable and header_taxable is not None:
        if abs(running_taxable - header_taxable) > TOTAL_TOLERANCE:
            flags.append(Flag(
                "totals.taxable_amount", Severity.BLOCK,
                f"the {len(lines)} lines add to {running_taxable:.2f}, "
                f"but the footer says {header_taxable:.2f} — a line may be missing",
            ))

    # The per-line tax check cannot catch a misread GST rate on its own,
    # because a model that recalculates the tax from its own wrong rate
    # produces a line that agrees with itself. Observed in practice: a 5% line
    # read as 12% arrived with the tax recomputed to match, and every per-line
    # rule passed. The footer is written by the supplier, not by the reader, so
    # comparing the lines against it breaks that self-agreement.
    header_tax = sum(
        _num(totals.get(part)) or 0.0 for part in ("cgst", "sgst", "igst")
    )
    # `running_tax` of zero means no line printed a tax figure — some layouts
    # carry only an HSN-wise summary at the foot — so there is nothing to
    # reconcile. Comparing an absent column against the footer flagged 14 of 45
    # correct invoices in one run.
    if seen_tax and running_tax > 0 and header_tax > 0:
        if abs(running_tax - header_tax) > TOTAL_TOLERANCE:
            flags.append(Flag(
                "totals.cgst", Severity.REVIEW,
                f"the lines carry {running_tax:.2f} of tax, but the footer "
                f"totals {header_tax:.2f} — a GST rate has been misread",
            ))

    return flags


def blocking(flags: Iterable[Flag]) -> list[Flag]:
    """The subset that must be resolved before the receipt can be posted."""
    return [f for f in flags if f.severity is Severity.BLOCK]
