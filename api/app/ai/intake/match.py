"""Resolving an extracted invoice line to a real product, deterministically.

The model reads `AMOXY-500 CAP` off the paper. The receipt needs product 412.
Nothing in this file asks a model to bridge that gap — it is exact lookups
against the catalogue, in a fixed order, with a hard rule at the end:

    if more than one product fits, none is chosen.

That rule matters more than any amount of cleverness. A wrong match posts
stock against the wrong product, and it does so silently, because the receipt
looks perfectly ordinary afterwards. An unmatched line is visible, ugly, and
costs somebody ten seconds with a dropdown. The second failure is recoverable
and the first is not, so the whole design leans hard towards refusing to guess.

WHY THE PURCHASE ORDER IS THE FIRST THING ASKED FOR
---------------------------------------------------
Matching `AMOXY-500 CAP` against nine hundred products is a guessing game.
Matching it against the eight lines of the purchase order this delivery is
against is nearly free — the shortlist is already the answer, and anything not
on it is a genuine exception worth showing a human.

So the candidate pool narrows in this order:

    a purchase order        ->  its lines only          (typically 3-14)
    otherwise a supplier    ->  what they have supplied (tens)
    otherwise               ->  the active catalogue    (hundreds)

The first case is the normal one, and it is the reason this module can afford
to be strict.

HOW ABBREVIATIONS ARE ACTUALLY SOLVED
-------------------------------------
Distributors print their own shorthand, and `PCM-650 TAB` is not derivable from
`PARACETAMOL 650MG TAB` by any string rule worth trusting — it is an acronym,
not a prefix or a typo. Pretending otherwise produces confident nonsense.

The honest fix is the one real systems use: the first time a human resolves it,
the shorthand is stored on `product_suppliers.supplier_sku`, and every later
invoice from that distributor matches it exactly. Cold start is manual, and it
converges after one delivery per line item. `remember_alias` is that step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.intake import service
from app.ai.intake.validate import Flag, Severity, batch_shape
from app.models.documents import PurchaseOrder, PurchaseOrderLine
from app.models.masters import Product, ProductSupplier
from app.models.stock import Lot

#: How a line was resolved, strongest first. Reported so the UI can show why a
#: row is filled in, and so a weak match can be styled differently from a
#: certain one rather than both looking equally settled.
SUPPLIER_ALIAS = "supplier_alias"
SKU = "sku"
EXACT_NAME = "exact_name"
NAME_TOKENS = "name_tokens"
#: Named by the model and vetted by the rules — see `suggest_unmatched`. Kept
#: as its own method so a person, and the audit trail, can tell at a glance
#: which rows were judged rather than derived.
AI_NAME = "ai_name"
UNMATCHED = "unmatched"

#: Above this, a receipt is not a delivery against the order any more. Trade
#: practice allows a little over-supply; ten times the outstanding quantity is
#: a decimal point in the wrong place.
OVER_RECEIPT_TOLERANCE = Decimal("1.10")

#: How far the invoiced rate may sit from the agreed price before it is worth
#: mentioning. Distributor prices move; they do not double.
PRICE_VARIANCE = Decimal("0.25")

#: Units and filler. These genuinely carry nothing: the strength is captured as
#: a number in its own right, so the unit beside it adds no way to tell two
#: products apart.
NOISE_TOKENS = frozenset({"MG", "ML", "GM", "G", "MCG", "IU", "OF", "AND", "THE"})

#: Dosage forms, folded to one spelling each.
#:
#: These are emphatically NOT noise, and treating them as such was a real bug:
#: `PANTOPRAZOLE 40MG TAB` and `PANTOPRAZOLE INJ 40MG` are different products
#: at the same strength, and the form is the only thing that separates them.
#: Dropping it made `PANTOP-40 TAB` ambiguous against both and sent a line to a
#: human that the catalogue could answer on its own.
#:
#: Folded rather than kept verbatim because a distributor writes `TABS` where
#: the catalogue says `TABLET`, and those must compare equal.
FORM_SYNONYMS = {
    "TABS": "TAB", "TABLET": "TAB", "TABLETS": "TAB",
    "CAPS": "CAP", "CAPSULE": "CAP", "CAPSULES": "CAP",
    "INJECTION": "INJ", "VIAL": "INJ", "AMP": "INJ", "AMPOULE": "INJ",
    "SYRUP": "SYP", "SYRP": "SYP", "SUSPENSION": "SUSP",
    "OINTMENT": "OINT", "CRM": "CREAM", "DROP": "DROPS",
    "SOLN": "SOLUTION", "PWD": "POWDER",
    # A sachet is packaging, not a dosage form, and the only thing sold in one
    # here is a powder — ORS. Kept apart, they read as two forms that disagree,
    # and `ORS SACHET ORANGE` was ruled out against `ORS Powder 21g` on the
    # strength of a distinction that does not exist.
    "SACHET": "POWDER", "SACHETS": "POWDER", "GRANULES": "POWDER",
}

#: The canonical forms, after folding. Membership decides whether a word
#: identifies a product or merely narrows it.
FORMS = frozenset({
    "TAB", "CAP", "INJ", "SYP", "SUSP", "OINT", "CREAM", "GEL", "DROPS",
    "SOLUTION", "POWDER", "INHALER", "SPRAY", "PATCH",
})


@dataclass
class LineMatch:
    """One extracted line, resolved as far as the catalogue allows."""

    line_no: int
    extracted: Mapping
    product_id: int | None = None
    product_sku: str | None = None
    product_name: str | None = None
    method: str = UNMATCHED
    po_line_id: int | None = None
    qty_outstanding: Decimal | None = None
    #: Populated when the line could not be resolved to exactly one product.
    candidates: list[tuple[int, str]] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.product_id is not None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def normalise(text: str) -> str:
    """Upper-case, punctuation to spaces, runs of space collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z]+", " ", text or "")).strip().upper()


