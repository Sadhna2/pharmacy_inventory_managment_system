# Invoice OCR benchmark

Fifty synthetic Indian pharma distributor invoices with exact ground truth,
plus a scorer. Used to decide which OCR approach the goods-receipt intake
feature should be built on, and to put a defensible number in the report.

```bash
cd bench/ocr
python3 generate.py            # 50 HTML + 50 ground-truth JSON
python3 render.py              # -> PNG, degraded per noise profile
python3 score.py --predictions out/pred/<system>
```

## Why synthetic

Real invoices would have to be hand-labelled, and hand-labels are themselves
a couple of percent wrong — which is the same order as the difference between
the systems being compared. Here the ground truth is generated *first* and the
document rendered *from* it, so the answer key cannot be wrong.

The trade-off is honest and worth stating in the report: these are clean
reproductions of the format, not photographs of real paper. A real-invoice
spot check on five or ten documents belongs alongside this, not instead of it.

## What varies

Six independent axes, so this measures generalisation rather than one template:

| Axis | Values |
|---|---|
| Layout | classic, boxed, minimal, dense, twocol |
| Font | serif, sans, condensed, mono, wide |
| Columns | 5 sets, differing in order and in which columns exist |
| Tax presentation | per-line split, single GST column, HSN summary at the foot |
| Expiry format | `08/27`, `08-27`, `AUG-27`, `08/2027` |
| Scan quality | clean, scan_light, scan_heavy, photo |

Line counts run 3–14, and half the invoices abbreviate product names the way a
distributor's own system does (`PCM-650 TAB` for Paracetamol 650mg).

## The traps, and why they matter here

- **Expiry is a month, not a day.** `08/27` means good through 31 Aug 2027.
  Parsing it as the 1st is wrong by a month, and that month decides whether a
  delivery is accepted and where the batch sits in FEFO.
- **Batch numbers are O/0 and I/1 minefields.** Fuzzy is worthless: a recall
  traces by exact batch code.
- **MRP is not rate.** MRP is the legal ceiling printed on the pack; rate is
  what we pay. Transposing them corrupts both margin and the price ceiling.
- **Free goods.** `10+1 free` arrives as 11 units. Ignoring the column
  understates stock by whatever the scheme was.

## Reading the score

Three separate numbers, because they have different consequences:

- **per-field accuracy** — where a system is weak
- **line recall / precision** — dropped lines are missing stock; invented
  lines are stock that does not exist
- **clean invoice rate** — the share with zero errors anywhere. This is the
  only figure that predicts how much typing a pharmacist actually avoids, and
  it is always far below per-field accuracy. In the mock run below, 95%+ on
  every field still yields 24% clean invoices.

## Validating the scorer

`mock_ocr.py` fabricates extractions with the failure modes above injected at
known rates. It says nothing about any real system — it exists so that the
scorer is known to detect and attribute each failure before real numbers are
run through it.

```bash
python3 mock_ocr.py --quality good   && python3 score.py --predictions out/pred/mock-good
python3 mock_ocr.py --quality poor   && python3 score.py --predictions out/pred/mock-poor
```

`good` → 24% clean invoices, `poor` → 0%, with per-field accuracy tracking the
injected rates. The scale discriminates.

## Adding a real system

Write one JSON per invoice into `out/pred/<name>/inv_NNN.json`, same shape as
`out/truth/inv_NNN.json`, then run the scorer. Nothing about the scorer is
specific to any model or library.

Note on sample size: 50 invoices is ~12 per noise bucket, which is enough for
a headline number but thin for comparing buckets. `generate.py --count 200`
if a per-bucket claim needs to hold up.
