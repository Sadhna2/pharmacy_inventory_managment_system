# Recorded invoice readings

Four supplier invoices, and what the model read from each of them.

Point `INTAKE_FIXTURE_DIR` at this directory and `POST /ai/intake/invoice`
replays the stored reading instead of calling out:

```
INTAKE_FIXTURE_DIR=fixtures/intake
```

That path is relative to the API's working directory, which is the repository's
`api/` locally and `/app` in the container — the same value works in both.

## What is still real when this is set

Only the transcription is replayed. Everything the accuracy claim rests on
still runs: the arithmetic checks against the invoice's own totals, the GSTIN
checksum, the batch-shape comparison against what the supplier has shipped
before, the product matching, the refusal to guess. None of it ever needed a
network — the model's reading is an input to that code, not an answer from it.

So a demo on a dead uplink is not a mock. It is the whole system with one
recorded input.

## Why record at all

The demo is five minutes long and runs on a conference network. One outbound
call, at the one moment somebody is watching, is the feature's entire exposure
to a captive portal and a shared uplink. Recording moves that risk to a moment
where failure costs a retry.

## The five that read perfectly

**482 of 482 printed fields, exactly right, on all five.** Every product name,
batch code, expiry, quantity, free quantity, rate, MRP and discount — scored
against the ground truth the documents were rendered from.

They are not five versions of one easy case. Between them they cover every
axis the generator varies:

| image | lines | layout / font | scan quality | tax shown as | expiries |
|---|---|---|---|---|---|
| `inv_031.png` | 14 | boxed / sans | **phone photo** | per-line column | `MM/YYYY` |
| `inv_002.png` | 14 | minimal / condensed | **heavy photocopy** | footer summary | `MON-YY` |
| `inv_048.png` | 12 | dense / mono | clean | split CGST/SGST | `MM/YY` |
| `inv_049.png` | 11 | twocol / wide | light scan | per-line column | `MM-YY` |
| `inv_020.png` | 14 | classic / serif | clean | footer summary | `MM/YY` |

Five layouts, five typefaces, all four scan qualities, all three ways of
printing tax, all four ways of writing an expiry, and three of them carry a
`FREE` goods column. 65 lines of typing, none of it done by hand.

## And one that does not

| image | lines | why it is here |
|---|---|---|
| `inv_023.png` | 8 | **the reading is wrong, and the checksum catches it** |

`inv_023` is the one worth showing, and it is the only finding in the whole
set. Its page carries two GSTINs — the supplier's in the letterhead and ours in
the "Billed to" block, exactly as a real invoice does. The reading takes one
character from the wrong one:

    printed   24AACCA1086G1Z2
    read      24AACPA1086G1Z2
                   ^ from our GSTIN, 27AAA**CP**1234A1Z5

Nothing about that looks wrong. Right shape, right length, right state code, a
number no human would query. The fifteenth character is a mod-36 checksum over
the other fourteen, so it fails arithmetic that needs no answer key — which is
the entire argument for this feature: the model is never asked to be right, it
is asked to produce something that can be checked.

The finding says a character is wrong. It does not say which, and it offers no
correction — recomputing the check digit would assume the other fourteen are
right, which here they are not, and would hand somebody a second wrong number
with a valid checksum to accept.

## Re-recording

The fixtures are keyed by the SHA-256 of the image, so a reading is bound to
the exact file that produced it. Re-render the images and the old fixtures stop
matching — delete them and record again:

```bash
python3 scripts/capture_intake_fixtures.py bench/ocr/out/png@2x/inv_0{02,20,23,31,48,49}.png
```

That needs `GEMINI_API_KEY` and reaches the model; it is the one operation here
that does. `bench/ocr/out/` is generated and not committed, so rebuild it first
with `generate.py` and `render.py --scale 2` — which is also why the images are
copied into `images/` rather than referenced where they were produced.