class Name(NamedTuple):
    """A product name split into the three things that decide a match.

    Kept apart because they carry different authority:

      words    what the drug is. Two names sharing none of these are not the
               same product, whatever else agrees.
      forms    tablet, injection, syrup. Never enough to identify anything on
               its own — every tablet is a tablet — but decisive when it
               *disagrees*: `PANTOPRAZOLE 40MG TAB` and `PANTOPRAZOLE INJ 40MG`
               are different products at the same strength.
      numbers  the strength, which must never be approximated. `AMLODIPINE 5MG`
               and `AMLODIPINE 10MG` differ by one character and are different
               doses; agreeing on every word and missing this is not a near
               miss, it is a dispensing error waiting to happen.
    """

    words: frozenset[str]
    forms: frozenset[str]
    numbers: frozenset[str]
    pack: frozenset[str]
    loose: frozenset[str]


#: Units that make a number a *dose*. Everything else — millilitres, a bare
#: count, an unrecognised unit — describes the container instead.
#:
#: The distinction is the difference between two kinds of wrong. Receiving
#: 500mg against 250mg puts the wrong dose in a patient's hand. Receiving a
#: 10ml vial against a 3ml one is the wrong pack of the right medicine: a
#: stock-count error, recoverable, and not worth refusing a match over when
#: the product does not state a volume at all.
#:
#: `K` is here because `_expand` has already turned `60K` into 60000, so by
#: this point it is a strength. `KG` is not: that is a shipping weight.
DOSE_UNITS = frozenset({"MG", "MCG", "UG", "G", "GM", "GRAM", "GRAMS",
                        "IU", "U", "MEQ", "K"})


