"""Draw every extraction error onto the image the model actually read.

    python3 show_errors.py --predictions out/pred/intake-v3

A table of mismatches tells you the model read `2027` where the invoice said
`2028`. It does not tell you the digit was sitting in a blurred mono font on a
third-generation photocopy. Looking at the pixels is how you find out whether
an error is a model weakness or an unreadable page, and those call for
different fixes.

HOW A CELL IS LOCATED
---------------------
Not by guessing coordinates. The invoice is re-rendered from its own HTML with
the offending cell filled magenta, cropped identically, and the magenta is then
found in the result — so the box lands exactly where the value is, whatever the
layout did.

The probe skips the blur, contrast and JPEG stages of `render.py` because those
change colour without moving anything. It keeps the rotation, which is the only
step that moves a pixel, so `photo` profiles line up too.

Outputs a full page per invoice with every error boxed, plus a zoom sheet where
the characters are large enough to argue about.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
OUT = HERE / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTH, HEIGHT = 1240, 1754
SCALE = 2
MARKER = (255, 0, 255)

RED = (220, 30, 40)
INK = (24, 24, 27)
MUTED = (110, 110, 118)
PAPER = (255, 255, 255)

#: Field name in the ground truth -> the column key used by `_format.columns`.
COLUMN_KEY = {
    "product_name": "product", "batch_no": "batch", "expiry_date": "exp",
    "quantity": "qty", "free_quantity": "free", "rate": "rate",
    "mrp": "mrp", "discount_pct": "disc",
}

#: Header fields, matched in the document's own markup.
HEADER_PATTERN = {
    "supplier_gstin": r"(GSTIN:\s*)([0-9A-Z]{15})",
    "drug_licence_no": r"(D\.L\. No\.:\s*)([0-9A-Z/]+)",
}


def _font(size: int, bold: bool = False):
    for path in (
        f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def shoot(html: Path, png: Path) -> None:
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--force-device-scale-factor={SCALE}", f"--window-size={WIDTH},{HEIGHT}",
         f"--screenshot={png}", f"file://{html.resolve()}"],
        check=True, capture_output=True, timeout=90,
    )


def content_box(img: Image.Image, margin: int = 36) -> tuple[int, int, int, int]:
    """The crop `render.py` applies, computed rather than assumed.

    Taken from the *unmarked* render so that adding a magenta cell cannot move
    the frame and shift every coordinate derived from it.
    """
    ink = img.convert("L").point(lambda p: 0 if p > 244 else 255)
    box = ink.getbbox()
    if box is None:
        return (0, 0, img.width, img.height)
    left, top, right, bottom = box
    m = margin * SCALE
    return (max(0, left - m), max(0, top - m),
            min(img.width, right + m), min(img.height, bottom + m))


def mark_html(source: str, field: str, line_no: int, columns: list[str]) -> str | None:
    """Fill one cell magenta, without disturbing the layout around it."""
    style = f'style="background:rgb{MARKER}"'

    if field in HEADER_PATTERN:
        pattern = HEADER_PATTERN[field]
        if not re.search(pattern, source):
            return None
        return re.sub(pattern, rf'\1<span {style}>\2</span>', source, count=1)

    key = COLUMN_KEY.get(field)
    if key is None or key not in columns:
        return None
    index = columns.index(key)

    # `class="items wide"` on wide layouts, so match the class list rather
    # than assuming `items` is the whole attribute.
    table = re.search(r'<table class="[^"]*\bitems\b[^"]*".*?</table>', source, re.S)
    if table is None:
        return None
    block = table.group(0)
    rows = re.findall(r"<tr>.*?</tr>", block, re.S)
    data_rows = [r for r in rows if "<td" in r]
    if line_no > len(data_rows):
        return None
    row = data_rows[line_no - 1]

    cells = re.findall(r"<td[^>]*>.*?</td>", row, re.S)
    if index >= len(cells):
        return None
    cell = cells[index]
    marked = re.sub(r"^<td", f"<td {style}", cell, count=1)
    return source.replace(block, block.replace(row, row.replace(cell, marked, 1), 1), 1)


def marker_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Where the magenta ended up. Tolerant, because JPEG is not."""
    pixels = img.convert("RGB").load()
    xs, ys = [], []
    for y in range(0, img.height, 2):
        for x in range(0, img.width, 2):
            r, g, b = pixels[x, y]
            if r > 150 and b > 150 and g < 110:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs) + 2, max(ys) + 2)


