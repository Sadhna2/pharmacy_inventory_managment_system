"""A fake OCR that fails the way real OCR fails — to validate the scorer.

    python3 bench/ocr/mock_ocr.py --quality good

This produces NO information about any real system. Its only job is to prove
that score.py detects each failure mode and attributes it correctly, so that
when a real model's numbers come back we know the scale is measuring
something. Running the scorer for the first time against a real system, with
no idea whether a 0% or a 100% would even be reported correctly, is how
benchmarks end up quietly broken.

The failure modes injected here are the ones this document class actually
produces: expiry read as the 1st of the month instead of the last, O/0 and
I/1 swapped in batch codes, MRP and rate columns transposed, the free-goods
column ignored, and the last line of a long table dropped.
"""

import argparse
import json
import random
import shutil
from datetime import date
from pathlib import Path

OUT = Path(__file__).parent / "out"

#: rate of each failure, by simulated quality tier
PROFILES = {
    "good":   dict(expiry_day=0.02, batch_confuse=0.03, swap_mrp_rate=0.01,
                   drop_free=0.05, drop_line=0.01, header_miss=0.02),
    "fair":   dict(expiry_day=0.15, batch_confuse=0.10, swap_mrp_rate=0.06,
                   drop_free=0.25, drop_line=0.05, header_miss=0.10),
    "poor":   dict(expiry_day=0.45, batch_confuse=0.30, swap_mrp_rate=0.20,
                   drop_free=0.60, drop_line=0.15, header_miss=0.35),
}
#: Noisier scans fail more often, whatever the underlying system.
NOISE_PENALTY = {"clean": 1.0, "scan_light": 1.3, "scan_heavy": 2.2, "photo": 2.8}
CONFUSIONS = str.maketrans("O0I1S5B8", "0O1IS5B8")


def corrupt(truth: dict, p: dict, rng: random.Random, noise: str) -> dict:
    scale = NOISE_PENALTY[noise]

    def hit(rate: float) -> bool:
        return rng.random() < min(rate * scale, 0.95)

    out = json.loads(json.dumps(truth))
    out.pop("_format", None)

    if hit(p["header_miss"]):
        out["supplier_gstin"] = (out["supplier_gstin"] or "").translate(CONFUSIONS)
    if hit(p["header_miss"]):
        out["drug_licence_no"] = None

    lines = []
    for line in out["lines"]:
        if hit(p["drop_line"]):
            continue
        if hit(p["expiry_day"]):
            # The classic: MM/YY read as the first of the month, not the last.
            d = date.fromisoformat(line["expiry_date"])
            line["expiry_date"] = d.replace(day=1).isoformat()
        if hit(p["batch_confuse"]):
            line["batch_no"] = line["batch_no"].translate(CONFUSIONS)
        if hit(p["swap_mrp_rate"]):
            line["mrp"], line["rate"] = line["rate"], line["mrp"]
        if hit(p["drop_free"]):
            line["free_quantity"] = 0
        lines.append(line)
    out["lines"] = lines
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality", choices=list(PROFILES), default="fair")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    dest = OUT / "pred" / f"mock-{args.quality}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    rng = random.Random(args.seed)
    index = {row["id"]: row for row in json.loads((OUT / "index.json").read_text())}
    for inv_id, meta in index.items():
        truth = json.loads((OUT / "truth" / f"{inv_id}.json").read_text())
        (dest / f"{inv_id}.json").write_text(
            json.dumps(corrupt(truth, PROFILES[args.quality], rng, meta["noise"]),
                       indent=2)
        )
    print(f"{len(index)} mock extractions ({args.quality}) -> {dest}")


if __name__ == "__main__":
    main()
