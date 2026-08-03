"""Permission catalogue and role assignments (ARCHITECTURE.md §10.3).

Permissions are strings. Roles are bundles of them. Cost visibility is a
permission, not a role — warehouse staff record stock but never see margins.
"""

from typing import Literal

# --- catalogue --------------------------------------------------------------

PERMISSIONS: dict[str, str] = {
    # master data
    "product.view": "View products",
    "product.manage": "Create and edit products",
    "master.view": "View suppliers, customers, warehouses",
    "master.manage": "Create and edit master data",
    # stock
    "stock.view": "View stock balances and movements",
    "stock.move": "Record stock movements",
    "stock.view_cost": "See unit cost and stock valuation",
    "stock.adjust": "Create stock adjustments",
    "stock.count": "Run cycle counts",
    # documents
    "po.view": "View purchase orders",
    "po.create": "Create purchase orders",
    "po.approve": "Approve purchase orders",
    "grn.create": "Receive goods",
    "so.view": "View sales orders",
    "so.create": "Create sales orders",
    "so.fulfil": "Pick, pack and ship sales orders",
    "transfer.view": "View stock transfers",
    "transfer.create": "Create stock transfers",
    "transfer.approve": "Approve and dispatch transfers",
    "adjustment.approve": "Approve stock adjustments",
    # pharmacy
    "recall.initiate": "Initiate a batch recall",
    # admin
    "user.manage": "Manage users and roles",
    "settings.manage": "Change system settings",
    "audit.view": "View the audit log",
    # AI (Layer 2 — permissions exist so roles need no migration later)
    "ai.view": "View forecasts, recommendations and anomalies",
    "ai.act": "Act on AI recommendations",
    "ai.ask": "Ask natural-language questions",
    "ocr.review": "Review and approve OCR extractions",
}

# --- role bundles -----------------------------------------------------------

ADMIN = "ADMIN"
MANAGER = "MANAGER"
STAFF = "STAFF"
CUSTOMER = "CUSTOMER"

#: The four role codes as a closed set.
#:
#: Declared as a Literal rather than `str` so that it survives into the OpenAPI
#: schema as a union. The browser generates its types from that schema, and a
#: bare `str` there would widen `role` to "any string" on the client — losing
#: the exhaustiveness check that stops a `switch` on role from silently missing
#: a case. Roles are a fixed set here, so promising callers a fixed set is
#: honest as well as useful.
RoleCode = Literal["ADMIN", "MANAGER", "STAFF", "CUSTOMER"]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    ADMIN: list(PERMISSIONS),  # everything
    MANAGER: [
        "product.view", "product.manage",
        "master.view", "master.manage",
        "stock.view", "stock.move", "stock.view_cost", "stock.adjust", "stock.count",
        "po.view", "po.create", "po.approve", "grn.create",
        "so.view", "so.create", "so.fulfil",
        "transfer.view", "transfer.create", "transfer.approve",
        "adjustment.approve",
        "recall.initiate",
        "ai.view", "ai.act", "ai.ask", "ocr.review",
    ],
    STAFF: [
        # Records stock all day, but cannot approve anything or see cost.
        "product.view",
        "master.view",
        "stock.view", "stock.move", "stock.count",
        "po.view", "grn.create",
        "so.view", "so.fulfil",
        "transfer.view",
        "ai.ask", "ocr.review",
    ],
    CUSTOMER: [
        # Heavily scoped: their own orders and nothing else.
        "so.view",
        "ai.ask",
    ],
}

ROLE_NAMES: dict[str, str] = {
    ADMIN: "Administrator",
    MANAGER: "Manager",
    STAFF: "Pharmacy Staff",
    CUSTOMER: "Customer",
}
