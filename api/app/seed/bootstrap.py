"""Bootstrap seed: roles, permissions, users, and a small pharmacy fixture.

This is the DEV seed — enough to log in and exercise every Layer 0/1 flow.
The full 2-year synthetic history for forecasting is Layer 2 work (§15).

    python -m app.seed.bootstrap
"""

import hashlib
import os
import sys
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import settings
from app.core.permissions import (
    ADMIN,
    CUSTOMER,
    MANAGER,
    PERMISSIONS,
    ROLE_NAMES,
    ROLE_PERMISSIONS,
    STAFF,
)
from app.core.security import hash_password, verify_password
from app.db.session import SessionLocal
from app.models.enums import (
    DrugSchedule,
    MovementType,
    SourcingPolicy,
    StorageCondition,
    TrackingMode,
)
from app.models.identity import Permission, Role, RolePermission, User
from app.models.masters import (
    Bin,
    Category,
    Customer,
    Product,
    ProductSupplier,
    Supplier,
    Uom,
    Warehouse,
)
from app.models.settings import FeatureFlag
from app.models.stock import Lot
from app.services import ledger

# Never bake a credential into the repository. Override with SEED_PASSWORD;
# the fallback exists so a fresh local clone seeds in one command, and is
# refused outside development by _check_password() below.
_DEFAULT_DEV_PASSWORD = "ChangeMe@123"
DEV_PASSWORD = os.environ.get("SEED_PASSWORD") or _DEFAULT_DEV_PASSWORD


#: The four demo accounts, at module level so the seed that creates them and
#: the password check below cannot drift apart.
USER_DEFS = [
    ("admin@pharmacy.co.in", "Priya Nair", ADMIN, None),
    ("manager@pharmacy.co.in", "Rahul Desai", MANAGER, None),
    ("staff@pharmacy.co.in", "Anjali Rao", STAFF, "BR-AND"),
    ("customer@cityhospital.co.in", "City Hospital", CUSTOMER, None),
]

#: The demo firm's GST registrations, one per state it trades in.
#:
#: One number per *state*, not per branch — the four Maharashtra warehouses
#: share a single registration because a registration covers a state, and only
#: Ahmedabad has its own because Gujarat is a separate one. The middle ten
#: characters are the firm's PAN and are identical in both, which is what makes
#: them read as one company rather than two.
#:
#: Both carry a correct mod-36 check digit, so the invoice scanner's own
#: validator accepts them — a seed that shipped an invalid GSTIN would have the
#: system contradicting itself the first time anyone photographed its output.
STATE_REGISTRATIONS = {
    "MH": "27AABCS9876P1ZA",
    "GJ": "24AABCS9876P1ZG",
}

#: The customer account and the buyer it speaks for, paired here so the two
#: cannot drift. Without the link the CUSTOMER role has no way to tell its own
#: orders from anyone else's and is shown none of them.
CUSTOMER_ACCOUNT_EMAIL = "customer@cityhospital.co.in"
CUSTOMER_ACCOUNT_CODE = "CUST-001"


def _check_password() -> None:
    """Refuse to create well-known accounts on anything but a dev machine."""
    if DEV_PASSWORD == _DEFAULT_DEV_PASSWORD and settings.env != "development":
        raise SystemExit(
            "Refusing to seed demo accounts with the default password while "
            f"ENV={settings.env}. Set SEED_PASSWORD to something private first."
        )


def reset_demo_passwords(db: Session) -> int:
    """Set the four demo accounts back to the current SEED_PASSWORD.

    Deliberately *not* run on every boot. Passwords are changeable through the
    application (`POST /auth/change-password`, and an admin reset), so rehashing
    on each `docker compose up` would quietly undo a real change every time the
    stack restarted — a worse bug than the one it fixes. This is an explicit
    act: `python -m app.seed.bootstrap --reset-passwords`.
    """
    changed = 0
    for email, *_ in USER_DEFS:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None and not verify_password(DEV_PASSWORD, user.password_hash):
            user.password_hash = hash_password(DEV_PASSWORD)
            changed += 1
    db.flush()
    return changed


