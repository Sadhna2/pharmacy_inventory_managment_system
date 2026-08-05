"""Shapes for invoice intake.

The response is deliberately not a goods receipt. It is a *draft* one, plus
everything a person needs to decide whether to accept it: what was read, how
each line was resolved, and every reason to look twice. The endpoint that
creates stock is still `POST /grn`, unchanged, and it is reached by submitting
this draft after a human has been through it.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class FlagOut(BaseModel):
    field: str
    severity: str = Field(description="BLOCK, REVIEW or INFO")
    message: str
    line_no: int | None = Field(
        None, description="1-based, matching the S.N. column on the paper. "
                          "Null for a finding about the invoice header."
    )
    suggestion: str | None = Field(
        None, description="A correction confident enough to offer, when one "
                          "exists. Never applied automatically."
    )


class CandidateOut(BaseModel):
    product_id: int
    label: str


class DraftLineOut(BaseModel):
    """One invoice line, read and resolved as far as the catalogue allows."""

    line_no: int

    # --- what the paper said -------------------------------------------------
    printed_name: str
    batch_no: str | None = None
    expiry_date: date | None = None
    quantity: float = 0
    free_quantity: float = 0
    rate: float | None = None
    mrp: float | None = None
    pack: str | None = None
    hsn: str | None = Field(
        None,
        description="HSN code as printed. Read by the validator, and returned "
                    "so a product created from an unmatched line arrives with "
                    "it already filled in — it is on the paper in front of the "
                    "receiver, and looking it up is the reason a new product "
                    "gets created with the field left blank.",
    )
    gst_rate: float | None = Field(
        None, description="GST percentage as printed, for the same reason.",
    )

    # --- what it was resolved to --------------------------------------------
    product_id: int | None = None
    sku: str | None = None
    product_name: str | None = None
    match_method: str = Field(
        description="supplier_alias, sku, exact_name, name_tokens or unmatched. "
                    "Shown so a weak match can be styled differently from a "
                    "certain one rather than both looking equally settled."
    )
    po_line_id: int | None = None
    qty_outstanding: float | None = Field(
        None, description="Still to come on the purchase order, if received "
                          "against one."
    )
    candidates: list[CandidateOut] = Field(
        default_factory=list,
        description="Offered when a line fits more than one product. A "
                    "shortlist beats an empty box; a wrong guess beats neither.",
    )
    flags: list[FlagOut] = Field(default_factory=list)


class IntakeSummaryOut(BaseModel):
    lines: int
    resolved: int
    unmatched: int
    blocking: int = Field(
        description="Findings that must be dealt with before this can be posted."
    )
    ready: bool = Field(
        description="Every line resolved and nothing blocking. Even then the "
                    "receipt is submitted by a person, never by this endpoint."
    )


class InvoiceIntakeOut(BaseModel):
    """A proposed goods receipt. Nothing has been created."""

    warehouse_id: int | None = Field(
        None, description="Where the goods land, when that is settled — from "
                          "the order, the receiver's own branch, or the form. "
                          "Null means nobody has said yet, which is allowed: "
                          "reading the paper is what tells you what arrived."
    )
    supplier_id: int | None = None
    purchase_order_id: int | None = None

    supplier_invoice_no: str | None = None
    supplier_invoice_date: date | None = None
    supplier_name_printed: str | None = Field(
        None, description="The supplier name as printed, which may not match "
                          "the supplier record this was filed against."
    )
    supplier_gstin_printed: str | None = None

    lines: list[DraftLineOut]
    flags: list[FlagOut] = Field(
        default_factory=list,
        description="Findings about the invoice as a whole rather than one line.",
    )
    summary: IntakeSummaryOut


class RememberAliasIn(BaseModel):
    """Teach the system what a distributor calls one of our products.

    Sent after a person resolves an unmatched line by hand. This is what turns
    the abbreviation problem from unsolvable into a one-off: a trade name like
    `OMEZ-20` cannot be derived from `OMEPRAZOLE 20MG CAP` by any rule worth
    trusting, but it only has to be answered once.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "supplier_id": 3,
                "product_id": 12,
                "printed_name": "OMEZ-20 CAP 10'S",
                "unit_cost": "38.50",
            }
        }
    )

    supplier_id: int
    product_id: int
    printed_name: str = Field(min_length=1, max_length=255)
    unit_cost: Decimal | None = Field(
        None,
        ge=0,
        description=(
            "The rate printed on the line this was resolved from. Used only "
            "when the product is not linked to this distributor yet, where it "
            "becomes the link's cost — a real figure off the invoice rather "
            "than one derived from the MRP."
        ),
    )
