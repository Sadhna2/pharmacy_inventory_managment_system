"""Extract the benchmark invoices with a Gemini vision model.

    python run_gemini.py --model gemini-3.1-flash-lite
    python run_gemini.py --limit 5            # smoke test first

Writes one JSON per invoice into `out/pred/<model>/`, in the ground-truth
shape, so `score.py --predictions out/pred/<model>` scores it like any other
system. Nothing here is specific to Gemini beyond the request format.

FREE-TIER RATE LIMITS
---------------------
15 requests/minute, 250k tokens/minute, 500 requests/day. One invoice is one
request, so 50 invoices is a tenth of the daily budget — but the per-minute
cap is the binding one, and hitting it returns 429 rather than queuing. The
throttle below paces requests at just under the limit instead of firing them
all and retrying, which is both faster overall and kinder to the quota.

WHY THE PROMPT IS SPECIFIC ABOUT FOUR THINGS
--------------------------------------------
They are the four traps in bench/ocr/README.md, and they are the difference
between a demo and something a pharmacist can use: an expiry read as the 1st
rather than the last of the month, a batch code "corrected" from O to 0, MRP
and rate transposed, and a free-goods column ignored. A general "read this
invoice" prompt gets all four wrong in a predictable way.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
OUT = HERE / "out"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

LINE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sn": {"type": "INTEGER"},
        "product_name": {"type": "STRING"},
        # Not scored. It is here to give the pack column somewhere to land:
        # with no field of its own, `1x200MD` had nowhere to go and its tail
        # kept being absorbed into the batch beside it ("EX73-25" -> "EX73-0MD"
        # on two separate invoices). A field the model must fill is a cheaper
        # fix than a prompt asking it not to make the mistake.
        "pack": {"type": "STRING"},
        "batch_no": {"type": "STRING"},
        "expiry_date": {"type": "STRING"},
        "quantity": {"type": "NUMBER"},
        "free_quantity": {"type": "NUMBER"},
        "mrp": {"type": "NUMBER"},
        "rate": {"type": "NUMBER"},
        "discount_pct": {"type": "NUMBER"},
    },
    "required": [
        "sn", "product_name", "pack", "batch_no", "expiry_date",
        "quantity", "free_quantity", "mrp", "rate", "discount_pct",
    ],
}

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "invoice_number": {"type": "STRING"},
        "invoice_date": {"type": "STRING"},
        "supplier_name": {"type": "STRING"},
        "supplier_gstin": {"type": "STRING"},
        "drug_licence_no": {"type": "STRING"},
        "lines": {"type": "ARRAY", "items": LINE_SCHEMA},
        "totals": {
            "type": "OBJECT",
            "properties": {
                "taxable_amount": {"type": "NUMBER"},
                "cgst": {"type": "NUMBER"},
                "sgst": {"type": "NUMBER"},
                "igst": {"type": "NUMBER"},
                "grand_total": {"type": "NUMBER"},
            },
            "required": ["taxable_amount", "cgst", "sgst", "igst", "grand_total"],
        },
    },
    "required": [
        "invoice_number", "invoice_date", "supplier_name",
        "supplier_gstin", "drug_licence_no", "lines", "totals",
    ],
}

PROMPT = """You are reading a scanned Indian pharmaceutical distributor's tax invoice.
Extract it exactly. This feeds a goods-receipt entry, so a wrong value becomes wrong stock.

Rules that matter more than anything else:

1. EXPIRY IS A MONTH, NOT A DAY. Invoices print expiry as `08/27`, `08-27`,
   `AUG-27` or `08/2027`. All of them mean the stock is good through the LAST
   day of that month. Return `2027-08-31`, never `2027-08-01`.
   Use the last calendar day: 28/29 for February, 30 or 31 otherwise.
   Check leap years — February 2028 ends on the 29th, not the 28th.

1b. THE EXPIRY YEAR IS PRINTED ON THE PAGE. Read it. Do not reuse the
   invoice's year, and do not infer a year from context. In `08-27` the `08`
   is the month and the `27` is the year 2027 — never a day of the month.
   Sanity-check yourself: pharmaceutical stock is dispatched with roughly six
   months to four years of shelf life left. If the expiry you produce is
   earlier than the invoice date, or less than three months after it, you have
   misread the year — go back to the pixels and read those digits again.

2. BATCH NUMBERS ARE COPIED CHARACTER BY CHARACTER. Do not tidy them, do not
   convert letter O to digit 0 or letter I to digit 1, do not change case.
   A recall traces by exact batch code, so a "corrected" batch is a lost batch.

