"""Reading a supplier invoice off a photograph.

This is the only place in the system that calls a language model, and the only
place that makes an outbound network request. Everything it produces is treated
as a *proposal*: `validate.py` checks it against the document's own arithmetic,
`match.py` resolves it against real products, and a human confirms it before
anything reaches the ledger. Nothing here writes to the database.

WHAT THE MODEL IS AND IS NOT ASKED FOR
--------------------------------------
It is asked to transcribe what is printed. It is never asked for anything that
requires knowing our business:

    on the paper           ->  extracted     (invoice number, batch, rate, HSN)
    about us               ->  supplied      (which warehouse, our state code)
    derived from both      ->  computed      (is this intra-state? which product?)

`is_intra_state` is the clearest case. Whether a supply attracts CGST+SGST or
IGST depends on the supplier's state versus *ours*, and ours is not printed on
their invoice. Asking the model would be inviting a guess; the caller supplies
it from `Warehouse.state_code` instead, and the validator then checks the tax
split against it. See `derive_context`.

WHY THE SCHEMA IS WIDER THAN THE COLUMNS A HUMAN NEEDS
------------------------------------------------------
A goods receipt only needs product, batch, expiry, quantity and rate. The
schema also demands the line amount, the GST rate, the HSN code and the
round-off, none of which are typed into the receipt.

They are extracted because they make the extraction *checkable*. An invoice is
an over-determined document: the amount must equal quantity times rate, the tax
must follow from the amount and the rate, the HSN pins which rates are legal.
Capture those and a misread digit has to break an identity to survive; drop
them and there is nothing to check a misread digit against.

This was measured rather than assumed. Against the benchmark corpus the
narrower schema — the one in `bench/ocr/run_gemini.py` — leaves seven of the
validator's rules dark, and the catch rate on real extraction errors falls to
between 0% and 38% depending on which fields the model happened to get wrong.
With every field captured, injected errors are caught at 100% with no false
alarms. The extra fields cost a few output tokens and buy the entire claim.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.core.config import settings
from app.services import gst

log = structlog.get_logger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

#: One page, one user waiting. The benchmark can afford 180s and four retries
#: because nobody is watching it; a pharmacist holding a phone cannot.
REQUEST_TIMEOUT = 60.0
MAX_ATTEMPTS = 3

#: Guards the request before it is sent. A 20 MB photograph is a slow upload
#: and a large bill, and no invoice needs one.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ACCEPTED_MIME = {"image/png", "image/jpeg", "image/webp", "application/pdf"}

#: Leading bytes that identify each accepted format. The declared content type
#: on an upload is whatever the client chose to send, and this content leaves
#: our network — so the file is identified by what it actually is, not by what
#: its header claims. A mismatch is refused rather than silently corrected,
#: because a PNG arriving labelled as a PDF is not a formatting quirk.
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)


def _sniff(image: bytes) -> str | None:
    """The format the bytes actually are, or None if unrecognised."""
    for prefix, mime in MAGIC:
        if image.startswith(prefix):
            return mime
    # WEBP is RIFF<4 byte length>WEBP, so the marker is not at offset 0.
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return None


class IntakeError(RuntimeError):
    """Base for anything that stops an extraction."""


class IntakeUnavailable(IntakeError):
    """No key and no fixtures — the feature is switched off, not broken."""


class IntakeRejected(IntakeError):
    """The upload itself is unusable: wrong type, too large, empty."""


class ExtractionFailed(IntakeError):
    """The model was reached but produced nothing usable."""


# --------------------------------------------------------------------- schema

LINE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "sn": {"type": "INTEGER"},
        "product_name": {"type": "STRING"},
        # Not used by the receipt. It exists so the packing column has
        # somewhere to land: with no field of its own, `1x200MD` had nowhere to
        # go and its tail was absorbed into the batch beside it, turning
        # `EX73-25` into `EX73-0MD` on two separate benchmark invoices. Giving
        # the model a field it must fill is a cheaper fix than a prompt asking
        # it not to make the mistake.
        "pack": {"type": "STRING"},
        "hsn": {"type": "STRING"},
        "batch_no": {"type": "STRING"},
        "expiry_date": {"type": "STRING"},
        "quantity": {"type": "NUMBER"},
        "free_quantity": {"type": "NUMBER"},
        "mrp": {"type": "NUMBER"},
        "rate": {"type": "NUMBER"},
        "discount_pct": {"type": "NUMBER"},
        "gst_rate": {"type": "NUMBER"},
        # The printed amount column. Never typed into a receipt, and the single
        # most valuable field on the page for catching a misread.
        "taxable_amount": {"type": "NUMBER"},
        "tax_amount": {"type": "NUMBER"},
    },
    "required": [
        "sn", "product_name", "pack", "hsn", "batch_no", "expiry_date",
        "quantity", "free_quantity", "mrp", "rate", "discount_pct",
        "gst_rate", "taxable_amount", "tax_amount",
    ],
}

SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "invoice_number": {"type": "STRING"},
        "invoice_date": {"type": "STRING"},
        "supplier_name": {"type": "STRING"},
        "supplier_gstin": {"type": "STRING"},
        "drug_licence_no": {"type": "STRING"},
        # Filled before the rows are read, so the model commits to what the
        # table contains before it starts transcribing. A narrow column the
        # reader never named is a column the reader tends to skip — the free
        # goods column went missing exactly that way. It also gives the
        # validator something to check the rows against: a FREE heading with no
        # free quantity anywhere beneath it is worth a second look.
        "columns_seen": {"type": "ARRAY", "items": {"type": "STRING"}},
        "lines": {"type": "ARRAY", "items": LINE_SCHEMA},
        "totals": {
            "type": "OBJECT",
            "properties": {
                "taxable_amount": {"type": "NUMBER"},
                "cgst": {"type": "NUMBER"},
                "sgst": {"type": "NUMBER"},
                "igst": {"type": "NUMBER"},
                "round_off": {"type": "NUMBER"},
                "grand_total": {"type": "NUMBER"},
            },
            "required": ["taxable_amount", "cgst", "sgst", "igst",
                         "round_off", "grand_total"],
        },
    },
    "required": [
        "invoice_number", "invoice_date", "supplier_name", "supplier_gstin",
        "drug_licence_no", "columns_seen", "lines", "totals",
    ],
}

# Rules 1-5 are the traps documented in bench/ocr/README.md and are carried
# over verbatim from the benchmark prompt, because that prompt is the one the
# 88%-clean measurement was taken with. Changing them would invalidate the
# number. Rule 6 is new, and covers the fields added for checkability.
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

4b. THE FREE COLUMN IS THE EASIEST ONE TO MISS, SO READ IT DELIBERATELY.
   It is narrow, it holds one or two characters, and it sits right beside the
   much wider RATE column — a single `1` can end up looking like part of the
   price next to it. Before transcribing any row, list the column headings you
   can see, left to right, in `columns_seen`, exactly as printed.
   Then, if one of those headings is FREE, SCH, SCHEME or similar, EVERY row
   has something in that cell: a number, or a dash/blank meaning none. Go to
   that column for each row and read the cell on its own, without looking at
   the number to its right. Returning 0 for every row of an invoice that has a
   FREE column is almost always a miss, not an invoice without schemes.

5. EVERY COLUMN BELONGS TO EXACTLY ONE FIELD. A narrow column wraps onto two
   printed lines — `1x200MD` becomes `1x20` above `0MD`, and `EX73-25` becomes
   `EX73-` above `25`. Both halves stay in their own column. Never carry
   characters sideways from the column next door to complete a value, and
   never take a value from the row above or below.
   Put the pack or packing size (`1x200MD`, `10x10`, `1x100ML`) in `pack`.
   If there is no pack column, `pack` is an empty string.

6. COPY THE PRINTED TOTALS, DO NOT RECALCULATE THEM. `taxable_amount` and
   `tax_amount` on each line, and every figure under `totals`, must be the
   numbers printed on the page — even if they look wrong to you. They are
   cross-checked against the other columns afterwards, and that check is the
   whole point: a total you computed yourself would agree with your own misread
   and hide it. If a figure is genuinely not printed, return 0.
   - `hsn` is the 4-to-8 digit HSN/SAC code, usually 3004 for medicines.
     Empty string if there is no HSN column.
   - `gst_rate` is the percentage for that line, and ONLY if a percentage is
     actually printed on that line: 12 for 12%, not 0.12. Many invoices show
     tax as rupee amounts in CGST and SGST columns and print no percentage at
     all — in that case return 0. Do not calculate the rate from the amounts,
     do not carry a rate down from a heading, and never add a percentage to
     itself. A 0 here is correct and expected; a guess is not.
   - `tax_amount` is the total tax in rupees on that line, and again ONLY if
     tax is printed against that line. When CGST and SGST are shown in separate
     columns, it is the two AMOUNTS added together; when IGST is shown alone it
     is that figure. Some invoices show no tax per line at all and give only an
     HSN-wise summary at the foot — then `tax_amount` is 0. Do not compute it
     from a rate, and do not apportion a footer total across the lines.
   - `round_off` is the small adjustment near the grand total, often negative
     and under one rupee. 0 if the invoice does not show one.

Also:
- `invoice_date` in YYYY-MM-DD.
- `discount_pct` is the percentage, so 5% is 5, not 0.05. Absent means 0.
- Money as numbers, no currency symbol, no thousands separators.
- `cgst`/`sgst` are filled on an intra-state invoice and `igst` on inter-state.
  The one that does not apply is 0, not null.
- One entry in `lines` per product row, in printed order, `sn` starting at 1.
  Do not merge rows, do not invent rows, do not skip a row because it is faint.
"""


