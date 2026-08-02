"""Generate 50 synthetic distributor invoices with exact ground truth.

    python3 bench/ocr/generate.py            # 50 invoices -> out/
    python3 bench/ocr/generate.py --count 20

Each invoice produces two files:

    out/html/inv_001.html    the document
    out/truth/inv_001.json   every field, exactly

The ground truth is generated FIRST and the document rendered FROM it, so the
answer key cannot drift from what is on the page. That is the whole reason for
synthesising rather than collecting real invoices: with real ones somebody has
to hand-label 50 documents, and hand-labels are themselves ~2% wrong.

Formats vary along six independent axes — layout, font, column set, tax
presentation, date format and scan quality — which is what makes this a test of
generalisation rather than of one template.
"""

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from catalogue import (
    COLUMN_SETS,
    DISTRIBUTORS,
    EXPIRY_FORMATS,
    FONTS,
    INVOICE_DATE_FORMATS,
    LAYOUTS,
    MANUFACTURERS,
    NOISE_PROFILES,
    PRODUCTS,
    TAX_STYLES,
)

OUT = Path(__file__).parent / "out"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Deliberately confusable characters. Real batch numbers are printed in
# whatever font the manufacturer chose, and 0/O and 1/I are the classic
# failure — worth having in the answer key so the score reflects it.
BATCH_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"


