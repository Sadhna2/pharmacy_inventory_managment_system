"""Where each endpoint is used in the interface, and what it actually does.

WHY THIS IS A TABLE AND NOT 88 DOCSTRINGS
-----------------------------------------
A reader landing on `/docs` gets a correct list of endpoints and no sense of
the product: nothing says that `POST /ai/reorder/orders` is the button on the
Replenishment screen, or that `POST /stock/movements/{id}/reverse` is how a
correction is made without deleting anything. That context lives in the
frontend, so the API documentation never learned it.

It is declared here, in one place, rather than sprinkled through the route
docstrings, for two reasons. The mapping is a property of the *pair* — screen
and endpoint — so keeping it beside one of them is arbitrary; and a single
table can be checked for completeness by a test, which is what stops a new
endpoint from quietly shipping undocumented. `test_api_docs.py` fails if any
route is missing an entry.

Screen names are the sidebar's own labels, prefixed by their section, so
"Operations → Purchasing" is a path a reader can actually follow.
"""

from __future__ import annotations

from typing import Any

#: "METHOD /path" -> (where it is used, what it does)
#:
#: Paths are exactly as FastAPI registers them, including the `{param}` braces.
USED_BY: dict[str, tuple[str, str]] = {
    # ---------------------------------------------------------------- auth
    "POST /api/v1/auth/login": (
        "Login screen",
        "Exchanges email and password for a short-lived access token and a "
        "refresh cookie. The only unauthenticated write in the system.",
    ),
    "POST /api/v1/auth/refresh": (
        "Every screen, in the background",
        "Silently renews an expired access token from the refresh cookie. The "
        "browser retries the original request once this succeeds, so a session "
        "never expires mid-action.",
    ),
    "POST /api/v1/auth/logout": (
        "Sidebar → Sign out",
        "Revokes the refresh token server-side. Revocation is immediate rather "
        "than waiting for expiry.",
    ),
    "GET /api/v1/auth/me": (
        "Every screen, on load",
        "The signed-in user with the resolved permission codes the interface "
        "uses to decide which controls exist at all.",
    ),
    "POST /api/v1/auth/change-password": (
        "Account menu",
        "Changes your own password; requires the current one.",
    ),
    # ----------------------------------------------------------- dashboard
    "GET /api/v1/stock/summary": (
        "Overview → Dashboard",
        "The headline tiles: total stock value at cost, distinct products "
        "held, and counts by stock status. Computed from the balance "
        "projection, never stored.",
    ),
    "GET /api/v1/stock/expiring": (
        "Overview → Dashboard, and Inventory → Stock (expiry filter)",
        "Batches by remaining shelf life, bucketed into expired / this month / "
        "next 90 days. Drives the expiry warning tile.",
    ),
    # ----------------------------------------------------------- inventory
    "GET /api/v1/products": (
        "Inventory → Products; product pickers in every form",
        "Paginated catalogue with search, category, schedule and storage "
        "filters. `include_retired` brings back withdrawn lines.",
    ),
    "POST /api/v1/products": (
        "Inventory → Products → New product",
        "Creates a catalogue entry. Also reachable from a scanned invoice line "
        "the catalogue could not resolve.",
    ),
    "GET /api/v1/products/{product_id}": (
        "Inventory → Products → row",
        "One product with its suppliers, batches on hand and reorder settings.",
    ),
    "PATCH /api/v1/products/{product_id}": (
        "Inventory → Products → Edit",
        "Partial update. Changing GST rate or HSN affects future documents "
        "only — posted tax is never recomputed.",
    ),
    "DELETE /api/v1/products/{product_id}": (
        "Inventory → Products → Retire",
        "Retires rather than deletes. A product referenced by history cannot "
        "be removed without breaking the ledger's foreign keys.",
    ),
    "GET /api/v1/stock/balances": (
        "Inventory → Stock; batch pickers in Transfers and Adjustments",
        "On-hand quantity at the full grain — product, warehouse, bin, batch, "
        "status — with each batch's own expiry and printed MRP. A projection "
        "of the ledger, rebuildable from it at any time.",
    ),
    "GET /api/v1/stock/movements": (
        "Inventory → Movements",
        "The append-only ledger itself, newest first, filterable by product, "
        "warehouse, type and date. Every quantity anywhere in the product is "
        "derived from these rows.",
    ),
    "POST /api/v1/stock/movements": (
        "Inventory → Movements → New movement",
        "Posts a single ledger entry. Refuses anything that would drive a "
        "batch negative, and takes a row lock so concurrent postings against "
        "one batch serialise.",
    ),
    "POST /api/v1/stock/movements/{movement_id}/reverse": (
        "Inventory → Movements → Reverse",
        "Writes an equal and opposite entry. The original row is never "
        "deleted or edited — the database rejects both — so the mistake and "
        "its correction both stay on the record.",
    ),
    # ---------------------------------------------------------- purchasing
    "GET /api/v1/purchase-orders": (
        "Operations → Purchasing",
        "Orders to distributors with received-vs-ordered quantities per line, "
        "so 'partially received' carries the arithmetic behind it.",
    ),
    "POST /api/v1/purchase-orders": (
        "Operations → Purchasing → New order",
        "Raises a DRAFT order. GST treatment — IGST versus CGST+SGST — is "
        "derived from the supplier's and the warehouse's state codes, not "
        "entered by the user.",
    ),
    "GET /api/v1/purchase-orders/{po_id}": (
        "Operations → Purchasing → row; Receive goods",
        "One order with its lines, used to prefill a goods receipt with what "
        "the delivery is expected to contain.",
    ),
    "POST /api/v1/purchase-orders/{po_id}/submit": (
        "Operations → Purchasing → Submit",
        "Moves a DRAFT to PENDING_APPROVAL.",
    ),
    "POST /api/v1/purchase-orders/{po_id}/approve": (
        "Operations → Purchasing → Approve",
        "Authorises the order. Refuses when the approver is the creator — the "
        "check is on identity, so an administrator cannot bypass it either.",
    ),
    "POST /api/v1/purchase-orders/{po_id}/cancel": (
        "Operations → Purchasing → Cancel",
        "Marks the order CANCELLED. Refused once it is fully received, or "
        "already cancelled. A partially-received order can still be "
        "cancelled — that is the ordinary case of a distributor short-"
        "shipping and the remainder never coming.",
    ),
    "GET /api/v1/goods-receipts": (
        "Operations → Purchasing → Receive goods",
        "Past receipts, with the supplier invoice number each was booked "
        "against.",
    ),
    "POST /api/v1/goods-receipts": (
        "Operations → Purchasing → Receive goods → Receive into stock",
        "The only way inbound stock is created. Creates batches, posts ledger "
        "entries and advances each line's received quantity, closing the "
        "order to RECEIVED or PARTIALLY_RECEIVED from the arithmetic rather "
        "than from a flag. Receiving into a warehouse the order was not for "
        "is refused — that is nearly always the wrong order picked from the "
        "list, and it would book stock to a branch that never saw it while "
        "closing an order that was never delivered. A genuine redirection is "
        "the per-line `cross_dock_warehouse_id` instead. Per line, "
        "`quarantine` lands the stock in QUARANTINE rather than AVAILABLE — "
        "used for Schedule H1/X and cold-chain goods pending a check.",
    ),
    # --------------------------------------------------------------- sales
    "GET /api/v1/sales-orders": (
        "Operations → Sales",
        "Customer orders with allocation and shipment status.",
    ),
    "POST /api/v1/sales-orders": (
        "Operations → Sales → New order",
        "Raises an order. Nothing leaves stock yet — allocation is a separate, "
        "deliberate step.",
    ),
    "GET /api/v1/sales-orders/{so_id}": (
        "Operations → Sales → row",
        "One order with its lines, allocations and shipments.",
    ),
    "GET /api/v1/sales-orders/{so_id}/invoice": (
        "Operations → Sales → Print invoice",
        "The order rendered as a GST tax invoice: the line table, the HSN-wise "
        "summary the statute asks for at the foot, and the grand total spelled "
        "out in lakh and crore rather than millions. Which tax columns appear "
        "is decided once from the document's own interstate flag — IGST, or "
        "CGST+SGST — never per line, because a zero-rated line would otherwise "
        "flip the table halfway down. Every HSN printed is the one frozen onto "
        "the line when the order was priced, so a reprint still matches the "
        "copy the customer holds after the catalogue has been corrected. "
        "Returns HTML, not a PDF: every browser already paginates a long table "
        "for printing better than a server-side renderer would, and it saves a "
        "native dependency on a 2 GB box.",
    ),
    "POST /api/v1/sales-orders/{so_id}/allocate": (
        "Operations → Sales → Allocate",
        "Reserves batches **FEFO** — earliest expiry first — for "
        "expiry-tracked products, FIFO by receipt date for everything else, "
        "and skips anything inside the minimum shelf-life floor so stock too "
        "close to expiry is never sent to a customer. Takes `FOR UPDATE` row "
        "locks, so two people allocating the same batch at once serialise "
        "instead of both being told it is available.",
    ),
    "POST /api/v1/sales-orders/{so_id}/ship": (
        "Operations → Sales → Ship",
        "Consumes the reservations and posts the outbound ledger entries, "
        "recording which batch actually went to which customer — the record a "
        "recall later traces.",
    ),
    "POST /api/v1/sales-orders/{so_id}/cancel": (
        "Operations → Sales → Cancel",
        "Releases any active reservations back to available stock.",
    ),
    # ----------------------------------------------------------- transfers
    "GET /api/v1/transfers": (
        "Operations → Transfers",
        "Branch-to-branch movements, including what is currently in transit.",
    ),
    "POST /api/v1/transfers": (
        "Operations → Transfers → New transfer",
        "Raises a DRAFT transfer between two locations.",
    ),
    "POST /api/v1/transfers/{transfer_id}/approve": (
        "Operations → Transfers → Approve",
        "Moves the transfer to APPROVED, which dispatch then requires. Note "
        "that unlike purchase orders and adjustments, this does **not** "
        "currently refuse self-approval — a transfer moves stock between two "
        "of your own branches rather than out of the business, so nothing is "
        "lost if one person does both, but it is an inconsistency worth "
        "knowing about rather than assuming.",
    ),
    "POST /api/v1/transfers/{transfer_id}/cancel": (
        "Operations \u2192 Transfers \u2192 row menu \u2192 Cancel transfer",
        "Abandons a transfer that has not shipped, so a document that cannot "
        "be dispatched can leave the list. Draft, pending and approved can be "
        "cancelled; once IN_TRANSIT the goods are on a road and the answer is "
        "to receive them and transfer them back, which leaves both movements "
        "in the ledger. Posts nothing \u2014 a transfer holds no stock before "
        "dispatch.",
    ),
    "POST /api/v1/transfers/{transfer_id}/dispatch": (
        "Operations → Transfers → Dispatch",
        "Posts stock out of the source into IN_TRANSIT. Deliberately two "
        "postings rather than one, so units are never invented at the "
        "destination nor lost in the gap.",
    ),
    "POST /api/v1/transfers/{transfer_id}/receive": (
        "Operations → Transfers → Receive",
        "Lands the in-transit stock at the destination, preserving each "
        "batch's identity and expiry across the move.",
    ),
    # --------------------------------------------------------- adjustments
    "GET /api/v1/adjustments": (
        "Operations → Adjustments",
        "Stock corrections — counts, damage, write-offs — and their approval "
        "state.",
    ),
    "POST /api/v1/adjustments": (
        "Operations → Adjustments → New adjustment",
        "Raises a correction for approval. Nothing moves until a second person "
        "signs it off.",
    ),
    "POST /api/v1/adjustments/{adjustment_id}/approve": (
        "Operations → Adjustments → Approve",
        "Approves and posts the ledger entries. Refuses self-approval.",
    ),
    "POST /api/v1/adjustments/{adjustment_id}/cancel": (
        "Operations → Adjustments → row menu → Cancel adjustment",
        "Withdraws an adjustment that has not posted, so a document the "
        "approver will not pass can leave the queue. Only PENDING_APPROVAL "
        "can be cancelled — once approved the entry is in the ledger, which "
        "is corrected by a reversing movement and never by editing. The "
        "raiser may cancel their own: withdrawing a document moves no stock.",
    ),
    # -------------------------------------------------------- analysis: AI
    "GET /api/v1/ai/forecast": (
        "Analysis → Demand forecast",
        "Holt-Winters exponential smoothing with level, trend and weekly "
        "seasonality, per product per branch over two years of ledger history. "
        "Every series is backtested against a seasonal-naive baseline and the "
        "winning method is named — a series that could not beat 'the same "
        "weekday last week' says so. Gated on `features.forecast`.",
    ),
    "GET /api/v1/ai/reorder": (
        "Analysis → Replenishment",
        "Reorder point = forecast demand over the lead time + safety stock, "
        "where safety stock is sized from both demand variance and the "
        "supplier's own lead-time variance. Each line returns its workings "
        "term by term, plus days of cover and a projected stockout date. "
        "Gated on `features.reorder`.",
    ),
    "POST /api/v1/ai/reorder/orders": (
        "Analysis → Replenishment → Raise draft order",
        "Turns selected recommendations into **DRAFT** purchase orders through "
        "the same service a human uses. It still needs a second person to "
        "approve — separation of duties is not waived because a machine "
        "suggested it. Later runs net the draft off, so the suggestion does "
        "not come back.",
    ),
    "GET /api/v1/ai/anomalies": (
        "Analysis → Exceptions",
        "Threshold detection over the ledger: after-hours movements, "
        "shrinkage, damage clusters and count variances, each measured against "
        "that location's own normal rather than one global rule. Every finding "
        "links to the movements it was computed from. Sensitivity is an "
        "administrator setting. Gated on `features.anomaly`.",
    ),
    "GET /api/v1/ai/lead-times": (
        "Analysis → Supplier lead times",
        "Median, p90 and standard deviation of actual delivery time per "
        "distributor, measured from your own purchase orders and goods "
        "receipts, plus how often each meets the date it promised. Measured, "
        "never quoted. Gated on `features.leadtime`.",
    ),
    "GET /api/v1/ai/lead-times/{supplier_id}": (
        "Analysis → Supplier lead times → row",
        "The orders and receipts a supplier's percentiles were computed from, "
        "because a buyer about to phone a distributor needs the order numbers, "
        "not a score.",
    ),
    # ------------------------------------------------- analysis: AI intake
    "POST /api/v1/ai/intake/invoice": (
        "Operations → Purchasing → Scan an invoice",
        "**The generative AI feature.** Reads a photographed distributor "
        "invoice into a draft goods receipt. The model produces structured "
        "JSON only; every reading is then checked by deterministic code — "
        "quantity x rate against the line amount, lines against the subtotal, "
        "and the GSTIN's mod-36 check character — and each product is matched "
        "under the same strength and dosage-form rules an ordinary lookup "
        "obeys. **Creates nothing**: there is no code path from here to the "
        "ledger. Findings are graded BLOCK only where wrong stock could reach "
        "a shelf. Gated on `features.invoice_ocr` and `grn.create`.",
    ),
    "POST /api/v1/ai/intake/alias": (
        "Operations → Purchasing → Scan an invoice → Remember this name",
        "Records what a distributor calls one of our products, on "
        "`product_suppliers.supplier_sku`. Taught once, every later invoice "
        "from that distributor matches exactly — which is why the unmatched "
        "rate falls to near zero after a supplier's first delivery.",
    ),
    "GET /api/v1/ai/intake/open-orders/{supplier_id}": (
        "Operations → Purchasing → Scan an invoice (order picker)",
        "Orders a delivery from this supplier could be against. Naming the "
        "order narrows product matching from the whole catalogue to a dozen "
        "lines — the difference between guessing and looking up.",
    ),
    # ---------------------------------------------------------- compliance
    "GET /api/v1/recalls": (
        "Compliance → Batch Recalls",
        "Open and closed recalls with their current status.",
    ),
    "POST /api/v1/recalls": (
        "Compliance → Batch Recalls → Start recall",
        "Freezes every unit of a batch into quarantine at every location at "
        "once. Short to implement because the ledger is batch-aware — this is "
        "a query over existing rows, not a new subsystem.",
    ),
    "GET /api/v1/recalls/{recall_id}/impact": (
        "Compliance → Batch Recalls → row",
        "Full traceability for the batch: every movement of it, every branch "
        "holding it, and every customer who already received some.",
    ),
    "POST /api/v1/recalls/{recall_id}/close": (
        "Compliance → Batch Recalls → Close",
        "Closes the recall. The trace stays in the audit log.",
    ),
    "GET /api/v1/lots": (
        "Compliance → Batch Recalls (batch picker); Inventory → Stock",
        "Batches with their expiry, supplier and printed MRP. MRP is stored "
        "per batch because it is a legal ceiling printed on the pack — a price "
        "rise on a new carton must not reprice older stock.",
    ),
    "POST /api/v1/lots": (
        "Created by goods receipt; exposed for corrections",
        "Registers a batch. Normally created as a side effect of receiving.",
    ),
    "GET /api/v1/audit": (
        "Compliance → Audit trail",
        "Every mutation with actor, action, entity and the before/after "
        "values. Append-only, enforced by the same database trigger that "
        "protects the ledger.",
    ),
    "GET /api/v1/audit/facets": (
        "Compliance → Audit trail (filters)",
        "The distinct actors, actions and entity types present, so the filters "
        "offer only values that exist.",
    ),
    # --------------------------------------------------------- master data
    "GET /api/v1/suppliers": (
        "Setup → Master data → Distributors; pickers in Purchasing",
        "Distributors with GSTIN and state code — the state code is what "
        "decides IGST versus CGST+SGST on every order.",
    ),
    "POST /api/v1/suppliers": (
        "Setup → Master data → New distributor",
        "Creates a distributor. The state code entered here is what every "
        "future order from this supplier uses to decide its GST split, so it "
        "is the one field worth double-checking. (GSTIN is length-checked "
        "only — the mod-36 check-character test runs in invoice intake, where "
        "the number is being read by a machine rather than typed by a "
        "person.)",
    ),
    "PATCH /api/v1/suppliers/{supplier_id}": (
        "Setup → Master data → Edit distributor",
        "Partial update. Changing the state code affects new orders only — "
        "tax already posted is never recomputed.",
    ),
    "DELETE /api/v1/suppliers/{supplier_id}": (
        "Setup → Master data → Retire distributor",
        "Retires rather than deletes: a distributor named on past orders "
        "cannot be removed without orphaning that history.",
    ),
    "GET /api/v1/customers": (
        "Setup → Master data → Customers; picker in Sales",
        "Institutional buyers — hospitals and clinics — with the GSTIN and "
        "state code that decide the GST split on their orders.",
    ),
    "POST /api/v1/customers": (
        "Setup → Master data → New customer",
        "Creates a customer. GSTIN is optional, since not every institutional "
        "buyer is registered; the state code is not, because the GST split on "
        "their orders depends on it.",
    ),
    "PATCH /api/v1/customers/{customer_id}": (
        "Setup → Master data → Edit customer",
        "Partial update of name, contact details, GSTIN or state code.",
    ),
    "DELETE /api/v1/customers/{customer_id}": (
        "Setup → Master data → Retire customer",
        "Retires rather than deletes, so shipment history — the record a "
        "recall traces — stays intact.",
    ),
    "GET /api/v1/warehouses": (
        "Setup → Master data → Locations; location pickers everywhere",
        "The central warehouse and branches. Each carries a state code, which "
        "drives the GST split, and branch users are scoped to exactly one.",
    ),
    "POST /api/v1/warehouses": (
        "Setup → Master data → New location",
        "Creates a branch or warehouse. Its state code decides IGST versus "
        "CGST+SGST for everything received into it.",
    ),
    "PATCH /api/v1/warehouses/{warehouse_id}": (
        "Setup → Master data → Edit location",
        "Partial update of name, address or state code.",
    ),
    "DELETE /api/v1/warehouses/{warehouse_id}": (
        "Setup → Master data → Retire location",
        "Retires rather than deletes, and refuses while the location still "
        "holds stock. Retiring an occupied branch would strand its units — "
        "gone from every picker, still counted in every total.",
    ),
    "GET /api/v1/warehouses/{warehouse_id}/bins": (
        "Setup → Master data → Locations → Bins",
        "Shelf and fridge positions within a location, flagged for cold chain "
        "and quarantine — the flags are what let a recall or a QC hold move "
        "stock somewhere it cannot be picked from.",
    ),
    "POST /api/v1/warehouses/{warehouse_id}/bins": (
        "Setup → Master data → Locations → Add bin",
        "Creates a bin under this location, with its cold-chain and "
        "quarantine flags.",
    ),
    "PATCH /api/v1/bins/{bin_id}": (
        "Setup → Master data → Edit bin",
        "Partial update. Designating an occupied bin cold-chain is refused — "
        "flipping the flag under existing stock would retroactively claim "
        "ambient stock had been refrigerated all along. Empty it first.",
    ),
    "DELETE /api/v1/bins/{bin_id}": (
        "Setup → Master data → Remove bin",
        "Retires a bin. Refused while stock still sits in it.",
    ),
    "GET /api/v1/categories": (
        "Setup → Master data → Categories; product form",
        "The therapeutic category tree. Categories are hierarchical, so a "
        "product inherits its parent's grouping in reporting.",
    ),
    "POST /api/v1/categories": (
        "Setup → Master data → New category",
        "Creates a category, optionally under an existing parent.",
    ),
    "PATCH /api/v1/categories/{category_id}": (
        "Setup → Master data → Edit category",
        "Renames a category or moves it under a different parent. The new "
        "parent chain is walked to the root first, so a move that would make "
        "a category its own ancestor is refused rather than producing a tree "
        "that no longer terminates.",
    ),
    "DELETE /api/v1/categories/{category_id}": (
        "Setup → Master data → Retire category",
        "Retires rather than deletes, and refuses while active "
        "sub-categories or products still point at it — reclassify those "
        "first, so nothing is left filed under a heading that no longer "
        "appears.",
    ),
    "GET /api/v1/uoms": (
        "Setup → Master data → Units; product form",
        "Units of measure — strip, box, vial, bottle. Every quantity in the "
        "ledger is counted in the product's own unit; nothing is converted "
        "behind your back.",
    ),
    "POST /api/v1/uoms": (
        "Setup → Master data → New unit",
        "Creates a unit of measure with its short code and display name.",
    ),
    # ------------------------------------------------------ users and roles
    "GET /api/v1/users": ("Setup → Users", "Staff accounts with their role and branch scope."),
    "POST /api/v1/users": (
        "Setup → Users → New user",
        "Creates an account. A branch scope of null means every branch.",
    ),
    "PATCH /api/v1/users/{user_id}": (
        "Setup → Users → Edit",
        "Changes role, branch scope or active state.",
    ),
    "POST /api/v1/users/{user_id}/password": (
        "Setup → Users → Reset password",
        "Administrator reset, for the 'I am locked out' call. Does not require "
        "the current password, and is audited.",
    ),
    "GET /api/v1/roles": (
        "Setup → Users (role picker)",
        "The four fixed roles and the permissions each carries.",
    ),
    # ---------------------------------------------------------- settings
    "GET /api/v1/settings": (
        "Setup → Settings",
        "Every tunable threshold — anomaly sensitivity, forecast horizon, "
        "service level — with its default, current value and allowed range, "
        "declared once in `app/core/tunables.py` and rendered by both the API "
        "and the interface.",
    ),
    "PATCH /api/v1/settings": (
        "Setup → Settings → Save",
        "Applies a batch of changes and the per-capability on/off switches. "
        "Returns the whole snapshot, because setting a value back to its "
        "default deletes the override. Every change is audited with its before "
        "and after.",
    ),
    "POST /api/v1/settings/reset": (
        "Setup → Settings → Reset to defaults",
        "Deletes every stored override, so each tunable falls back to the "
        "default declared in `app/core/tunables.py`. Audited like any other "
        "settings change.",
    ),
    "GET /api/v1/settings/features": (
        "Every screen, for the sidebar",
        "Which capabilities are live, so the navigation does not offer a "
        "screen that would 404. Not a security boundary — each gated endpoint "
        "checks the same switch itself.",
    ),
    # ------------------------------------------------------------- health
    "GET /health/live": (
        "Not used by the interface — Docker and the load balancer",
        "Liveness: the process is up. Does not touch the database.",
    ),
    "GET /health/ready": (
        "Not used by the interface — Docker, CI and the deploy smoke test",
        "Readiness: the process is up *and* the database answers. This is what "
        "`depends_on: service_healthy` and the post-deploy check wait on.",
    ),
}


