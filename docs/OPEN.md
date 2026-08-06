# Open work

Short list of what is agreed but not yet built. Delete a line when it ships.

## Decided

- **Goods with no purchase order**: scan to raise the order, then receive
  against it. Two steps, one way in — every receipt ends up with an order and
  an invoice behind it.
- **Approval stays.** A scanned order lands as a draft and someone else
  approves it before stock can be received. Approval is what stops one person
  inventing a purchase; the invoice being in hand does not change that.

- **Receiving somewhere the order did not name stays refused.** The per-line
  cross-dock already gets the stock to the right branch and raises the real
  transfer, so nothing is impossible — and the refusal keeps catching the far
  commoner mistake, which is picking the wrong order off the list.

## Fixed, waiting on you to confirm

- **Allocate no longer leaves the row saying Draft.** You were right. The
  allocate call bypassed the shared action helper — because it needs the
  response body, to show which batches FEFO picked — and so nothing refreshed
  the list. Worse, its error handler re-sent the same request just to get a
  message into the banner, which on a half-successful call was a second
  attempt to reserve stock. Both fixed; verified Draft → Allocated with no
  refresh.

- **Customers split into two lists.** Master data now has **Institutions** and
  **Retail buyers** as separate tabs. The Type column is gone — the tab says
  which list you are on — and Phone takes its place, which is the column that
  matters for a retail buyer. "New institution" / "New retail buyer" defaults
  the form to the right kind.

- **Search on both, and on Distributors.** Matches name, code or GSTIN,
  case-insensitively, debounced. Filtered on the server rather than in the
  browser: institutions stay in the dozens but every walk-in served becomes a
  row, so the retail list only grows. GSTIN is searchable because someone
  holding an invoice has the number in front of them and the trading name may
  not match what was typed in here.

- **The test suite no longer touches your data.** `./scripts/test.sh` runs the
  whole thing the way CI does — throwaway `postgres:16` container, migrations
  from scratch, full seed, API on :8001, 572 backend tests, then the frontend
  lint/typecheck/build — and deletes the container afterwards. About four
  minutes. `--backend` skips the frontend, `--keep` leaves the database up,
  and extra arguments go to pytest.

- **Walk-in customer on the fly.** "＋ Walk-in customer…" at the top of the
  customer picker on a new sales order. Name, a state defaulted to the branch,
  optional GSTIN. Saves a real customer and selects it, so the order carries on.
  A Maharashtra buyer off the Mumbai warehouse comes out CGST + SGST, which is
  what the state default is there for.

- **Invoice scanning moved to New order.** "Scan an invoice" on Purchasing now
  opens the order form, not the receipt. The scan fills the products,
  quantities and rates; a line it cannot name blocks the button rather than
  quietly dropping off the order. Receive goods has no scanner at all now —
  selecting the order already fills its quantities and its branch.

- **The scanned file is kept.** Stored against the order (Postgres `bytea`, no
  new infrastructure), and offered as a download under the order picker on
  Receive goods — so the cartons can be checked against the paper they came
  with. One per order: re-scanning replaces it, because a second scan of the
  same delivery is a correction rather than a second document.

- **Recalls multiplying.** Two causes. Re-seeding added a second pair every
  time, because raising a recall freezes stock and closing one scraps it and
  neither can be undone — so the seed now leaves the first pair alone instead
  of trying to replace it. The other was me: every run of the test suite raises
  one against the demo database. Rebuilt from scratch, so you are back to two.

- **Horizontal scrollbar on the operations tables**, with the row menu pushed
  off the right edge. Mine: I gave the menu column `width: 1%` expecting it to
  hug, but `table-fixed` takes a declared width literally, so the cell became
  twelve pixels and its button overflowed the table. Reverted, and the reason
  is written into `DataTable.tsx` so it does not get re-added.

