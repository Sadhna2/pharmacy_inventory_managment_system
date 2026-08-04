"""Measure product matching: how often does an invoice line find its product?

    python3 match_bench.py

The generator knows both what a distributor *printed* (`PCM-650 TAB`) and what
the product actually is (`PARACETAMOL 650MG TAB`), so match accuracy can be
measured without a database and without hand-labelling anything.

THE NUMBER THAT MATTERS IS THE MIDDLE ONE
-----------------------------------------
Three outcomes, and they are not equally bad:

    correct     the line is filled in and right
    unmatched   the line is empty and a human picks from a dropdown
    WRONG       the line is filled in and wrong

The third is the only unrecoverable one. Stock posts against the wrong product
and the receipt looks entirely normal afterwards — nothing downstream ever
questions it. An unmatched line is visible and costs ten seconds. So the
matcher is built to refuse rather than guess, and this benchmark exists mainly
to prove the WRONG column stays at zero.

COLD START AND CONVERGENCE
--------------------------
Brand names cannot be derived. `OMEZ-20` is not a truncation of `OMEPRAZOLE`,
it is a trade name, and no string rule reaches it without also reaching a dozen
wrong things. The design answer is not a cleverer rule but memory: a human
resolves it once, `remember_alias` writes it to `product_suppliers.supplier_sku`,
and every later invoice from that distributor matches exactly.

So the report shows both ends — the first delivery, and the steady state after
each distinct shorthand has been resolved once.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from app.ai.intake.match import normalise, resolve_product  # noqa: E402

OUT = Path(__file__).parent / "out"


@dataclass
class Catalogued:
    """The three fields `resolve_product` reads. Not an ORM object on purpose:
    the matcher must stay testable without a database."""

    id: int
    sku: str
    name: str


def load() -> tuple[list[Catalogued], list[tuple[str, int]], dict[str, int]]:
    catalogue: dict[str, int] = {}
    lines: list[tuple[str, int]] = []
    for path in sorted((OUT / "truth").glob("inv_*.json")):
        for line in json.loads(path.read_text())["lines"]:
            canonical = line["canonical_name"]
            catalogue.setdefault(canonical, len(catalogue) + 1)
            lines.append((line["product_name"], catalogue[canonical]))
    products = [Catalogued(i, f"SKU-{i:03d}", name) for name, i in catalogue.items()]
    return products, lines, catalogue


def run(products, lines, aliases) -> tuple[int, int, int, Counter]:
    correct = wrong = unmatched = 0
    misses: Counter[str] = Counter()
    for printed, want in lines:
        found, _method, _shortlist = resolve_product(printed, products, aliases)
        if found is None:
            unmatched += 1
            misses[printed] += 1
        elif found.id == want:
            correct += 1
        else:
            wrong += 1
            misses[f"WRONG  {printed}"] += 1
    return correct, wrong, unmatched, misses


def report(title: str, correct: int, wrong: int, unmatched: int, total: int) -> None:
    print(f"\n  {title}")
    print("  " + "-" * 54)
    print(f"  {'correct':<12}{correct:>5}/{total}  = {100 * correct / total:5.1f}%")
    print(f"  {'WRONG':<12}{wrong:>5}/{total}  = {100 * wrong / total:5.1f}%"
          f"{'   <- must stay at zero' if not wrong else '   <- INVESTIGATE'}")
    print(f"  {'unmatched':<12}{unmatched:>5}/{total}  = "
          f"{100 * unmatched / total:5.1f}%   (a dropdown, not a defect)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-unmatched", action="store_true",
                        help="show the shorthands a human has to resolve once")
    args = parser.parse_args()

    products, lines, _ = load()
    if not lines:
        sys.exit("no ground truth in out/truth — run generate.py first")
    total = len(lines)
    print(f"\n  Product matching — {len(products)} products, {total} invoice lines")

    correct, wrong, unmatched, misses = run(products, lines, {})
    report("FIRST DELIVERY  (nothing learned yet)", correct, wrong, unmatched, total)

    if args.list_unmatched and misses:
        print("\n  shorthands needing one human resolution:")
        for name, count in misses.most_common():
            print(f"    {count:3}x  {name}")

    # What `remember_alias` would have stored: one resolution per distinct
    # printed name. A name maps to exactly one product in this corpus, which is
    # asserted rather than assumed — if a distributor ever printed the same
    # shorthand for two products, an alias would be the wrong mechanism.
    by_name: dict[str, set[int]] = {}
    for printed, want in lines:
        by_name.setdefault(normalise(printed), set()).add(want)
    collisions = {n: ids for n, ids in by_name.items() if len(ids) > 1}
    if collisions:
        print(f"\n  !! {len(collisions)} shorthand(s) map to more than one product; "
              "aliasing would be unsafe for these")

    learned = {name: next(iter(ids)) for name, ids in by_name.items()
               if len(ids) == 1 and any(normalise(m) == name for m in misses)}
    correct2, wrong2, unmatched2, _ = run(products, lines, learned)
    report(f"STEADY STATE  (after {len(learned)} one-off resolutions)",
           correct2, wrong2, unmatched2, total)

    print(f"\n  {len(learned)} corrections buy "
          f"{100 * (correct2 - correct) / total:.1f} points of accuracy, once.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
