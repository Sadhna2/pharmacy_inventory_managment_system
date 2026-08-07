# Entity–Relationship Diagram

42 tables and 2 views, 92 foreign keys. Drawing all of them at once produces
something nobody can read, so they are shown here in five groups that match
how the schema is actually used. Every relationship below was read from
`information_schema` on a live database, not from memory.

---

## 1. Identity, roles and audit

```mermaid
erDiagram
    roles ||--o{ users : "assigned to"
    roles ||--o{ role_permissions : grants
    permissions ||--o{ role_permissions : "granted by"
    users ||--o{ refresh_tokens : holds
    users ||--o{ audit_logs : "acted"
    warehouses ||--o{ users : "scoped to"

    roles {
        int id PK
        string code UK "ADMIN MANAGER STAFF CUSTOMER"
        string name
    }
    permissions {
        int id PK
        string code UK "e.g. grn.create"
        string description
    }
    role_permissions {
        int role_id FK
        int permission_id FK
    }
    users {
        int id PK
        string email UK
        string password_hash
        int role_id FK
        int warehouse_id FK "null = all branches"
        bool is_active
    }
    refresh_tokens {
        int id PK
        int user_id FK
        string token_hash
        datetime revoked_at "null = live"
    }
    audit_logs {
        int id PK
        int actor_user_id FK
        string action
        string entity_type
        json before_json
        json after_json
        datetime created_at
    }
```

`users.warehouse_id` is the whole of branch scoping: `NULL` means every
branch, a value means that one. `audit_logs` is **append-only** — the same
`reject_mutation()` trigger that protects the ledger rejects `UPDATE` and
`DELETE` here.

---

## 2. Catalogue and master data

```mermaid
erDiagram
    categories ||--o{ categories : "parent of"
    categories ||--o{ products : classifies
    uoms ||--o{ products : "measured in"
    products ||--o{ product_suppliers : "sourced via"
    suppliers ||--o{ product_suppliers : supplies
    suppliers ||--o{ lots : "manufactured by"
    products ||--o{ lots : "batched as"
    warehouses ||--o{ bins : contains

    products {
        int id PK
        string sku UK
        string name
        int category_id FK
        int uom_id FK
        string schedule "H H1 X G OTC"
        string storage "AMBIENT COLD_CHAIN COOL FROZEN"
        decimal mrp
        string hsn_code
        decimal gst_rate
        bool is_active
    }
    lots {
        int id PK
        int product_id FK
        int supplier_id FK
        string lot_code
        date expiry_date
        decimal mrp "printed on THIS batch"
        decimal purchase_cost
    }
    product_suppliers {
        int product_id FK
        int supplier_id FK
        string supplier_sku "learned invoice alias"
        bool is_preferred
    }
    suppliers {
        int id PK
        string name
        string gstin
        string state_code
    }
    warehouses {
        int id PK
        string name
        string state_code "drives IGST vs CGST+SGST"
    }
```

Two details that matter:

- **`lots.mrp` is per batch, not per product.** MRP is a legal ceiling printed
  on the pack, so a price rise on a new carton must not reprice older stock
  still on the shelf.
- **`product_suppliers.supplier_sku`** is where invoice intake remembers what a
  distributor calls one of our products. Teach it once, and every later invoice
  from that distributor matches exactly.

---

## 3. The stock ledger

```mermaid
erDiagram
    products ||--o{ stock_movements : moves
    warehouses ||--o{ stock_movements : "at"
    lots ||--o{ stock_movements : "of batch"
    bins ||--o{ stock_movements : "in"
    users ||--o{ stock_movements : "posted by"
    serials ||--o{ stock_movements : "tracks"

    products ||--o{ stock_balances : "held as"
    warehouses ||--o{ stock_balances : "at"
    lots ||--o{ stock_balances : "of batch"

    sales_order_lines ||--o{ stock_reservations : reserves
    lots ||--o{ stock_reservations : "of batch"
    lots ||--o{ recalls : "subject of"

    stock_movements {
        int id PK "APPEND ONLY"
        int product_id FK
        int warehouse_id FK
        int lot_id FK
        decimal quantity "signed"
        string movement_type
        int reverses_id "correction, never delete"
        datetime occurred_at
        int created_by FK
    }
    stock_balances {
        int product_id FK
        int warehouse_id FK
        int lot_id FK
        decimal quantity "PROJECTION - rebuildable"
        string status "AVAILABLE QUARANTINE DAMAGED"
    }
    stock_reservations {
        int id PK
        int sales_order_line_id FK
        int lot_id FK
        decimal quantity
        string status "ACTIVE RELEASED CONSUMED"
    }
    recalls {
        int id PK
        int lot_id FK
        int initiated_by FK
        string status
    }
```

> `stock_movements` is the only source of truth. `stock_balances` is a
> projection kept for query speed and can be rebuilt from the ledger at any
> time — a test asserts the two agree.

Corrections are **reversing entries**: `reverses_id` points at the original
row, which is never removed.

---

## 4. Operations documents

