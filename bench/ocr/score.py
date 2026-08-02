"""Score an OCR system's extractions against the ground truth.

    python3 bench/ocr/score.py --predictions out/pred/gemini-3.1-flash-lite
    python3 bench/ocr/score.py --predictions out/pred/unlimited-ocr --by noise

Drop one JSON per invoice into the predictions directory, named after the
invoice (`inv_001.json`), in the same shape as the ground truth. Whatever
produced it — a repo, an API, a human — is irrelevant to the scoring.

WHAT IS BEING MEASURED, and why it is not one number
----------------------------------------------------
"95% accurate" is meaningless for this task. Three separate things can go
wrong and they have completely different consequences:

  header fields   getting the supplier wrong means the GRN books against the
                  wrong distributor's account
  line matching   a dropped or hallucinated line means stock that exists on
                  the shelf but not in the system, or the reverse
  field values    a wrong batch number breaks recall tracing; a wrong expiry
                  puts expired stock into FEFO

So the report gives per-field accuracy, line recall/precision, and a
"clean invoice rate" — the share of invoices with zero errors anywhere,
which is the only number that predicts how much typing a pharmacist avoids.

Comparison is tolerant where the domain is tolerant and strict where it is
not. Money is compared to the paisa. Batch numbers are compared exactly,
because "close" is worthless for a recall. Product names are matched
fuzzily, because the invoice says "PCM-650 TAB" and we call it
"Paracetamol 650mg".
"""

import argparse
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

OUT = Path(__file__).parent / "out"

#: Fields compared once per invoice.
HEADER_FIELDS = [
    "invoice_number", "invoice_date", "supplier_name",
    "supplier_gstin", "drug_licence_no",
]
#: Fields compared per matched line.
LINE_FIELDS = [
    "product_name", "batch_no", "expiry_date", "quantity",
    "free_quantity", "mrp", "rate", "discount_pct",
]
TOTAL_FIELDS = ["taxable_amount", "cgst", "sgst", "igst", "grand_total"]

#: Compared loosely — an abbreviation is a correct read, not an error.
FUZZY = {"product_name", "supplier_name"}
#: Compared to the paisa.
MONEY = {"mrp", "rate", "taxable_amount", "cgst", "sgst", "igst", "grand_total"}

#: Which printed column a line field can be read from.
#:
#: The generator varies which columns an invoice prints — five different sets —
#: so on a document with no MRP column there is no MRP to read. Scoring it
#: anyway marks a system wrong for correctly reporting nothing, and the ground
#: truth still carries the value because it describes the underlying order
#: rather than the page.
#:
#: This went unnoticed because `mock_ocr.py` fabricates its answers from the
#: ground truth rather than from the rendered document, so the mock "reads"
#: columns that were never printed and scores clean. Any real system fails
#: them all. Two of the first three Gemini extractions lost every MRP and
#: every free-quantity for exactly this reason.
COLUMN_FOR = {
    "product_name": "product",
    "batch_no": "batch",
    "expiry_date": "exp",
    "quantity": "qty",
    "free_quantity": "free",
    "mrp": "mrp",
    "rate": "rate",
    "discount_pct": "disc",
}


def printed_fields(truth: dict) -> list[str]:
    """The line fields this particular invoice actually shows."""
    columns = set((truth.get("_format") or {}).get("columns") or [])
    if not columns:  # older truth files without format metadata
        return list(LINE_FIELDS)
    return [f for f in LINE_FIELDS if COLUMN_FOR.get(f, f) in columns]


