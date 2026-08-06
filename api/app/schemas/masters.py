from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    DrugSchedule,
    SourcingPolicy,
    StorageCondition,
    TrackingMode,
)
from app.services import gst


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    parent_id: int | None = None
    is_active: bool = True
    #: How many products sit in this class. Retiring one with products in it
    #: is refused, so the count is the reason shown before the attempt.
    product_count: int = 0


class CategoryIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "name": "Antidiabetics",
            "parent_id": None
    }})

    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None


class CategoryUpdate(BaseModel):
    # Every PATCH example here sends one or two fields on purpose. Only the
    # fields present are written, so an example listing all of them would
    # teach the opposite of how the endpoint works.
    model_config = ConfigDict(json_schema_extra={"example": {"parent_id": 4}})

    name: str | None = Field(None, min_length=1, max_length=128)
    parent_id: int | None = None
    is_active: bool | None = None


class UomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    product_count: int = 0


class UomIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "code": "VIAL",
            "name": "Vial"
    }})

    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=64)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    description: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    uom_id: int
    uom_code: str | None = None
    tracking_mode: TrackingMode

    composition: str | None = None
    manufacturer: str | None = None
    pack_size: str | None = None
    drug_schedule: DrugSchedule
    storage_condition: StorageCondition
    is_prescription_required: bool

    hsn_code: str | None = None
    gst_rate: Decimal
    barcode: str | None = None
    reorder_point: Decimal
    safety_stock_days: int
    sourcing_policy: SourcingPolicy
    mrp: Decimal | None = None
    is_active: bool

    # Populated on list/detail endpoints from the balance projection.
    qty_on_hand: Decimal | None = None
    qty_available: Decimal | None = None


class ProductIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "sku": "GLIM-2",
            "name": "Glimepiride 2mg",
            "description": "Sulfonylurea for type 2 diabetes",
            "category_id": 1,
            "uom_id": 1,
            "tracking_mode": "LOT_EXPIRY",
            "composition": "Glimepiride",
            "manufacturer": "Sun Pharma",
            "pack_size": "10 tablets",
            "drug_schedule": "H",
            "storage_condition": "AMBIENT",
            "is_prescription_required": True,
            "hsn_code": "30049099",
            "gst_rate": "12",
            "reorder_point": 150,
            "safety_stock_days": 10,
            "sourcing_policy": "EITHER",
            "mrp": "62.00"
    }})

    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    category_id: int | None = None
    uom_id: int
    tracking_mode: TrackingMode = TrackingMode.NONE

    composition: str | None = Field(None, max_length=255)
    manufacturer: str | None = Field(None, max_length=255)
    pack_size: str | None = Field(None, max_length=64)
    drug_schedule: DrugSchedule = DrugSchedule.OTC
    storage_condition: StorageCondition = StorageCondition.AMBIENT
    is_prescription_required: bool = False

    hsn_code: str | None = Field(None, max_length=8)
    gst_rate: Decimal = Field(Decimal("12"), ge=0, le=100)
    barcode: str | None = Field(None, max_length=64)
    reorder_point: Decimal = Field(Decimal("0"), ge=0)
    safety_stock_days: int = Field(7, ge=0, le=365)
    sourcing_policy: SourcingPolicy = SourcingPolicy.EITHER
    mrp: Decimal | None = Field(None, ge=0)
    is_active: bool = True


class ProductUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"reorder_point": "2830", "safety_stock_days": 21}
        }
    )

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    category_id: int | None = None
    composition: str | None = None
    manufacturer: str | None = None
    pack_size: str | None = None
    drug_schedule: DrugSchedule | None = None
    storage_condition: StorageCondition | None = None
    is_prescription_required: bool | None = None
    hsn_code: str | None = None
    gst_rate: Decimal | None = Field(None, ge=0, le=100)
    barcode: str | None = None
    reorder_point: Decimal | None = Field(None, ge=0)
    safety_stock_days: int | None = Field(None, ge=0, le=365)
    sourcing_policy: SourcingPolicy | None = None
    mrp: Decimal | None = Field(None, ge=0)
    is_active: bool | None = None


def check_branch_gstin(model):
    """A branch's GSTIN must be real, and must belong to the branch's state.

    Both halves matter. The checksum catches a mistyped character — GSTIN
    carries a mod-36 check digit, so a wrong number is provably wrong rather
    than merely unfamiliar. The prefix catches the likelier mistake: pasting
    head office's registration into a branch in another state, which is the
    exact confusion this column exists to end.

    The state check is skipped when the state is unknown to `STATE_CODES` or
    when only one of the two fields is being patched — refusing an edit on the
    strength of a value not in front of us would make the field unmaintainable.
    """
    gstin = (model.gstin or "").strip().upper()
    if not gstin:
        return model
    if not gst.gstin_is_valid(gstin):
        raise ValueError(
            f"{gstin!r} is not a valid GSTIN — the check digit does not match, "
            f"so at least one character is wrong"
        )
    if gst.gstin_state_matches(gstin, model.state_code or "") is False:
        expected = gst.gstin_prefix_for_state(model.state_code or "")
        raise ValueError(
            f"a GSTIN opens with its state's code, so a branch in "
            f"{model.state_code} needs one starting {expected}, not "
            f"{gstin[:2]} — a registration belongs to one state"
        )
    model.gstin = gstin
    return model


class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    is_central: bool
    state_code: str
    gstin: str | None = None
    address: str | None = None
    is_active: bool


class WarehouseIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "code": "BR-THN",
            "name": "Thane Branch",
            "is_central": False,
            "state_code": "MH",
            "gstin": "27AABCS9876P1ZA",
            "address": "Ghodbunder Road, Thane West 400607"
    }})

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    is_central: bool = False
    state_code: str = Field(min_length=2, max_length=2)
    gstin: str | None = None
    address: str | None = None
    is_active: bool = True

    _check_gstin = model_validator(mode="after")(check_branch_gstin)


class WarehouseUpdate(BaseModel):
    """`code` is omitted on purpose — documents and reports refer to it."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"address": "Plot 14, MIDC Andheri, Mumbai 400093"}
        }
    )

    name: str | None = Field(None, min_length=1, max_length=255)
    is_central: bool | None = None
    state_code: str | None = Field(None, min_length=2, max_length=2)
    gstin: str | None = None
    address: str | None = None
    is_active: bool | None = None

    _check_gstin = model_validator(mode="after")(check_branch_gstin)


class BinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    warehouse_id: int
    code: str
    zone: str | None = None
    is_cold_chain: bool
    is_quarantine: bool
    is_active: bool


class BinIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "code": "C-01",
            "zone": "COLD",
            "is_cold_chain": True,
            "is_quarantine": False
    }})

    code: str = Field(min_length=1, max_length=32)
    zone: str | None = Field(None, max_length=32)
    is_cold_chain: bool = False
    is_quarantine: bool = False
    is_active: bool = True


class BinUpdate(BaseModel):
    """`code` is absent: stock rows point at a bin by id, but people find a
    location by the label on the shelf. Renaming it would make the two
    disagree with no way to tell which is right."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"zone": "COLD-A", "is_cold_chain": True}}
    )

    zone: str | None = Field(None, max_length=32)
    is_cold_chain: bool | None = None
    is_quarantine: bool | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    gstin: str | None = None
    state_code: str
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    payment_terms_days: int
    is_active: bool


class SupplierIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "code": "DIST-004",
            "name": "Meridian Healthcare Distributors",
            "gstin": "27AABCM1234K1Z9",
            "state_code": "MH",
            "contact_person": "Nikhil Shah",
            "phone": "+91 98200 11223",
            "email": "orders@meridianhc.co.in",
            "payment_terms_days": 45
    }})

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    gstin: str | None = Field(None, min_length=15, max_length=15)
    state_code: str = Field(min_length=2, max_length=2)
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    payment_terms_days: int = Field(30, ge=0, le=365)
    is_active: bool = True


class SupplierUpdate(BaseModel):
    """`code` is omitted on purpose — purchase orders refer to it.

    `state_code` is editable because a wrong one is a common data-entry slip,
    and past orders are unaffected: each stores the GST regime it was raised
    under rather than recomputing it from the supplier.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"phone": "+91 98200 44556", "payment_terms_days": 60}
        }
    )

    name: str | None = Field(None, min_length=1, max_length=255)
    gstin: str | None = Field(None, min_length=15, max_length=15)
    state_code: str | None = Field(None, min_length=2, max_length=2)
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    payment_terms_days: int | None = Field(None, ge=0, le=365)
    is_active: bool | None = None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    is_institutional: bool
    gstin: str | None = None
    state_code: str
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    credit_limit: Decimal
    is_active: bool


class CustomerIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
            "code": "CUST-004",
            "name": "Sunrise Nursing Home",
            "is_institutional": True,
            "gstin": "27AACCS5678M1Z3",
            "state_code": "MH",
            "phone": "+91 22 2841 5566",
            "credit_limit": "200000"
    }})

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    is_institutional: bool = False
    gstin: str | None = Field(None, min_length=15, max_length=15)
    state_code: str = Field(min_length=2, max_length=2)
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    credit_limit: Decimal = Field(Decimal("0"), ge=0)
    is_active: bool = True


class WalkInCustomerIn(BaseModel):
    """A person at the counter, captured while the sale is being rung up.

    Deliberately not `CustomerIn`. That schema asks for a code, and the code is
    the one thing the person taking the order must not invent — two counters
    inventing `CUST-005` on the same afternoon is a conflict the customer has
    to stand and watch. The server allocates it from the same gap-free counter
    the documents use.

    A blank GSTIN is the ordinary case, not a missing field: a supply to an
    unregistered person is a perfectly legal B2C sale and the invoice carries
    the tax split without a recipient GSTIN. Asking for one would make the
    common case look like an error.
    """

    model_config = ConfigDict(json_schema_extra={"example": {
            "name": "Ramesh Kulkarni",
            "state_code": "MH",
    }})

    name: str = Field(min_length=1, max_length=255)
    gstin: str | None = Field(None, min_length=15, max_length=15)
    #: Where the buyer is, which decides CGST+SGST against IGST. Left out it
    #: becomes the branch's own state — the counter sale, and the answer that
    #: is right nearly every time.
    state_code: str | None = Field(None, min_length=2, max_length=2)
    phone: str | None = Field(None, max_length=32)

    _check_gstin = model_validator(mode="after")(check_branch_gstin)


class CustomerUpdate(BaseModel):
    """`code` is omitted on purpose — sales orders refer to it."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"credit_limit": "250000.00"}}
    )

    name: str | None = Field(None, min_length=1, max_length=255)
    is_institutional: bool | None = None
    gstin: str | None = Field(None, min_length=15, max_length=15)
    state_code: str | None = Field(None, min_length=2, max_length=2)
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    credit_limit: Decimal | None = Field(None, ge=0)
    is_active: bool | None = None