def locate(html_path: Path, truth: dict, errors: list[dict], work: Path) -> dict:
    """Bounding box for each error, in the coordinates of the degraded PNG."""
    source = html_path.read_text()
    columns = list(truth["_format"]["columns"])

    plain = work / "plain.png"
    shoot(html_path, plain)
    crop = content_box(Image.open(plain).convert("RGB"))

    boxes: dict[int, tuple[int, int, int, int]] = {}
    for n, error in enumerate(errors):
        marked = mark_html(source, error["field"], error["line_no"], columns)
        if marked is None:
            continue
        probe_html = work / f"probe_{n}.html"
        probe_html.write_text(marked)
        probe_png = work / f"probe_{n}.png"
        shoot(probe_html, probe_png)

        img = Image.open(probe_png).convert("RGB").crop(crop)
        # Rotation is the only stage of degrade() that moves a pixel. Blur,
        # contrast and JPEG change colour in place, so the probe skips them and
        # keeps its magenta legible.
        if truth["_format"]["noise"] == "photo":
            img = img.rotate(-1.4, resample=Image.BICUBIC, expand=False,
                             fillcolor=(246, 244, 240))
        box = marker_box(img)
        if box:
            boxes[n] = box
    return boxes


def annotate(page: Image.Image, errors, boxes) -> Image.Image:
    """The full page, every bad cell boxed and numbered, with a caption strip."""
    img = page.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    label_font = _font(30, bold=True)

    for n, _error in enumerate(errors):
        if n not in boxes:
            continue
        x0, y0, x1, y1 = boxes[n]
        pad = 6
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=RED, width=5)
        tag = str(n + 1)
        tw = draw.textlength(tag, font=label_font)
        draw.ellipse([x0 - pad - 34, y0 - pad - 17, x0 - pad - 34 + 34, y0 - pad + 17],
                     fill=RED)
        draw.text((x0 - pad - 17 - tw / 2, y0 - pad - 15), tag, font=label_font, fill=PAPER)

    strip_font, small = _font(30, bold=True), _font(26)
    line_h = 42
    strip = Image.new("RGB", (img.width, 30 + line_h * (len(errors) + 1) + 20), PAPER)
    d = ImageDraw.Draw(strip)
    y = 16
    for n, error in enumerate(errors):
        marker = f"{n + 1}." if n in boxes else "-."
        where = f"line {error['line_no']}" if error["line_no"] else "header"
        d.text((28, y), marker, font=strip_font, fill=RED)
        d.text((70, y), f"{where}  {error['field']}", font=strip_font, fill=INK)
        d.text((470, y), "invoice says", font=small, fill=MUTED)
        d.text((640, y), str(error["expected"]), font=strip_font, fill=INK)
        d.text((980, y), "model read", font=small, fill=MUTED)
        d.text((1140, y), str(error["got"]), font=strip_font, fill=RED)
        y += line_h

    out = Image.new("RGB", (img.width, img.height + strip.height), PAPER)
    out.paste(img, (0, 0))
    out.paste(strip, (0, img.height))
    return out