def _warn_if_password_drifted(db: Session) -> None:
    """Say so when SEED_PASSWORD no longer opens the accounts it names.

    Changing SEED_PASSWORD in `.env` and restarting looks like it should take
    effect, and does nothing: the seed short-circuits on an already-populated
    database, so the stored hash keeps whatever the *first* run used. You then
    meet a login that rejects the only password you were told to use, with
    nothing on screen connecting the two.
    """
    if not os.environ.get("SEED_PASSWORD"):
        return  # nothing was asked for, so nothing can have drifted
    admin = db.scalar(select(User).where(User.email == USER_DEFS[0][0]))
    if admin is None or verify_password(DEV_PASSWORD, admin.password_hash):
        return
    print(
        "\n  WARNING: SEED_PASSWORD does not match the existing demo accounts."
        "\n  They keep the password from the run that created them — this "
        "database was\n  seeded before, so the new value has not been applied "
        "and will not sign you in."
        "\n"
        "\n  Apply it:   python -m app.seed.bootstrap --reset-passwords"
        "\n  Start over: docker compose down -v && docker compose up\n"
    )


def sync_permissions(db: Session) -> dict[str, Permission]:
    existing = {p.code: p for p in db.scalars(select(Permission)).all()}
    for code, description in PERMISSIONS.items():
        if code not in existing:
            perm = Permission(code=code, description=description)
            db.add(perm)
            existing[code] = perm
    db.flush()
    return existing


#: Every capability from the brief, including the two that are not built.
#:
#: The unbuilt ones are rows here rather than absences on purpose: the settings
#: screen shows them greyed out and labelled, so the system states its own
#: scope honestly instead of quietly having no menu item. `is_implemented` is
#: the only thing that decides whether a toggle can be switched on at all, and
#: it is set from the code, never from the database.
FEATURES: list[tuple[str, str, str, bool]] = [
    (
        "features.reorder",
        "Replenishment",
        "Suggests what to buy and how much, from the demand forecast and each "
        "distributor's measured lead time. Raises draft purchase orders that "
        "still need approving.",
        True,
    ),
    (
        "features.forecast",
        "Demand forecast",
        "Predicts units per product per branch. Three methods are backtested "
        "against held-out data and the best one wins per series.",
        True,
    ),
    (
        "features.anomaly",
        "Exceptions",
        "Finds movements that broke from the pattern around them — demand "
        "breaks, shrinkage, large write-offs and out-of-hours activity.",
        True,
    ),
    (
        "features.leadtime",
        "Supplier lead times",
        "Measures how long each distributor actually takes, from your own "
        "purchase orders and goods receipts.",
        True,
    ),
    (
        "features.nl_reporting",
        "Ask a question",
        "Type an inventory question in plain English and have it fill in the "
        "filters on the screen it belongs to. Designed, not built.",
        False,
    ),
    (
        "features.invoice_ocr",
        "Invoice scanning",
        "Read a distributor invoice from a photo or PDF and pre-fill a goods "
        "receipt, for a human to check line by line. Every reading is checked "
        "against the invoice's own arithmetic and its GSTIN checksum before "
        "anyone sees it, and the endpoint writes nothing to the ledger.",
        True,
    ),
]


def sync_feature_flags(db: Session) -> int:
    """Idempotent, and safe to run against a database that is already seeded.

    Labels and descriptions are refreshed from the code every time, but
    `is_enabled` is left alone once the row exists — an administrator who
    switched something off should not have it switched back on by a deploy.
    """
    existing = {f.key: f for f in db.scalars(select(FeatureFlag)).all()}
    for order, (key, label, description, implemented) in enumerate(FEATURES):
        flag = existing.get(key)
        if flag is None:
            db.add(
                FeatureFlag(
                    key=key,
                    label=label,
                    description=description,
                    category="AI",
                    # Built features ship on. An unbuilt one cannot be on.
                    is_enabled=implemented,
                    is_implemented=implemented,
                    sort_order=order,
                )
            )
            continue
        flag.label = label
        flag.description = description
        flag.sort_order = order
        # A feature that has just been built. Its row was forced off while it
        # was unimplemented, and leaving `is_enabled` alone — the rule for
        # every other row — would keep it off forever on any database that
        # already exists, including the deployed one. Nobody switched it off,
        # so there is no administrator decision here to preserve: this is the
        # same "built features ship on" rule the insert branch applies, caught
        # one deploy later.
        if implemented and not flag.is_implemented:
            flag.is_enabled = True
        flag.is_implemented = implemented
        if not implemented:
            # Belt and braces: a feature that was removed must not stay live
            # because a row said it was on.
            flag.is_enabled = False
    db.flush()
    return len(FEATURES)


def sync_roles(db: Session, perms: dict[str, Permission]) -> dict[str, Role]:
    roles: dict[str, Role] = {
        r.code: r for r in db.scalars(select(Role)).all()
    }
    for code, granted in ROLE_PERMISSIONS.items():
        role = roles.get(code)
        if role is None:
            role = Role(code=code, name=ROLE_NAMES[code])
            db.add(role)
            db.flush()
            roles[code] = role

        held = {rp.permission.code for rp in role.permissions}
        for perm_code in granted:
            if perm_code not in held:
                db.add(
                    RolePermission(role_id=role.id, permission_id=perms[perm_code].id)
                )
    db.flush()
    return roles