def _batch(rng: random.Random) -> str:
    shape = rng.choice(["AA0000", "A00000", "AAA000", "0000AA", "AA00-00"])
    out = []
    for ch in shape:
        if ch == "A":
            out.append(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"))
        elif ch == "0":
            out.append(rng.choice("0123456789"))
        else:
            out.append(ch)
    return "".join(out)


def _expiry(rng: random.Random, invoice_day: date) -> date:
    """Somewhere between 6 and 36 months out, always the last day of a month.

    Pharma expiry is a month, not a day: "08/27" means stock is good through
    31 August 2027. Anything that parses it as the 1st is wrong by a month, and
    that month is the difference between accepting and rejecting a delivery.
    """
    months = rng.randint(6, 36)
    year = invoice_day.year + (invoice_day.month + months - 1) // 12
    month = (invoice_day.month + months - 1) % 12 + 1
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _fmt_expiry(d: date, style: str) -> str:
    if style == "mm/yy":
        return f"{d.month:02d}/{d.year % 100:02d}"
    if style == "mm-yy":
        return f"{d.month:02d}-{d.year % 100:02d}"
    if style == "mon-yy":
        return f"{MONTHS[d.month - 1]}-{d.year % 100:02d}"
    return f"{d.month:02d}/{d.year}"


def _fmt_date(d: date, style: str) -> str:
    if style == "dd/mm/yyyy":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    if style == "dd-mm-yyyy":
        return f"{d.day:02d}-{d.month:02d}-{d.year}"
    if style == "dd.mm.yyyy":
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    return f"{d.day:02d}-{MONTHS[d.month - 1].title()}-{d.year}"


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def build_truth(index: int, rng: random.Random) -> dict:
    """The invoice as data. The HTML is a rendering of exactly this."""
    dist_name, city, state, gst_prefix, area = rng.choice(DISTRIBUTORS)
    inv_day = date(2026, rng.randint(1, 8), rng.randint(1, 28))

    layout = LAYOUTS[index % len(LAYOUTS)]
    font_css, font_name = FONTS[index % len(FONTS)]
    columns = COLUMN_SETS[index % len(COLUMN_SETS)]
    tax_style = TAX_STYLES[index % len(TAX_STYLES)]
    exp_fmt = EXPIRY_FORMATS[index % len(EXPIRY_FORMATS)]
    date_fmt = INVOICE_DATE_FORMATS[index % len(INVOICE_DATE_FORMATS)]
    noise = NOISE_PROFILES[index % len(NOISE_PROFILES)]

    # Half the invoices abbreviate product names the way a distributor's own
    # system does. Matching "PCM-650 TAB" back to "Paracetamol 650mg" is a
    # genuine part of the job, not a trick.
    abbreviate = rng.random() < 0.5
    # Intra-state means CGST+SGST; our buyer is in Maharashtra (27).
    intra_state = state == "27"

    lines = []
    for n, product in enumerate(rng.sample(PRODUCTS, rng.randint(3, 14)), start=1):
        name, pack, hsn, gst_rate, mrp_range, abbrev = product
        mrp = _money(rng.uniform(*mrp_range))
        # Distributors buy at a margin off MRP — 18% to 32% is the usual band.
        rate = _money(mrp * rng.uniform(0.68, 0.82))
        qty = rng.choice([1, 2, 3, 5, 10, 10, 20, 25, 50, 100])
        # Scheme goods: "10+1 free" is how the trade discounts without
        # touching the printed rate.
        free = rng.choice([0, 0, 0, 0, 1, 2, qty // 10]) if qty >= 10 else 0
        disc = rng.choice([0.0, 0.0, 2.5, 5.0, 7.5, 10.0])

        gross = _money(rate * qty)
        after_disc = _money(gross * (1 - disc / 100))
        tax = _money(after_disc * gst_rate / 100)

        lines.append({
            "sn": n,
            "product_name": abbrev if abbreviate else name,
            "canonical_name": name,
            "manufacturer": rng.choice(MANUFACTURERS),
            "pack": pack,
            "hsn": hsn,
            "batch_no": _batch(rng),
            "expiry_date": _expiry(rng, inv_day).isoformat(),
            "quantity": qty,
            "free_quantity": free,
            "mrp": mrp,
            "rate": rate,
            "discount_pct": disc,
            "gst_rate": gst_rate,
            "taxable_amount": after_disc,
            "tax_amount": tax,
            "line_total": _money(after_disc + tax),
        })

    taxable = _money(sum(x["taxable_amount"] for x in lines))
    tax_total = _money(sum(x["tax_amount"] for x in lines))
    gross_total = _money(taxable + tax_total)
    # Indian invoices round the payable to the rupee and show the adjustment.
    rounded = float(round(gross_total))
    round_off = _money(rounded - gross_total)

    return {
        "invoice_id": f"inv_{index:03d}",
        "invoice_number": f"{rng.choice(['GST', 'INV', 'SI', 'TI'])}/"
                          f"{inv_day.year % 100:02d}-{(inv_day.year + 1) % 100:02d}/"
                          f"{rng.randint(1000, 9999)}",
        "invoice_date": inv_day.isoformat(),
        "supplier_name": dist_name,
        "supplier_gstin": f"{gst_prefix}{rng.randint(1000, 9999)}"
                          f"{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}1Z"
                          f"{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ0123456789')}",
        "supplier_address": f"{area}, {city} - {rng.randint(110000, 700000)}",
        "supplier_state_code": state,
        "drug_licence_no": f"{rng.randint(20, 21)}B/{rng.randint(1000, 9999)}/"
                           f"{rng.randint(2018, 2025)}",
        "is_intra_state": intra_state,
        "lines": lines,
        "totals": {
            "taxable_amount": taxable,
            "cgst": _money(tax_total / 2) if intra_state else 0.0,
            "sgst": _money(tax_total / 2) if intra_state else 0.0,
            "igst": 0.0 if intra_state else tax_total,
            "round_off": round_off,
            "grand_total": rounded,
        },
        # Not printed on the page — this is how a failure gets attributed to a
        # format rather than to "OCR is bad".
        "_format": {
            "layout": layout,
            "font": font_name,
            "columns": columns,
            "tax_style": tax_style,
            "expiry_format": exp_fmt,
            "date_format": date_fmt,
            "noise": noise,
            "abbreviated_names": abbreviate,
            "line_count": len(lines),
        },
    }


# --- rendering --------------------------------------------------------------

HEADERS = {
    "sn": "S.N.", "product": "PRODUCT DESCRIPTION", "pack": "PACK", "hsn": "HSN",
    "batch": "BATCH", "exp": "EXP", "qty": "QTY", "free": "FREE", "mrp": "M.R.P.",
    "rate": "RATE", "disc": "DIS%", "amount": "AMOUNT",
}
NUMERIC = {"sn", "qty", "free", "mrp", "rate", "disc", "amount"}

#: Relative width of each column under `table-layout: fixed`. Product
#: descriptions run to 25 characters; a serial number never exceeds two.
WIDTH_UNITS = {
    "sn": 3, "product": 26, "pack": 7, "hsn": 6, "batch": 9, "exp": 7,
    "qty": 5, "free": 5, "mrp": 8, "rate": 8, "disc": 5, "amount": 10,
}
TAX_COL_UNITS = 9


def _cell(key: str, line: dict, fmt: dict) -> str:
    return {
        "sn": str(line["sn"]),
        "product": line["product_name"],
        "pack": line["pack"],
        "hsn": line["hsn"],
        "batch": line["batch_no"],
        "exp": _fmt_expiry(date.fromisoformat(line["expiry_date"]), fmt["expiry_format"]),
        "qty": str(line["quantity"]),
        "free": str(line["free_quantity"]) if line["free_quantity"] else "-",
        "mrp": f"{line['mrp']:.2f}",
        "rate": f"{line['rate']:.2f}",
        "disc": f"{line['discount_pct']:.1f}" if line["discount_pct"] else "-",
        "amount": f"{line['taxable_amount']:.2f}",
    }[key]


def _styles(fmt: dict, font_css: str) -> str:
    """Per-layout CSS. Each layout is a different document, not a reskin."""
    base = f"""
      @page {{ size: A4; margin: 0; }}
      * {{ box-sizing: border-box; }}
      body {{ font-family: {font_css}; margin: 0; padding: 12mm 10mm;
              width: 210mm; min-height: 297mm; background: #fff; color: #000;
              font-size: 9.5pt; }}
      table {{ width: 100%; border-collapse: collapse; }}
      .r {{ text-align: right; }}  .c {{ text-align: center; }}
      .sm {{ font-size: 8pt; }}    .b {{ font-weight: bold; }}
      .title {{ font-size: 15pt; font-weight: bold; letter-spacing: .5px; }}
      .muted {{ color: #333; }}
      tfoot td {{ padding-top: 4px; }}
      /* Twelve columns do not fit A4 at 9.5pt, and a column bleeding past the
         ruled box reads as a rendering bug rather than as a real invoice.
         Distributors solve this the same way: they shrink the type. */
      .items {{ table-layout: fixed; }}
      .items.wide {{ font-size: 7.5pt; }}
      .items.wide th, .items.wide td {{ padding-left: 2px; padding-right: 2px; }}
      .items td, .items th {{ word-break: break-word; }}
    """
    variants = {
        "classic": """
          body { padding: 10mm; }
          .doc { border: 2px solid #000; padding: 6px; }
          .head { border-bottom: 2px solid #000; padding-bottom: 6px; }
          .items th { border: 1px solid #000; background: #eee; padding: 4px 3px;
                      font-size: 8pt; }
          .items td { border: 1px solid #000; padding: 3px; }
        """,
        "boxed": """
          .doc { border: 1px solid #000; }
          .head { display: flex; justify-content: space-between;
                  border-bottom: 1px solid #000; padding: 8px; }
          .partybox { border-bottom: 1px solid #000; padding: 6px 8px; }
          .items { border-top: 1px solid #000; }
          .items th { border-bottom: 1px solid #000; border-right: 1px solid #000;
                      padding: 5px 3px; font-size: 8pt; background: #f4f4f4; }
          .items td { border-right: 1px solid #000; padding: 4px 3px; }
          .items tr td { border-bottom: 1px dotted #999; }
        """,
        "minimal": """
          .doc { }
          .head { padding-bottom: 10px; }
          .items th { border-bottom: 1.5px solid #000; padding: 6px 4px;
                      font-size: 8pt; text-align: left; }
          .items td { border-bottom: 1px solid #ddd; padding: 5px 4px; }
        """,
        "dense": """
          body { font-size: 8pt; padding: 8mm; }
          .doc { border: 1px solid #000; padding: 4px; }
          .head { border-bottom: 1px solid #000; padding-bottom: 4px; }
          .items th { border-bottom: 1px solid #000; padding: 2px;
                      font-size: 7pt; background: #ddd; }
          .items td { padding: 1.5px 2px; border-bottom: 1px solid #eee; }
        """,
        "twocol": """
          .doc { border: 1px double #000; padding: 8px; }
          .head { text-align: center; border-bottom: 3px double #000;
                  padding-bottom: 8px; }
          .parties { display: flex; gap: 12px; margin: 8px 0; }
          .parties > div { flex: 1; border: 1px solid #666; padding: 6px; }
          .items th { border-top: 1px solid #000; border-bottom: 1px solid #000;
                      padding: 5px 3px; font-size: 8pt; }
          .items td { padding: 4px 3px; }
          .items tbody tr:nth-child(even) { background: #f7f7f7; }
        """,
    }
    return base + variants[fmt["layout"]]


def render_html(truth: dict, font_css: str) -> str:
    fmt = truth["_format"]
    cols = fmt["columns"]
    t = truth["totals"]
    tax_style = fmt["tax_style"]

    units = [WIDTH_UNITS[c] for c in cols]
    head_cells = "".join(
        f'<th class="{"r" if c in NUMERIC else ""}">{HEADERS[c]}</th>' for c in cols
    )
    # Tax on the line itself, when the format shows it there.
    if tax_style == "split_columns":
        head_cells += '<th class="r">CGST</th><th class="r">SGST</th>' \
            if truth["is_intra_state"] else '<th class="r">IGST</th>'
    elif tax_style == "single_column":
        head_cells += '<th class="r">GST%</th>'

    body_rows = []
    for line in truth["lines"]:
        cells = "".join(
            f'<td class="{"r" if c in NUMERIC else ""}">{_cell(c, line, fmt)}</td>'
            for c in cols
        )
        if tax_style == "split_columns":
            half = line["tax_amount"] / 2
            cells += (
                f'<td class="r">{half:.2f}</td><td class="r">{half:.2f}</td>'
                if truth["is_intra_state"]
                else f'<td class="r">{line["tax_amount"]:.2f}</td>'
            )
        elif tax_style == "single_column":
            cells += f'<td class="r">{line["gst_rate"]}%</td>'
        body_rows.append(f"<tr>{cells}</tr>")

    tax_cols = (
        2 if tax_style == "split_columns" and truth["is_intra_state"]
        else 1 if tax_style != "footer_summary" else 0
    )
    wide = " wide" if len(cols) + tax_cols >= 11 else ""

    total_units = sum(units) + tax_cols * TAX_COL_UNITS
    colgroup = "".join(
        f'<col style="width:{u * 100 / total_units:.2f}%">'
        for u in units + [TAX_COL_UNITS] * tax_cols
    )

    span = len(cols) - 1 + (
        2 if tax_style == "split_columns" and truth["is_intra_state"]
        else 1 if tax_style != "footer_summary" else 0
    )

    # HSN-wise tax table — the only place the tax appears in this variant.
    hsn_table = ""
    if tax_style == "footer_summary":
        by_hsn: dict[str, list[float]] = {}
        for line in truth["lines"]:
            slot = by_hsn.setdefault(line["hsn"], [0.0, 0.0, line["gst_rate"]])
            slot[0] += line["taxable_amount"]
            slot[1] += line["tax_amount"]
        rows = "".join(
            f"<tr><td>{hsn}</td><td class='r'>{v[0]:.2f}</td>"
            f"<td class='r'>{v[2]}%</td><td class='r'>{v[1]:.2f}</td></tr>"
            for hsn, v in sorted(by_hsn.items())
        )
        hsn_table = (
            "<table class='items sm' style='margin-top:8px;width:60%'>"
            "<thead><tr><th>HSN</th><th class='r'>TAXABLE</th>"
            "<th class='r'>RATE</th><th class='r'>TAX</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    tax_rows = (
        f"<tr><td class='r' colspan='{span}'>CGST</td>"
        f"<td class='r'>{t['cgst']:.2f}</td></tr>"
        f"<tr><td class='r' colspan='{span}'>SGST</td>"
        f"<td class='r'>{t['sgst']:.2f}</td></tr>"
        if truth["is_intra_state"]
        else f"<tr><td class='r' colspan='{span}'>IGST</td>"
             f"<td class='r'>{t['igst']:.2f}</td></tr>"
    )

    party_block = f"""
      <div><span class="b">Billed to:</span><br>
        SADHNA PHARMACY CHAIN PVT LTD<br>
        Central Warehouse, Andheri East, Mumbai - 400059<br>
        GSTIN: 27AAACP1234A1Z5 &nbsp; D.L.: 20B/1122/2019<br>
        State: Maharashtra (27)
      </div>"""

    return f"""<meta charset="utf-8"><style>{_styles(fmt, font_css)}</style>
<div class="doc">
  <div class="head">
    <div class="title">{truth['supplier_name']}</div>
    <div class="sm muted">{truth['supplier_address']}<br>
      GSTIN: {truth['supplier_gstin']} &nbsp;|&nbsp;
      D.L. No.: {truth['drug_licence_no']}</div>
    <div class="b" style="margin-top:6px">TAX INVOICE</div>
  </div>

  <table class="sm" style="margin:8px 0">
    <tr>
      <td><span class="b">Invoice No.:</span> {truth['invoice_number']}</td>
      <td class="r"><span class="b">Date:</span>
        {_fmt_date(date.fromisoformat(truth['invoice_date']), fmt['date_format'])}</td>
    </tr>
  </table>

  <div class="{'parties' if fmt['layout'] == 'twocol' else 'partybox'} sm">
    {party_block}
  </div>

  <table class="items{wide}">
    <colgroup>{colgroup}</colgroup>
    <thead><tr>{head_cells}</tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
    <tfoot>
      <tr><td class="r b" colspan="{span}">Taxable Value</td>
          <td class="r b">{t['taxable_amount']:.2f}</td></tr>
      {tax_rows}
      <tr><td class="r" colspan="{span}">Round Off</td>
          <td class="r">{t['round_off']:+.2f}</td></tr>
      <tr><td class="r b" colspan="{span}">GRAND TOTAL</td>
          <td class="r b">{t['grand_total']:.2f}</td></tr>
    </tfoot>
  </table>

  {hsn_table}

  <div class="sm muted" style="margin-top:14px">
    Goods once sold will not be taken back. Subject to
    {truth['supplier_address'].split(',')[-2].strip()} jurisdiction.<br>
    Certified that the particulars given above are true and correct.
    <div class="r b" style="margin-top:18px">
      For {truth['supplier_name']}<br><br>Authorised Signatory</div>
  </div>
</div>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260801)
    args = parser.parse_args()

    (OUT / "html").mkdir(parents=True, exist_ok=True)
    (OUT / "truth").mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    index_rows = []
    for i in range(1, args.count + 1):
        truth = build_truth(i, rng)
        font_css = FONTS[i % len(FONTS)][0]
        (OUT / "html" / f"{truth['invoice_id']}.html").write_text(
            render_html(truth, font_css), encoding="utf-8"
        )
        (OUT / "truth" / f"{truth['invoice_id']}.json").write_text(
            json.dumps(truth, indent=2), encoding="utf-8"
        )
        index_rows.append({
            "id": truth["invoice_id"],
            **truth["_format"],
            "supplier": truth["supplier_name"],
        })

    (OUT / "index.json").write_text(json.dumps(index_rows, indent=2), encoding="utf-8")
    print(f"{args.count} invoices -> {OUT}")
    for axis in ("layout", "font", "tax_style", "expiry_format", "noise"):
        counts: dict[str, int] = {}
        for row in index_rows:
            counts[str(row[axis])] = counts.get(str(row[axis]), 0) + 1
        print(f"  {axis:14} {counts}")


if __name__ == "__main__":
    main()