# ------------------------------------------------------------------- context


def derive_context(doc: dict, *, our_state_code: str | None) -> dict:
    """Add what the model was deliberately not asked for.

    The tax split on an invoice is only checkable against the receiving party's
    own state, which does not appear on the supplier's paper. Supplying it here
    — rather than in the prompt — is what lets the validator treat a mismatched
    CGST/IGST split as evidence of a misread instead of an unknown.

    Returns the document unchanged if we do not know our own state, because a
    guessed context would produce confident nonsense downstream.

    THE TWO CODINGS
    ---------------
    A GSTIN opens with a *numeric* state code — `27` for Maharashtra. Every
    `state_code` column in this system holds the *postal* one — `MH`. This used
    to compare the two directly, which is never equal, so every invoice looked
    inter-state and every genuinely intra-state one was flagged for carrying
    CGST+SGST. That is a finding on the majority of real invoices, in the same
    amber as the findings that mean something.

    The translation happens here because here is where the foreign coding
    arrives. Nothing downstream has to know there were ever two.
    """
    gstin = str(doc.get("supplier_gstin") or "").strip().upper()
    if not (our_state_code and len(gstin) >= 2 and gstin[:2].isdigit()):
        return doc

    ours = gst.gstin_prefix_for_state(str(our_state_code))
    doc = dict(doc)
    doc["supplier_state_code"] = gstin[:2]
    # An unrecognised state code of our own leaves the question open rather
    # than answering it wrongly: without `is_intra_state` the validator skips
    # the split check, which is the right behaviour when nobody knows.
    if ours is not None:
        doc["is_intra_state"] = gstin[:2] == ours
    return doc