- **The seller's details never reached the deployed API.** `SELLER_LEGAL_NAME`,
  `SELLER_GSTIN` and `SELLER_ADDRESS` were in `.env.example` and in the
  server's own `.env` from the start, and were never listed in the `api`
  service's `environment:` block — so compose read them for `${...}`
  interpolation and passed none of them into the container. Every **Print
  invoice on the server refused**, with the same 409 a branch with nothing
  recorded gets, while the values sat in a file two directories up. Now
  forwarded. This also corrects something I told you earlier: prod's bad
  `SELLER_GSTIN` check digit was never printed on anything, because the
  setting never arrived — it starts mattering the moment this deploys.

- **Contact details on the invoice.** Two phone numbers and an email in the
  seller block, from `SELLER_PHONE` / `SELLER_EMAIL`. The buyer's own phone
  and email print in the same block — `Customer` already carried both columns.
  Absent details are left off rather than printed as an em dash: "Phone: —"
  states a fact about our database, not about the supply.

- **The invoice prints, and saves as PDF.** The button used to leave a tab of
  HTML for you to find the print menu in. It now opens the print dialogue,
  where "Save as PDF" is a destination on every desktop browser — so the same
  button both prints and downloads. No server-side PDF renderer: that is a
  heavy dependency and the box has 2 GB.

- **A closed form no longer remembers what was in it.** Scanning an invoice,
  going back, and reopening the scanner showed the previous scan's lines.
  `Modal` unmounts its own subtree when it closes, but the form component
  holding the draft state sits *above* that call and stays mounted, so nothing
  was ever discarded. All eleven forms are now mounted only while open, and
  the reason is written into `Modal` so it does not get undone. Verified in
  the browser: typed a quantity, closed, reopened — empty.

- **Phone and email when capturing a walk-in.** The panel asked for a name, a
  state and a GSTIN and stopped, though the endpoint already accepted a phone
  and `customers` has carried both columns all along. A counter buyer is the
  party with no other record behind them — no account manager, no purchase
  order — so if a batch they were sold is recalled, these two fields are the
  whole means of telling them. Optional, because a sale cannot be held up over
  a number nobody wants to give, but asked for, because nobody adds them
  later. Blank is stored as null rather than an empty string, so the invoice
  can decide whether to print a contact line by asking whether there is one.
  The full customer form already had both — checked.

- **The deploy waits for the tests now.** It used to trigger on a push to
  `main` alongside CI and wait only on its own build job, so the two raced: a
  commit whose tests failed was published and rolled out anyway, and the only
  sign was a red X beside a working deployment. `ci.yml` is a reusable
  workflow now and `deploy.yml` calls it as its first job — the pipeline is
  test → build → deploy → verify, all on the exact commit being deployed.
  Validated with actionlint; it cannot be exercised for real until this push,
  because GitHub Actions only runs on GitHub.

- **An invoice no longer borrows another state's registration.** The firm's
  configured GSTIN stands in for a branch that has none — that fallback was
  written for a chain trading in one state, and the code never checked the
  condition. So an Ahmedabad order printed "State: GJ (24)" beside a number
  opening `27`, on a document captioned TAX INVOICE, against which no buyer
  could claim input credit. Now only a registration held in the branch's own
  state substitutes; anything else refuses and names the branch.

- **The seed converges on a running server.** `sync_permissions` and
  `sync_roles` were called from inside `seed()`, which `main()` returns before
  reaching whenever a user already exists — so the deployed box had the
  authorisation rows it was provisioned with and no others, and the next PR to
  add a permission would have shipped an endpoint that refused everybody. The
  two categories are named apart now: `converge()` for what must be true of
  every database (permissions, roles, feature flags) and runs unconditionally;
  `seed()` for demo fixtures on an empty one. Guarded by
  `tests/test_seed_convergence.py`.

## Next up — agreed, not yet built

- [ ] **Decide what to do about DAMAGE in the seed** — see the audit below.
      One line in `history.py`, or a deliberate note that both shapes are
      intended.

## Not code — yours to do, nothing here is waiting on me

- [ ] **Rotate `GEMINI_API_KEY`** — it leaked into a transcript.
- [ ] **Set `SELLER_GSTIN` in the server's `~/.env`** to a number with a valid
      check digit. The one there, `27AABCS9876P1ZK`, fails mod-36.
