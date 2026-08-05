# Software Requirements Specification

**Project** — Multi-branch pharmacy chain inventory management
**Version** — 1.0
**Status** — Implemented; see §9 for what was deliberately left out

---

## 1. Introduction

### 1.1 Purpose

This document specifies the requirements for an inventory management system
for a pharmacy chain operating several branches from a central warehouse. It
covers the stock ledger, the operational documents that move stock, the access
control around them, and an analysis layer that includes one generative-AI
feature.

### 1.2 Scope

The system tracks pharmaceutical stock by **batch and expiry** across multiple
locations, enforces **Indian GST** treatment on purchase and sale, applies
**FEFO** (first-expired-first-out) allocation, and maintains an **append-only
ledger** from which every quantity is derived.

It does **not** handle payments, billing to patients, e-way bills, or
prescription dispensing. Those are named explicitly in §9 so that their absence
is a decision rather than an oversight.

### 1.3 Definitions

| Term | Meaning |
|---|---|
| **Lot / batch** | A manufactured run with one code and one expiry date |
| **FEFO** | First-Expired-First-Out — allocate the batch expiring soonest |
| **GRN** | Goods Receipt Note — record of what physically arrived |
| **MRP** | Maximum Retail Price, printed per pack; a legal ceiling |
| **GSTIN** | 15-character GST identifier; the 15th is a mod-36 checksum |
| **IGST / CGST+SGST** | Interstate vs intrastate GST split |
| **Ledger** | `stock_movements`; append-only, the sole source of stock truth |
| **Schedule H / H1 / X** | Indian drug classes with dispensing restrictions |

### 1.4 Assumptions

- One legal entity, one GST state of registration per warehouse.
- All amounts in INR; all business dates in Asia/Kolkata.
- Data is synthetic; no real patient or prescription data is handled.
- Single-instance deployment on AWS free tier (2 GB RAM).

### 1.5 Constraints

| Constraint | Consequence |
|---|---|
| 2 GB RAM, one instance | No broker, no managed DB; images built off-box |
| 10 days, 5 people | Layered so work parallelises without cross-stack conflicts |
| AI use is mandatory | One generative feature, deliberately bounded (§4.5) |
| Synthetic data only | Fixed RNG seed so every environment holds identical rows |

---

## 2. Overall description

### 2.1 Product perspective

A self-contained web application: React SPA, FastAPI backend, PostgreSQL
database, served behind Caddy. The only outbound dependency is a Google Gemini
call used solely for reading invoice photographs, and the system runs fully
without it.

### 2.2 User classes

| Role | Description | Notable limits |
|---|---|---|
| **Administrator** | Full access | Everything, including settings and users |
| **Manager** | Runs purchasing and approvals | Sees cost; cannot change system settings |
| **Pharmacy Staff** | Records stock all day | **Cannot see cost**; scoped to one branch; approves nothing |
| **Customer** | Institutional buyer | Own orders only |

### 2.3 Operating environment

Any modern browser. Server: Linux, Docker, PostgreSQL 16. Deployed on an ARM64
EC2 instance; developed on macOS, Windows and Linux.

---

## 3. Functional requirements

Requirements are numbered `FR-<area>-<n>`. Each is implemented and covered by
tests unless marked otherwise.

### 3.1 Identity and access (FR-AUTH)

| ID | Requirement |
|---|---|
| FR-AUTH-1 | Users authenticate with email and password; passwords stored only as hashes |
| FR-AUTH-2 | Sessions use short-lived JWT access tokens with refresh tokens |
| FR-AUTH-3 | Refresh tokens are revocable; revocation is immediate |
| FR-AUTH-4 | Every endpoint declares a required permission |
| FR-AUTH-5 | Permissions are granted to roles, not to users |
| FR-AUTH-6 | A branch-scoped user cannot read or write another branch's stock, **including via a payload naming that branch** |
| FR-AUTH-7 | Staff cannot see cost prices anywhere in the API or interface |

### 3.2 Master data (FR-MD)

| ID | Requirement |
|---|---|
| FR-MD-1 | Maintain products with SKU, category, unit of measure, HSN code, GST rate, drug schedule and storage class |
| FR-MD-2 | Maintain suppliers with GSTIN and state code |
| FR-MD-3 | Maintain customers, warehouses and bins |
| FR-MD-4 | Categories are hierarchical |
| FR-MD-5 | Records are retired, never deleted, once referenced |
| FR-MD-6 | Record which suppliers supply which products, and each supplier's own code for them |

### 3.3 Stock (FR-STK)

| ID | Requirement |
|---|---|
| FR-STK-1 | **All stock quantities derive from an append-only ledger** |
| FR-STK-2 | The ledger rejects `UPDATE` and `DELETE` at the database level |
| FR-STK-3 | Corrections are reversing entries; the original row survives |
| FR-STK-4 | Stock is tracked per product, per warehouse, per batch |
| FR-STK-5 | Every batch carries its own expiry date and its own printed MRP |
| FR-STK-6 | Allocation follows FEFO, respecting a minimum-shelf-life floor |
| FR-STK-7 | Stock cannot go negative; the attempt is refused with a conflict |
| FR-STK-8 | Concurrent allocation of the same batch is serialised by row locks |
| FR-STK-9 | Balances are a projection, rebuildable from the ledger at any time |
| FR-STK-10 | Stock has status: available, quarantine, damaged, in-transit, returned |

