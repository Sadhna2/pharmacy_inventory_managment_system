"""Run the benchmark through the *shipping* extraction path.

`run_gemini.py` calls the API directly with its own schema and prompt. Useful
for comparing models, but it measures a code path no user ever exercises — and
the two drifted: the product schema captures the amount, GST rate, HSN and
round-off that the validator needs, and the benchmark schema does not.

This runs `app.ai.intake.service.extract_invoice`, the same function the
endpoint calls. A number produced here is a number about the product.

    python3 run_intake.py --name intake-wide
    python3 run_intake.py --limit 3 --name smoke      # try it first

Then:

    python3 score.py         --predictions out/pred/intake-wide   # accuracy
    python3 validate_bench.py --predictions out/pred/intake-wide  # catch rate

RATE LIMITS
-----------
The free tier allows 15 requests/minute and 500/day; one invoice is one
request. The service itself does not throttle, because it handles a single
document for a waiting user. Pacing is a batch concern, so it lives here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "out"
sys.path.insert(0, str(HERE.parents[1] / "api"))


def _load_key() -> None:
    """Take the key from the project .env if it is not already in the shell.

    The API reads `api/.env`; the benchmark key lives in the project root.
    Rather than duplicate a secret into a second file, read it here and let
    pydantic-settings pick it up from the environment.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return
    env = HERE.parents[1] / ".env"
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        if raw.startswith("GEMINI_API_KEY="):
            os.environ["GEMINI_API_KEY"] = raw.split("=", 1)[1].strip().strip("\"'")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="intake", help="output folder under out/pred")
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--rpm", type=float, default=14.0,
                        help="requests per minute; the free tier allows 15")
    parser.add_argument("--png-dir", default="png@2x",
                        help="image set under out/ (png is ~150dpi, png@2x ~300dpi)")
    args = parser.parse_args()

    _load_key()
    from app.ai.intake.service import IntakeError, extract_invoice  # noqa: PLC0415

    pngs = sorted((OUT / args.png_dir).glob("inv_*.png"))
    if args.limit:
        pngs = pngs[: args.limit]
    if not pngs:
        sys.exit(f"no images in out/{args.png_dir} — run generate.py then render.py")

    dest = OUT / "pred" / args.name
    dest.mkdir(parents=True, exist_ok=True)

    gap = 60.0 / args.rpm
    started = time.monotonic()
    failures = 0

    for index, png in enumerate(pngs, 1):
        pause = started + (index - 1) * gap - time.monotonic()
        if pause > 0:
            time.sleep(pause)

        began = time.monotonic()
        try:
            doc = extract_invoice(png.read_bytes(), mime_type="image/png")
        except IntakeError as exc:
            failures += 1
            note = f"FAILED: {exc}"
        except Exception as exc:  # noqa: BLE001 — one bad page must not stop the run
            failures += 1
            note = f"FAILED: {type(exc).__name__}: {exc}"
        else:
            doc["invoice_id"] = png.stem
            (dest / f"{png.stem}.json").write_text(json.dumps(doc, indent=1))
            note = f"{len(doc.get('lines', []))} lines"
        print(f"  {png.stem}  {time.monotonic() - began:5.1f}s  {note}", flush=True)

    done = len(pngs) - failures
    print(f"\n{done}/{len(pngs)} extracted into out/pred/{args.name}")
    if failures:
        print(f"{failures} failed — rerun to fill the gaps; a missing file "
              "scores as a total miss")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