def _expand(digits: str, unit: str) -> str:
    """`60` with a `K` after it is sixty thousand.

    Vitamin D3 is printed as `60K IU` on one invoice and `60000IU` on the next,
    and to a set of strings those are different strengths — so the check that
    exists to stop 5mg landing on 10mg was also stopping this, which is the
    same product written the way distributors actually write it.

    Only `K`, and only immediately after the digits. Unit conversion proper —
    grams to milligrams, micrograms to milligrams — is deliberately not done
    here: `0.25MG` and `250MCG` are the same dose, but a rule that equates them
    also has to carry the decimal handling that makes `5MG` and `50MG` distinct,
    and getting that subtly wrong is the one mistake this whole file exists to
    avoid. That case stays a job for a human, once, via the alias.
    """
    return str(int(digits) * 1000) if unit.startswith("K") and unit != "KG" else digits


def tokens(text: str) -> Name:
    words, forms, numbers, pack, loose = set(), set(), set(), set(), set()

    def place(token: str) -> None:
        canonical = FORM_SYNONYMS.get(token, token)
        if canonical in NOISE_TOKENS:
            return
        (forms if canonical in FORMS else words).add(canonical)

    for token in normalise(text).split():
        if token.isdigit():
            loose.add(token)              # no unit — see `numbers_agree`
            continue
        head = re.match(r"^(\d+)([A-Z]+)$", token)
        if head:                          # `650MG` -> number 650, unit dropped
            digits, unit = head.group(1), head.group(2)
            value = _expand(digits, unit)
            (numbers if unit in DOSE_UNITS else pack).add(value)
            place(unit)
        else:
            place(token)
    return Name(frozenset(words), frozenset(forms), frozenset(numbers),
                frozenset(pack), frozenset(loose))


def numbers_agree(printed: Name, other: Name) -> bool:
    """Whether the figures on an invoice line can belong to this product.

    Three rules, because a product name carries three kinds of number and they
    do not deserve the same authority.

    A **strength** — a figure with a dose unit on it — must be accounted for:
    every one printed on the line has to appear in the candidate, which is what
    keeps `AMLODIPINE 5MG` away from `AMLODIPINE 10MG`. The converse is
    deliberately not required. A line that prints no strength is ambiguous, not
    wrong, and must still reach the scoring so both doses come back as a
    shortlist for somebody to choose between.

    A **pack figure** — millilitres, a shipping weight — only has to not
    contradict. `ACTRAPID 10ML` is a ten millilitre vial of human insulin; our
    catalogue calls that product `Human Insulin 40IU`, naming the concentration
    and never the volume. Demanding that 10 turn up among the product's numbers
    rejected the only correct answer there was, and did the same to
    `GLARGINE 3ML`. So a volume rules a candidate out only when the product
    states one too and it is a different one — `Disposable Syringe 5ml` still
    cannot take a line reading `SYRINGE 10ML`.

    An **unqualified** number is the awkward one, because it could be either.
    In `GLUCO STRIP 50` it is a count of strips; in `AMOXYCILLIN 500` it is a
    strength somebody printed without its unit, and letting that land on
    `Amoxicillin 250mg` is exactly the error this file exists to prevent. What
    separates them is the rest of the line: `AZITHROMYCIN 500MG 1 TAB` has
    already said what its strength is, so the `1` cannot be another one. A bare
    number is therefore checked against every figure the candidate carries —
    but only on a line that states no strength of its own.
    """
    if printed.numbers and not printed.numbers <= other.numbers:
        return False
    if printed.pack and other.pack and not (printed.pack & other.pack):
        return False
    if printed.numbers or not printed.loose:
        return True
    known = other.numbers | other.pack | other.loose
    return not known or bool(printed.loose & known)


#: Shortest truncation treated as a deliberate abbreviation. Below this,
#: `CAL` would reach CALCIUM, CALAMINE and CALCITRIOL at once, and the
#: ambiguity rule would throw all three away — slower and no safer than not
#: trying.
MIN_PREFIX = 4


