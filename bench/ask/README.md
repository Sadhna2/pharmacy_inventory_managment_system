# Ask-the-database accuracy harness

Forty-four cases with a written-down answer key: 30 single-turn questions, 8
two-turn follow-up pairs, 6 as a microphone mangles them. They live in
`api/tests/golden_questions.py`, next to the pipeline they measure, so the API's
own tests and this harness score against one definition of "right" rather than
one each.

**What is here is the key, not a number.** No run has been scored against it,
and none is quoted. `bench/ocr` next door says 99.8% per-field accuracy because
a scorer was run and its output was read; the same standard applies here, so
this file stays numberless until it can meet it.

## Producing a score

The key is the part that took judgement; the runner is thin, and its contract
is fully specified below so that writing it involves no further decisions.

1. Seed a database — the exact sequence is in the module docstring of
   `app/seed/showcase.py`, and all three steps are needed. The questions are
   written against that dataset: Andheri, Bandra, Pune, Ahmedabad and the
   central warehouse, MedPlus and Gujarat Health Traders, the products in
   `app/seed/bootstrap.py`. Several of them — the recall trace, the quarantined
   stock, the transfer still in flight — only have anything to find because
   `showcase` puts the awkward states there on purpose.
2. Ask each question through the same endpoint the UI uses, so the run measures
   the shipped path and not a copy of it. For a follow-up pair, ask both turns
   in order, passing only the previous question and its SQL — that one step is
   the whole of the memory contract, and a runner that passes a transcript is
   measuring something the product does not do.
3. Record one JSON per question in `out/run/<name>/`: the SQL that was
   generated, the shape of what came back, and whether the pipeline refused or
   asked back. Record the SQL even when the pipeline declined — a refusal that
   generated a mutation and then thought better of it is a different fact from
   one that never generated it.
4. Score by the rules in the next two sections. Every check is a set
   containment test over identifiers in the recorded SQL plus two enum
   comparisons — nothing in the scorer is fuzzy, and nothing in it is a model.
   A scorer that parses the SQL rather than matching substrings can be stricter
   on the follow-up pairs, and the dataclass docstring says exactly where.

`unknown_tables()` in the key returns the table names it mentions that do not
exist in the ORM metadata. It should return the empty set, and a run should
start by asserting that — a typo in the key is the one defect that passes
silently, because the assertion still runs and is simply about nothing.

## Why an answer key and not a demo

Ask a model a question about a database and the wrong answer looks exactly like
the right one. A number, formatted well, delivered confidently. There is no
tell — no misspelling, no ragged edge, nothing a reader can catch. This is the
respect in which text-to-SQL is harder to trust than the invoice reader: a
misread invoice fails an arithmetic identity, and there is no equivalent
identity for "which distributor delivered the most units last quarter".

So the questions are fixed first, in a file committed before any tuning, and
each one records what a correct answer has to touch. The score is then a
property of the run, not an impression of it.

## What is scored, and what is deliberately not

Not the numbers. A golden file full of expected row counts rots the first time
the seed changes, and then it gets regenerated from whatever the pipeline
currently returns — which is how an answer key quietly becomes a transcript of
the bug. What is checked survives a reseed:

| Check | What it catches |
|---|---|
| `must_reference_tables` | the SQL cannot be right without reading these |
| `must_not_reference_tables` | a right-looking number from the wrong table |
| `expected_shape` | a ranking collapsed to one figure, a series to a total |
| `outcome` | a question that must be refused, or asked back about |
| `must_resolve_to` | a distorted product name landing on the wrong SKU |
| `must_carry` / `must_drop` | a follow-up that forgot a filter, or kept one |

`must_not_reference_tables` and `outcome` are the two that carry the weight.
Two entries in the set return an entirely plausible number from the wrong
table, and the wrongness is visible nowhere except in which tables were
touched. `FORBIDDEN_IDENTIFIERS` sits alongside them and is checked on every
question rather than per entry: `password_hash` and `refresh_tokens.token_hash`
have no part in any inventory question ever asked.

## Reading the score

Five numbers, because they cost five different things. A single headline would
average a cosmetic miss against a data-loss bug, and the average would be
reassuring.

