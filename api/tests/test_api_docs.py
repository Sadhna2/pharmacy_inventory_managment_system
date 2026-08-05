"""The 'Used by' documentation stays complete and stays true.

`app/core/api_docs.py` says, for every endpoint, which screen calls it and what
it computes. That is exactly the kind of documentation that rots: someone adds
a route, the table does not grow, and `/docs` quietly goes back to being a list
of paths. These tests make that a build failure rather than a discovery.

They check both directions. A route with no entry fails, and an entry naming a
route that no longer exists fails too — a stale line is worse than a missing
one, because it reads as current.
"""

from __future__ import annotations

import pytest
from fastapi.openapi.utils import get_openapi

from app.core import api_docs
from app.main import app

#: Documenting the automatic HEAD/OPTIONS handlers would be noise.
DOCUMENTED_METHODS = {"get", "post", "patch", "put", "delete"}


def _raw_schema() -> dict:
    """The document before annotation.

    Deliberately *not* `app.routes`: this FastAPI keeps each `include_router`
    call as a wrapper object rather than flattening its routes upward, so
    walking that list finds only the two health endpoints and every real route
    silently escapes the check. The generated schema is both authoritative and
    exactly what `/docs` renders.
    """
    return get_openapi(
        title=app.title, version=app.version, description=app.description,
        routes=app.routes,
    )


def _live_operations(schema: dict | None = None) -> set[str]:
    """Every operation the API publishes, as "METHOD /path"."""
    schema = schema or _raw_schema()
    return {
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
        if method in DOCUMENTED_METHODS
    }


def test_every_endpoint_says_where_it_is_used():
    missing = sorted(_live_operations() - set(api_docs.USED_BY))
    assert not missing, (
        "These endpoints have no entry in api_docs.USED_BY, so /docs will not "
        "say which screen calls them:\n  " + "\n  ".join(missing)
    )


def test_no_entry_describes_an_endpoint_that_no_longer_exists():
    """A stale line is worse than a missing one — it reads as current."""
    stale = sorted(set(api_docs.USED_BY) - _live_operations())
    assert not stale, (
        "api_docs.USED_BY describes endpoints that are not registered:\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.parametrize("operation", sorted(api_docs.USED_BY))
def test_each_entry_names_a_screen_and_explains_itself(operation):
    screen, purpose = api_docs.USED_BY[operation]
    assert screen.strip(), f"{operation} has an empty screen"
    # Long enough to be a sentence about behaviour, not a restated path.
    assert len(purpose.strip()) > 30, f"{operation} has a placeholder purpose"


def test_every_tag_in_use_is_described_and_ordered():
    """An undescribed tag renders as a bare heading with no section name."""
    in_use = {
        tag
        for operations in _raw_schema()["paths"].values()
        for method, operation in operations.items()
        if method in DOCUMENTED_METHODS
        for tag in operation.get("tags", [])
    }
    described = {group["name"] for group in api_docs.TAG_GROUPS}
    assert in_use - described == set(), "tags with no description in TAG_GROUPS"
    assert described - in_use == set(), "TAG_GROUPS describes tags nothing uses"


def test_the_schema_carries_the_used_by_line():
    """End to end: what /docs renders actually contains the mapping."""
    app.openapi_schema = None  # force a rebuild; other tests may have cached it
    schema = app.openapi()
    app.openapi_schema = None

    allocate = schema["paths"]["/api/v1/sales-orders/{so_id}/allocate"]["post"]
    assert "**Used by:** Operations → Sales → Allocate" in allocate["description"]
    assert "FEFO" in allocate["description"]

    # The docstring the route already had must survive, not be replaced.
    scan = schema["paths"]["/api/v1/ai/intake/invoice"]["post"]
    assert "**Used by:** Operations → Purchasing → Scan an invoice" in scan["description"]
    assert scan["description"].index("**Used by:**") > 0

    tag_names = [tag["name"] for tag in schema["tags"]]
    assert tag_names.index("auth") < tag_names.index("health"), "tag order not applied"


def test_annotate_leaves_an_undocumented_operation_alone():
    """It appends to what it knows; it never invents a description."""
    schema = {"paths": {"/nope": {"get": {"description": "untouched"}}}}
    assert api_docs.annotate(schema)["paths"]["/nope"]["get"]["description"] == "untouched"
