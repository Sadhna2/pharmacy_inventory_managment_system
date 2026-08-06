# Presentation slides

Markdown slides, `---` between them. Renders as-is in any Markdown viewer, and
works directly in Marp, Reveal.js or Slidev if you want a projected deck.

Speaker notes are in blockquotes and are not meant to be read aloud verbatim.

---

## Pharmacy Inventory Management

### Multi-branch stock, batch-tracked, with AI-assisted invoice intake

Team of 5 · 10 days · FastAPI + React + PostgreSQL on AWS

> Open on the deployed site, already signed in as Manager.

---

## The problem

Ordinary stock software gets pharmacy wrong in four ways:

- **Stock is not fungible** — two boxes differ by batch, expiry and printed MRP
- **History must survive being wrong** — a recall asks "where did every unit go"
- **Data enters at receiving** — typed off a paper invoice, slowly, with errors
- **Nobody trusts a black box with regulated stock**

> The fourth is the one that shapes the AI decision. Hold it.

---

## One rule underneath everything

> ### Stock is never stored as a number.
> ### It is derived by summing an append-only ledger.

- `stock_movements` is **insert-only**
- Enforced by a **database trigger**, not by convention
- A correction is a **reversing entry** — the original row survives

> Try `DELETE FROM stock_movements` live in psql if there's time. It's refused.

---

## Why that rule pays for itself

| Because the ledger is append-only… | You get |
|---|---|
| Every movement is permanent | Recall tracing is a query, not a feature |
| Balances are derived | Any balance is reproducible from history |
| Corrections are entries | "Who changed this, and when" always answers |

`stock_balances` exists only for speed — and a test rebuilds it from the
ledger and asserts they agree.

---

## The AI question

The mandate was to use AI.

The real question was **how to use it on regulated stock data without making
the system less trustworthy.**

> Pause here. This is the slide the whole talk turns on.

---

## The answer

> ### The model is never allowed to produce an answer.
> ### It may only produce structured input to code that already validates.

---

## Why this works: invoices are over-determined

A supplier invoice carries more information than it needs:

- quantity × rate **must equal** the line amount
- lines **must sum to** the subtotal
- the 15th character of a GSTIN is a **mod-36 checksum** over the other 14

**None of it needs an answer key.**

A misreading fails arithmetic the document carries within itself — caught
structurally, not by somebody noticing.

---

## Three hard limits on the model

1. **The endpoint creates nothing.** No code path from intake to the ledger.
   Worst case is a form with a wrong number in it.

2. **Matches obey the same rules a human lookup obeys.** 500 mg can never land
   on 250 mg, however confident the model was.

3. **Findings graded by blast radius.** BLOCK only where wrong stock could
   reach a shelf.

---

## Demo — scanning an invoice

1. Purchasing → **Scan an invoice**
2. Photograph of a real distributor invoice
3. 14 lines fill in: batches, expiries, quantities, rates
4. **One flag: GSTIN checksum does not match** — a character was misread
5. Fix it, submit — *now* stock exists

> This is the moment. The system caught its own model being wrong,
> using arithmetic printed on the paper.

---

## Accuracy, measured honestly

**50 generated invoices** — varying layout, font, columns, tax presentation,
expiry format, scan quality.

Ground truth written **first**, document rendered *from* it — so the answer key
cannot itself be wrong.

| Fields scored | 3,539 |
|---|---|
| Wrong | 8 |
| Field accuracy | **99.8%** |
| **Lines dropped or invented** | **0 / 0** |
| **Whole invoices perfect** | **44 / 50 (88%)** |

`batch_no` and `quantity` — where an error puts wrong stock on a shelf — **100%**.

> Say the 88% out loud. Per-field accuracy flatters document tasks; volunteering
> the harsher number is what makes the 99.8% believable. All 6 imperfect
> invoices fail on a field the validator checks — GSTIN, licence no, date.

---

## It learns each distributor

Printed name → our catalogue, remembered per supplier.

- First delivery: a few lines need a human
- Every delivery after: **matches exactly**

Across six demo invoices: **70 of 73 lines resolved**.

The 3 refusals are *correct* — ambiguous pack sizes, offered as a shortlist
rather than guessed.

---

## Being honest about what is a model

**One** capability is generative. **Four** are statistics. We label them that way.

| Capability | Method |
|---|---|
| Invoice intake | **Gemini — vision + language** |
| Demand forecast | Holt-Winters exponential smoothing |
| Exception detection | Thresholds on the ledger |
| Replenishment | Reorder point + safety stock |
| Supplier lead times | Measured percentiles |

> Calling Holt-Winters "AI forecasting" is the easy version and a worse one.

---

## The analysis layer is deletable

Everything it produces is **recomputed on request** and never written back.

Delete `app/ai/` and you still have a working inventory system.

That is the strongest available statement that the analysis **cannot corrupt
the records it reads**.

---

## Controls that are actually enforced

- Every endpoint declares a **permission**
- Staff **cannot see cost** — anywhere
- A branch user naming another branch in a payload is **refused**
- **Separation of duties**: creator ≠ approver, on orders, transfers, adjustments
- Feature switches close **routes**, not just menu items

> Turning a capability off in Settings 404s its API. Demo it if asked.

---

## Engineering

| | |
|---|---|
| Database | 42 tables, 2 views, 92 foreign keys |
| API | 96 operations, OpenAPI documented |
| History | ~53,000 ledger movements over 2 years |
| Tests | **392**, against a real Postgres and a real HTTP server |
| Deploy | CI builds arm64 images → GHCR → EC2, tagged by commit SHA |

Browser types are generated from the live OpenAPI document — **CI fails if they
drift**.

---

## Run it yourself

```bash
git clone <repo> && cd pharmacy_inventory_managment_system
cp .env.example .env && docker compose up
```

**Docker is the only prerequisite.** macOS, Windows or Linux.

http://localhost:8080 · `admin@pharmacy.co.in` / `ChangeMe@123`

> We tested this from an empty directory on a clean machine. It found four
> broken steps, which we fixed.

---

## What we'd do next

- Fix an intermittent race in the e2e suite (~1 run in 10, different tests each time)
- Record the second model call so matching works fully offline
- Read the invoice pack column to resolve remaining ambiguous matches
- Natural-language reporting — designed, flagged in-product as not built

> Volunteering limitations is more convincing than being asked about them.

---

## In one sentence

> The guarantees hold **because of how the system is built** —
> an append-only ledger the database itself protects, and a model that
> can only produce something checkable —
> **not because everyone remembered to be careful.**

### Questions
