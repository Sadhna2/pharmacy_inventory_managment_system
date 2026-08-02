"""Render the generated HTML to images, then degrade them like real scans.

    python3 bench/ocr/render.py

Chrome headless gives a clean, born-digital page. That is the easy case and
not what arrives in a pharmacy — invoices come in as flatbed scans, third-
generation photocopies, and phone photos taken at an angle under a tube light.
An OCR benchmark on clean renders overstates accuracy by a wide margin, so
each image is degraded according to the noise profile fixed in its ground
truth, and the score can be broken down by that profile afterwards.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

OUT = Path(__file__).parent / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTH, HEIGHT = 1240, 1754  # A4 at ~150 dpi


def shoot(html: Path, png: Path, scale: int = 1) -> None:
    # The window size stays in CSS pixels, so the page lays out identically at
    # every scale — only the sampling density changes. That is what makes the
    # scales comparable: same wrapping, same column widths, more pixels.
    subprocess.run(
        [
            CHROME, "--headless", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", f"--force-device-scale-factor={scale}",
            f"--window-size={WIDTH},{HEIGHT}",
            f"--screenshot={png}", f"file://{html.resolve()}",
        ],
        check=True, capture_output=True, timeout=90,
    )


def crop_to_content(png: Path, margin: int = 36, scale: int = 1) -> None:
    """Trim the blank tail of the A4 sheet.

    A four-line invoice occupies the top third of the page, and the rest is
    paper. Keeping it costs real money when the image goes to a vision model —
    tokens scale with pixels — and teaches the benchmark nothing. Trim before
    degrading, so the noise is applied to the region that matters.
    """
    img = Image.open(png).convert("RGB")
    ink = img.convert("L").point(lambda p: 0 if p > 244 else 255)
    box = ink.getbbox()
    if box is None:
        return
    margin *= scale
    left, top, right, bottom = box
    img.crop((
        max(0, left - margin), max(0, top - margin),
        min(img.width, right + margin), min(img.height, bottom + margin),
    )).save(png)


def degrade(png: Path, profile: str, scale: int = 1) -> None:
    """Apply the wear that separates a PDF from what the pharmacist hands you.

    Blur radii are multiplied by `scale`. A 300 dpi scan of a page is not a
    sharper page — it is the same physical smudge measured with twice as many
    pixels, so the radius in pixels doubles with it. Leaving the radii fixed
    would make a higher-resolution render quietly *cleaner* as well as larger,
    and any accuracy gain could no longer be attributed to resolution.
    """
    if profile == "clean":
        return

    img = Image.open(png).convert("RGB")

    if profile == "scan_light":
        # Flatbed: slight grey cast, a touch of softness, faint sensor noise.
        img = ImageEnhance.Contrast(img).enhance(0.92)
        img = ImageEnhance.Brightness(img).enhance(0.97)
        img = img.filter(ImageFilter.GaussianBlur(0.4 * scale))
        img.save(png, quality=88)
        return

    if profile == "scan_heavy":
        # Photocopy of a photocopy: crushed midtones, thickened strokes,
        # the grey wash that eats thin serifs and decimal points.
        img = ImageEnhance.Contrast(img).enhance(1.35)
        img = ImageEnhance.Brightness(img).enhance(0.88)
        img = img.filter(ImageFilter.GaussianBlur(0.8 * scale))
        img = img.convert("L").point(lambda p: min(255, int(p * 1.06))).convert("RGB")
        img.save(png, quality=62)
        return

    # photo: shot handheld — rotated, unevenly lit, JPEG-compressed twice.
    img = img.rotate(-1.4, resample=Image.BICUBIC, expand=False, fillcolor=(246, 244, 240))
    width, height = img.size
    # A soft diagonal gradient stands in for a window on one side of the page.
    shade = Image.linear_gradient("L").resize((width, height)).rotate(30, fillcolor=128)
    shade = ImageEnhance.Contrast(shade).enhance(0.28)
    img = Image.composite(img, ImageEnhance.Brightness(img).enhance(0.74), shade)
    img = img.filter(ImageFilter.GaussianBlur(0.6 * scale))
    img.save(png, quality=52)
    img = Image.open(png)
    img.save(png, quality=58)  # second generation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scale", type=int, default=1,
        help="Sampling density. 1 is ~150 dpi, 2 is ~300 dpi — the scanning "
             "standard for OCR. Renders to out/png@<n>x when above 1, so the "
             "existing set and its scores stay intact for comparison.",
    )
    args = parser.parse_args()

    if not Path(CHROME).exists():
        raise SystemExit(f"Chrome not found at {CHROME}")

    scale = args.scale
    png_dir = OUT / ("png" if scale == 1 else f"png@{scale}x")
    if png_dir.exists():
        shutil.rmtree(png_dir)
    png_dir.mkdir(parents=True)

    index = json.loads((OUT / "index.json").read_text())
    for row in index:
        html = OUT / "html" / f"{row['id']}.html"
        png = png_dir / f"{row['id']}.png"
        shoot(html, png, scale)
        crop_to_content(png, scale=scale)
        degrade(png, row["noise"], scale)
        print(f"  {row['id']}  {row['layout']:8} {row['font']:9} {row['noise']}")

    total = sum(p.stat().st_size for p in png_dir.glob("*.png"))
    print(f"\n{len(index)} images -> {png_dir}  ({total / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
