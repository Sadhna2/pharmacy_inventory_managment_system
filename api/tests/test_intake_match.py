"""Resolving invoice lines to catalogue products (app/ai/intake/match.py).

The functions that decide a match take a candidate list rather than a session,
so the interesting behaviour is testable without a database — which is the
point. These are the rules that decide whether stock lands against the right
product, and they should be provable in milliseconds, not only inside an
end-to-end run.

The rule under test throughout: **when more than one product fits, none is
chosen**. A wrong match is silent and unrecoverable; an unmatched line is
visible and costs somebody ten seconds.
"""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.ai.intake import match as match_module
from app.ai.intake.match import (
    AI_NAME,
    EXACT_NAME,
    NAME_TOKENS,
    SKU,
    SUPPLIER_ALIAS,
    UNMATCHED,
    LineMatch,
    normalise,
    resolve_product,
    tokens,
)
from app.ai.intake.validate import Flag, Severity


@dataclass
class FakeProduct:
    """Enough of a Product for the matcher — it only reads three fields."""

    id: int
    sku: str
    name: str


CATALOGUE = [
    FakeProduct(1, "PARA-650", "PARACETAMOL 650MG TAB"),
    FakeProduct(2, "AMOX-500", "AMOXYCILLIN 500MG CAP"),
    FakeProduct(3, "AMLO-5", "AMLODIPINE 5MG TAB"),
    FakeProduct(4, "AMLO-10", "AMLODIPINE 10MG TAB"),
    FakeProduct(5, "PANTO-40", "PANTOPRAZOLE 40MG TAB"),
    FakeProduct(6, "CALCI-D3", "CALCIUM + VIT D3 TAB"),
    FakeProduct(7, "COUGH-100", "COUGH SYRUP 100ML"),
]


def resolve(printed, catalogue=None, aliases=None):
    return resolve_product(printed, catalogue or CATALOGUE, aliases or {})


# ------------------------------------------------------------- normalisation