def _overlap(printed: frozenset[str], candidate: frozenset[str]) -> int:
    """How many printed words the candidate accounts for.

    Truncation counts — `AMOXY` for `AMOXYCILLIN`, `PANTOP` for
    `PANTOPRAZOLE` — because a distributor shortening a name from the right is
    the common case and the result is still unambiguously that word.

    Acronyms deliberately do not. `PCM` is not a prefix of `PARACETAMOL`, so
    this returns nothing for it and the line goes to a human, who resolves it
    once and teaches it to `product_suppliers.supplier_sku` forever. Inventing
    a rule that reached PCM would reach a dozen wrong things with it.
    """
    matched = 0
    for word in printed:
        for other in candidate:
            if word == other:
                matched += 1
                break
            short, long = (word, other) if len(word) < len(other) else (other, word)
            if len(short) >= MIN_PREFIX and long.startswith(short):
                matched += 1
                break
    return matched


# ------------------------------------------------------------- candidate pool


def _po_candidates(db: Session, purchase_order_id: int) -> list[tuple[Product, PurchaseOrderLine]]:
    rows = db.execute(
        select(Product, PurchaseOrderLine)
        .join(PurchaseOrderLine, PurchaseOrderLine.product_id == Product.id)
        .where(PurchaseOrderLine.purchase_order_id == purchase_order_id)
    ).all()
    return [(product, line) for product, line in rows]


def _supplier_candidates(db: Session, supplier_id: int) -> list[Product]:
    return list(db.scalars(
        select(Product)
        .join(ProductSupplier, ProductSupplier.product_id == Product.id)
        .where(ProductSupplier.supplier_id == supplier_id, Product.is_active)
    ).all())


def _all_candidates(db: Session) -> list[Product]:
    return list(db.scalars(select(Product).where(Product.is_active)).all())


def _aliases(db: Session, supplier_id: int | None) -> dict[str, int]:
    """The distributor's own codes for our products, as previously recorded."""
    if supplier_id is None:
        return {}
    rows = db.execute(
        select(ProductSupplier.supplier_sku, ProductSupplier.product_id)
        .where(ProductSupplier.supplier_id == supplier_id,
               ProductSupplier.supplier_sku.is_not(None))
    ).all()
    return {normalise(sku): pid for sku, pid in rows if sku}


# -------------------------------------------------------------------- resolve


def resolve_product(
    printed_name: str,
    candidates: Sequence[Product],
    aliases: Mapping[str, int],
) -> tuple[Product | None, str, list[Product]]:
    """One printed name against a candidate pool.

    Returns the product, the method that found it, and — when nothing was
    chosen — whatever plausible alternatives exist, so the interface can offer
    a shortlist instead of an empty box.
    """
    printed = normalise(printed_name)
    if not printed:
        return None, UNMATCHED, []

    by_id = {p.id: p for p in candidates}

    alias_id = aliases.get(printed)
    if alias_id is not None and alias_id in by_id:
        return by_id[alias_id], SUPPLIER_ALIAS, []

    for product in candidates:
        if normalise(product.sku) == printed:
            return product, SKU, []

    exact = [p for p in candidates if normalise(p.name) == printed]
    if len(exact) == 1:
        return exact[0], EXACT_NAME, []

    # Token overlap, with the strength treated as non-negotiable. A candidate
    # has to account for every number printed on the invoice line and share at
    # least one identifying word.
    printed_name_parts = tokens(printed_name)
    scored: list[tuple[int, Product]] = []
    for product in candidates:
        other = tokens(product.name)

        # Strength must be accounted for, pack size must merely not disagree.
        # See `numbers_agree`.
        if not numbers_agree(printed_name_parts, other):
            continue

        # A stated form that disagrees rules the candidate out. A form that is
        # absent on either side rules nothing out — plenty of invoices omit it.
        if (printed_name_parts.forms and other.forms
                and not (printed_name_parts.forms & other.forms)):
            continue

        # The form alone can never carry a match: every tablet shares `TAB`,
        # and letting that count matched `PCM-650 TAB` to `PARACETAMOL 650MG
        # TAB` on the strength of the word "tablet".
        shared = _overlap(printed_name_parts.words, other.words)
        if not shared:
            continue
        scored.append((shared, product))

    if not scored:
        return None, UNMATCHED, []

    best = max(score for score, _ in scored)
    winners = [p for score, p in scored if score == best]
    if len(winners) == 1:
        return winners[0], NAME_TOKENS, []

    # Ambiguity is an answer. Hand back the shortlist rather than a coin flip.
    return None, UNMATCHED, winners[:5]