- **refusal rate** — the share of the two REFUSE questions that were declined.
  Anything below 100% and the rest of the report is irrelevant. One asks the
  system to write off stock, one asks for a password hash.
- **clarification rate** — the CLARIFY questions that asked back instead of
  guessing. A guess here is a confident number for a question that had two
  correct answers, with nothing on screen to say a choice was made.
- **table accuracy** — over the 30 golden and 6 spoken questions: whether the
  required tables were read and the forbidden ones were not. This is where the
  semantic layer either earns its place or does not.
- **follow-up carry-over** — over the 8 pairs, and worth splitting REFINE from
  NEW, because they fail in opposite directions and a combined figure hides
  both. A pipeline that inherits everything scores perfectly on the five
  REFINEs and zero on the three NEWs; so does one that inherits nothing, with
  the columns swapped. Only the split says which.
- **shape accuracy** — reported separately and last, because it is cosmetic and
  would otherwise dilute the four above.

Expect table accuracy to sit well below whatever the "looks about right" rate
is, exactly as clean-invoice rate sits well below per-field accuracy in
`bench/ocr`. That gap is the number worth arguing about.

## What each part of the set is for

**30 golden questions**, covering simple lookups, filtered detail, grouped
aggregation, multi-hop joins and time windows — and, within those, the cases
that are hard for a reason:

- **Two where the semantic layer is the entire answer.** These are the paired
  test of what `app/ai/ask/schema_context.py` puts in the prompt: a rule stated
  there that changes no answer here is costing tokens for nothing, and one of
  these failing means the rule is stated and not landing. *"How much
  Amoxicillin can I promise a customer at Andheri today?"* cannot be answered
  from `stock_movements` at all — what can be promised is on-hand minus
  reserved, and a reservation posts no movement, because nothing has physically
  moved. Summing the ledger overstates by exactly the quantity already promised
  to somebody else. And *"how many units have we written off as damaged this
  year?"* doubles if reversing entries are counted: a correction is a second
  row with the same `movement_type`, the opposite sign and
  `reference_type = 'REVERSAL'`, so `SUM(ABS(quantity))` counts the mistake and
  the apology.
- **One that must be refused because it changes data.** Not by asking the model
  to behave: what it emits is not a single `SELECT`, and the guard rejects it
  before a connection is touched.
- **One that must be refused because it asks for password hashes.** A legal,
  well-formed `SELECT` that any statement-shape check waves straight through,
  which is why the column list is checked separately.
- **One that is genuinely ambiguous.** *"How much insulin do we have left?"* —
  the catalogue holds two, at 850 and 195 a vial. Summing them means nothing;
  picking one silently is a wrong answer with no tell.

**8 two-turn follow-up pairs**, testing the one-step memory rule: the second
turn may inherit the previous question and its SQL, nothing more. Five are
REFINE (including one that must *widen* by dropping an inherited branch filter,
and one where only the product survives) and three are NEW, where dragging a
filter along is the failure. The NEW cases are the quieter ones — a stale
filter narrows the result, and a smaller number never looks suspicious.

**6 questions as dictated**, with real product names from `app/seed/` put
through the distortions a microphone produces: "Metformin 500 at Andheri"
arrives as *"met foreman five hundred at and Harry"*, "Montelukast" as *"monty
lucas"*. These share no whole token with the catalogue entry, so substring
matching fails them outright. One of the six must ask back rather than resolve:
even read perfectly, "pantoprazole 40" names both a strip and a vial, and dose
form is the part a ward cannot substitute.

## Adding a system

Nothing above is specific to a model or a library. Swapping the model behind
the pipeline changes the run directory name and nothing else, which is what
makes the key usable for a comparison rather than only for a regression check.

Note on sample size, in the same spirit as `bench/ocr`: 30 questions is enough
for a headline and thin for a per-category claim. Three time-window questions
cannot tell a 90% pipeline from a 70% one, and the two refusals are a tripwire
rather than a measurement — they prove the guard fires on the two attacks
anybody would try first, not that it fires on every attack. If a claim about
one category has to hold up, that category needs more questions, and they need
writing before the run rather than after it.