### 3.4 Operations (FR-OPS)

| ID | Requirement |
|---|---|
| FR-OPS-1 | Raise purchase orders against suppliers, with GST computed by state pair |
| FR-OPS-2 | **An order must be approved by someone other than its creator** |
| FR-OPS-3 | Receive goods against an order, or as an unordered delivery |
| FR-OPS-4 | A receipt may not be booked to a warehouse the order was not for |
| FR-OPS-5 | Receiving creates batches and posts ledger entries |
| FR-OPS-6 | Receipts may be held for QC, landing in quarantine rather than available stock |
| FR-OPS-7 | Sales orders reserve stock; shipment consumes the reservation |
| FR-OPS-8 | Transfers between branches move through an in-transit state |
| FR-OPS-9 | Adjustments require a second approver |
| FR-OPS-10 | A batch can be recalled: stock freezes and every movement of it is traceable |
| FR-OPS-11 | Document numbers are gap-free per series |

### 3.5 GST (FR-GST)

| ID | Requirement |
|---|---|
| FR-GST-1 | Interstate transactions attract IGST; intrastate attract CGST + SGST |
| FR-GST-2 | The split is derived from supplier and warehouse state codes, not entered |
| FR-GST-3 | Tax is computed per line and rounded per document |
| FR-GST-4 | GSTIN is validated including its mod-36 check character |

### 3.6 Analysis (FR-AI)

| ID | Requirement |
|---|---|
| FR-AI-1 | Forecast demand per product per branch from ledger history |
| FR-AI-2 | **Every forecast is backtested against a seasonal-naive baseline, and the winning method is named** |
| FR-AI-3 | Recommend reorder quantities from reorder point and safety stock, showing the workings |
| FR-AI-4 | A recommendation may raise a **DRAFT** order only; approval remains human |
| FR-AI-5 | Detect exceptions — after-hours movement, shrinkage, damage clusters, count variance — against each location's own normal |
| FR-AI-6 | Every exception links to the ledger rows it was computed from |
| FR-AI-7 | Measure supplier lead times as distributions, never quoted figures |
| FR-AI-8 | Read a photographed supplier invoice into a **draft** goods receipt |
| FR-AI-9 | **No analysis output writes to the stock ledger** |
| FR-AI-10 | Each capability has an administrator switch **enforced server-side** |

### 3.7 Invoice intake, specifically (FR-OCR)

| ID | Requirement |
|---|---|
| FR-OCR-1 | Accept a photograph or PDF of a distributor invoice |
| FR-OCR-2 | Extract header, line items, batch codes, expiries, quantities, rates and tax |
| FR-OCR-3 | **Verify every extraction against the invoice's own arithmetic** — quantity × rate = line amount, lines sum to subtotal, tax consistent with totals |
| FR-OCR-4 | Verify the supplier GSTIN checksum |
| FR-OCR-5 | Match printed product names to the catalogue under the same strength and dosage-form rules an ordinary lookup obeys |
| FR-OCR-6 | Report what could not be resolved, with a shortlist, rather than guessing |
| FR-OCR-7 | Grade findings by blast radius: BLOCK only where wrong stock could reach a shelf |
| FR-OCR-8 | **The endpoint creates nothing** — no stock, no document, no ledger row |
| FR-OCR-9 | Remember a distributor's own name for a product, so later invoices match exactly |
| FR-OCR-10 | Operate from recorded fixtures with no network, for demonstration and tests |
| FR-OCR-11 | Without an API key, answer 503 and leave the rest of the system unaffected |

### 3.8 Audit (FR-AUD)

| ID | Requirement |
|---|---|
| FR-AUD-1 | Every mutation records actor, action, entity, before and after |
| FR-AUD-2 | The audit log is append-only, enforced by database trigger |
| FR-AUD-3 | Settings and feature-flag changes are audited with before/after values |

---

## 4. Non-functional requirements

### 4.1 Security

| ID | Requirement |
|---|---|
| NFR-SEC-1 | Passwords hashed; never logged, never returned |
| NFR-SEC-2 | The database publishes no port outside the container network |
| NFR-SEC-3 | Authorisation is checked server-side on every request; the interface only hides what the server already refuses |
| NFR-SEC-4 | Feature switches close routes, not merely menu items |
| NFR-SEC-5 | No secret is committed; `.env` is gitignored and `.env.example` holds only publishable local values |
| NFR-SEC-6 | Uploaded files are size- and type-checked before being read into memory |

### 4.2 Reliability

| ID | Requirement |
|---|---|
| NFR-REL-1 | Migrations must complete before the API accepts traffic |
| NFR-REL-2 | Ledger invariants hold under concurrent writes |
| NFR-REL-3 | Failure of the AI layer must not affect stock operations |
| NFR-REL-4 | The system runs without an AI API key, degraded but not broken |

### 4.3 Performance

