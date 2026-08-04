"""Record what the model reads from an invoice, so a demo never needs a network.

    python3 scripts/capture_intake_fixtures.py bench/ocr/out/png/inv_044.png ...
    python3 scripts/capture_intake_fixtures.py --force <image>   # re-read it

Each image is sent through `app.ai.intake.service.extract_invoice` — the same
function the endpoint calls — and the reading is written to

    api/fixtures/intake/<sha256-of-the-image>.json

Point INTAKE_FIXTURE_DIR at that directory and the endpoint replays the stored
reading instead of calling out. Everything downstream of the extractor — the
validator, the matcher, the flags on the form — still runs for real, because
none of it was ever the part that needed a network.

WHY THIS EXISTS
---------------
The demo is five minutes long and happens on a conference network. A single
outbound call, at the one moment somebody is watching, is the whole feature's
exposure to a captive portal and a shared uplink. Recording the reading moves
that risk to now, where a failure costs a retry instead of the presentation.

The images are copied in alongside the readings, because a fixture keyed by a
digest is worthless without the file that hashes to it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "api" / "fixtures" / "intake"

MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def _load_key() -> None:
    """Take the key from the project .env unless the shell already has one.

    Capturing is the one operation here that genuinely needs the key, and it
    is run by hand, from the repository root, by somebody who already has one
    in `.env`. Reading it here beats a second copy of a secret in a second
    file.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return
    env = ROOT / ".env"
    if not env.is_file():
        return
    for raw in env.read_text().splitlines():
        if raw.startswith("GEMINI_API_KEY="):
            os.environ["GEMINI_API_KEY"] = raw.split("=", 1)[1].strip().strip("\"'")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--dest", type=Path, default=FIXTURES)
    parser.add_argument("--force", action="store_true",
                        help="re-read an image that already has a fixture")
    args = parser.parse_args()

    # A fixture directory in the environment would make this replay its own
    # recordings and capture nothing. Capturing is the one path that must
    # always reach the model.
    os.environ["INTAKE_FIXTURE_DIR"] = ""
    _load_key()
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("no GEMINI_API_KEY — set it in .env or the environment")

    sys.path.insert(0, str(ROOT / "api"))
    from app.ai.intake.service import IntakeError, extract_invoice  # noqa: PLC0415

    images = args.dest / "images"
    images.mkdir(parents=True, exist_ok=True)

    failures = 0
    for path in args.images:
        if not path.is_file():
            print(f"  {path}  MISSING")
            failures += 1
            continue

        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:16]
        fixture = args.dest / f"{digest}.json"

        if fixture.is_file() and not args.force:
            print(f"  {path.name}  {digest}  already recorded")
            continue

        mime = MIME.get(path.suffix.lower(), "image/png")
        try:
            doc = extract_invoice(raw, mime_type=mime)
        except IntakeError as exc:
            print(f"  {path.name}  FAILED: {exc}")
            failures += 1
            continue

        fixture.write_text(json.dumps(doc, indent=1) + "\n")
        shutil.copyfile(path, images / path.name)
        print(f"  {path.name}  {digest}  {len(doc.get('lines', []))} lines")

    print(f"\nfixtures in {args.dest}")
    if failures:
        print(f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