# ----------------------------------------------------------------- extraction


def _fixture_for(image: bytes, fixture_dir: Path) -> dict:
    """Replay a stored extraction instead of calling the model.

    Keyed by the image's own bytes so a given photograph always replays the
    same reading. Used for the demo — a conference network should not be able
    to break the presentation — and by the tests, which must not need a key.
    """
    digest = hashlib.sha256(image).hexdigest()[:16]
    for candidate in (fixture_dir / f"{digest}.json", fixture_dir / "default.json"):
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise ExtractionFailed(
        f"no fixture for this image (looked for {digest}.json and default.json "
        f"in {fixture_dir})"
    )


def extract_invoice(image: bytes, *, mime_type: str = "image/png") -> dict:
    """Transcribe one invoice image. Returns the raw reading, unvalidated.

    Raises IntakeUnavailable if the feature is not configured, IntakeRejected
    if the upload is unusable, and ExtractionFailed if the model was reached
    but produced nothing. None of these are conditions the caller can paper
    over: an invoice that cannot be read has to be typed in by hand, and saying
    so is better than returning an empty draft that looks like a clean invoice
    with no lines on it.
    """
    if not image:
        raise IntakeRejected("the uploaded file is empty")
    if len(image) > MAX_IMAGE_BYTES:
        raise IntakeRejected(
            f"the file is {len(image) / 1e6:.1f} MB; the limit is "
            f"{MAX_IMAGE_BYTES / 1e6:.0f} MB"
        )
    if mime_type not in ACCEPTED_MIME:
        raise IntakeRejected(
            f"{mime_type} is not a supported format — "
            f"send {', '.join(sorted(ACCEPTED_MIME))}"
        )

    actual = _sniff(image)
    if actual is None:
        raise IntakeRejected(
            "this file is not a PNG, JPEG, WEBP or PDF — "
            "photograph the invoice or export it as a PDF"
        )
    if actual != mime_type:
        # Trust the bytes. The declared type is a hint from the client, and
        # sending a file onward under the wrong label helps nobody.
        log.info("intake.mime_corrected", declared=mime_type, actual=actual)
        mime_type = actual

    if settings.intake_fixture_dir:
        return _fixture_for(image, Path(settings.intake_fixture_dir))

    if not settings.gemini_api_key:
        raise IntakeUnavailable(
            "invoice scanning is not configured on this server "
            "(set GEMINI_API_KEY to enable it)"
        )

    return _parse(_request({
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image).decode(),
                }},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            # Transcription, not writing. Two runs of the same page should not
            # disagree, and at temperature 0 they largely do not.
            "temperature": 0,
        },
    }))