```mermaid
erDiagram
    suppliers ||--o{ purchase_orders : "ordered from"
    warehouses ||--o{ purchase_orders : "delivered to"
    purchase_orders ||--o{ purchase_order_lines : contains
    purchase_orders ||--o{ goods_receipts : "fulfilled by"
    goods_receipts ||--o{ goods_receipt_lines : contains
    purchase_order_lines ||--o{ goods_receipt_lines : "received against"
    lots ||--o{ goods_receipt_lines : "creates batch"

    customers ||--o{ sales_orders : places
    sales_orders ||--o{ sales_order_lines : contains
    sales_orders ||--o{ shipments : "shipped as"
    shipments ||--o{ shipment_lines : contains

    stock_transfers ||--o{ stock_transfer_lines : contains
    stock_adjustments ||--o{ stock_adjustment_lines : contains

    purchase_orders {
        int id PK
        string po_number UK
        int supplier_id FK
        int warehouse_id FK
        int created_by FK
        int approved_by FK "must differ from created_by"
        bool is_interstate "IGST vs CGST+SGST"
        string status
        decimal grand_total
    }
    goods_receipts {
        int id PK
        string grn_number UK
        int purchase_order_id FK "null = unordered delivery"
        int warehouse_id FK
        int received_by FK
        string supplier_invoice_no
    }
    sales_orders {
        int id PK
        string so_number UK
        int customer_id FK
        int warehouse_id FK
        string status
    }
    stock_transfers {
        int id PK
        int from_warehouse_id FK
        int to_warehouse_id FK
        int created_by FK
        int approved_by FK
        string status "DRAFT IN_TRANSIT COMPLETED"
    }
    stock_adjustments {
        int id PK
        int warehouse_id FK
        int created_by FK
        int approved_by FK "second person required"
        string status
    }
```

**Separation of duties is structural**, not a convention: `created_by` and
`approved_by` are distinct columns and the service refuses when they match.

A transfer is two ledger postings, not one — stock leaves the source, sits in
`IN_TRANSIT`, and arrives at the destination — so units are never invented or
lost in the gap.

---

## 5. AI-generated artefacts

```mermaid
erDiagram
    forecast_runs ||--o{ forecasts : produced
    forecast_runs ||--o{ forecast_accuracy : "backtested as"
    forecast_runs ||--o{ reorder_suggestions : informed
    products ||--o{ forecasts : "for"
    warehouses ||--o{ forecasts : "at"
    products ||--o{ reorder_suggestions : "for"
    suppliers ||--o{ reorder_suggestions : "from"
    purchase_orders ||--o{ reorder_suggestions : "became"
    users ||--o{ reorder_suggestions : "decided by"

    forecast_runs {
        int id PK
        datetime created_at
        int horizon_days
        string ledger_high_water "cache key"
    }
    forecasts {
        int id PK
        int run_id FK
        int product_id FK
        int warehouse_id FK
        date target_date
        decimal predicted_qty
        string method "winner, named"
    }
    forecast_accuracy {
        int id PK
        int run_id FK
        int product_id FK
        decimal mape
        string method
        bool beat_baseline "vs seasonal-naive"
    }
    reorder_suggestions {
        int id PK
        int run_id FK
        int product_id FK
        int supplier_id FK
        decimal suggested_qty
        decimal reorder_point
        decimal safety_stock
        int purchase_order_id FK "if acted on"
        int decided_by FK
    }
```

These tables are **derived artefacts**, not business records. Every one is
recomputed from `stock_movements`; none feeds back into stock. `forecasts`
records which method won *and* that it beat the seasonal-naive baseline — a
series that could not beat "the same weekday last week" says so rather than
quietly using the fancier model.

⚠️ **All four are drawn here and all four are empty in practice.** Nothing in
the application writes a row to any of them: the figures are computed from the
ledger when a screen asks for them and are never persisted. They are modelled
because the shape is part of the design, and the distinction between *present
in the schema* and *populated* matters more here than anywhere else in this
document — a query against them is valid SQL that returns nothing, and "no
rows" reads as *there is nothing to reorder*. Ask is told this explicitly, and
declines such a question in words rather than answering it with an empty table.

**Neither AI feature stores anything here.** Invoice intake's only persistent
trace is the learned alias on `product_suppliers.supplier_sku` and a row in
`audit_logs`; the extraction itself is never saved — it becomes a form, and the
form becomes a goods receipt only if a person submits it. Ask writes nothing at
all: it holds one read-only connection, and its answers live only in the
browser tab that asked.

---

## Views

| View | Purpose |
|---|---|
| `v_stock_by_warehouse` | Balances rolled up per product per branch |
| `v_expiring_stock` | Batches by remaining shelf life, for the expiry screens |

## Supporting tables

`app_settings` and `feature_flags` (administrator-tunable thresholds and the
per-capability off switches, both audited), `document_sequences` (gap-free
document numbering), `calendar_days` (business-day arithmetic), `jobs`
(background job records), `serials` (unit-level tracking where a batch is not
granular enough).