def zoom_panel(page: Image.Image, box, error, invoice_id: str, noise: str,
               width: int = 1180) -> Image.Image:
    """A close crop around one error, scaled up until the strokes are arguable."""
    x0, y0, x1, y1 = box
    pad_x, pad_y = 260, 46
    crop = page.convert("RGB").crop((
        max(0, x0 - pad_x), max(0, y0 - pad_y),
        min(page.width, x1 + pad_x), min(page.height, y1 + pad_y),
    ))
    factor = min(3.0, width / max(1, crop.width))
    crop = crop.resize((int(crop.width * factor), int(crop.height * factor)),
                       Image.LANCZOS)

    d = ImageDraw.Draw(crop)
    d.rectangle([(x0 - max(0, x0 - pad_x)) * factor - 4,
                 (y0 - max(0, y0 - pad_y)) * factor - 4,
                 (x1 - max(0, x0 - pad_x)) * factor + 4,
                 (y1 - max(0, y0 - pad_y)) * factor + 4], outline=RED, width=4)

    head, body, small = _font(30, bold=True), _font(28), _font(24)
    panel = Image.new("RGB", (width, crop.height + 108), PAPER)
    panel.paste(crop, ((width - crop.width) // 2, 74))
    p = ImageDraw.Draw(panel)
    where = f"line {error['line_no']}" if error["line_no"] else "header"
    p.text((20, 16), f"{invoice_id}  ·  {where}  ·  {error['field']}",
           font=head, fill=INK)
    p.text((20, 48), f"{noise} scan", font=small, fill=MUTED)
    p.text((20, crop.height + 80), "invoice says", font=small, fill=MUTED)
    p.text((175, crop.height + 76), str(error["expected"]), font=body, fill=INK)
    p.text((420, crop.height + 80), "model read", font=small, fill=MUTED)
    p.text((565, crop.height + 76), str(error["got"]), font=body, fill=RED)
    p.line([(0, panel.height - 1), (width, panel.height - 1)], fill=(226, 226, 232))
    return panel


def parse_errors(report_entry: dict) -> list[dict]:
    """Turn the scorer's error strings back into fields we can locate."""
    parsed = []
    for text in report_entry["errors"]:
        m = re.match(r"line (\d+) (\w+): expected '?(.*?)'?, got '?(.*?)'?$", text)
        if m:
            parsed.append({"line_no": int(m.group(1)), "field": m.group(2),
                           "expected": m.group(3), "got": m.group(4)})
            continue
        m = re.match(r"(\w+): expected '?(.*?)'?, got '?(.*?)'?$", text)
        if m:
            parsed.append({"line_no": 0, "field": m.group(1),
                           "expected": m.group(2), "got": m.group(3)})
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", default="out/pred/intake-v3")
    parser.add_argument("--dest", default="out/errors")
    args = parser.parse_args()

    if not Path(CHROME).exists():
        sys.exit(f"Chrome not found at {CHROME}")

    report_path = Path(args.predictions) / "_report.json"
    if not report_path.exists():
        sys.exit(f"no _report.json in {args.predictions} — run score.py first")
    report = json.loads(report_path.read_text())

    dest = Path(args.dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # Chrome is not permitted to read the macOS per-user temp directory, so the
    # probe pages are written beside the originals it already loads from.
    work = OUT / ".probe"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    panels: list[Image.Image] = []
    try:
        for entry in report:
            if not entry["errors"]:
                continue
            invoice_id = entry["invoice_id"]
            truth = json.loads((OUT / "truth" / f"{invoice_id}.json").read_text())
            errors = parse_errors(entry)
            if not errors:
                continue

            page = Image.open(OUT / "png@2x" / f"{invoice_id}.png")
            boxes = locate(OUT / "html" / f"{invoice_id}.html", truth, errors, work)

            annotate(page, errors, boxes).save(dest / f"{invoice_id}.png")
            noise = truth["_format"]["noise"]
            for n, error in enumerate(errors):
                if n in boxes:
                    panels.append(zoom_panel(page, boxes[n], error, invoice_id, noise))
            found = sum(1 for n in range(len(errors)) if n in boxes)
            print(f"  {invoice_id}  {len(errors)} error(s), {found} located  [{noise}]")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if panels:
        head = _font(38, bold=True)
        title = Image.new("RGB", (panels[0].width, 84), PAPER)
        ImageDraw.Draw(title).text(
            (20, 26), f"Every extraction error — {len(panels)} of "
                      f"{sum(len(parse_errors(e)) for e in report if e['errors'])}",
            font=head, fill=INK)
        sheet = Image.new("RGB", (panels[0].width,
                                  title.height + sum(p.height for p in panels)), PAPER)
        sheet.paste(title, (0, 0))
        y = title.height
        for panel in panels:
            sheet.paste(panel, (0, y))
            y += panel.height
        sheet.save(dest / "_all_errors.png")
        print(f"\nzoom sheet -> {dest}/_all_errors.png")
    print(f"annotated pages -> {dest}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
