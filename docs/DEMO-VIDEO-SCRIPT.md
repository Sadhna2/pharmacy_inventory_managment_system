# Demo video — shot script

**Target length** 5–6 minutes. **Recording** 1280×800, browser zoom 100%,
sidebar expanded. Sign in beforehand so no password is typed on camera.

Before recording:

```bash
cp .env.example .env && docker compose up
```

Wait for the seed to finish, then sign in as `manager@pharmacy.co.in`. Have a
demo invoice image open in a folder ready to drag in. If the venue network is
unreliable, set `INTAKE_FIXTURE_DIR=fixtures/intake` and use a recorded
invoice — everything except the transcription still runs live.

---

## Shot 1 — What this is (0:00–0:25)

**Screen** Dashboard.

**Say**

> A pharmacy chain with a central warehouse and four branches. Everything here
> is tracked by batch and expiry, because two boxes of the same medicine are
> not interchangeable — different batch, different expiry, different printed
> price ceiling.

**Do** Hover the stock and expiry tiles. Don't click yet.

---

## Shot 2 — The ledger (0:25–1:00)

**Screen** Movements.

**Say**

> Stock is never stored as a number. Every quantity in this system is derived
> by summing this ledger, and the ledger is append-only — the database itself
> rejects updates and deletes.

**Do** Scroll the movements list. Point at a reversing entry.

**Say**

> A correction is a reversing entry. The original row stays and the balance
> moves because a second row says so. That's why a recall can trace every unit
> of a batch: nothing was ever overwritten.

---

## Shot 3 — Where data actually enters (1:00–1:20)

**Screen** Purchasing.

**Say**

> Here's where the data really enters the system. A delivery arrives with a
> paper invoice in the carton, and somebody types in every batch code, expiry,
> quantity and rate. That typing is slow, and it's where the errors come from.

**Do** Let the AI callout sit on screen for a beat.

---

## Shot 4 — The rule, before the demo (1:20–1:45)

**Screen** Stay on the callout; read from it.

**Say**

> So we photograph the invoice instead. But this is regulated stock, so the
> rule we set was: the model is never allowed to produce an answer. It only
> produces structured input to code that already validates.
>
> That works because an invoice is over-determined. Quantity times rate must
> equal the line amount. Lines must sum to the subtotal. The fifteenth
> character of a GSTIN is a checksum over the other fourteen. None of that
> needs an answer key.

---

## Shot 5 — Scan it (1:45–2:40)

**Do** Click **Scan an invoice** → upload the invoice → wait.

**Say while it works**

> One call out, for the transcription only. The arithmetic, the checksum and
> the product matching all run locally.

**Do** Let the filled form land. Scroll the lines slowly.

**Say**

> Fourteen lines. Batch codes, expiries, quantities, rates — and product names
> matched to our catalogue, including trade names no rule could reach, like
> OMEZ-20 for omeprazole.

---

## Shot 6 — It catches itself (2:40–3:20) — **the key shot**

**Do** Scroll to the findings. Land on the GSTIN checksum flag.

**Say**

> And here's the part that matters. It's flagged the supplier's GSTIN: the
> checksum doesn't match, so at least one character was misread.
>
> Nothing external told it that. The fifteenth character is a mod-36 checksum
> over the other fourteen, so the document checks its own transcription. The
> model was wrong, and arithmetic printed on the paper caught it.

**Do** Correct the character. Watch the flag settle from amber to green.

**Say**

> Fix it, and the finding clears.

---

## Shot 7 — Nothing has happened yet (3:20–3:45)

**Say**

> Everything you've seen so far has created nothing. No stock, no document, no
> ledger row. There is no code path from the scanner to the ledger — the worst
> case for a total misread is a form with wrong numbers in it.

**Do** Click **Receive into stock**.

**Say**

> Only now does stock exist, because a person submitted it.

**Do** Cut to Stock and show the new batches with their expiries.

---

## Shot 8 — Statistics, labelled as statistics (3:45–4:20)

**Do** Demand forecast → open one series.

**Say**

> Two capabilities are generative models. Four are statistics, and we label
> them that way. This is Holt-Winters exponential smoothing, and every series is
> backtested against "the same weekday last week" — if it can't beat that
> baseline, it says so rather than quietly using the fancier model.

**Do** Replenishment → expand one line's workings.

**Say**

> Reorder point and safety stock, with the workings shown term by term. It can
> raise a draft purchase order — but a second person still has to approve it.
> Separation of duties isn't waived because a machine suggested it.

---

## Shot 9 — Ask it a question (4:20–5:15) — **the second key shot**

**Do** Analysis → Ask. Type: *"which batches expire in the next 90 days and how
many units are in them?"*

**Say**

> The second generative feature. I type a question, and I get rows.

**Do** Open **Show the SQL**.

**Say**

> And this is the part that matters. The model didn't write the answer — it
> wrote this query, one SELECT. A guard refuses anything that isn't a single
> read, Postgres plans it before it runs, and it executes read-only, timed out
> and capped. What's on screen is rows Postgres returned, and the query is
> right here to disagree with.

**Do** Ask: *"which customers owe us money?"* Wait for the refusal.

**Say**

> Now watch it decline. There is no payments table in this system — we record
> what moved, not what's owed. The tempting answer was available: add up every
> order. That would have named a walk-in customer who paid cash at the counter
> as a debtor, and it would have looked completely reasonable.
>
> Refusing is the feature.

> Pause on the refusal for a beat. It is the least flashy thing in the demo and
> the most convincing.

---

## Shot 10 — The switch is real (5:15–5:35)

**Do** Settings (as Admin) → toggle **Invoice scanning** off → back to
Purchasing → the callout is gone.

**Say**

> And when an administrator turns a capability off, its API routes close with
> it. It's enforced on the server, not hidden in the menu — a stale tab or
> someone with the URL gets a 404.

**Do** Toggle it back on.

---

## Shot 11 — Close (5:35–6:00)

**Screen** README, or the terminal with `docker compose up`.

**Say**

> Docker is the only prerequisite — one command on macOS, Windows or Linux.
> Four hundred and ninety-four tests run against a real Postgres and a real
> HTTP server on every push.
>
> The guarantees hold because of how the system is built — an append-only
> ledger the database protects, and a model that can only produce something
> checkable — not because everyone remembered to be careful.

---

## If something goes wrong on camera

| Problem | Recovery |
|---|---|
| Scan is slow or times out | Set `INTAKE_FIXTURE_DIR=fixtures/intake` and re-record shot 5; say the transcription is replayed and everything else runs live |
| No API key / 503 | Same fixture path; the feature degrades rather than breaking |
| Ask is slow, or the quota is spent | It has no fixture mode — cut shot 9 and say so, or ask a question you have already run this session. Do not retry on camera; a spinner is worse than a missing shot |
| Ask answers something you did not expect | Open the SQL and read it aloud. Being able to see why is the claim being made; a surprising answer with its query attached demonstrates it better than a tidy one |
| A line won't match | That is the designed behaviour — show the shortlist and say refusing beats guessing a wrong batch |
| Seed is empty | `docker compose down -v && docker compose up` |
