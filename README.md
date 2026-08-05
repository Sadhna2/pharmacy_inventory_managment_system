# Pharmacy Inventory Management System

Multi-branch pharmacy chain — 1 central warehouse, 5 branches, retail + institutional B2B.
Built on an **append-only stock ledger**: no row in `stock_movements` is ever updated or
deleted, and every balance you see is derived from it.

**Status:** Layer 0 (spine), Layer 1 (operations), five of the six Layer 2
analysis features and **invoice intake** are complete and verified, along with
an administrator settings screen that makes every AI threshold and feature
switch tunable at runtime. 273 tests, most of them end-to-end against a live
server and a real Postgres.

Invoice intake photographs a distributor's invoice and fills in the goods
receipt — the one place a language model earns its keep here, and the one place
its output is checked by arithmetic before anyone sees it (below).

Natural-language reporting is designed but not built; it is shipped switched
off and the API refuses its routes while the switch is off, so nothing
half-finished is reachable. Sales invoicing and payment tracking are specified
but not started — the system records what moved, not yet what is owed.

Code comments cite `ARCHITECTURE.md` by section. That file is the internal
design reference and is deliberately not published here; the citations are
kept because they explain *why* a given line is written the way it is.

---

## Running it

Two ways, and they produce **the same system**. Both build their data with the
same command — `python -m app.seed.demo` — so the app a developer sees and the
app on the server are the same app with the same rows in it.

### The whole stack, exactly as deployed

```bash
cp .env.example .env && docker compose up
```

Then open **http://localhost:8080** and sign in as `admin@pharmacy.co.in` with
the password **`ChangeMe@123`** (that is what `.env.example` sets; change
`SEED_PASSWORD` and re-seed to use your own).