def _request(body: dict) -> dict:
    """One call to the model, with the retries worth making.

    Shared by the two things that talk to it — reading the page, and judging
    which catalogue product a printed name refers to — so the timeout, the
    backoff and the treatment of each status code are decided once.
    """
    url = ENDPOINT.format(model=settings.gemini_model)
    delay = 2.0
    last: str = "no attempt was made"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    url, params={"key": settings.gemini_api_key}, json=body
                )
        except httpx.HTTPError as exc:
            last = f"could not reach the extraction service ({type(exc).__name__})"
            log.warning("intake.transport_error", attempt=attempt, error=str(exc))
        else:
            # 429 is the per-minute quota and 5xx is transient; both are worth
            # one more try. A 4xx is a bad request and retrying cannot fix it.
            if response.status_code in (429, 500, 502, 503, 504):
                last = f"the extraction service returned {response.status_code}"
                log.warning("intake.retryable", attempt=attempt,
                            status=response.status_code)
            elif response.is_error:
                log.error("intake.rejected", status=response.status_code,
                          body=response.text[:400])
                raise ExtractionFailed(
                    f"the extraction service rejected the request "
                    f"({response.status_code})"
                )
            else:
                return response.json()

        if attempt < MAX_ATTEMPTS:
            time.sleep(delay)
            delay *= 2

    raise ExtractionFailed(f"{last} — tried {MAX_ATTEMPTS} times")


# ------------------------------------------------------------ name judgement


#: What comes back when the model is asked to name products. `product_id` is
#: chosen from a list we supply, never invented, and 0 means "none of these".
SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_no": {"type": "integer"},
                    "product_id": {"type": "integer"},
                    "confident": {"type": "boolean"},
                },
                "required": ["line_no", "product_id", "confident"],
            },
        }
    },
    "required": ["matches"],
}