@pytest.mark.parametrize("raw,expected", [
    ("AMOXY-500  CAP", "AMOXY 500 CAP"),
    ("paracetamol 650mg", "PARACETAMOL 650MG"),
    ("CALCIUM + VIT D3", "CALCIUM VIT D3"),
    ("  ", ""),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_a_name_splits_into_words_form_and_strength():
    parsed = tokens("PARACETAMOL 650MG TAB")
    assert parsed.words == {"PARACETAMOL"}   # what it is
    assert parsed.forms == {"TAB"}           # narrows, never identifies
    assert parsed.numbers == {"650"}         # never approximated


def test_dosage_form_spellings_are_folded():
    assert tokens("AMOXYCILLIN CAPSULES").forms == tokens("AMOXY CAPS").forms


def test_a_sachet_is_packaging_not_a_form_of_its_own():
    """`ORS SACHET ORANGE` and `ORS Powder 21g` are the same thing.

    Held apart they read as two dosage forms that disagree, which rules the
    candidate out before anything else gets a say.
    """
    catalogue = [FakeProduct(30, "ORS-21", "ORS POWDER 21G")]
    product, method, _ = resolve("ORS SACHET ORANGE", catalogue=catalogue)
    assert (product.id, method) == (30, NAME_TOKENS)


def test_the_form_alone_cannot_carry_a_match():
    """Every tablet shares `TAB`; letting that count matched PCM to paracetamol."""
    assert tokens("PCM-650 TAB").words & tokens("PARACETAMOL 650MG TAB").words == set()


# ------------------------------------------------------- strength versus pack


def test_a_millilitre_figure_is_a_pack_size_not_a_strength():
    """`10ML` describes the vial. `40IU` describes the medicine in it."""
    parsed = tokens("ACTRAPID 10ML")
    assert parsed.numbers == set()
    assert parsed.pack == {"10"}
    assert tokens("HUMAN INSULIN 40IU").numbers == {"40"}


def test_a_bare_number_is_held_to_the_weaker_rule():
    """`GLUCO STRIP 50` is fifty strips, not a dose of fifty of anything."""
    assert tokens("GLUCO STRIP 50").loose == {"50"}
    assert tokens("GLUCO STRIP 50").numbers == set()


@pytest.mark.parametrize("printed,product,fits", [
    # The bug this rule was written for: a volume the catalogue never states.
    ("ACTRAPID 10ML", "HUMAN INSULIN 40IU", True),
    ("GLARGINE 3ML", "INSULIN GLARGINE 100IU", True),
    ("GLUCO STRIP 50", "GLUCOMETER STRIPS", True),
    # The trailing `1` in "1 TAB" is a pack count and must not veto the 500mg.
    ("AZITHROMYCIN 500MG 1 TAB", "AZITHROMYCIN 500MG", True),
    # ...and none of that is allowed to loosen the strength check.
    ("AMOXYCILLIN 500MG", "AMOXICILLIN 250MG", False),
    ("ATORVASTATIN 20MG", "ATORVASTATIN 10MG", False),
    # A pack size stated on *both* sides still has to agree — receiving a 10ml
    # syringe against a 5ml one is the wrong item, just not a dosing error.
    ("SYRINGE 10ML", "DISPOSABLE SYRINGE 5ML", False),
    ("COUGH SYRUP 200ML", "COUGH SYRUP 100ML", False),
    # On a line that states no strength, a bare number might be one — so it is
    # checked against every figure the candidate carries.
    ("AMOXYCILLIN 500", "AMOXICILLIN 250MG", False),
    ("AMOXYCILLIN 500", "AMOXICILLIN 500MG", True),
    # ...but a candidate that carries no figures at all cannot contradict it.
    ("SURGICAL GLOVES 100", "SURGICAL GLOVES MEDIUM", True),
])
def test_numbers_agree(printed, product, fits):
    assert match_module.numbers_agree(
        tokens(printed), tokens(product)
    ) is fits


def test_a_volume_does_not_let_a_different_medicine_through():
    """Relaxing the number check must not relax the *word* check."""
    catalogue = [FakeProduct(20, "INS-HUM", "HUMAN INSULIN 40IU")]
    product, method, _ = resolve("PARACETAMOL 10ML", catalogue=catalogue)
    assert (product, method) == (None, UNMATCHED)


def test_a_disagreeing_form_rules_a_candidate_out():
    """Same drug, same strength, different product."""
    catalogue = [
        FakeProduct(10, "PANTO-40", "PANTOPRAZOLE 40MG TAB"),
        FakeProduct(11, "PANTO-INJ", "PANTOPRAZOLE INJ 40MG"),
    ]
    product, method, _ = resolve("PANTOP-40 TAB", catalogue=catalogue)
    assert (product.id, method) == (10, NAME_TOKENS)
    product, _, _ = resolve("PANTOP INJ 40MG", catalogue=catalogue)
    assert product.id == 11


# ----------------------------------------------------------------- the ladder


def test_a_supplier_alias_wins_outright():
    """What this distributor calls it, recorded the first time a human said so."""
    product, method, _ = resolve("PCM-650 TAB", aliases={"PCM 650 TAB": 1})
    assert (product.id, method) == (1, SUPPLIER_ALIAS)


def test_our_own_sku_matches():
    product, method, _ = resolve("AMOX-500")
    assert (product.id, method) == (2, SKU)


def test_an_exact_name_matches():
    product, method, _ = resolve("Amoxycillin 500mg Cap")
    assert (product.id, method) == (2, EXACT_NAME)


def test_a_truncated_name_matches():
    """`AMOXY-500 CAP` is `AMOXYCILLIN 500MG CAP` shortened from the right."""
    product, method, _ = resolve("AMOXY-500 CAP")
    assert (product.id, method) == (2, NAME_TOKENS)


def test_another_truncation():
    product, method, _ = resolve("PANTOP-40 TAB")
    assert (product.id, method) == (5, NAME_TOKENS)


# ------------------------------------------------------------ refusing to guess


def test_an_acronym_is_not_guessed_at():
    """`PCM` is not a prefix of `PARACETAMOL`, and no string rule makes it one.

    Reaching it would mean a rule loose enough to reach a dozen wrong things.
    It goes to a human once, then lives in `supplier_sku` forever.
    """
    product, method, _ = resolve("PCM-650 TAB")
    assert product is None
    assert method == UNMATCHED


def test_the_strength_is_never_approximated():
    """AMLODIPINE 5MG and 10MG differ by one character and are different doses."""
    product, _, _ = resolve("AMLODIPINE 5MG TAB")
    assert product.id == 3
    product, _, _ = resolve("AMLODIPINE 10MG TAB")
    assert product.id == 4


def test_a_missing_strength_leaves_it_ambiguous():
    """`AMLODIPINE TAB` fits both doses, so neither is chosen."""
    product, method, shortlist = resolve("AMLODIPINE TAB")
    assert product is None
    assert method == UNMATCHED
    assert {p.id for p in shortlist} == {3, 4}


def test_an_ambiguous_line_hands_back_a_shortlist():
    """An empty box is worse than a shortlist; a wrong guess is worse than both."""
    _, _, shortlist = resolve("AMLODIPINE TAB")
    assert len(shortlist) == 2


def test_an_unknown_product_matches_nothing():
    product, method, shortlist = resolve("INSULIN GLARGINE 100IU")
    assert product is None
    assert method == UNMATCHED
    assert shortlist == []


def test_an_empty_name_matches_nothing():
    product, method, _ = resolve("   ")
    assert product is None and method == UNMATCHED


def test_a_short_fragment_does_not_reach_across_products():
    """`CAL` is below the abbreviation floor, so it stays unresolved."""
    product, _, _ = resolve("CAL TAB")
    assert product is None


# --------------------------------------------------------------- pool effects


def test_a_narrow_pool_resolves_what_a_wide_one_cannot():
    """The purchase order is why this module can afford to be strict.

    `AMLODIPINE TAB` is hopeless against the full catalogue and obvious against
    an order that contains exactly one amlodipine.
    """
    on_order = [FakeProduct(3, "AMLO-5", "AMLODIPINE 5MG TAB")]
    product, method, _ = resolve("AMLODIPINE TAB", catalogue=on_order)
    assert (product.id, method) == (3, NAME_TOKENS)


def test_an_alias_outside_the_pool_is_not_used():
    """A remembered code for a product that is not a candidate proves nothing."""
    product, method, _ = resolve(
        "PCM-650 TAB",
        catalogue=[FakeProduct(2, "AMOX-500", "AMOXYCILLIN 500MG CAP")],
        aliases={"PCM 650 TAB": 1},
    )
    assert product is None and method == UNMATCHED


# ------------------------------------------------- judgement about names


class FakeSession:
    """`suggest_unmatched` reads the catalogue and nothing else."""

    def __init__(self, products):
        self.products = products

    def scalars(self, _statement):
        return self

    def all(self):
        return self.products


def _pending(line_no: int, printed: str) -> LineMatch:
    """A line the rules gave up on, as `match_lines` leaves it."""
    match = LineMatch(line_no=line_no, extracted={"product_name": printed})
    match.flags.append(Flag(
        "product_name", Severity.BLOCK,
        f"{printed!r} does not match a product in the catalogue", line_no,
    ))
    return match


def _suggest(monkeypatch, matches, answers):
    """Run the pass with the model's reply stubbed. No key, no network."""
    monkeypatch.setattr(
        match_module.service, "suggest_products",
        lambda lines, catalogue: answers,
    )
    monkeypatch.setattr(
        match_module, "_all_candidates", lambda db: CATALOGUE,
    )
    return match_module.suggest_unmatched(FakeSession(CATALOGUE), matches)


def test_a_spelling_variant_is_named(monkeypatch):
    """The case the rules cannot reach: one letter, and it is a different word.

    `AMOXICILLIN` and `AMOXYCILLIN` share no token and neither is a prefix of
    the other, so token overlap finds nothing and always will.
    """
    matches = [_pending(1, "AMOXICILLIN 500MG CAP")]
    assert _suggest(monkeypatch, matches, {1: (2, True)}) == 1
    assert matches[0].product_id == 2
    assert matches[0].method == AI_NAME


def test_a_named_line_stops_reporting_itself_as_unmatched(monkeypatch):
    """A row cannot be both resolved and not; the old finding is replaced."""
    matches = [_pending(1, "AMOXICILLIN 500MG CAP")]
    _suggest(monkeypatch, matches, {1: (2, True)})
    product_flags = [f for f in matches[0].flags if f.field == "product_name"]
    assert len(product_flags) == 1
    assert product_flags[0].severity is Severity.REVIEW
    assert "check it against the carton" in product_flags[0].message


def test_a_guess_says_so(monkeypatch):
    matches = [_pending(1, "AMOXICILLIN 500MG CAP")]
    _suggest(monkeypatch, matches, {1: (2, False)})
    assert "a guess, not a certainty" in matches[0].flags[-1].message


def test_the_model_cannot_overrule_the_strength(monkeypatch):
    """The one thing it is never allowed to do.

    `AMLODIPINE 5MG` onto `AMLODIPINE 10MG TAB` is the mistake that puts the
    wrong dose on a shelf, so arithmetic vetoes it however sure the model is.
    """
    matches = [_pending(1, "AMLODIPINE 5MG TAB")]
    assert _suggest(monkeypatch, matches, {1: (4, True)}) == 0
    assert matches[0].product_id is None
    assert matches[0].flags[0].severity is Severity.BLOCK


def test_the_model_cannot_overrule_the_dosage_form(monkeypatch):
    """An injection is not a tablet, whatever the name says."""
    matches = [_pending(1, "PANTOPRAZOLE 40MG INJ")]
    assert _suggest(monkeypatch, matches, {1: (5, True)}) == 0
    assert matches[0].product_id is None


def test_a_product_that_is_not_on_the_list_is_ignored(monkeypatch):
    """It answers with an id from a list we supplied. Anything else is noise."""
    matches = [_pending(1, "SOMETHING ELSE 10MG")]
    assert _suggest(monkeypatch, matches, {1: (999, True)}) == 0
    assert matches[0].product_id is None


def test_saying_nothing_leaves_the_line_where_it_was(monkeypatch):
    matches = [_pending(1, "TELMISARTAN 40MG TAB")]
    assert _suggest(monkeypatch, matches, {}) == 0
    assert matches[0].product_id is None
    assert matches[0].flags[0].severity is Severity.BLOCK


def test_already_matched_lines_are_never_second_guessed(monkeypatch):
    """The rules had an answer. Nothing here is allowed to replace it."""
    settled = LineMatch(line_no=1, extracted={"product_name": "PARACETAMOL 650MG TAB"},
                        product_id=1, product_sku="PARA-650",
                        product_name="PARACETAMOL 650MG TAB", method=EXACT_NAME)
    assert _suggest(monkeypatch, [settled], {1: (3, True)}) == 0
    assert settled.product_id == 1
    assert settled.method == EXACT_NAME


@pytest.mark.parametrize("printed,product_id", [
    ("AMOXICILLIN 500MG CAP", 2),      # spelling
    ("PARACETAMOL 650 MG TABLET", 1),  # spacing and a spelled-out form
    ("PANTOP-40", 5),                  # a trade name no rule reaches
])
def test_names_the_rules_cannot_reach_survive_the_checks(monkeypatch, printed, product_id):
    matches = [_pending(1, printed)]
    assert _suggest(monkeypatch, matches, {1: (product_id, True)}) == 1
    assert matches[0].product_id == product_id


# --------------------------------------------------- remembering what they call it


@dataclass
class FakeLink:
    """Enough of a ProductSupplier for `remember_alias`."""

    product_id: int
    supplier_id: int
    supplier_sku: str | None = None
    unit_cost: Decimal | None = None
    is_preferred: bool = False


class AliasSession:
    """A session that answers the three questions `remember_alias` asks."""

    def __init__(self, links=(), products=()):
        self.links = list(links)
        self.products = {p.id: p for p in products}
        self.added: list = []

    def scalar(self, _statement):
        # The only `scalar` call is the existing-link lookup, and the statement
        # is filtered by the pair, so the fake matches on the pair it was told.
        return self.links[0] if self.links else None

    def get(self, _model, key):
        return self.products.get(key)

    def add(self, obj):
        self.added.append(obj)
        self.links.append(obj)

    def flush(self):
        pass


@dataclass
class FakePricedProduct:
    id: int
    sku: str
    name: str
    mrp: Decimal | None = None


def test_a_first_delivery_from_a_distributor_can_still_be_taught():
    """The case that matters, and the one that used to be refused.

    You learn what a wholesaler calls something the first time they send it,
    which is exactly when no product-supplier link exists yet. Requiring one
    meant the answer could only be recorded for pairs somebody had already set
    up by hand, so the line came back unmatched on every future delivery.
    """
    db = AliasSession(products=[FakePricedProduct(7, "INS-HUM", "HUMAN INSULIN 40IU")])
    assert match_module.remember_alias(
        db, supplier_id=3, product_id=7,
        printed_name="ACTRAPID 10ML", unit_cost=Decimal("146.05"),
    )
    assert len(db.added) == 1
    link = db.added[0]
    assert link.supplier_sku == "ACTRAPID 10ML"
    assert link.unit_cost == Decimal("146.05")
    # A link invented while receiving is a record of one delivery, not a
    # decision about where to buy from next time.
    assert link.is_preferred is False


def test_a_new_link_falls_back_to_the_mrp_when_no_rate_is_given():
    db = AliasSession(products=[FakePricedProduct(7, "OMEZ-20", "OMEPRAZOLE 20MG",
                                                  mrp=Decimal("72.00"))])
    assert match_module.remember_alias(
        db, supplier_id=3, product_id=7, printed_name="OMEZ-20 CAP",
    )
    assert db.added[0].unit_cost == Decimal("72.00")


def test_an_existing_link_without_a_code_is_filled_in_rather_than_duplicated():
    link = FakeLink(product_id=7, supplier_id=3)
    db = AliasSession(links=[link])
    assert match_module.remember_alias(
        db, supplier_id=3, product_id=7, printed_name="OMEZ-20 CAP",
    )
    assert link.supplier_sku == "OMEZ 20 CAP"
    assert db.added == []


def test_a_code_somebody_set_deliberately_is_never_overwritten():
    link = FakeLink(product_id=7, supplier_id=3, supplier_sku="THEIR-OWN-CODE")
    db = AliasSession(links=[link])
    assert not match_module.remember_alias(
        db, supplier_id=3, product_id=7, printed_name="OMEZ-20 CAP",
    )
    assert link.supplier_sku == "THEIR-OWN-CODE"


def test_an_empty_name_teaches_nothing():
    db = AliasSession(products=[FakePricedProduct(7, "OMEZ-20", "OMEPRAZOLE 20MG")])
    assert not match_module.remember_alias(
        db, supplier_id=3, product_id=7, printed_name="   ",
    )
    assert db.added == []