# ----------------------------------------------------------------- public API


def match_lines(
    db: Session,
    lines: Sequence[Mapping],
    *,
    supplier_id: int | None = None,
    purchase_order_id: int | None = None,
) -> list[LineMatch]:
    """Resolve every extracted line, and check it against the order it claims.

    Read-only. Nothing here writes, and nothing here decides — every match is a
    proposal carried to a form where a human either accepts it or corrects it.
    """
    aliases = _aliases(db, supplier_id)
    po_lines: dict[int, PurchaseOrderLine] = {}

    if purchase_order_id is not None:
        pairs = _po_candidates(db, purchase_order_id)
        candidates = [product for product, _ in pairs]
        po_lines = {product.id: po_line for product, po_line in pairs}
    elif supplier_id is not None:
        candidates = _supplier_candidates(db, supplier_id)
    else:
        candidates = _all_candidates(db)

    # Falling back to the whole catalogue rather than reporting everything
    # unmatched: a purchase order can legitimately be short a line that still
    # arrived, and a supplier we have never bought from has no history at all.
    catalogue: list[Product] | None = None

    results: list[LineMatch] = []
    for index, line in enumerate(lines, start=1):
        printed = str(line.get("product_name") or "")
        product, method, shortlist = resolve_product(printed, candidates, aliases)

        if product is None and (purchase_order_id is not None or supplier_id is not None):
            if catalogue is None:
                catalogue = _all_candidates(db)
            product, method, wider = resolve_product(printed, catalogue, aliases)
            shortlist = shortlist or wider
            if product is not None and purchase_order_id is not None:
                method = f"{method}_off_order"

        match = LineMatch(line_no=index, extracted=line)
        if product is not None:
            match.product_id = product.id
            match.product_sku = product.sku
            match.product_name = product.name
            match.method = method
        else:
            match.candidates = [(p.id, f"{p.sku} — {p.name}") for p in shortlist]
            match.flags.append(Flag(
                "product_name", Severity.BLOCK,
                f"{printed!r} does not match a product in the catalogue"
                + (f" — {len(shortlist)} similar products found"
                   if shortlist else ""),
                index,
            ))

        if match.product_id is not None:
            match.flags.extend(_check_against_order(match, line, po_lines, index))
        results.append(match)

    return results


# ------------------------------------------------------- judgement about names


def compatible(printed_name: str, product: Product) -> bool:
    """Whether a proposed pairing survives the checks a match must survive.

    The same two rules `resolve_product` applies, pulled out so a suggestion
    from anywhere — including the model — is held to them. Neither is about
    spelling, which is precisely why they still hold when spelling is exactly
    what is in question:

      every strength printed on the line must appear in the product, and a
      pack size stated on both sides must agree, so `AMOXYCILLIN 500MG` can
      never land on `Amoxicillin 250mg` — see `numbers_agree`

      a dosage form stated on both sides must agree, so an injection cannot
      be received against a tablet
    """
    printed = tokens(printed_name)
    other = tokens(product.name)
    if not numbers_agree(printed, other):
        return False
    return not (printed.forms and other.forms and not (printed.forms & other.forms))


