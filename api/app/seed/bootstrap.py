"""Bootstrap seed: roles, permissions, users, and a small pharmacy fixture.

This is the DEV seed — enough to log in and exercise every Layer 0/1 flow.
The full 2-year synthetic history for forecasting is Layer 2 work (§15).

    python -m app.seed.bootstrap
"""

import os
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
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
from app.core.security import hash_password
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


def _check_password() -> None:
    """Refuse to create well-known accounts on anything but a dev machine."""
    if DEV_PASSWORD == _DEFAULT_DEV_PASSWORD and settings.env != "development":
        raise SystemExit(
            "Refusing to seed demo accounts with the default password while "
            f"ENV={settings.env}. Set SEED_PASSWORD to something private first."
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
        "receipt, for a human to check line by line. Designed, not built.",
        False,
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
    perms = sync_permissions(db)
    roles = sync_roles(db, perms)
    print(f"  permissions: {len(perms)}   roles: {len(roles)}")

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
    user_defs = [
        ("admin@pharmacy.co.in", "Priya Nair", ADMIN, None),
        ("manager@pharmacy.co.in", "Rahul Desai", MANAGER, None),
        ("staff@pharmacy.co.in", "Anjali Rao", STAFF, "BR-AND"),
        ("customer@cityhospital.co.in", "City Hospital", CUSTOMER, None),
    ]
    for email, full_name, role_code, wh_code in user_defs:
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
            barcode=f"890{abs(hash(sku)) % 10_000_000_000:010d}",
            sourcing_policy=_sourcing_for(storage, sched),
        )
        db.add(product)
        products[sku] = product
    db.flush()

    # Link every product to a preferred distributor.
    if not db.scalar(select(ProductSupplier)):
        distributors = list(suppliers.values())
        for i, product in enumerate(products.values()):
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
                lot_code=f"{sku}-B{idx}",
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


def main() -> None:
    _check_password()
    db = SessionLocal()
    try:
        # Seeding is not idempotent — it would collide on unique constraints
        # and double every opening balance. `docker compose up` runs this on
        # every start, so an already-populated database is a no-op, not a
        # crash. Use `db.sh reset` (or drop the volume) to reseed.
        # Feature flags sync on every run, unlike the fixture below. New
        # capabilities have to reach an existing installation without anyone
        # dropping their database to get a menu item.
        flags = sync_feature_flags(db)
        db.commit()

        if db.scalar(select(User.id).limit(1)) is not None:
            print(f"Database already seeded — skipping. ({flags} feature flags synced)")
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