def _norm(value) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _similar(a, b) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _equal(field: str, truth, pred) -> bool:
    """One comparison rule per field family, matching what the domain tolerates."""
    if truth is None and pred is None:
        return True
    if pred is None:
        return False

    if field in FUZZY:
        # 0.55 rather than 0.9: "PCM-650 TAB" vs "PARACETAMOL 650MG TAB" is a
        # correct read of an abbreviated invoice. The product *matching* step
        # that resolves it to a SKU is a separate concern, scored separately.
        return _similar(truth, pred) >= 0.55

    if field in MONEY:
        try:
            return abs(float(truth) - float(pred)) < 0.011
        except (TypeError, ValueError):
            return False

    if field in {"quantity", "free_quantity", "discount_pct"}:
        try:
            return abs(float(truth or 0) - float(pred or 0)) < 1e-6
        except (TypeError, ValueError):
            return False

    # Dates, batch numbers, GSTIN, invoice number: exact after normalisation.
    return _norm(truth) == _norm(pred)


def match_lines(truth_lines: list[dict], pred_lines: list[dict]) -> list[tuple]:
    """Pair predicted lines to true lines before comparing fields.

    A model may emit lines in a different order, or merge two into one. Scoring
    positionally would then report every field of every line as wrong and hide
    what actually happened. Batch number is the strongest key — it is unique
    within an invoice — with a name+quantity fallback for when the batch itself
    was misread.
    """
    remaining = list(enumerate(pred_lines))
    pairs: list[tuple] = []

    for t in truth_lines:
        best, best_score = None, 0.0
        for idx, p in remaining:
            score = 0.0
            if _norm(p.get("batch_no")) and _norm(p.get("batch_no")) == _norm(t["batch_no"]):
                score = 1.0
            else:
                score = 0.6 * _similar(t["product_name"], p.get("product_name"))
                if str(t["quantity"]) == str(p.get("quantity")):
                    score += 0.3
                if _equal("mrp", t["mrp"], p.get("mrp")):
                    score += 0.1
            if score > best_score:
                best, best_score = idx, score
        if best is not None and best_score >= 0.5:
            pred = next(p for i, p in remaining if i == best)
            remaining = [(i, p) for i, p in remaining if i != best]
            pairs.append((t, pred))
        else:
            pairs.append((t, None))  # missed line

    # Anything left over is a line the model invented.
    pairs.extend((None, p) for _, p in remaining)
    return pairs