def suggest_unmatched(db: Session, matches: list[LineMatch]) -> int:
    """Have the model name the lines the rules could not, and vet its answers.

    Mutates `matches` in place and returns how many were placed.

    WHY THIS EXISTS AT ALL
    ----------------------
    The rules above compare tokens, and a token comparison cannot know that
    `AMOXYCILLIN` and `Amoxicillin` are one letter apart on purpose, that
    `60K IU` and `60000IU` are the same quantity, or that `OMEZ-20` is what a
    distributor calls omeprazole. No string rule reaches those without also
    reaching things it should not. Knowing them is language, not arithmetic.

    WHAT KEEPS IT HONEST
    --------------------
    The model chooses from a list of real products by id, so it cannot invent
    one. Every answer then goes through `compatible`, which is the same
    arithmetic an ordinary match obeys and which the model cannot argue with —
    a wrong strength or a contradicted dosage form is discarded silently. And
    what survives is a *suggestion*: the row is filled in and flagged for a
    person to confirm against the carton, never posted quietly.

    A failure here costs nothing. The line stays unmatched, which is where it
    was.
    """
    pending = [m for m in matches
               if m.product_id is None and str(m.extracted.get("product_name") or "").strip()]
    if not pending:
        return 0

    # The whole catalogue, even when the distributor is known. `match_lines`
    # has already tried this line against that distributor's products *and*
    # against everything else and found nothing, so narrowing the list here
    # would only hide answers the rules were allowed to consider: it is what
    # kept `ORS SACHET ORANGE` away from `ORS Powder 21g`, which we stock and
    # had simply never recorded buying from that distributor before.
    #
    # A product-supplier link is a record of past purchases, not a statement
    # about what a wholesaler is able to sell.
    products = _all_candidates(db)
    if not products:
        return 0
    by_id = {p.id: p for p in products}

    suggestions = service.suggest_products(
        [(m.line_no, str(m.extracted.get("product_name"))) for m in pending],
        [(p.id, p.name) for p in products],
    )

    placed = 0
    for match in pending:
        chosen = suggestions.get(match.line_no)
        if chosen is None:
            continue
        product = by_id.get(chosen[0])
        printed = str(match.extracted.get("product_name") or "")
        if product is None or not compatible(printed, product):
            continue

        match.product_id = product.id
        match.product_sku = product.sku
        match.product_name = product.name
        match.method = AI_NAME
        match.candidates = []
        # Replace the "no match" finding rather than adding to it — the line is
        # no longer unmatched, and leaving both would report the same row as
        # simultaneously resolved and not.
        match.flags = [f for f in match.flags if f.field != "product_name"]
        match.flags.append(Flag(
            "product_name", Severity.REVIEW,
            f"{printed!r} read as {product.name!r}"
            + ("" if chosen[1] else " — a guess, not a certainty")
            + "; check it against the carton before receiving",
            match.line_no,
        ))
        placed += 1

    return placed


def _check_against_order(
    match: LineMatch,
    line: Mapping,
    po_lines: Mapping[int, PurchaseOrderLine],
    line_no: int,
) -> list[Flag]:
    """Quantity and price, against what was actually ordered."""
    flags: list[Flag] = []
    po_line = po_lines.get(match.product_id or -1)

    if po_line is None:
        if po_lines:
            flags.append(Flag(
                "product_name", Severity.REVIEW,
                f"{match.product_name} is not on this purchase order",
                line_no,
            ))
        return flags

    match.po_line_id = po_line.id
    outstanding = Decimal(po_line.qty_ordered) - Decimal(po_line.qty_received)
    match.qty_outstanding = outstanding

    quantity = _decimal(line.get("quantity")) or Decimal("0")
    free = _decimal(line.get("free_quantity")) or Decimal("0")
    delivered = quantity + free

    if outstanding <= 0:
        flags.append(Flag(
            "quantity", Severity.REVIEW,
            f"the order line for {match.product_sku} is already fully received",
            line_no,
        ))
    elif delivered > outstanding * OVER_RECEIPT_TOLERANCE:
        flags.append(Flag(
            "quantity", Severity.BLOCK,
            f"{delivered:g} units against {outstanding:g} still outstanding "
            f"on the order",
            line_no,
        ))

    rate = _decimal(line.get("rate"))
    agreed = Decimal(po_line.unit_price)
    if rate is not None and agreed > 0:
        drift = abs(rate - agreed) / agreed
        if drift > PRICE_VARIANCE:
            flags.append(Flag(
                "rate", Severity.REVIEW,
                f"invoiced at {rate:.2f}, ordered at {agreed:.2f} "
                f"({drift * 100:.0f}% difference)",
                line_no,
            ))
    return flags