- [ ] **Enter the five branch GSTINs** after this deploys, in Master data →
      Locations. The four Maharashtra branches take `27AABCS9876P1ZA`,
      Ahmedabad takes `24AABCS9876P1ZG`. This is configuration, not a
      workaround: a GSTIN is a real registration the business holds, so the
      seed fills them in only on a database it built itself. Until Ahmedabad
      has its own, its invoices refuse rather than print a wrong one.

## What deploying PR #19 does to the server's data — checked

Nothing is replaced. `alembic upgrade head` runs four migrations (`c7`–`ca`),
every one of them `add_column` or `create_table`; the drops are all in
`downgrade()`, which a deploy never calls. The two backfills fill only the
column they just added. `app.seed.demo` then does nothing: bootstrap returns on
an existing user, and history and showcase are both `--if-empty` against rows
prod already has. Deploy runs `pull`, `up -d`, `image prune` — no `down -v`, so
`pgdata` is untouched. The seed changes on this branch (recall guard, history
timestamps, `compact.py`) only take effect on a fresh build or `--rebuild`.

## The GST and ledger audit — what was checked, and what it found

Run them again with `scripts/audit_gst.py` and `scripts/audit_ledger.py`
(`PYTHONPATH=. .venv/bin/python ../scripts/audit_gst.py` from `api/`). Both are
read-only. Each check states what the number *ought* to be from the rule, not
from the code that produced it, so a wrong implementation and a wrong check
would have to agree by accident.

**GST — nothing wrong found.** 1,260 line cases and 56 document cases swept
across every statutory rate (0, 0.25, 3, 5, 12, 18, 28) against quantities and
prices sitting on the rounding boundaries, both regimes: 0 failures. All 17
stored orders and 31 lines recomputed from quantity, price and rate: 0
discrepancies. The HSN summary reconciles to `subtotal` and `tax_total` on
every invoice. The halves of an intrastate tax sum exactly to the whole rather
than being rounded separately, which is the usual way an invoice ends up a
paisa short of itself.

One thing that looked like a failure was my own check being wrong: a
`round_off` of exactly +0.50 is correct. A ₹3.50 total rounds half-up to ₹4.
The bound is asymmetric — greater than −0.50, up to and including +0.50 — and
the sweep now says so.

**Coverage, which is the real finding.** There are **43,388 sale postings in
the ledger and 17 sales orders**. The two years of generated history is written
straight to the ledger as movements, with no order documents behind it. That is
fine for the forecasting and analysis screens, which read movements — but it
means no historical sale has a tax record or can produce an invoice, and the
stored data exercises the tax engine on 31 lines. Thirty-one lines is not
evidence about a tax engine, which is why `tests/test_gst_sweep.py` now covers
the input space instead: 1,316 cases, no database, under a second.

**Ledger — nothing wrong found.** Over 53,108 rows: the projection equals the
sum of the postings on all 2,784 (product, warehouse, lot, status) keys;
`rebuild_balances()` reproduces the live projection exactly on all 2,784; no
negative stock; nothing reserved beyond what is on hand; both transfer legs
cancel on all 2,003 (transfer, product) pairs; no zero-quantity rows; every
idempotency key unique. Both triggers are attached, and UPDATE and DELETE were
each attempted against the ledger and each refused.

**One inconsistency, in the seed rather than the code.** `DAMAGE` is posted two
different ways. `showcase.py` writes a balanced pair — 12 out of AVAILABLE, 12
into DAMAGED, a status move. `history.py:834` writes a single leg: 12 out of
AVAILABLE and nothing anywhere else, a write-off ("carton discarded"). So "how
much damaged stock do we hold" answers 24 and "how much was damaged" answers
36, and both are defensible readings of the same movement type — which is the
problem. Nothing is lost and no balance is wrong. It is not reachable through
the app at all: no service or endpoint posts `DAMAGE`, only the seed does.

## Known and left alone

- `python -m app.seed.compact` trims the demo data; `--movement-days N` also
  shortens the ledger by folding older rows into opening balances. That costs
  the demand forecast, exceptions and replenishment, which read the history.
  `python -m app.seed.demo --rebuild` puts everything back.