3. MRP IS NOT RATE. MRP is the maximum retail price printed on the pack — the
   legal ceiling. Rate is what the buyer pays the distributor, and is always
   lower. If a column is headed MRP, PTR, Rate, Price or Net Rate, decide from
   the heading and the relative size, and never swap them.

4. FREE GOODS ARE A SEPARATE QUANTITY. A scheme like `10+1`, `Free: 1` or a
   column headed FREE/SCH means extra units delivered but not charged. Put the
   charged units in `quantity` and the free units in `free_quantity`. If there
   is no free-goods column, `free_quantity` is 0.

5. EVERY COLUMN BELONGS TO EXACTLY ONE FIELD. A narrow column wraps onto two
   printed lines — `1x200MD` becomes `1x20` above `0MD`, and `EX73-25` becomes
   `EX73-` above `25`. Both halves stay in their own column. Never carry
   characters sideways from the column next door to complete a value, and
   never take a value from the row above or below.
   Put the pack or packing size (`1x200MD`, `10x10`, `1x100ML`) in `pack`.
   If there is no pack column, `pack` is an empty string.

Also:
- `invoice_date` in YYYY-MM-DD.
- `discount_pct` is the percentage, so 5% is 5, not 0.05. Absent means 0.
- Money as numbers, no currency symbol, no thousands separators.
- `cgst`/`sgst` are filled on an intra-state invoice and `igst` on inter-state.
  The one that does not apply is 0, not null.
- One entry in `lines` per product row, in printed order, `sn` starting at 1.
  Do not merge rows, do not invent rows, do not skip a row because it is faint.
"""


def extract(client: httpx.Client, model: str, key: str, png: Path, retries: int = 4) -> dict:
    body = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(png.read_bytes()).decode(),
                }},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            # Deterministic: this is transcription, not writing.
            "temperature": 0,
        },
    }

    delay = 5.0
    for attempt in range(retries):
        response = client.post(
            ENDPOINT.format(model=model), params={"key": key}, json=body, timeout=180.0
        )
        if response.status_code in (429, 500, 503):
            # 429 here means the per-minute cap; waiting is the only fix.
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        payload = response.json()
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            reason = payload.get("candidates", [{}])[0].get("finishReason", "?")
            raise RuntimeError(f"no content (finishReason={reason})") from None
        return json.loads(text)

    raise RuntimeError(f"gave up after {retries} attempts (rate limited)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument(
        "--rpm", type=float, default=14.0,
        help="Requests per minute. Free tier allows 15; stay just under.",
    )
    parser.add_argument("--name", default=None, help="Output folder name")
    parser.add_argument(
        "--png-dir", default="png",
        help="Image set under out/ to read. `png` is ~150 dpi; render.py "
             "--scale 2 writes `png@2x` at ~300 dpi.",
    )
    args = parser.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        env = HERE.parents[1] / ".env"
        if env.exists():
            for raw in env.read_text().splitlines():
                if raw.startswith("GEMINI_API_KEY="):
                    key = raw.split("=", 1)[1].strip()
    if not key:
        sys.exit("GEMINI_API_KEY not set, and not found in the project .env")

    pngs = sorted((OUT / args.png_dir).glob("inv_*.png"))
    if args.limit:
        pngs = pngs[: args.limit]
    if not pngs:
        sys.exit(f"No rendered invoices in out/{args.png_dir}. "
                 "Run generate.py then render.py first.")

    dest = OUT / "pred" / (args.name or args.model)
    dest.mkdir(parents=True, exist_ok=True)

    gap = 60.0 / args.rpm
    failures = 0
    started = time.monotonic()

    with httpx.Client() as client:
        for index, png in enumerate(pngs, 1):
            invoice_id = png.stem
            slot = started + (index - 1) * gap
            pause = slot - time.monotonic()
            if pause > 0:
                time.sleep(pause)

            began = time.monotonic()
            try:
                record = extract(client, args.model, key, png)
                record["invoice_id"] = invoice_id
                (dest / f"{invoice_id}.json").write_text(json.dumps(record, indent=1))
                note = f"{len(record.get('lines', []))} lines"
            except Exception as exc:  # noqa: BLE001 — one bad invoice must not stop the run
                failures += 1
                note = f"FAILED: {str(exc)[:70]}"
            print(f"  {invoice_id}  {time.monotonic() - began:5.1f}s  {note}", flush=True)

    done = len(pngs) - failures
    print(f"\n{done}/{len(pngs)} extracted into {dest.relative_to(HERE)}")
    if failures:
        print(f"{failures} failed — rerun to fill the gaps, the scorer counts a missing file as a total miss")
    print(f"score it:  python score.py --predictions out/pred/{dest.name}")


if __name__ == "__main__":
    main()