#: Tag order and per-tag descriptions, named after the sidebar sections.
#:
#: Passing this to `get_openapi(tags=...)` also fixes the order Swagger UI
#: renders in. Without it the tags appear in route-registration order, which
#: puts `health` in the middle and scatters the four analysis routers — a
#: reader cannot see that the API has a shape.
TAG_GROUPS: list[dict[str, str]] = [
    {
        "name": "auth",
        "description": (
            "Sign-in, token refresh and the permission set every screen reads "
            "on load."
        ),
    },
    {
        "name": "stock",
        "description": (
            "**Overview → Dashboard** and **Inventory → Stock / Movements**. "
            "The append-only ledger and the balance projection derived from "
            "it. Every quantity the product displays comes from here."
        ),
    },
    {
        "name": "products",
        "description": "**Inventory → Products**. The medicine catalogue.",
    },
    {
        "name": "lots",
        "description": (
            "Batches, with expiry and the MRP printed on the pack. Referenced "
            "by **Inventory → Stock** and **Compliance → Batch Recalls**."
        ),
    },
    {
        "name": "purchasing",
        "description": (
            "**Operations → Purchasing**. Orders to distributors, and the "
            "goods receipts that are the only way stock enters the system."
        ),
    },
    {
        "name": "sales",
        "description": (
            "**Operations → Sales**. Customer orders, FEFO allocation and "
            "shipment."
        ),
    },
    {
        "name": "transfers",
        "description": (
            "**Operations → Transfers**. Branch-to-branch movement, posted as "
            "two entries with an in-transit state between them."
        ),
    },
    {
        "name": "adjustments",
        "description": (
            "**Operations → Adjustments**. Counts, damage and write-offs, "
            "each requiring a second person's approval."
        ),
    },
    {
        "name": "AI · forecasting",
        "description": (
            "**Analysis → Demand forecast**. Holt-Winters over the ledger, "
            "backtested against a seasonal-naive baseline."
        ),
    },
    {
        "name": "AI · reorder",
        "description": (
            "**Analysis → Replenishment**. Reorder points from forecast "
            "demand and lead-time variance, and the draft orders raised from "
            "them."
        ),
    },
    {
        "name": "AI · anomalies",
        "description": (
            "**Analysis → Exceptions**. Threshold detection over the ledger, "
            "measured against each location's own normal."
        ),
    },
    {
        "name": "AI · lead time",
        "description": (
            "**Analysis → Supplier lead times**. Delivery percentiles "
            "measured from your own orders, not quoted by the supplier."
        ),
    },
    {
        "name": "AI · invoice intake",
        "description": (
            "**Operations → Purchasing → Scan an invoice**. The generative "
            "feature: a photographed invoice read into a draft goods receipt, "
            "with every reading checked by deterministic code before a human "
            "sees it. Creates nothing."
        ),
    },
    {
        "name": "recalls",
        "description": (
            "**Compliance → Batch Recalls**. Quarantine a batch everywhere at "
            "once, and trace where it already went."
        ),
    },
    {
        "name": "audit",
        "description": (
            "**Compliance → Audit trail**. Every mutation with actor, action "
            "and before/after values. Append-only."
        ),
    },
    {
        "name": "master data",
        "description": (
            "**Setup → Master data**. Distributors, customers, locations, "
            "bins, categories and units."
        ),
    },
    {
        "name": "users",
        "description": "**Setup → Users**. Accounts, roles and branch scope.",
    },
    {
        "name": "settings",
        "description": (
            "**Setup → Settings**. Tunable thresholds and the per-capability "
            "on/off switches, both enforced server-side."
        ),
    },
    {
        "name": "health",
        "description": (
            "Not used by the interface. Docker's healthcheck, the load "
            "balancer and the post-deploy smoke test read these."
        ),
    },
]


def describe(method: str, path: str) -> tuple[str, str] | None:
    """The (screen, purpose) pair for one operation, if it has been declared."""
    return USED_BY.get(f"{method.upper()} {path}")


def annotate(schema: dict[str, Any]) -> dict[str, Any]:
    """Append the 'Used by' block to every operation's description, in place.

    Written as a pure-ish transform over the finished document rather than as
    route decorators so that it applies uniformly and can be tested without
    standing up the app.
    """
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            found = describe(method, path)
            if found is None:
                continue
            screen, purpose = found
            existing = (operation.get("description") or "").rstrip()
            operation["description"] = (
                f"{existing}\n\n---\n\n**Used by:** {screen}\n\n{purpose}"
            ).lstrip()
    return schema
