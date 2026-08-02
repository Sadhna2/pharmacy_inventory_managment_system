# Pharmacy Inventory Management System

Multi-branch pharmacy chain — 1 central warehouse, 5 branches, retail + institutional B2B.
Built on an **append-only stock ledger**: no row in `stock_movements` is ever updated or
deleted, and every balance you see is derived from it.

**Status:** Layer 0 (spine), Layer 1 (operations) and four of the six Layer 2
analysis features are complete and verified, along with an administrator
settings screen that makes every AI threshold and feature switch tunable at
runtime. 90 end-to-end tests, run against a live server and a real Postgres —
two of them skip themselves when the seeded dataset has nothing to reorder,
rather than asserting against data that happens not to exist.

Natural-language reporting and invoice OCR are designed but not built; both are
shipped switched off and the API refuses their routes while the switch is off,
so nothing half-finished is reachable. Sales invoicing and payment tracking are
specified but not started — the system records what moved, not yet what is owed.

Code comments cite `ARCHITECTURE.md` by section. That file is the internal
design reference and is deliberately not published here; the citations are
kept because they explain *why* a given line is written the way it is.

---

## Running it

Nothing installs globally. Postgres runs as a project-local cluster on port **55432**
with its data in `api/.pgdata`; Python lives in `api/.venv`. All three are gitignored.

**1 — database**

```bash
./scripts/db.sh start && cd api && .venv/bin/alembic upgrade head && SEED_PASSWORD='pick-something-here' .venv/bin/python -m app.seed.bootstrap
```

**2 — API** (http://127.0.0.1:8000, docs at `/docs`)

```bash
cd api && .venv/bin/uvicorn app.main:app --port 8000
```

**3 — web** (http://localhost:5173, proxies `/api` to the backend)

```bash
npm run dev --prefix web
```

**4 — synthetic trading history** (optional, ~30s — needed by everything under *Analysis*)

```bash
cd api && .venv/bin/python -m app.seed.history --days 730
```

Generates two years of sales, purchases, transfers and expiries across all six
locations, plus a handful of deliberately planted anomalies for the exception
detector to find. Tagged `SYNTH`, so `--reset` removes exactly this and leaves
the hand-built demo fixture alone. Without it the Analysis screens load fine and
honestly report that there is not enough history to say anything.

### Tests

```bash
cd api && SEED_PASSWORD='the-same-value-you-seeded-with' .venv/bin/python -m pytest tests/ -q
```

### Starting over

`./scripts/db.sh reset` drops and recreates the database. It terminates open
connections first, so you do **not** need to stop uvicorn — but you do need to
re-run the migrate + seed step afterwards.

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
5. **Stock.** Balances carry the full grain: product, location, bin, batch, status.
6. **Analysis → Supplier lead times.** Apex Pharma Supply comes out *Erratic* —
   7 days typically, 15 at worst, meeting its own promised date 49% of the time.
   Open it: the percentiles are backed by the actual purchase orders they came from.
7. **Analysis → Replenishment**, then open the syringe line. The safety stock is
   ~2,900 units and the workings show why: almost all of it is the *supply*
   variance term, i.e. that one distributor. Change supplier, and the branch
   holds a fraction of the stock. That is the demo's money shot.
8. **Raise draft order**, then reload. The suggestion does not come back — the
   draft is netted off. It is still only a DRAFT, and a second person has to
   approve it.
9. **Analysis → Exceptions.** ~90 findings out of 52,000 movements, including a
   3am adjustment and three unexplained count variances on the same controlled
   drug at the same branch. Open one: it shows what it was measured against and
   the ledger rows behind it.
10. **Analysis → Demand forecast.** Every series was backtested before it was
   shown — the table lists the methods that lost.
11. Resize to a phone. Tables become card lists; the sidebar becomes a drawer.

---

## Layout

```
api/     FastAPI + SQLAlchemy 2.x + Alembic. app/services/ledger.py is the only write path.
  app/ai/       Layer 2. Reads the ledger, never writes to it.
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
  the inventory system is unaffected. The one exception is the reorder feature's
  "raise draft order", which creates a `DRAFT` purchase order through the same
  service any human would use, under its own `ai.act` permission, and still
  needs a second person to approve it.
