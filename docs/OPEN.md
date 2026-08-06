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

## Known and left alone

- `python -m app.seed.compact` trims the demo data; `--movement-days N` also
  shortens the ledger by folding older rows into opening balances. That costs
  the demand forecast, exceptions and replenishment, which read the history.
  `python -m app.seed.demo --rebuild` puts everything back.
