"""Turn Unlimited-OCR's raw output into scoreable predictions.

    python3 bench/ocr/parse_unlimited.py
    python3 bench/ocr/score.py --predictions out/pred/unlimited-ocr

Reads the raw strings the Colab notebook brought back (out/raw/unlimited-ocr/)
and writes one prediction per invoice into out/pred/unlimited-ocr/, in the
ground-truth shape, so score.py treats it like any other system.

WHY PARSING IS A SEPARATE STEP
------------------------------
Unlimited-OCR is a document parser, not a schema-constrained extractor: its
native output is markdown. Gemini gets JSON for free via responseSchema, so
some of the gap between them is the harness rather than the model, and keeping
parsing local and re-runnable is what makes that gap adjustable without paying
for GPU time twice. The notebook saves both a JSON attempt and the native
markdown; this prefers the former and falls back to the latter.

THE MARKDOWN PATH IS A STARTING POINT
-------------------------------------
It was written before anyone had seen this model's real output, so the header
synonyms below are an educated guess at what it prints. Check
`out/raw/unlimited-ocr/inv_001.json` against them before trusting a low score —
a parser that silently misses a column looks exactly like a model that cannot
read one, and that is the same mistake the scorer itself already made once.
"""

import calendar
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
RAW = HERE / "out" / "raw" / "unlimited-ocr"
DEST = HERE / "out" / "pred" / "unlimited-ocr"

#: Printed header -> our field. Lowercased, non-alphanumerics stripped.
HEADERS = {
    "product_name": ["product", "description", "particulars", "item", "name", "goods"],
    "pack": ["pack", "packing", "packsize"],
    "batch_no": ["batch", "batchno", "bno", "lot"],
    "expiry_date": ["exp", "expiry", "expdt", "expdate"],
    "quantity": ["qty", "quantity", "nos"],
    "free_quantity": ["free", "sch", "scheme", "freeqty"],
    "mrp": ["mrp", "mrprs"],
    "rate": ["rate", "ptr", "price", "netrate", "pts"],
    "discount_pct": ["disc", "discount", "dis", "discpct"],
}
NUMERIC = {"quantity", "free_quantity", "mrp", "rate", "discount_pct"}

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def clean(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def number(text) -> float:
    """A printed money/quantity cell as a number. Blank and `-` mean zero."""
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[^0-9.\-]", "", str(text or ""))
    if s in ("", "-", "."):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def expiry(text) -> str | None:
    """`08/27`, `08-2027`, `AUG-27` -> the LAST day of that month."""
    s = str(text or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s

    month = year = None
    m = re.search(r"([A-Za-z]{3,})[^A-Za-z0-9]*(\d{2,4})", s)
    if m and m.group(1)[:3].lower() in MONTHS:
        month, year = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})[^0-9]+(\d{2,4})", s)
        if m:
            month, year = int(m.group(1)), int(m.group(2))
    if not month or year is None or not 1 <= month <= 12:
        return None

    # A two-digit year on a pharma invoice is this century; nothing on the
    # shelf expires in 1927.
    if year < 100:
        year += 2000
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def from_json(text: str) -> dict | None:
    """The JSON attempt, tolerant of a markdown fence or trailing prose."""
    if not text:
        return None
    body = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None


def from_markdown(text: str) -> dict | None:
    """Best effort on a pipe table: find the widest one and read its header."""
    if not text:
        return None
    rows = [r for r in text.splitlines() if r.count("|") >= 4]
    if len(rows) < 2:
        return None

    def cells(row):
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header, best = None, -1
    for index, row in enumerate(rows):
        got = cells(row)
        hits = sum(1 for c in got
                   for names in HEADERS.values() if clean(c) in names)
        if hits > best:
            header, best = index, hits
    if best < 3:
        return None

    columns = cells(rows[header])
    where = {}
    for position, label in enumerate(columns):
        for field, names in HEADERS.items():
            if clean(label) in names and field not in where:
                where[field] = position

    lines = []
    for row in rows[header + 1:]:
        got = cells(row)
        if len(got) < len(columns) - 1 or set("".join(got)) <= set("-: "):
            continue
        line = {"sn": len(lines) + 1}
        for field, position in where.items():
            if position >= len(got):
                continue
            raw = got[position]
            if field == "expiry_date":
                line[field] = expiry(raw)
            elif field in NUMERIC:
                line[field] = number(raw)
            else:
                line[field] = raw
        if line.get("product_name"):
            lines.append(line)

    return {"lines": lines} if lines else None


def normalise(record: dict) -> dict:
    """Coerce whatever we parsed into exactly the shape score.py expects."""
    out = {
        "invoice_number": record.get("invoice_number"),
        "invoice_date": record.get("invoice_date"),
        "supplier_name": record.get("supplier_name"),
        "supplier_gstin": record.get("supplier_gstin"),
        "drug_licence_no": record.get("drug_licence_no"),
        "lines": [],
        "totals": {},
    }
    for index, line in enumerate(record.get("lines") or [], 1):
        out["lines"].append({
            "sn": line.get("sn") or index,
            "product_name": line.get("product_name"),
            "batch_no": line.get("batch_no"),
            "expiry_date": expiry(line.get("expiry_date")),
            "quantity": number(line.get("quantity")),
            "free_quantity": number(line.get("free_quantity")),
            "mrp": number(line.get("mrp")),
            "rate": number(line.get("rate")),
            "discount_pct": number(line.get("discount_pct")),
        })
    totals = record.get("totals") or {}
    for field in ("taxable_amount", "cgst", "sgst", "igst", "grand_total"):
        out["totals"][field] = number(totals.get(field))
    return out


def main() -> None:
    if not RAW.exists():
        raise SystemExit(
            f"No raw output at {RAW}.\n"
            "Unzip the notebook's download so that inv_001.json lands there."
        )
    DEST.mkdir(parents=True, exist_ok=True)

    counts = {"json": 0, "markdown": 0, "failed": 0}
    for path in sorted(RAW.glob("inv_*.json")):
        raw = json.loads(path.read_text())
        record, how = from_json(raw.get("json", "")), "json"
        if record is None:
            record, how = from_markdown(raw.get("native", "")), "markdown"
        if record is None:
            counts["failed"] += 1
            print(f"  {path.stem}  no parse — check the raw output by hand")
            continue
        counts[how] += 1
        out = normalise(record)
        out["invoice_id"] = path.stem
        (DEST / f"{path.stem}.json").write_text(json.dumps(out, indent=1))

    print(f"\nparsed {counts['json']} from JSON, {counts['markdown']} from "
          f"markdown, {counts['failed']} unparseable")
    print(f"score it:  python score.py --predictions out/pred/unlimited-ocr")
    if counts["failed"]:
        print("\nA high failure count usually means the header synonyms in "
              "HEADERS do not match what this model prints. Read one raw file "
              "before concluding the model is bad at reading invoices.")


if __name__ == "__main__":
    main()
