"""Measure the validator's ceiling by injecting known errors.

`validate_bench.py` measures the catch rate against whatever mistakes a model
happened to make on fifty invoices — a handful of errors, most of them the same
kind. That is the number that matters operationally, but it says almost nothing
about the errors the model *didn't* happen to make this time.

This takes ground truth, which is correct by construction, injects one
realistic OCR failure at a time, and asks whether the validator notices. It
answers the complementary question: for each way an invoice can be misread, is
there a rule that catches it?

    python3 inject_bench.py
    python3 inject_bench.py --mutation "rate/MRP swapped"

The first line of output is the one to read sceptically. Unmutated ground truth
must produce zero flags — if it does not, the rest of the table is measuring
bugs in the rules rather than detection of errors in the data.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from app.ai.intake.validate import (  # noqa: E402
    batch_shape,
    gstin_check_digit,
    validate_invoice,
)

OUT = Path(__file__).parent / "out"

#: Fixed, so a run is comparable with the last one. Nothing here depends on the
#: particular draw — it only decides which line of an invoice gets corrupted.
SEED = 20260804

MUTATIONS: dict[str, Callable] = {}


def mutation(name: str):
    def register(fn):
        MUTATIONS[name] = fn
        return fn
    return register


def _transpose(value: float) -> float:
    """Swap the leading two digits — the classic misread of a printed figure."""
    digits = list(f"{value:.2f}".replace(".", ""))
    if len(digits) < 3:
        return value
    digits[0], digits[1] = digits[1], digits[0]
    return float("".join(digits)) / 100


@mutation("amount transposed")
def _(doc, rng):
    line = rng.choice(doc["lines"])
    before = line["taxable_amount"]
    line["taxable_amount"] = _transpose(before)
    return line["taxable_amount"] != before


@mutation("rate/MRP swapped")
def _(doc, rng):
    options = [ln for ln in doc["lines"] if ln.get("mrp") and ln["mrp"] > ln["rate"]]
    if not options:
        return False
    line = rng.choice(options)
    line["rate"], line["mrp"] = line["mrp"], line["rate"]
    return True


@mutation("quantity misread")
def _(doc, rng):
    rng.choice(doc["lines"])["quantity"] *= 10
    return True


@mutation("gst rate misread")
def _(doc, rng):
    line = rng.choice(doc["lines"])
    line["gst_rate"] = 18 if line["gst_rate"] != 18 else 5
    return True


@mutation("line dropped")
def _(doc, rng):
    if len(doc["lines"]) < 2:
        return False
    doc["lines"].pop(rng.randrange(len(doc["lines"])))
    return True


@mutation("line duplicated")
def _(doc, rng):
    doc["lines"].append(dict(rng.choice(doc["lines"])))
    return True


@mutation("expiry read as the 1st")
def _(doc, rng):
    line = rng.choice(doc["lines"])
    line["expiry_date"] = line["expiry_date"][:8] + "01"
    return True


@mutation("expiry year misread")
def _(doc, rng):
    line = rng.choice(doc["lines"])
    line["expiry_date"] = str(int(line["expiry_date"][:4]) + 20) + line["expiry_date"][4:]
    return True


@mutation("batch garbled")
def _(doc, rng):
    line = rng.choice(doc["lines"])
    line["batch_no"] += rng.choice("ABX")
    return True


@mutation("batch absorbed a column")
def _(doc, rng):
    line = rng.choice(doc["lines"])
    line["batch_no"] = f"{line['batch_no']} {line['product_name']} {line.get('pack', '')}"
    return True


@mutation("GSTIN digit misread")
def _(doc, rng):
    gstin = doc["supplier_gstin"]
    at = rng.randrange(2, 14)
    doc["supplier_gstin"] = (
        gstin[:at] + ("8" if gstin[at] != "8" else "3") + gstin[at + 1:]
    )
    return True


@mutation("tax total misread")
def _(doc, rng):
    before = doc["totals"]["grand_total"]
    doc["totals"]["grand_total"] = _transpose(before)
    return doc["totals"]["grand_total"] != before


@mutation("product name lost")
def _(doc, rng):
    rng.choice(doc["lines"])["product_name"] = ""
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation", default=None, help="run only this one")
    args = parser.parse_args()

    truths = {p.stem: json.loads(p.read_text())
              for p in sorted((OUT / "truth").glob("inv_*.json"))}
    if not truths:
        sys.exit("no ground truth in out/truth — run generate.py first")

    # The corpus builds GSTINs from a real state prefix and random characters,
    # so the check digit is random and the checksum rule would fire on every
    # document. Repair them first: the point here is to measure detection of
    # injected errors, not to rediscover that the fixtures are synthetic.
    for truth in truths.values():
        gstin = truth["supplier_gstin"]
        truth["supplier_gstin"] = gstin[:14] + (gstin_check_digit(gstin[:14]) or gstin[14])

    def vocabulary(exclude: str) -> set[str]:
        return {batch_shape(line["batch_no"])
                for key, other in truths.items() if key != exclude
                for line in other["lines"] if line.get("batch_no")}

    noise = 0
    for key, truth in truths.items():
        flags = validate_invoice(truth, known_batch_shapes=vocabulary(key))
        if flags:
            noise += 1
            if noise <= 3:
                print(f"  !! unmutated {key} flagged: {flags[0]}")
    print(f"\n  false alarms on unmutated ground truth: {noise}/{len(truths)}")
    if noise:
        print("  ^ fix this before reading the table below")

    chosen = ({args.mutation: MUTATIONS[args.mutation]}
              if args.mutation else MUTATIONS)
    if args.mutation and args.mutation not in MUTATIONS:
        sys.exit(f"unknown mutation. try one of: {', '.join(MUTATIONS)}")

    rng = random.Random(SEED)
    print(f"\n  {'injected error':<28}{'caught':>8}{'n':>6}")
    print("  " + "-" * 42)
    total_caught = total = 0

    for name, mutate in chosen.items():
        caught = applied = 0
        for key, truth in truths.items():
            doc = copy.deepcopy(truth)
            if not mutate(doc, rng):
                continue          # the mutation did not apply to this invoice
            applied += 1
            if validate_invoice(doc, known_batch_shapes=vocabulary(key)):
                caught += 1
        total_caught += caught
        total += applied
        rate = f"{100 * caught / applied:.0f}%" if applied else "-"
        print(f"  {name:<28}{rate:>8}{applied:>6}")

    print("  " + "-" * 42)
    print(f"  {'ALL':<28}{100 * total_caught / total:>7.0f}%{total:>6}")
    print("\n  A mutation that changes nothing — transposing 11.00, dropping a")
    print("  line from a one-line invoice — is skipped, not counted as a miss.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