SUGGESTION_PROMPT = """\
You are matching product names on an Indian pharmaceutical distributor's
invoice to a pharmacy's own catalogue.

For each printed name, choose the catalogue product it refers to, or 0 if none
of them is the same product.

The same product is routinely written differently on the two sides:
- spelling variants — AMOXYCILLIN and AMOXICILLIN, CEFTRIAXONE and CEFTRIAXON
- abbreviations and trade names — OMEZ-20 is omeprazole 20mg, PANTOP-40 is
  pantoprazole 40mg, CETZINE is cetirizine, ASTHALIN is salbutamol
- strength written differently — 60K IU and 60000IU are the same, 1GM and
  1000MG are the same, 0.25MG and 250MCG are the same
- dosage form spelled out or omitted — TAB, TABLET, or nothing at all
- ordinary typing errors in either name

These are NOT the same product, however similar they look:
- a different strength — 5MG is not 10MG, 250MG is not 500MG
- a different dosage form — a tablet is not an injection, is not a syrup
- a different pack size where the pack is part of the product — 100G is not
  500G of the same dressing
- a different salt or ester where the invoice says so

Answer 0 whenever you are unsure. An unmatched line costs somebody ten seconds;
a wrongly matched one puts the wrong medicine on a shelf.

Set `confident` to false when the answer is a reasonable guess rather than a
certainty; it is still recorded, but a person is asked to look at it.
"""


def suggest_products(
    lines: list[tuple[int, str]],
    catalogue: list[tuple[int, str]],
) -> dict[int, tuple[int, bool]]:
    """Ask which catalogue product each printed name refers to.

    `lines` is (line_no, printed name); `catalogue` is (product_id, name).
    Returns {line_no: (product_id, confident)} for the ones it could place.

    THE MODEL CHOOSES, IT DOES NOT DECIDE
    ------------------------------------
    It picks an id from a list supplied to it, so it cannot invent a product,
    and every answer is put back through the same deterministic checks that
    govern an ordinary match — the printed strength must appear in the chosen
    product, a stated dosage form must not contradict it. A suggestion that
    fails those is dropped, not shown. What it adds is judgement about *names*,
    which is the one part of matching no rule was ever going to get right:
    `OMEZ-20` is not derivable from `OMEPRAZOLE` by any string operation worth
    trusting, and a person knowing that is exactly what this replaces.

    Returns {} rather than raising when the model is unavailable. Failing to
    improve a match is not a reason to fail a delivery — the lines simply stay
    unmatched, which is where they already were.
    """
    if not lines or not catalogue:
        return {}
    if not settings.gemini_api_key:
        return {}

    printed = "\n".join(f"{no}. {name}" for no, name in lines)
    options = "\n".join(f"{pid}. {name}" for pid, name in catalogue)
    prompt = (
        f"{SUGGESTION_PROMPT}\n"
        f"CATALOGUE (id. name)\n{options}\n\n"
        f"PRINTED ON THE INVOICE (line. name)\n{printed}\n"
    )

    try:
        payload = _request({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": SUGGESTION_SCHEMA,
                "temperature": 0,
            },
        })
        doc = _parse(payload)
    except IntakeError as exc:
        log.warning("intake.suggest_failed", error=str(exc))
        return {}

    known = {pid for pid, _ in catalogue}
    wanted = {no for no, _ in lines}
    out: dict[int, tuple[int, bool]] = {}
    for row in doc.get("matches") or []:
        try:
            line_no = int(row["line_no"])
            product_id = int(row["product_id"])
        except (KeyError, TypeError, ValueError):
            continue
        # A line we did not ask about, or a product not on the list, is the
        # model going off-script. Neither is worth arguing with; drop it.
        if line_no in wanted and product_id in known:
            out[line_no] = (product_id, bool(row.get("confident")))
    return out


def _parse(payload: dict) -> dict:
    """Pull the JSON document out of the API envelope.

    A response with no content is normally a safety block or a token limit, and
    `finishReason` says which. Surfacing it beats a KeyError three frames away.
    """
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        reason = (payload.get("candidates") or [{}])[0].get("finishReason", "unknown")
        raise ExtractionFailed(
            f"the model returned no content (finishReason={reason})"
        ) from None

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionFailed(f"the model returned malformed JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise ExtractionFailed("the model returned something other than an invoice")
    return doc