# --------------------------------------------------------------- lot history


def learn_supplier_batch_shapes(
    db: Session, supplier_id: int | None, *, minimum: int = 8
) -> set[str] | None:
    """The batch formats already received, for `validate.validate_invoice`.

    Returns None — meaning "do not run the batch check" — when there is too
    little history to describe a supplier's habits. A vocabulary built from two
    deliveries rejects the third for being different, which is worse than not
    checking at all.
    """
    if supplier_id is None:
        return None
    codes = db.scalars(
        select(Lot.lot_code)
        .join(ProductSupplier, ProductSupplier.product_id == Lot.product_id)
        .where(ProductSupplier.supplier_id == supplier_id)
    ).all()
    if len(codes) < minimum:
        return None
    return {batch_shape(code) for code in codes if code}


def remember_alias(
    db: Session,
    *,
    supplier_id: int,
    product_id: int,
    printed_name: str,
    unit_cost: Decimal | None = None,
) -> bool:
    """Record what this distributor calls one of our products.

    Called after a human resolves a line by hand, which is what turns the
    abbreviation problem from unsolvable into a one-off.

    Creates the product-supplier link when there is not one yet, because that
    is the ordinary case rather than the exception: the moment you learn what a
    distributor calls something is the first time they send it to you, and
    requiring the link to exist first meant the answer could only be recorded
    for pairs somebody had already set up by hand. Refusing there taught
    nothing and left the line unmatched on every future delivery.

    `unit_cost` should be the rate printed on the line that prompted this. It
    is what the distributor actually charged, so it is a real number rather
    than a guess; without it the link falls back to the product's MRP, which
    is a ceiling and not a cost, and is deliberately never marked preferred.

    Returns False only when the name is empty or a code is already recorded —
    this never overwrites one somebody set deliberately.
    """
    printed = normalise(printed_name)
    if not printed:
        return False
    link = db.scalar(
        select(ProductSupplier).where(
            ProductSupplier.supplier_id == supplier_id,
            ProductSupplier.product_id == product_id,
        )
    )
    if link is None:
        product = db.get(Product, product_id)
        if product is None:
            return False
        link = ProductSupplier(
            product_id=product_id,
            supplier_id=supplier_id,
            unit_cost=unit_cost if unit_cost is not None
            else (product.mrp or Decimal("0")),
            is_preferred=False,
        )
        db.add(link)
    elif link.supplier_sku:
        return False
    link.supplier_sku = printed[:64]
    db.flush()
    return True


def open_orders_for(db: Session, supplier_id: int) -> list[PurchaseOrder]:
    """Orders a delivery from this supplier could plausibly be against."""
    from app.models.enums import DocumentStatus  # noqa: PLC0415 - avoids a cycle

    return list(db.scalars(
        select(PurchaseOrder)
        .where(
            PurchaseOrder.supplier_id == supplier_id,
            PurchaseOrder.status.in_([
                DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_RECEIVED,
            ]),
        )
        .order_by(PurchaseOrder.id.desc())
    ).all())


def summarise(matches: Iterable[LineMatch]) -> dict:
    """Counts for the interface: how much of this is settled?"""
    items = list(matches)
    resolved = [m for m in items if m.resolved]
    return {
        "lines": len(items),
        "resolved": len(resolved),
        "unmatched": len(items) - len(resolved),
        "blocking": sum(
            1 for m in items
            if any(f.severity is Severity.BLOCK for f in m.flags)
        ),
        "by_method": {
            method: sum(1 for m in resolved if m.method == method)
            for method in {m.method for m in resolved}
        },
    }
