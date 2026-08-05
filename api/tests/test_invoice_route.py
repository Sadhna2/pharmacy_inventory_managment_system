"""The invoice endpoint's contract (`GET /sales-orders/{so_id}/invoice`).

The renderer itself is pinned down in `test_invoice_html.py`; what is at stake
here is everything the *route* decides on the way in. Four things would break
silently without these.

The seller. The firm's registered *name* is configuration; the registration
itself belongs to the branch, because GST registers per state and a branch in
another state is a separately registered person. The branch also supplies the
address the goods left from. With no registration to be found on either the
branch or the firm the route refuses outright, because a document headed TAX
INVOICE without the supplier's GSTIN still renders, still looks right, and is
not a tax invoice anyone can claim credit against. A placeholder or an em dash
there would be worse than no document at all.

The status. An invoice is raised against a supply that has happened. A draft or
cancelled order has no supply behind it, so printing one would put an invoice
number against a sale that may never occur.

The HSN. The line carries the code that was in force when the order was priced.
Reaching through to `product.hsn_code` instead would reprint last year's
invoice under this year's classification, so the copy in our file and the copy
the customer holds would quietly disagree — the precise failure the frozen
column on TaxLineMixin exists to prevent.

The permission. A tax invoice names a customer, their GSTIN and what they buy.
Losing the `so.view` gate would put all of that behind a URL anyone signed in
could guess.

And the response itself: HTML with an HTML content type, because the whole
delivery mechanism is the browser's own print dialogue. A route that answered
JSON would leave the Print invoice button opening a tab full of escaped markup.

Runs with no database and no network — the session is a stub, the way
`test_intake_router.py` does it. The end-to-end path over a real server is
`test_e2e.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.permissions import ADMIN, STAFF
from app.db.session import get_db
from app.main import app
from app.models.enums import DocumentStatus

# The firm-wide fallback registration, and the Gujarat branch's own. Both
# carry a correct mod-36 check digit and open with their own state's numeric
# code — 27 Maharashtra, 24 Gujarat — so a test that reads the state against
# the number has something true to read. The middle ten characters are the
# firm's PAN and are shared, which is what makes two registrations one company.
SELLER_NAME = "Sadhna Pharma Distributors Pvt Ltd"
SELLER_GSTIN = "27AABCS9876P1ZA"
GUJARAT_GSTIN = "24AABCS9876P1ZG"
SELLER_ADDRESS = "Unit 4, MIDC Phase II\nBhiwandi 421302"

# --- Stubs ------------------------------------------------------------------


@dataclass
class _Party:
    name: str
    address: str | None
    gstin: str | None
    state_code: str


@dataclass
class _Warehouse:
    """A branch: where the goods left from, and under whose registration.

    `gstin` is last and defaults to None so the older positional stubs still
    build, and because None is the meaningful case as well as the convenient
    one — it is what every row held before the column existed, and what a
    single-state chain still holds.
    """

    name: str
    address: str | None
    state_code: str
    gstin: str | None = None


@dataclass
class _Product:
    name: str
    sku: str
    pack_size: str | None = None
    #: Deliberately not the line's. Present so a test can prove the printed
    #: invoice never reaches through the line to the catalogue.
    hsn_code: str | None = None


@dataclass
class _Line:
    product: _Product
    hsn_code: str | None
    qty_ordered: Decimal
    unit_price: Decimal
    taxable_value: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal


@dataclass
class _Order:
    so_number: str
    order_date: date
    customer: _Party
    warehouse: _Warehouse
    warehouse_id: int
    status: DocumentStatus
    lines: list[_Line]
    is_interstate: bool
    place_of_supply: str | None
    subtotal: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal


@dataclass
class _Role:
    code: str = ADMIN


@dataclass
class _User:
    """What the gates read: permissions, and the role/branch pair for scoping."""

    permission_codes: tuple[str, ...] = ("so.view",)
    role: _Role = field(default_factory=_Role)
    warehouse_id: int | None = None


class _Session:
    """Just enough Session for this endpoint: the one order lookup.

    The statement is still built for real, eager-load options and all, so a
    relationship renamed out from under the route fails here rather than at
    the first request in production.
    """

    def __init__(self, order: _Order | None) -> None:
        self.order = order

    def scalar(self, _statement: object) -> _Order | None:
        return self.order


WAREHOUSE = _Warehouse(
    name="Bhiwandi Central Warehouse",
    address="Survey 118, Kalyan Road\nBhiwandi 421302",
    state_code="MH",
)

CUSTOMER = _Party(
    name="Sancheti Hospital",
    address="16 Shivajinagar\nPune 411005",
    gstin="27AAACS1234A1Z5",
    state_code="MH",
)


def _order(**overrides: object) -> _Order:
    """One shipped intra-state order: 10 strips at 88.06, 12% GST, CGST+SGST."""
    order = _Order(
        so_number="SO-000042",
        order_date=date(2026, 8, 3),
        customer=CUSTOMER,
        warehouse=WAREHOUSE,
        warehouse_id=1,
        status=DocumentStatus.SHIPPED,
        lines=[
            _Line(
                product=_Product(
                    name="Paracetamol 650mg Tablet",
                    sku="PAR-650",
                    pack_size="10x10",
                    hsn_code="99999999",
                ),
                hsn_code="30049099",
                qty_ordered=Decimal("10"),
                unit_price=Decimal("88.06"),
                taxable_value=Decimal("880.60"),
                gst_rate=Decimal("12.00"),
                cgst_amount=Decimal("52.84"),
                sgst_amount=Decimal("52.83"),
                igst_amount=Decimal("0.00"),
                line_total=Decimal("986.27"),
            )
        ],
        is_interstate=False,
        place_of_supply="MH",
        subtotal=Decimal("880.60"),
        tax_total=Decimal("105.67"),
        round_off=Decimal("-0.27"),
        grand_total=Decimal("986.00"),
    )
    for name, value in overrides.items():
        setattr(order, name, value)
    return order


@pytest.fixture
def registered(monkeypatch):
    """A configured seller — the ordinary case, in which invoices can issue.

    Set per-test rather than in a `.env`, so a developer's own configuration
    can neither satisfy nor break these, and so one test can drop it to prove
    the refusal.
    """
    monkeypatch.setattr(settings, "seller_legal_name", SELLER_NAME)
    monkeypatch.setattr(settings, "seller_gstin", SELLER_GSTIN)
    monkeypatch.setattr(settings, "seller_address", SELLER_ADDRESS)


@pytest.fixture
def fetch(registered):
    """Ask for one order's invoice as a signed-in user, with no database.

    `get_current_user` is what gets replaced rather than the permission gate
    itself: `require_permission("so.view")` builds a fresh closure on every
    call, so it can never be looked up as an override key — and overriding the
    user leaves the real gate running, which is the half worth testing. The
    default user is an admin, so warehouse scoping stays out of the way of the
    tests that are not about it.

    The client is not entered as a context manager on purpose. That would run
    the app's lifespan, and its forecast warm-up reaches for the database this
    file exists to do without.
    """
    client = TestClient(app)

    def _fetch(
        order: _Order | None,
        *,
        permissions: tuple[str, ...] = ("so.view",),
        user: _User | None = None,
    ):
        signed_in = user or _User(permissions)
        app.dependency_overrides[get_db] = lambda: _Session(order)
        app.dependency_overrides[get_current_user] = lambda: signed_in
        return client.get("/api/v1/sales-orders/42/invoice")

    yield _fetch
    app.dependency_overrides.clear()


# --- The document ------------------------------------------------------------


def test_the_endpoint_returns_a_printable_html_document(fetch):
    response = fetch(_order())

    assert response.status_code == 200
    # Not `== "text/html"`: FastAPI appends the charset.
    assert response.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in response.text
    assert "TAX INVOICE" in response.text
    assert "SO-000042" in response.text


def test_the_line_table_carries_the_products_own_details(fetch):
    """The route eager-loads `lines.product`; the renderer prints from it."""
    body = fetch(_order()).text

    assert "Paracetamol 650mg Tablet" in body
    assert "PAR-650" in body
    assert "10x10" in body


def test_an_intra_state_order_prints_the_cgst_and_sgst_split(fetch):
    body = fetch(_order()).text

    assert "CGST" in body and "SGST" in body
    assert "IGST" not in body


def test_an_inter_state_order_prints_igst_instead(fetch):
    """The flag on the document decides the columns, not the amounts."""
    body = fetch(_order(is_interstate=True, place_of_supply="KA")).text

    assert "IGST" in body
    assert "CGST" not in body and "SGST" not in body


# --- The seller --------------------------------------------------------------


def test_the_seller_is_named_as_the_firm_whichever_branch_supplied(fetch):
    """One company, whichever of its registrations made the supply.

    The legal name is the firm's throughout — a branch is not a separate
    company, it is the same PAN registered in a second state.
    """
    body = fetch(_order()).text

    assert SELLER_NAME in body
    # The parties table prints seller first, buyer second.
    assert body.index(SELLER_NAME) < body.index("Sancheti Hospital")


def test_the_address_printed_is_the_branch_the_goods_left_from(fetch):
    """Rule 46 wants the place of business the supply was made from."""
    body = fetch(_order()).text

    assert "Survey 118, Kalyan Road" in body


def test_the_firms_own_address_stands_in_for_a_branch_with_none_recorded(fetch):
    body = fetch(_order(warehouse=_Warehouse(WAREHOUSE.name, None, "MH"))).text

    assert "Unit 4, MIDC Phase II" in body


def test_no_invoice_is_issued_at_all_without_a_configured_gstin(fetch, monkeypatch):
    """Rule 46(b) makes the supplier's GSTIN mandatory, so there is no partial
    version of this document to fall back on.

    Printing one with an em dash where the registration belongs produces
    something that looks like a tax invoice, is captioned as one, and cannot be
    used as one — the buyer's input credit is refused on it. Refusing to
    generate it puts the failure where someone can fix it.
    """
    monkeypatch.setattr(settings, "seller_gstin", "")

    response = fetch(_order())

    assert response.status_code == 409
    detail = response.json()["detail"]
    # Both places it could have come from, named: the branch by its own name,
    # because "configure your GSTIN" is useless to someone with five branches.
    assert "Bhiwandi Central Warehouse" in detail
    assert "SELLER_GSTIN" in detail
    # And nothing resembling the document leaked into the refusal.
    assert "TAX INVOICE" not in response.text


def test_a_configured_gstin_with_no_legal_name_is_equally_refused(fetch, monkeypatch):
    """46(b) asks for the name and address alongside the number."""
    monkeypatch.setattr(settings, "seller_legal_name", "")

    assert fetch(_order()).status_code == 409


def test_the_branch_supplies_the_registration_the_goods_went_out_under(fetch):
    """The point of the whole column.

    GST registers per state, so a branch in Gujarat is a separately registered
    person with its own GSTIN — and the first two characters of a GSTIN *are*
    the state's numeric code. Before this, the route printed the state from the
    warehouse and the number from configuration, so a Gujarat order carried
    "State: GJ (24)" beside a GSTIN opening "27". The document disagreed with
    itself on its own face, and no buyer could have claimed credit against it.
    """
    gujarat = _Warehouse(
        name="Ahmedabad Branch",
        address="Plot 22, Naroda GIDC\nAhmedabad 382330",
        state_code="GJ",
        gstin=GUJARAT_GSTIN,
    )

    body = fetch(_order(warehouse=gujarat, is_interstate=True, place_of_supply="MH")).text

    assert GUJARAT_GSTIN in body
    assert SELLER_GSTIN not in body
    # The number and the state beside it now say the same thing.
    assert "GJ" in body and "24" in body


def test_a_branch_with_its_own_registration_overrides_the_firms(fetch):
    """Same state, still the branch's own — the fallback is a fallback, not a
    tie-break. Otherwise a chain that had recorded its registrations properly
    would still print the configured one wherever the two happened to differ,
    and nobody would notice until they differed for a reason."""
    own = "27AAPFU0939F1ZV"
    branch = _Warehouse(WAREHOUSE.name, WAREHOUSE.address, "MH", own)

    body = fetch(_order(warehouse=branch)).text

    assert own in body
    assert SELLER_GSTIN not in body


def test_the_firms_registration_stands_in_for_a_branch_with_none_recorded(fetch):
    """Every row held null the day the column was added, and a chain that has
    only ever traded in one state has no reason to fill it in. Those invoices
    must keep printing exactly what they printed before."""
    body = fetch(_order()).text  # WAREHOUSE carries no GSTIN

    assert SELLER_GSTIN in body


def test_a_branch_registration_alone_is_enough_to_issue(fetch, monkeypatch):
    """The firm-wide setting is not a precondition once branches carry their
    own — a chain that records registrations per branch should never have to
    also set a chain-wide one."""
    monkeypatch.setattr(settings, "seller_gstin", "")
    branch = _Warehouse(WAREHOUSE.name, WAREHOUSE.address, "MH", "27AAPFU0939F1ZV")

    response = fetch(_order(warehouse=branch))

    assert response.status_code == 200
    assert "27AAPFU0939F1ZV" in response.text


def test_the_buyers_gstin_prints_as_recorded(fetch):
    assert "27AAACS1234A1Z5" in fetch(_order()).text


def test_the_invoice_is_signed_off_in_the_registered_firms_name(fetch):
    assert f"For {SELLER_NAME}" in fetch(_order()).text


# --- Frozen classification ---------------------------------------------------


def test_the_hsn_printed_is_the_one_frozen_on_the_line(fetch):
    """A corrected catalogue must not rewrite an invoice already issued."""
    body = fetch(_order()).text

    assert "30049099" in body
    assert "99999999" not in body


def test_the_place_of_supply_prints_the_state_code_with_its_statutory_number(fetch):
    """Nothing here stores "Maharashtra", so the code itself is the name."""
    assert "MH (27)" in fetch(_order()).text


def test_an_order_with_no_place_of_supply_still_renders(fetch):
    """Older rows predate the column. A blank cell beats a 500."""
    response = fetch(_order(place_of_supply=None))

    assert response.status_code == 200
    assert "TAX INVOICE" in response.text


# --- Refusals ----------------------------------------------------------------


def test_an_order_that_does_not_exist_is_a_404(fetch):
    response = fetch(None)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_the_invoice_is_refused_without_permission_to_view_sales_orders(fetch):
    """A tax invoice names the customer, their GSTIN and what they bought."""
    response = fetch(_order(), permissions=())

    assert response.status_code == 403
    assert "so.view" in response.json()["detail"]


@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.DRAFT,
        DocumentStatus.PENDING_APPROVAL,
        DocumentStatus.APPROVED,
        DocumentStatus.ALLOCATED,
        DocumentStatus.PICKED,
        DocumentStatus.CANCELLED,
    ],
)
def test_no_invoice_before_the_goods_have_gone_out(fetch, status):
    """An invoice is evidence of a supply, so it follows the supply.

    Allocated and picked are the near misses worth naming: the stock is spoken
    for and even off the shelf, but nothing has left the building, and an
    invoice raised there would sit in the customer's books against a delivery
    that can still be cancelled.
    """
    response = fetch(_order(status=status))

    assert response.status_code == 409
    assert "SO-000042" in response.json()["detail"]


@pytest.mark.parametrize("status", [DocumentStatus.SHIPPED, DocumentStatus.COMPLETED])
def test_a_shipped_or_completed_order_invoices(fetch, status):
    assert fetch(_order(status=status)).status_code == 200


def test_a_branch_user_cannot_print_another_branchs_invoice(fetch):
    """Every other read here is warehouse-scoped, and this one prints more.

    404 rather than 403 on purpose: the same answer as an order that does not
    exist, so probing IDs cannot map out another branch's order book.
    """
    outsider = _User(("so.view",), _Role(STAFF), warehouse_id=7)

    response = fetch(_order(warehouse_id=1), user=outsider)

    assert response.status_code == 404


def test_a_branch_user_prints_their_own_branchs_invoice(fetch):
    insider = _User(("so.view",), _Role(STAFF), warehouse_id=1)

    assert fetch(_order(warehouse_id=1), user=insider).status_code == 200