def converge(db: Session) -> str:
    """Everything that must be true of *any* database this code runs against.

    There are two kinds of thing in this module and telling them apart is the
    whole point of this function existing.

    `seed()` below builds a demonstration: products, branches, users, two
    invented GST registrations. It is fixture data, it runs once, and it must
    never touch a database it did not create — a made-up registration number
    printed on a real tax invoice is worse than a missing one.

    This is the other kind. Permissions, the roles that hold them and the
    feature flags that expose them are not data the business owns; they are
    what the running code *is*, expressed as rows because the authorisation
    check reads them from a table. A deployment whose permission rows lag the
    code is a deployment where new endpoints 403 for everybody.

    That is not hypothetical. `sync_permissions` and `sync_roles` used to be
    called from inside `seed()`, which `main()` returns before reaching on any
    database that already holds a user — so on the server they had not run
    since the day it was first provisioned. Feature flags escaped only because
    somebody hit the problem once and lifted that single call out by hand.
    Naming the category is what stops the next one being missed: convergent
    work goes here, fixtures go in `seed()`, and `main()` runs this one
    unconditionally.

    Idempotent by construction — every step below adds what is absent and
    leaves what is present alone — so it is safe on an empty database, on a
    two-year-old one, and on every container restart in between.
    """
    perms = sync_permissions(db)
    roles = sync_roles(db, perms)
    flags = sync_feature_flags(db)
    return f"permissions: {len(perms)}   roles: {len(roles)}   flags: {flags}"