def score_invoice(truth: dict, pred: dict | None) -> dict:
    """Per-field hits and misses for one invoice."""
    result = {
        "invoice_id": truth["invoice_id"],
        "fields": {},
        "lines_expected": len(truth["lines"]),
        "lines_matched": 0,
        "lines_missed": 0,
        "lines_hallucinated": 0,
        "errors": [],
    }
    if pred is None:
        result["errors"].append("no prediction produced")
        result["lines_missed"] = len(truth["lines"])
        for field in HEADER_FIELDS + TOTAL_FIELDS + printed_fields(truth):
            result["fields"][field] = (0, 1)
        return result

    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for field in HEADER_FIELDS:
        ok = _equal(field, truth.get(field), pred.get(field))
        tally[field][1] += 1
        tally[field][0] += int(ok)
        if not ok:
            result["errors"].append(
                f"{field}: expected {truth.get(field)!r}, got {pred.get(field)!r}"
            )

    pred_totals = pred.get("totals") or {}
    for field in TOTAL_FIELDS:
        ok = _equal(field, truth["totals"].get(field), pred_totals.get(field))
        tally[field][1] += 1
        tally[field][0] += int(ok)
        if not ok:
            result["errors"].append(
                f"totals.{field}: expected {truth['totals'].get(field)!r}, "
                f"got {pred_totals.get(field)!r}"
            )

    # Only the columns this invoice prints — see COLUMN_FOR.
    line_fields = printed_fields(truth)

    for t_line, p_line in match_lines(truth["lines"], pred.get("lines") or []):
        if t_line is None:
            result["lines_hallucinated"] += 1
            result["errors"].append(
                f"line not on the invoice: {(p_line or {}).get('product_name')!r}"
            )
            continue
        if p_line is None:
            result["lines_missed"] += 1
            result["errors"].append(f"line dropped: {t_line['product_name']!r}")
            for field in line_fields:
                tally[field][1] += 1
            continue

        result["lines_matched"] += 1
        for field in line_fields:
            ok = _equal(field, t_line.get(field), p_line.get(field))
            tally[field][1] += 1
            tally[field][0] += int(ok)
            if not ok:
                result["errors"].append(
                    f"line {t_line['sn']} {field}: expected {t_line.get(field)!r}, "
                    f"got {p_line.get(field)!r}"
                )

    result["fields"] = {k: tuple(v) for k, v in tally.items()}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", required=True,
                        help="Directory of inv_NNN.json extractions")
    parser.add_argument("--by", default="noise",
                        choices=["noise", "layout", "font", "tax_style",
                                 "expiry_format", "abbreviated_names"],
                        help="Second breakdown axis")
    parser.add_argument("--errors", type=int, default=0,
                        help="Print this many failing invoices in detail")
    args = parser.parse_args()

    pred_dir = Path(args.predictions)
    index = {row["id"]: row for row in json.loads((OUT / "index.json").read_text())}

    results, totals = [], defaultdict(lambda: [0, 0])
    by_axis = defaultdict(lambda: [0, 0])  # (clean invoices, invoices)

    for inv_id in sorted(index):
        truth = json.loads((OUT / "truth" / f"{inv_id}.json").read_text())
        pred_path = pred_dir / f"{inv_id}.json"
        pred = json.loads(pred_path.read_text()) if pred_path.exists() else None

        row = score_invoice(truth, pred)
        results.append(row)
        for field, (hit, total) in row["fields"].items():
            totals[field][0] += hit
            totals[field][1] += total

        clean = (
            not row["errors"]
            and row["lines_missed"] == 0
            and row["lines_hallucinated"] == 0
        )
        bucket = str(index[inv_id][args.by])
        by_axis[bucket][0] += int(clean)
        by_axis[bucket][1] += 1

    print(f"\n  OCR accuracy — {pred_dir.name}   ({len(results)} invoices)\n")
    print("  FIELD                 ACCURACY      n")
    print("  " + "-" * 40)
    for field in HEADER_FIELDS + LINE_FIELDS + TOTAL_FIELDS:
        hit, total = totals.get(field, (0, 0))
        if not total:
            continue
        print(f"  {field:<20}  {hit / total:>7.1%}  {total:>5}")

    lines_expected = sum(r["lines_expected"] for r in results)
    matched = sum(r["lines_matched"] for r in results)
    hallucinated = sum(r["lines_hallucinated"] for r in results)
    clean = sum(
        1 for r in results
        if not r["errors"] and not r["lines_missed"] and not r["lines_hallucinated"]
    )

    print("\n  LINE ITEMS")
    print("  " + "-" * 40)
    print(f"  recall (found / on invoice)     {matched / max(lines_expected, 1):>7.1%}")
    print(f"  precision (found / reported)    "
          f"{matched / max(matched + hallucinated, 1):>7.1%}")
    print(f"  dropped                         {lines_expected - matched:>7}")
    print(f"  invented                        {hallucinated:>7}")

    print(f"\n  CLEAN INVOICES (zero errors)    {clean}/{len(results)}"
          f"  = {clean / len(results):.1%}")

    print(f"\n  BY {args.by.upper()}")
    print("  " + "-" * 40)
    for bucket in sorted(by_axis):
        ok, total = by_axis[bucket]
        print(f"  {bucket:<20}  {ok}/{total} clean   {ok / total:>6.1%}")

    if args.errors:
        print("\n  SAMPLE FAILURES")
        print("  " + "-" * 40)
        shown = 0
        for row in results:
            if not row["errors"]:
                continue
            print(f"\n  {row['invoice_id']}  ({index[row['invoice_id']]['layout']}, "
                  f"{index[row['invoice_id']]['noise']})")
            for err in row["errors"][:8]:
                print(f"    - {err}")
            shown += 1
            if shown >= args.errors:
                break

    (pred_dir / "_report.json").write_text(json.dumps(results, indent=2))
    print(f"\n  full per-invoice detail -> {pred_dir / '_report.json'}\n")


if __name__ == "__main__":
    main()