**Docker is the only prerequisite** — no Python, no Node, no Postgres. It works
the same on macOS, Windows and Linux, and it is the recommended path on
Windows, where the native instructions below do not apply. On Windows use
[Docker Desktop](https://docs.docker.com/desktop/install/windows-install/) and
run the command in PowerShell (use two commands if `&&` gives you trouble).

Postgres, the API and Caddy, built and wired the way production is. The first
start migrates and seeds (~1 min); later starts reuse the volume and are
instant. This is the closest thing to the live site you can run, and if you
are checking whether something behaves the way it will on the server, check it
here.

To wipe the data and start over: `docker compose down -v && docker compose up`.

### Or natively, for a faster edit loop

macOS and Linux only — this path uses a bash script and a local Postgres
build. On Windows, use Docker above.

Nothing installs globally. Postgres runs as a project-local cluster on port **55432**
with its data in `api/.pgdata`; Python lives in `api/.venv`. All three are gitignored,
which is why the first three commands below exist: a fresh clone has none of them.

Prerequisites: Python 3.14, Node 22, and PostgreSQL 16
(`brew install postgresql@16`).

**0 — first time only**

```bash
python3 -m venv api/.venv && api/.venv/bin/pip install -r api/requirements.txt
```

```bash
npm install --prefix web
```

```bash
cp api/.env.example api/.env
```

That one matters: it points the API and Alembic at the project-local cluster on
port 55432. Without it they both try the default 5432, find nothing, and fail
with a connection error that looks like Postgres is down when it is simply
somewhere else. (The `.env` in the repository root is a separate file, read by
Docker Compose only.)

```bash
./scripts/db.sh init
```

`init` creates the cluster, starts it and adds the `pharmacy` database. Later
sessions just need `./scripts/db.sh start`.

**1 — database**

```bash
./scripts/db.sh start && cd api && .venv/bin/alembic upgrade head && SEED_PASSWORD='ChangeMe@123' .venv/bin/python -m app.seed.demo
```

**2 — API** (http://127.0.0.1:8000, docs at `/docs`)

```bash
cd api && .venv/bin/uvicorn app.main:app --port 8000
```

**3 — web** (http://localhost:5173, proxies `/api` to the backend)

```bash
npm run dev --prefix web
```

### What the seed builds

`app.seed.demo` runs three steps in order and skips any already applied, which
is why the same command is safe in the container, in CI and on your laptop.

| step | what it adds | cost |
|---|---|---|
| `bootstrap` | 28 permissions, 4 roles, demo users, 33 products, 5 locations | instant |
| `history --days 730` | two years of sales, purchases, transfers and expiries, plus planted anomalies for the exception detector | ~30s |
| `showcase` | damaged stock, a customer return, a failed QC check, retired products, orders awaiting approval, recalls | instant |

The third exists because the simulation models a chain that *works* — it never
drops a carton or withdraws a product, so most of the status badges and half
the filters had nothing behind them.

The data is deterministic (fixed RNG seed), so a given commit produces the same
history everywhere. It is anchored to today, so a database seeded last week
holds batches a week nearer expiry — `--rebuild` regenerates.

Run individual steps by hand if you want (`app.seed.history --reset`,
`app.seed.showcase`); `demo` is just the three of them with the guards on.

### Invoice intake (the AI feature)

Optional, and off unless you give it a key. Get one from Google AI Studio and
put it in `.env`:

```bash
GEMINI_API_KEY=your-key-here
```

Leave it empty and the feature switches *itself* off — the endpoint answers 503
and every other screen works normally. Nothing else in the system depends on it.

**To run it with no network at all**, point it at the recorded readings:

```bash
INTAKE_FIXTURE_DIR=fixtures/intake
```

Six invoices and what the model read from each are stored in
`api/fixtures/intake/`, keyed by the SHA-256 of the image, so a reading is bound
to the exact file that produced it. Only the *transcription* is replayed — the
arithmetic checks, the GSTIN checksum, the batch-format comparison and the
product matching all still run, because none of them ever made a network call.
A demo on a dead uplink is the whole system with one recorded input, not a mock.

One caveat worth knowing: matching an unfamiliar trade name (`OMEZ-20` →
omeprazole) is a *second* model call and is not recorded, so offline it returns
nothing and those lines come back for a human to pick. Nothing breaks; fewer
rows fill themselves in.

### Tests

```bash
cd api && SEED_PASSWORD='the-same-value-you-seeded-with' .venv/bin/python -m pytest tests/ -q
```

⚠️ The suite runs against `DATABASE_URL` — in CI that is a throwaway container,
but locally it is **your dev database**, and it leaves its fixtures behind:
`PROBE-…` products, `Temporary Depot` warehouses, draft orders and test recalls.
They are harmless but they accumulate, and they will show up in a demo. Rebuild
before showing the app to anyone (below).

### Starting over

`./scripts/db.sh reset` drops and recreates the database. It terminates open
connections first, so you do **not** need to stop uvicorn. The full rebuild:

```bash
./scripts/db.sh reset
cd api && .venv/bin/alembic upgrade head && .venv/bin/python -m app.seed.demo
```

Under Docker the equivalent is `docker compose down -v && docker compose up`.

### Local and live

The container start-up runs `alembic upgrade head && python -m app.seed.demo`,
which is the line above. Business dates come from `app/core/clock.py`, pinned
to `Asia/Kolkata`, rather than from `date.today()` — the server runs on UTC and
a laptop does not, and for five and a half hours every evening they would
otherwise disagree about what day it is and answer the same question
differently. `TZ` is set on the containers too, so their logs read in the same
timezone the app reasons in.

What legitimately differs: the images are prebuilt in CI rather than on the box
(2 GB cannot run `vite build`), Caddy is given a real hostname so it fetches a
certificate, and the secrets are real. Nothing else.

---

## Demo accounts

The seed creates four accounts. **The password is not stored in this repository** —
set `SEED_PASSWORD` before seeding and use that value to sign in:

```bash
export SEED_PASSWORD='pick-something-here'
```

If you leave it unset, the seed falls back to a placeholder for local convenience
and refuses to run at all unless `ENV=development`. Run the seed step to see which
password is in effect — it prints it on completion.

| Email | Role | What it demonstrates |
|---|---|---|
| `admin@pharmacy.co.in` | Admin | Everything |
| `manager@pharmacy.co.in` | Manager | Approvals, cost visibility, recalls |
| `staff@pharmacy.co.in` | Staff | Scoped to Andheri branch; **cannot see cost** |
| `customer@cityhospital.co.in` | Customer | Own orders only |

Sign in as staff and then as manager on the same screen — costs appear for one and
not the other. That is `stock.view_cost` as a *permission*, not a role.

---

## What to show a judge

1. **Batch Recalls → Start recall.** Pick a batch, give a reason. Every branch holding
   it is frozen at once and every customer who already received it is listed. The whole
   feature is ~30 lines of service code because the ledger is lot-aware.
2. **Movements**, straight after. The recall appears as balanced pairs — `−455` leaving
   *Available*, `+455` entering *Quarantine* — at every location. Nothing was edited.
3. **Purchasing → Approve** on an order you created yourself. It is refused:
   *"A purchase order must be approved by someone other than its creator."*
   The check is on identity, not role — an administrator cannot bypass it
   either, because separation of duties a role can override is not separation
   of duties.
4. **Purchasing → Receive goods**, and pick an approved order first. The
   destination, products and agreed cost fill themselves in and each row says
   what the order still expects; all that is left to type is the supplier's
   invoice number and the batch and expiry printed on the carton, which do not
   exist until the goods are made. Receive part of a line and the order goes
   *Partially received* with the arithmetic on the row — `4 of 10` — rather
   than a status word with nothing behind it. Try receiving it into the wrong
   branch and the server refuses.
5. **Receive goods → Scan invoice.** Photograph a distributor's invoice and
   fourteen lines of batch codes, expiries, quantities and rates fill
   themselves in. Then look at what it *says about itself*: `'OMEZ-20 CAP' read
   as 'Omeprazole 20mg'; check it against the carton` — the model named it, and
   the row is filled in but flagged, never posted quietly.

   Scan `inv_023` for the part worth showing. Its page carries two GSTINs, the
   supplier's and ours, and the reading takes one character from the wrong one:
   `24AACPA1086G1Z2`. Right shape, right length, right state code, a number no
   human would query — and the fifteenth character is a mod-36 checksum over the
   other fourteen, so it fails arithmetic that needs no answer key. **The model
   is never asked to be right; it is asked to produce something that can be
   checked.** The finding says a character is wrong and offers no correction,
   because recomputing the check digit would assume the other fourteen are right
   and hand somebody a second wrong number with a valid checksum.

   Then fix a row and watch the panel above settle: pick the product for an
   unmatched line and the count drops, the finding moves to *answered on the
   form*, struck through. Findings about the **paper** — the checksum, the tax
   split, arithmetic that does not add up — stay standing, because no amount of
   typing changes what the supplier printed.

   Nothing here moves stock. There is no code path from this endpoint to the
   ledger; it returns a proposal and a person presses the same button as before.
6. **Stock.** Balances carry the full grain: product, location, bin, batch, status.
7. **Analysis → Supplier lead times.** Apex Pharma Supply comes out *Erratic* —
   7 days typically, 15 at worst, meeting its own promised date 49% of the time.
   Open it: the percentiles are backed by the actual purchase orders they came from.
8. **Analysis → Replenishment**, then open the syringe line. The safety stock is
   ~2,900 units and the workings show why: almost all of it is the *supply*
   variance term, i.e. that one distributor. Change supplier, and the branch
   holds a fraction of the stock. That is the demo's money shot.
9. **Raise draft order**, then reload. The suggestion does not come back — the
   draft is netted off. It is still only a DRAFT, and a second person has to
   approve it.
10. **Analysis → Exceptions.** ~90 findings out of 52,000 movements, including a
   3am adjustment and three unexplained count variances on the same controlled
   drug at the same branch. Open one: it shows what it was measured against and
   the ledger rows behind it.
11. **Analysis → Demand forecast.** Every series was backtested before it was
   shown — the table lists the methods that lost.
12. Resize to a phone. Tables become card lists; the sidebar becomes a drawer.

---

## Layout

```
api/     FastAPI + SQLAlchemy 2.x + Alembic. app/services/ledger.py is the only write path.
  app/ai/       Layer 2. Reads the ledger, never writes to it.
  app/ai/intake/  Invoice reading: service (the model call), validate (the checks
                  that make a misread detectable), match (line to catalogue product).
  fixtures/     Six invoices and their recorded readings, for a demo with no network.
  app/seed/     bootstrap.py (demo fixture) and history.py (2 years of synthetic trading).
  app/core/     tunables.py declares every setting once; the API and UI both render from it.
web/     React 19 + Vite + Tailwind v4 + TanStack Query.
bench/   OCR benchmark harness — 50 generated distributor invoices and a scorer.
docs/    Product guide: one page covering every screen, built from source + screenshots.
scripts/ db.sh — project-local Postgres control.
```

### The OCR benchmark

`bench/ocr/` generates 50 Indian distributor invoices that vary across layout,
font, column set, tax presentation, expiry format and scan quality. Ground truth
is written *first* and the document rendered from it, so the answer key cannot be
wrong.

`gemini-3.1-flash-lite` scores **88% clean invoices** — 100% on batch number,
rate and quantity, with zero lines dropped or invented across 462. Read the
per-field table rather than a single number: a wrong batch breaks recall tracing,
while an abbreviated product name does not.

```bash
cd bench/ocr && python3 generate.py && python3 render.py && python3 score.py --predictions out/pred/<system>
```

### Deployment

CI builds arm64 images and rolls them out over SSH; the production compose
overlay also accepts locally-loaded images, so a manual `docker save` → `scp` →
`docker load` deploy works with no CI at all. Images are never built on the
server — it has 2 GB and a frontend build would exhaust it.

---

Three invariants worth knowing before changing anything:

- **`app/services/ledger.py` is the single write path to stock.** A DB trigger
  (`reject_mutation`) rejects any UPDATE or DELETE on `stock_movements`, so corrections
  must be posted as reversing entries.
- **`stock_balances` is a projection, never a source of truth.** It is maintained by an
  AFTER INSERT trigger in the same transaction. `rebuild_balances()` recomputes it from
  the ledger; `test_rebuild_balances_matches_ledger` asserts the two agree exactly.
- **`app/ai/` is a read-only bolt-on.** Nothing under it writes to the ledger, and
  nothing under it is stored — every forecast, finding and recommendation is
  recomputed from `stock_movements` on request. Delete the whole directory and
  the inventory system is unaffected. Two deliberate exceptions, both of which
  go through the same service a human would and neither of which touches stock:
  the reorder feature's "raise draft order" creates a `DRAFT` purchase order
  under its own `ai.act` permission and still needs a second person to approve
  it; and invoice intake records what a distributor calls a product on
  `product_suppliers.supplier_sku`, once, after a person has answered it.

  Invoice intake in particular **creates nothing** on its own. The endpoint
  returns a proposal — there is no code path from it to `ledger.post_movement`,
  which is why the worst outcome of a misread invoice is a form with a wrong
  number in it that somebody corrects before pressing the button.