def _digits(*parts: str) -> int:
    """A stable number derived from text.

    Not `hash()`. Python salts string hashing per process unless PYTHONHASHSEED
    is pinned, so `hash(sku)` gives a different answer on every run — which
    means the barcode printed on a product locally, in CI and on the server
    were three different numbers for the same medicine. A seed that claims to
    build one dataset everywhere cannot derive anything from `hash()`.
    """
    digest = hashlib.blake2b("|".join(parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


# The formats manufacturers actually print, in the proportions a distributor's
# invoice shows them. Not decoration: `learn_supplier_batch_shapes` reads the
# lot codes already received from a supplier and `validate_invoice` notes any
# code on a scanned invoice whose shape is unlike them. Seeded lots reading
# `MET-500-B1` taught the vocabulary a shape no manufacturer uses, so every
# real batch code arriving on a real invoice looked unfamiliar and the note
# fired on all of them — a check that fires on everything says nothing.
_BATCH_SHAPES = ["AA0000", "A00000", "AAA000", "0000AA", "AA00-00"]
_BATCH_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def _batch_code(sku: str, idx: int) -> str:
    """A manufacturer-style batch code, the same one on every seed run."""
    n = _digits(sku, str(idx))
    shape = _BATCH_SHAPES[n % len(_BATCH_SHAPES)]
    n //= len(_BATCH_SHAPES)
    out = []
    for ch in shape:
        if ch == "A":
            out.append(_BATCH_LETTERS[n % len(_BATCH_LETTERS)])
            n //= len(_BATCH_LETTERS)
        elif ch == "0":
            out.append(str(n % 10))
            n //= 10
        else:
            out.append(ch)
    return "".join(out)


def _sourcing_for(
    storage: StorageCondition, schedule: DrugSchedule
) -> SourcingPolicy:
    """Route a product through the hub, direct to branch, or either.

    Cold chain goes via the central warehouse because one validated cold room
    is far cheaper than six, and Schedule H1/X because fewer custody points
    means fewer places to audit. Plain OTC goes direct — the extra handling
    day costs more than the pooling saves. Everything else is a judgement the
    reorder engine gets to make per order.
    """
    if storage in (StorageCondition.COLD_CHAIN, StorageCondition.FROZEN):
        return SourcingPolicy.VIA_CENTRAL
    if schedule in (DrugSchedule.H1, DrugSchedule.X):
        return SourcingPolicy.VIA_CENTRAL
    if schedule == DrugSchedule.OTC:
        return SourcingPolicy.DIRECT
    return SourcingPolicy.EITHER


def seed(db: Session) -> None:
    """The demonstration fixture: an empty database only.

    Everything here is invented — the branches, the catalogue, the two GST
    registrations in `STATE_REGISTRATIONS`. That is fine for a database this
    function created and wrong for one it did not, so `main()` runs it only
    when there is no user. Anything that must also reach an established
    installation belongs in `converge()` above, not here.
    """
    # Redundant on the path through main(), which converges first. Kept so this
    # function stands up on its own — a fresh database needs the roles to exist
    # before it can attach one to a user, and a caller reaching for `seed` has
    # no reason to know that.
    print(f"  {converge(db)}")
    roles: dict[str, Role] = {r.code: r for r in db.scalars(select(Role)).all()}

    # --- UOMs ---------------------------------------------------------------
    uoms: dict[str, Uom] = {u.code: u for u in db.scalars(select(Uom)).all()}
    for code, name in [
        ("STRIP", "Strip"), ("BOX", "Box"), ("BOTTLE", "Bottle"),
        ("VIAL", "Vial"), ("EA", "Each"), ("TUBE", "Tube"),
    ]:
        if code not in uoms:
            uom = Uom(code=code, name=name)
            db.add(uom)
            uoms[code] = uom
    db.flush()

    # --- Categories (therapeutic classes) -----------------------------------
    cats: dict[str, Category] = {c.name: c for c in db.scalars(select(Category)).all()}
    for name in [
        "Cardiovascular", "Antidiabetic", "Antibiotics", "Analgesics",
        "Respiratory", "Gastrointestinal", "Vitamins & Supplements",
        "Cold Chain", "Consumables & Devices",
    ]:
        if name not in cats:
            cat = Category(name=name)
            db.add(cat)
            cats[name] = cat
    db.flush()

    # --- Locations: central warehouse + branches ----------------------------
    warehouses: dict[str, Warehouse] = {
        w.code: w for w in db.scalars(select(Warehouse)).all()
    }
    branch_defs = [
        ("CW-MUM", "Central Warehouse - Mumbai", True, "MH"),
        ("BR-AND", "Andheri Branch (Hospital)", False, "MH"),
        ("BR-BAN", "Bandra Branch (Residential)", False, "MH"),
        ("BR-PUN", "Pune Branch (Commercial)", False, "MH"),
        ("BR-AHM", "Ahmedabad Branch", False, "GJ"),  # different state -> IGST
    ]
    for code, name, is_central, state in branch_defs:
        if code not in warehouses:
            wh = Warehouse(
                code=code, name=name, is_central=is_central, state_code=state
            )
            db.add(wh)
            warehouses[code] = wh
        # Set on existing rows too, so a database seeded before this column
        # existed gains its registrations without being rebuilt. Never
        # overwritten: a real GSTIN entered by hand outranks a demo one.
        if not warehouses[code].gstin:
            warehouses[code].gstin = STATE_REGISTRATIONS[state]
    db.flush()

    # --- Bins, including a cold room in the central warehouse ---------------
    central = warehouses["CW-MUM"]
    if not db.scalar(select(Bin).where(Bin.warehouse_id == central.id)):
        for zone, count, cold in [("A", 8, False), ("B", 8, False), ("COLD", 4, True)]:
            for i in range(1, count + 1):
                db.add(
                    Bin(
                        warehouse_id=central.id,
                        code=f"{zone}-{i:02d}",
                        zone=zone,
                        is_cold_chain=cold,
                    )
                )
        db.add(
            Bin(
                warehouse_id=central.id, code="QUAR-01", zone="QUAR",
                is_quarantine=True,
            )
        )
    for code in ("BR-AND", "BR-BAN", "BR-PUN", "BR-AHM"):
        wh = warehouses[code]
        if not db.scalar(select(Bin).where(Bin.warehouse_id == wh.id)):
            for i in range(1, 5):
                db.add(Bin(warehouse_id=wh.id, code=f"S-{i:02d}", zone="SHELF"))
            db.add(
                Bin(warehouse_id=wh.id, code="FRIDGE-1", zone="COLD",
                    is_cold_chain=True)
            )
    db.flush()

    # --- Users --------------------------------------------------------------
    for email, full_name, role_code, wh_code in USER_DEFS:
        if not db.scalar(select(User).where(User.email == email)):
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(DEV_PASSWORD),
                    full_name=full_name,
                    role_id=roles[role_code].id,
                    warehouse_id=warehouses[wh_code].id if wh_code else None,
                )
            )
    db.flush()

    # --- Distributors -------------------------------------------------------
    suppliers: dict[str, Supplier] = {
        s.code: s for s in db.scalars(select(Supplier)).all()
    }
    supplier_defs = [
        ("DIST-001", "MedPlus Distributors", "27AABCU9603R1ZX", "MH", 5),
        ("DIST-002", "Apex Pharma Supply", "27AAACT2727Q1ZW", "MH", 9),
        ("DIST-003", "Gujarat Health Traders", "24AACCG0527D1Z8", "GJ", 12),
    ]
    for code, name, gstin, state, _lead in supplier_defs:
        if code not in suppliers:
            sup = Supplier(
                code=code, name=name, gstin=gstin, state_code=state,
                payment_terms_days=30,
            )
            db.add(sup)
            suppliers[code] = sup
    db.flush()

    # --- Customers ----------------------------------------------------------
    if not db.scalar(select(Customer)):
        db.add_all([
            Customer(code="CUST-001", name="City Hospital", is_institutional=True,
                     gstin="27AABCC1234D1Z5", state_code="MH",
                     credit_limit=Decimal("500000")),
            Customer(code="CUST-002", name="Sunrise Clinic", is_institutional=True,
                     gstin="27AABCS5678E1Z9", state_code="MH",
                     credit_limit=Decimal("100000")),
            Customer(code="CUST-003", name="Gujarat Nursing Home",
                     is_institutional=True, gstin="24AABCG9999F1Z2",
                     state_code="GJ", credit_limit=Decimal("200000")),
            Customer(code="WALK-IN", name="Walk-in Customer", state_code="MH"),
        ])
    db.flush()

    # The customer login speaks for a buyer, and this is where it is told
    # which one. Done here rather than beside the other accounts above because
    # the users are created before any customer exists — and unconditionally
    # rather than only on a fresh database, so a seed run repairs an account
    # that predates the column.
    buyer = db.scalar(select(Customer).where(Customer.code == CUSTOMER_ACCOUNT_CODE))
    account = db.scalar(select(User).where(User.email == CUSTOMER_ACCOUNT_EMAIL))
    if buyer is not None and account is not None:
        account.customer_id = buyer.id
    db.flush()

    # --- Products -----------------------------------------------------------
    # (sku, name, composition, category, uom, tracking, schedule, storage,
    #  gst, mrp, reorder)
    product_defs = [
        ("MET-500", "Metformin 500mg", "Metformin Hydrochloride", "Antidiabetic",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "45.00", 200),
        ("AML-5", "Amlodipine 5mg", "Amlodipine Besylate", "Cardiovascular",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "38.50", 150),
        ("ATO-10", "Atorvastatin 10mg", "Atorvastatin Calcium", "Cardiovascular",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "92.00", 120),
        ("PAR-650", "Paracetamol 650mg", "Paracetamol", "Analgesics",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "30.00", 400),
        ("AMOX-500", "Amoxicillin 500mg", "Amoxicillin Trihydrate", "Antibiotics",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "78.00", 100),
        ("CET-10", "Cetirizine 10mg", "Cetirizine Hydrochloride", "Respiratory",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "22.00", 180),
        ("INS-GLA", "Insulin Glargine 100IU", "Insulin Glargine", "Cold Chain",
         "VIAL", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.COLD_CHAIN, 5, "850.00", 40),
        ("ORS-21", "ORS Powder 21g", "Oral Rehydration Salts",
         "Gastrointestinal", "EA", TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 5, "18.00", 300),
        ("ALP-025", "Alprazolam 0.25mg", "Alprazolam", "Analgesics",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H1,
         StorageCondition.AMBIENT, 12, "55.00", 50),
        ("VITD3", "Vitamin D3 60000IU", "Cholecalciferol",
         "Vitamins & Supplements", "STRIP", TrackingMode.LOT,
         DrugSchedule.OTC, StorageCondition.AMBIENT, 18, "65.00", 90),
        ("SYR-5ML", "Disposable Syringe 5ml", None, "Consumables & Devices",
         "EA", TrackingMode.NONE, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "8.00", 500),
        ("COT-100", "Absorbent Cotton 100g", None, "Consumables & Devices",
         "EA", TrackingMode.NONE, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "45.00", 100),

        # --- what the distributors actually ship -------------------------
        # The twelve above were chosen to exercise the *system* — one of each
        # tracking mode, drug schedule and storage condition. They were never
        # chosen to resemble a real pharmacy's shelf, and it showed the moment
        # a scanned invoice was matched against them: fourteen lines read
        # perfectly off the page and eight of them had nothing to match to,
        # because no branch of this chain stocked telmisartan.
        #
        # These are the rest of what a distributor's invoice carries. Names
        # are the generic ones a person would type; the trade names printed on
        # real invoices — OMEZ-20, PANTOP-40, ASTHALIN — are deliberately NOT
        # aliased here. Meeting one for the first time and having somebody
        # answer it once is the behaviour worth showing, and pre-loading the
        # answer would hide it.
        ("PANTO-40", "Pantoprazole 40mg", "Pantoprazole Sodium",
         "Gastrointestinal", "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "145.00", 160),
        ("PANTO-INJ", "Pantoprazole Injection 40mg", "Pantoprazole Sodium",
         "Gastrointestinal", "VIAL", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "48.00", 60),
        ("OMEZ-20", "Omeprazole 20mg", "Omeprazole", "Gastrointestinal",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "72.00", 140),
        ("TELMA-40", "Telmisartan 40mg", "Telmisartan", "Cardiovascular",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "118.00", 130),
        ("GLIMI-2", "Glimepiride 2mg", "Glimepiride", "Antidiabetic",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "82.00", 110),
        ("AZITH-500", "Azithromycin 500mg", "Azithromycin Dihydrate",
         "Antibiotics", "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "130.00", 90),
        ("CEFTRI-1G", "Ceftriaxone Injection 1g", "Ceftriaxone Sodium",
         "Antibiotics", "VIAL", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "70.00", 70),
        ("MONTEK-10", "Montelukast 10mg", "Montelukast Sodium", "Respiratory",
         "STRIP", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "140.00", 100),
        ("SALBU-INH", "Salbutamol Inhaler", "Salbutamol Sulphate",
         "Respiratory", "EA", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.AMBIENT, 12, "190.00", 60),
        ("INS-HUM", "Human Insulin 40IU", "Human Insulin", "Cold Chain",
         "VIAL", TrackingMode.LOT_EXPIRY, DrugSchedule.H,
         StorageCondition.COLD_CHAIN, 5, "195.00", 45),
        ("COUGH-100", "Cough Syrup 100ml", "Dextromethorphan Hydrobromide",
         "Respiratory", "EA", TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "110.00", 120),
        ("ANTACID-200", "Antacid Suspension 200ml",
         "Magnesium Hydroxide + Simethicone", "Gastrointestinal", "EA",
         TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "132.00", 100),
        ("CALCI-D3", "Calcium + Vitamin D3", "Calcium Carbonate + Cholecalciferol",
         "Vitamins & Supplements", "STRIP", TrackingMode.LOT_EXPIRY,
         DrugSchedule.OTC, StorageCondition.AMBIENT, 12, "125.00", 140),
        ("FEFOL", "Iron + Folic Acid", "Ferrous Fumarate + Folic Acid",
         "Vitamins & Supplements", "STRIP", TrackingMode.LOT_EXPIRY,
         DrugSchedule.OTC, StorageCondition.AMBIENT, 12, "55.00", 150),
        ("DICLO-GEL", "Diclofenac Gel 30g", "Diclofenac Diethylamine",
         "Analgesics", "EA", TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "86.00", 90),
        ("BETADINE-100", "Povidone Iodine 100ml", "Povidone Iodine",
         "Consumables & Devices", "EA", TrackingMode.LOT_EXPIRY,
         DrugSchedule.OTC, StorageCondition.AMBIENT, 12, "100.00", 80),
        ("COT-500", "Absorbent Cotton 500g", None, "Consumables & Devices",
         "EA", TrackingMode.NONE, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "195.00", 60),
        ("GLOVES-M", "Surgical Gloves Medium", None, "Consumables & Devices",
         "EA", TrackingMode.NONE, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "350.00", 80),
        ("N95-MASK", "N95 Mask", None, "Consumables & Devices",
         "EA", TrackingMode.NONE, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 5, "310.00", 120),
        ("THERMO-DIG", "Digital Thermometer", None, "Consumables & Devices",
         "EA", TrackingMode.NONE, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 18, "200.00", 50),
        ("GLUCO-STRIP", "Glucometer Strips", None, "Consumables & Devices",
         "EA", TrackingMode.LOT_EXPIRY, DrugSchedule.OTC,
         StorageCondition.AMBIENT, 12, "750.00", 40),
    ]

    products: dict[str, Product] = {
        p.sku: p for p in db.scalars(select(Product)).all()
    }
    for (sku, name, comp, cat, uom, mode, sched, storage, gst, mrp,
         reorder) in product_defs:
        if sku in products:
            continue
        product = Product(
            sku=sku,
            name=name,
            composition=comp,
            manufacturer="Generic Pharma Ltd",
            pack_size="10 units" if uom == "STRIP" else "1 unit",
            category_id=cats[cat].id,
            uom_id=uoms[uom].id,
            tracking_mode=mode,
            drug_schedule=sched,
            storage_condition=storage,
            is_prescription_required=sched in (DrugSchedule.H, DrugSchedule.H1,
                                               DrugSchedule.X),
            hsn_code="30049099",
            gst_rate=Decimal(gst),
            mrp=Decimal(mrp),
            reorder_point=Decimal(reorder),
            barcode=f"890{_digits(sku) % 10_000_000_000:010d}",
            sourcing_policy=_sourcing_for(storage, sched),
        )
        db.add(product)
        products[sku] = product
    db.flush()

    # Link every product to a preferred distributor.
    #
    # Per product, not all-or-nothing. The guard here used to be "does any
    # ProductSupplier row exist", which is true the moment the first seed runs
    # — so every product added to the catalogue afterwards had no distributor
    # behind it forever. That is not cosmetic: invoice matching narrows its
    # candidates to the chosen supplier's products, so an unlinked product is
    # one a scanned line can never be matched to, and the reader reports it as
    # a name it could not find while the product sits in the catalogue.
    linked = set(db.scalars(select(ProductSupplier.product_id)).all())
    distributors = list(suppliers.values())
    if distributors:
        for i, product in enumerate(products.values()):
            if product.id in linked:
                continue
            supplier = distributors[i % len(distributors)]
            db.add(
                ProductSupplier(
                    product_id=product.id,
                    supplier_id=supplier.id,
                    unit_cost=(product.mrp or Decimal("50")) * Decimal("0.7"),
                    lead_time_days=[5, 9, 12][i % 3],
                    moq=Decimal("10"),
                    pack_qty=Decimal("10"),
                    is_preferred=True,
                )
            )
    db.flush()

    # --- Opening stock ------------------------------------------------------
    admin = db.scalar(select(User).where(User.email == "admin@pharmacy.co.in"))
    if not db.scalar(select(Lot)):
        _seed_opening_stock(db, products, warehouses, admin.id)

    print(f"  products: {len(products)}   warehouses: {len(warehouses)}")


def _seed_opening_stock(db, products, warehouses, user_id: int) -> None:
    """Opening balances with a realistic spread of expiry dates.

    Deliberately includes near-expiry and already-expired batches so FEFO,
    expiry alerts and write-off flows all have something to act on.
    """
    central = warehouses["CW-MUM"]
    cold_bin = db.scalar(
        select(Bin).where(Bin.warehouse_id == central.id, Bin.is_cold_chain.is_(True))
    )
    normal_bin = db.scalar(
        select(Bin).where(
            Bin.warehouse_id == central.id,
            Bin.is_cold_chain.is_(False),
            Bin.is_quarantine.is_(False),
        )
    )

    today = clock.today()
    # Three batches per tracked product: healthy, near-expiry, and long-dated.
    expiry_offsets = [420, 25, 730]
    quantities = [Decimal("500"), Decimal("120"), Decimal("300")]

    for sku, product in products.items():
        if product.tracking_mode == TrackingMode.NONE:
            ledger.post_movement(
                db,
                product_id=product.id,
                warehouse_id=central.id,
                quantity=Decimal("1000"),
                movement_type=MovementType.OPENING_BALANCE,
                user_id=user_id,
                bin_id=normal_bin.id,
                unit_cost=(product.mrp or Decimal("10")) * Decimal("0.7"),
                notes="Opening balance",
            )
            for branch_code in ("BR-AND", "BR-BAN", "BR-PUN", "BR-AHM"):
                branch = warehouses[branch_code]
                shelf = db.scalar(
                    select(Bin).where(
                        Bin.warehouse_id == branch.id,
                        Bin.is_cold_chain.is_(False),
                    )
                )
                ledger.post_movement(
                    db,
                    product_id=product.id,
                    warehouse_id=branch.id,
                    quantity=Decimal("150"),
                    movement_type=MovementType.OPENING_BALANCE,
                    user_id=user_id,
                    bin_id=shelf.id if shelf else None,
                    unit_cost=(product.mrp or Decimal("10")) * Decimal("0.7"),
                    notes="Opening balance",
                )
            continue

        target_bin = (
            cold_bin
            if product.storage_condition == StorageCondition.COLD_CHAIN
            else normal_bin
        )
        cost = (product.mrp or Decimal("50")) * Decimal("0.7")
        lots: list[Lot] = []

        for idx, (offset, qty) in enumerate(zip(expiry_offsets, quantities, strict=True), start=1):
            lot = Lot(
                product_id=product.id,
                lot_code=_batch_code(sku, idx),
                mfg_date=today - timedelta(days=120),
                expiry_date=today + timedelta(days=offset),
            )
            db.add(lot)
            db.flush()
            lots.append(lot)

            ledger.post_movement(
                db,
                product_id=product.id,
                warehouse_id=central.id,
                quantity=qty,
                movement_type=MovementType.OPENING_BALANCE,
                user_id=user_id,
                bin_id=target_bin.id,
                lot_id=lot.id,
                unit_cost=cost,
                notes="Opening balance",
            )

        # Give each branch working stock too, so a branch-scoped staff user
        # does not land on an empty dashboard. Branches get the near-expiry
        # batch as well, which is what makes FEFO visible at branch level.
        for branch_code in ("BR-AND", "BR-BAN", "BR-PUN", "BR-AHM"):
            branch = warehouses[branch_code]
            branch_bin = db.scalar(
                select(Bin).where(
                    Bin.warehouse_id == branch.id,
                    Bin.is_cold_chain.is_(
                        product.storage_condition == StorageCondition.COLD_CHAIN
                    ),
                )
            )
            if branch_bin is None:
                continue
            for lot, qty in zip(lots[:2], (Decimal("60"), Decimal("25")), strict=True):
                ledger.post_movement(
                    db,
                    product_id=product.id,
                    warehouse_id=branch.id,
                    quantity=qty,
                    movement_type=MovementType.OPENING_BALANCE,
                    user_id=user_id,
                    bin_id=branch_bin.id,
                    lot_id=lot.id,
                    unit_cost=cost,
                    notes="Opening balance",
                )


def _sync_catalogue(db: Session) -> None:
    """Bring an already-seeded database's catalogue up to this commit.

    Deliberately a hand-run command and not part of the deploy. The catalogue
    is fixture data — invented products, invented distributors — and a seeder
    that reaches into an existing installation to add stock items is one that
    will eventually put an invented product on a real order. Somebody has to
    decide to run this.

    But a demo server is exactly the installation where it needs running.
    Production was seeded on 3 August; the day after, the catalogue grew from
    twelve products chosen to exercise the system into the fifty-odd a
    distributor's invoice actually lists. `seed()` never runs twice, so the
    server kept the twelve — and a scanned invoice naming pantoprazole
    injection had nothing to match against, on a system whose whole point is
    matching scanned invoices.

    Safe on any database: every step in `seed()` adds what is absent and
    leaves what is present alone. Users are matched by email, bins and opening
    stock are skipped once they exist, and nothing is ever updated — a price
    someone corrected by hand stays corrected.
    """
    before = db.scalar(select(func.count()).select_from(Product)) or 0
    links_before = db.scalar(select(func.count()).select_from(ProductSupplier)) or 0

    seed(db)
    db.commit()

    after = db.scalar(select(func.count()).select_from(Product)) or 0
    links_after = db.scalar(select(func.count()).select_from(ProductSupplier)) or 0
    print(
        f"Catalogue synced. products {before} -> {after} "
        f"(+{after - before}), distributor links {links_before} -> {links_after} "
        f"(+{links_after - links_before})."
    )
    if after == before and links_after == links_before:
        print("Nothing was missing — this database was already up to date.")


def main() -> None:
    reset_passwords = "--reset-passwords" in sys.argv
    sync_catalogue = "--sync-catalogue" in sys.argv
    _check_password()
    db = SessionLocal()
    try:
        # Before the early return, and deliberately: this is the work that has
        # to reach an installation that already exists. Permissions and roles
        # sat below it for months, so the server's authorisation rows were
        # whatever they had been on the day it was provisioned, and any new
        # permission would have deployed an endpoint that refused everybody.
        # See converge() for why the two categories are named apart.
        summary = converge(db)
        db.commit()

        # The fixture, by contrast, is not idempotent — it would collide on
        # unique constraints and double every opening balance. `docker compose
        # up` runs this on every start, so an already-populated database is a
        # no-op, not a crash. Use `db.sh reset` (or drop the volume) to reseed.
        if db.scalar(select(User.id).limit(1)) is not None:
            if sync_catalogue:
                _sync_catalogue(db)
                return
            print(f"Database already seeded — skipping. ({summary})")
            if reset_passwords:
                n = reset_demo_passwords(db)
                db.commit()
                print(
                    f"Reset {n} demo account(s) to the current SEED_PASSWORD."
                    if n
                    else "Demo accounts already match SEED_PASSWORD — nothing to do."
                )
            else:
                _warn_if_password_drifted(db)
            return

        print("Seeding pharmacy inventory...")
        seed(db)
        db.commit()
        print(f"\nDone. Sign in with any of these (password: {DEV_PASSWORD})")
        print("  admin@pharmacy.co.in      - full access")
        print("  manager@pharmacy.co.in    - approvals, costs, AI")
        print("  staff@pharmacy.co.in      - Andheri branch, no costs")
        print("  customer@cityhospital.co.in   - own orders only")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