| ID | Requirement |
|---|---|
| NFR-PERF-1 | List screens respond in under 500 ms over two years of history (~53,000 ledger rows) |
| NFR-PERF-2 | Forecast fitting is warmed off the request path at startup |
| NFR-PERF-3 | Each service runs within an explicit memory ceiling totalling under 2 GB |

### 4.4 Portability

| ID | Requirement |
|---|---|
| NFR-PORT-1 | `docker compose up` brings up the whole system on macOS, Windows or Linux with Docker as the only prerequisite |
| NFR-PORT-2 | Line endings are pinned so a Windows checkout cannot break Linux containers |
| NFR-PORT-3 | Nothing is installed globally; the native path keeps Postgres and Python inside the repository |

### 4.5 Maintainability

| ID | Requirement |
|---|---|
| NFR-MNT-1 | The analysis layer is deletable without affecting Layers 0 and 1 |
| NFR-MNT-2 | Browser types are generated from the live OpenAPI document, and CI fails if they drift |
| NFR-MNT-3 | Lint and type checks run in CI on every push |

---

## 5. Use cases

### UC-1 — Receive a delivery by photographing its invoice

**Actor** Manager or Staff (`grn.create`)

1. Opens Purchasing → Receive goods.
2. Optionally names the purchase order, which narrows matching.
3. Photographs the distributor's invoice.
4. System extracts the document, checks its arithmetic and GSTIN checksum,
   matches each line to the catalogue, and returns a **draft** with findings.
5. User resolves anything flagged — an unreadable batch code, an ambiguous
   product, an expiry that predates the invoice.
6. User submits. **Only now** does stock exist.

**Alternate** — no API key, or scanning switched off: the form is filled in by
hand exactly as before.

**Guarantee** — nothing in steps 3–5 writes to the ledger.

### UC-2 — Order stock that is running out

**Actor** Manager (`ai.view`, `po.create`), then a second approver

1. Opens Replenishment; sees products below reorder point with workings shown.
2. Accepts a recommendation, which raises a **DRAFT** purchase order.
3. **A different user** approves it.
4. Order is sent to the distributor.

### UC-3 — Ship an order to a hospital

**Actor** Staff (`so.fulfil`)

1. Sales order allocates stock **FEFO**, respecting shelf-life floor.
2. Allocation reserves batches; concurrent allocation is serialised.
3. Shipment consumes reservations and posts ledger entries.

### UC-4 — Recall a batch

**Actor** Manager (`recall.initiate`)

1. Names the batch; all stock of it freezes into quarantine.
2. System traces every movement of that batch across branches.
3. Recall is closed once resolved; the trace remains in the audit log.

### UC-5 — Correct a mistake

**Actor** Manager (`stock.adjust`)

1. Posts a reversing entry against the original movement.
2. The original row **remains**; the balance moves because a second row says so.
3. A second approver signs off the adjustment.

---

## 6. Interface requirements

- **API** — REST over HTTP, JSON, documented as OpenAPI 3 at `/docs`; 88
  operations across 64 paths.
- **Errors** — RFC 7807 problem details with a stable `type` per error class.
- **Web** — responsive; every destructive action confirms; every screen states
  what it is showing and over what period.

---

## 7. Data requirements

40 tables and 2 views; see [ER-DIAGRAM.md](ER-DIAGRAM.md). Retention is
indefinite — nothing is hard-deleted; records are retired.

---

## 8. Verification

| Method | Coverage |
|---|---|
| Automated tests | 284, run in CI on every push against a real PostgreSQL and a real HTTP server |
| Ledger invariants | Balances rebuilt from the ledger and compared |
| Concurrency | Parallel allocation of one batch asserted to serialise |
| RBAC | Each role asserted against permitted and forbidden endpoints |
| OCR accuracy | 50 generated invoices, ground truth written before rendering; 3,539 fields scored |
| Schema drift | CI fails if browser types diverge from the live OpenAPI document |

---

## 9. Out of scope

Named deliberately, so their absence is a decision:

- Payments, patient billing, insurance claims
- E-way bill generation and GST return filing
- Prescription capture and dispensing against a prescription
- Barcode hardware integration (barcodes are stored, not scanned by device)
- Multi-entity or multi-currency operation
- Natural-language reporting (`features.nl_reporting` — designed, not built)

---

## 10. Traceability

| Requirement group | Implementation | Tests |
|---|---|---|
| FR-AUTH | `app/core/deps.py`, `app/api/v1/auth.py` | `test_rbac.py` |
| FR-MD | `app/api/v1/masters.py`, `products.py` | `test_masters.py` |
| FR-STK | `app/services/ledger.py`, `app/api/v1/stock.py` | `test_e2e.py`, `test_ledger.py` |
| FR-OPS | `app/api/v1/operations.py` | `test_e2e.py` |
| FR-GST | `app/services/gst.py` | `test_gst.py` |
| FR-AI | `app/ai/` | `test_ai.py`, `test_settings.py` |
| FR-OCR | `app/ai/intake/` | `test_intake_router.py`, `test_intake_match.py` |
| FR-AUD | `app/services/audit.py` | `test_e2e.py`, `test_settings.py` |
