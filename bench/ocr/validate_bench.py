"""Measure the validator against real extractions.

`score.py` answers "how often is the model right?". This answers the question
that actually decides whether the feature is safe to ship: **when the model is
wrong, do we notice?**

Those are different numbers and the second one matters more. An extraction that
is 88% clean is unusable if the 12% arrives silently; the same extraction is
useful if every bad field arrives with a flag on it, because then a human looks
at exactly the fields worth looking at.

    python3 validate_bench.py --predictions out/pred/gemini-3.1-fl-300dpi

WHY LEAVE-ONE-OUT
-----------------
The batch-format check needs to know what this supplier's batch codes look
like. In production that vocabulary comes from the lot history already in the
database — data entirely separate from the invoice being received. Learning it
here from all fifty ground truths, then validating those same fifty, would be
circular and would flatter the result.

So each invoice is validated against a vocabulary learned from the *other*
forty-nine. That is the honest analogue of "we have received from this supplier
before, and this is what their codes looked like".
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from app.ai.intake.service import derive_context  # noqa: E402
from app.ai.intake.validate import (  # noqa: E402
    Severity,
    batch_shape,
    validate_invoice,
)

#: A state code none of the benchmark's distributors use, so that standing in
#: for "our" state on an inter-state invoice cannot accidentally match.
ELSEWHERE = "07"

OUT = Path(__file__).parent / "out"

#: Which truth field each printed column corresponds to. Only columns actually
#: present on a given invoice are comparable — the generator knows MRP even
#: when the layout does not print it, and scoring against an invisible cell
#: would invent errors the model never made.
COLUMN_FIELD = {
    "batch": "batch_no", "exp": "expiry_date", "qty": "quantity",
    "free": "free_quantity", "rate": "rate", "mrp": "mrp",
    "disc": "discount_pct",
}


def _unexercisable(flag) -> bool:
    """Checks this corpus cannot honestly test.

    `generate.py` builds supplier GSTINs by picking a real state prefix and
    then random characters, so the fifteenth character is random too and
    essentially never the correct mod-36 check digit. The checksum rule
    therefore fires on all fifty invoices — not because the extraction is
    wrong, but because the *paper* is synthetic.

    Counting that as a false alarm would understate the validator; silently
    dropping it would overstate it. So it is excluded here and reported in the
    dormant section, and the rule itself is covered by unit tests against real
    published GSTINs instead.
    """
    return flag.field == "supplier_gstin" and "checksum" in flag.message


def _same(a: object, b: object) -> bool:
    """Compare as the invoice would read them, not as Python types."""
    if a is None or b is None:
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < 0.005
    return str(a).strip().upper() == str(b).strip().upper()


def real_errors(truth: dict, pred: dict) -> list[tuple[int, str]]:
    """Where the extraction genuinely differs from the paper.

    Restricted to columns the layout actually prints, and matched by position,
    which is what the scorer does.
    """
    columns = [COLUMN_FIELD[c] for c in truth["_format"]["columns"] if c in COLUMN_FIELD]
    found: list[tuple[int, str]] = []
    lines = pred.get("lines") or []
    for i, tline in enumerate(truth["lines"]):
        pline = lines[i] if i < len(lines) else {}
        for field in columns:
            if not _same(pline.get(field), tline.get(field)):
                found.append((i + 1, field))
    for field in ("invoice_number", "invoice_date", "supplier_gstin"):
        if not _same(pred.get(field), truth.get(field)):
            found.append((0, field))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True,
                        help="directory of extractions, e.g. out/pred/<system>")
    parser.add_argument("--verbose", action="store_true",
                        help="list every error and whether it was flagged")
    args = parser.parse_args()

    pred_dir = Path(args.predictions)
    if not pred_dir.is_dir():
        sys.exit(f"no such directory: {pred_dir}")

    truths = {p.stem: json.loads(p.read_text())
              for p in sorted((OUT / "truth").glob("inv_*.json"))}
    preds = {p.stem: json.loads(p.read_text())
             for p in sorted(pred_dir.glob("inv_*.json")) if p.stem in truths}
    if not preds:
        sys.exit(f"no predictions in {pred_dir} matching the ground truth")

    # Leave-one-out batch vocabulary, per the note above.
    shapes_by_invoice = {
        k: {batch_shape(line["batch_no"]) for line in t["lines"] if line.get("batch_no")}
        for k, t in truths.items()
    }

    caught_invoices = clean_flagged = clean_blocked = dirty = clean = 0
    field_caught: Counter[str] = Counter()
    field_missed: Counter[str] = Counter()
    check_fired: Counter[str] = Counter()
    detail: list[str] = []

    for key in sorted(preds):
        truth, pred = truths[key], preds[key]
        known = set().union(*(s for k, s in shapes_by_invoice.items() if k != key))

        # Which state we are in is a fact about us, not about the supplier's
        # paper, so the endpoint supplies it from the receiving warehouse. The
        # bench stands in for that here — without it the tax-split rule cannot
        # run, and it is the check that proves a misread state code.
        pred = derive_context(
            pred,
            our_state_code=(truth["supplier_state_code"]
                            if truth["is_intra_state"] else ELSEWHERE),
        )
        flags = [f for f in validate_invoice(pred, known_batch_shapes=known)
                 if not _unexercisable(f)]
        errors = real_errors(truth, pred)
        flagged_cells = {(f.line_no or 0, f.field) for f in flags}
        for flag in flags:
            check_fired[flag.field] += 1

        if errors:
            dirty += 1
            hit = [e for e in errors if e in flagged_cells]
            if hit:
                caught_invoices += 1
            for line_no, field in errors:
                (field_caught if (line_no, field) in flagged_cells
                 else field_missed)[field] += 1
                if args.verbose:
                    mark = "CAUGHT " if (line_no, field) in flagged_cells else "missed "
                    detail.append(f"  {mark} {key} line {line_no} {field}")
        else:
            clean += 1
            if flags:
                clean_flagged += 1
            # The number that decides whether the feature is usable. An
            # advisory note on a correct invoice costs a glance; a BLOCK costs
            # the receiver a forced decision, and enough of those teach them to
            # click through every warning including the real ones.
            blockers = [f for f in flags if f.severity is Severity.BLOCK]
            if blockers:
                clean_blocked += 1
            if args.verbose:
                for flag in flags:
                    mark = "BLOCKED" if flag.severity is Severity.BLOCK else "advisory"
                    detail.append(f"  {mark:8} {key} {flag}")

    name = pred_dir.name
    total_err = sum(field_caught.values()) + sum(field_missed.values())
    print(f"\n  Validator catch rate — {name}   ({len(preds)} invoices)\n")
    print("  ERROR DETECTION")
    print("  " + "-" * 52)
    print(f"  invoices containing at least one error   {dirty}")
    print(f"    ...with at least one error flagged     {caught_invoices}"
          f"{f'  = {100*caught_invoices/dirty:.0f}%' if dirty else ''}")
    print(f"  individual wrong fields                  {total_err}")
    print(f"    ...flagged                             {sum(field_caught.values())}"
          f"{f'  = {100*sum(field_caught.values())/total_err:.0f}%' if total_err else ''}")

    if field_caught or field_missed:
        print("\n  BY FIELD")
        print("  " + "-" * 52)
        print(f"  {'field':<20}{'caught':>8}{'missed':>8}")
        for field in sorted(set(field_caught) | set(field_missed)):
            print(f"  {field:<20}{field_caught[field]:>8}{field_missed[field]:>8}")

    print("\n  FALSE ALARMS")
    print("  " + "-" * 52)
    print(f"  invoices with no error at all            {clean}")
    print(f"    ...that raised any flag                {clean_flagged}"
          f"{f'  = {100*clean_flagged/clean:.1f}%' if clean else ''}")
    print(f"    ...that were BLOCKED                   {clean_blocked}"
          f"{f'  = {100*clean_blocked/clean:.1f}%' if clean else ''}"
          "   <- the one that matters")

    print("\n  WHICH CHECKS FIRED")
    print("  " + "-" * 52)
    if check_fired:
        for field, count in check_fired.most_common():
            print(f"  {field:<28}{count:>6}")
    else:
        print("  none")

    # A check that never fires on this data has not been shown to work. Saying
    # so is the difference between a benchmark and a demo.
    sample = next(iter(preds.values()))
    dormant = []
    if not (sample.get("totals") or {}).get("round_off"):
        dormant.append("round-off bound (extraction omits the cell)")
    if not any("taxable_amount" in ln for ln in sample.get("lines", [])):
        dormant.append("line arithmetic qty x rate = amount (extraction omits amount)")
    if not any("gst_rate" in ln for ln in sample.get("lines", [])):
        dormant.append("tax identity and HSN/GST agreement (extraction omits both)")
    dormant.append("GSTIN checksum (synthetic GSTINs carry random check digits; "
                   "covered by unit tests against real ones)")
    if dormant:
        print("\n  NOT EXERCISED BY THIS EXTRACTION")
        print("  " + "-" * 52)
        for item in dormant:
            print(f"  - {item}")
        print("\n  These are the strongest checks available and they are dark")
        print("  because the extraction schema does not capture the fields.")
        print("  Widening the prompt turns them on.")

    if args.verbose and detail:
        print("\n  DETAIL")
        print("  " + "-" * 52)
        for line in detail:
            print(line)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
