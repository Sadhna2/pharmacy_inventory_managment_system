"""The briefing the SQL model is given (app/ai/ask/schema_context.py).

This is the one file in the ask pipeline whose failures are silent. A validator
that breaks refuses queries and somebody notices within a minute; a briefing
that breaks produces queries that run, return a plausible number, and are
wrong. Nothing downstream can catch that, because there is nothing malformed
about the SQL.

So what is asserted here is not "the string is well-formed" but the three ways
it could go quietly stale:

  * a table exists that the model was never told about, so it is never queried
  * an enum value is spelled differently in the prompt than in the database,
    so the WHERE clause matches nothing and reads as "there were none"
  * the schema drifted away from the models, which is what happens the moment
    any of it is hand-written rather than reflected

The third is the one worth the most care, and the only way to prove it is to
change the metadata and watch the output change with it.

Needs no database — everything here reads SQLAlchemy metadata, which is built
at import time.
"""

import pytest
from sqlalchemy import Column, Integer, String, Table

from app.ai.ask.schema_context import (
    TOKEN_BUDGET,
    build_schema_context,
    invalidate_schema_context,
)
from app.db.base import Base
from app.models.enums import DocumentStatus, StockStatus


@pytest.fixture
def context() -> str:
    """A freshly built briefing, so an earlier test's cache cannot mask a bug."""
    invalidate_schema_context()
    return build_schema_context()


def test_every_mapped_table_is_described_to_the_model(context: str) -> None:
    missing = [
        table.name
        for table in Base.metadata.sorted_tables
        if f"TABLE {table.name}\n" not in context
    ]
    assert missing == [], (
        f"{missing} exist in the database but not in the briefing. The model "
        f"cannot query a table it has never heard of."
    )


def test_every_column_of_the_ledger_is_described(context: str) -> None:
    """stock_movements is where the interesting questions land, so spot-check
    it in full rather than trusting that the table header implies its body."""
    block = context.split("TABLE stock_movements\n")[1].split("TABLE ")[0]
    for column in Base.metadata.tables["stock_movements"].columns:
        assert column.name in block, f"stock_movements.{column.name} is missing"


def test_foreign_keys_are_shown_so_the_model_can_join(context: str) -> None:
    assert "product_id int NN ->products.id" in context
    assert "created_by int NN ->users.id" in context
    # Nullable ones matter most: they are the joins that need to be outer.
    assert "lot_id int ->lots.id" in context


def test_composite_unique_constraints_are_shown(context: str) -> None:
    """A batch is unique per product, not globally. Without this the model will
    join lots to anything on lot_code and quietly merge two manufacturers."""
    assert "UNIQUE(product_id, lot_code)" in context
    assert "UNIQUE(warehouse_id, code)" in context


def test_document_status_values_appear_exactly_as_the_database_spells_them(
    context: str,
) -> None:
    """The 'CANCELED' trap: a model that guesses the American spelling writes a
    query that runs and returns nothing, which reads as an answer."""
    line = next(
        row for row in context.splitlines() if row.strip().startswith("document_status:")
    )
    for member in DocumentStatus:
        assert f" {member.value} " in f" {line} ", (
            f"{member.value} is missing from the document_status line"
        )
    assert "CANCELLED" in line
    assert "CANCELED " not in line


def test_stock_status_values_appear_verbatim(context: str) -> None:
    assert (
        "  stock_status: AVAILABLE | QUARANTINE | DAMAGED | IN_TRANSIT | "
        "RETURNED_PENDING" in context
    )
    assert {member.value for member in StockStatus} == {
        "AVAILABLE",
        "QUARANTINE",
        "DAMAGED",
        "IN_TRANSIT",
        "RETURNED_PENDING",
    }, "StockStatus changed; the line asserted above must change with it"


def test_a_new_table_reaches_the_briefing_without_anyone_editing_it() -> None:
    """The proof that the schema half is reflected and not transcribed.

    A hand-written schema passes every other test in this file on the day it is
    written. It fails this one, which is the only test that asks what happens
    after the next migration.
    """
    invalidate_schema_context()
    assert "TABLE ask_reflection_probe" not in build_schema_context()

    probe = Table(
        "ask_reflection_probe",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("shelf_label", String(24), nullable=False),
    )
    try:
        invalidate_schema_context()
        rebuilt = build_schema_context()
    finally:
        # Leave the metadata as it was found; every other test in the suite
        # reads the same registry.
        Base.metadata.remove(probe)
        invalidate_schema_context()

    assert "TABLE ask_reflection_probe" in rebuilt
    assert "shelf_label varchar(24) NN" in rebuilt, (
        "the column's real type and nullability must be reflected, not just its name"
    )
    assert "TABLE ask_reflection_probe" not in build_schema_context()


def test_the_cache_is_reused_until_it_is_invalidated() -> None:
    invalidate_schema_context()
    first = build_schema_context()
    assert build_schema_context() is first, "rebuilt when it should have been cached"
    invalidate_schema_context()
    assert build_schema_context() is not first, "invalidate() did not drop the cache"


def test_the_semantic_layer_carries_the_traps_that_produce_wrong_numbers(
    context: str,
) -> None:
    """Each of these is a fact no schema dump conveys and every one of them
    changes an answer. Losing one costs nothing at import time and everything
    at question time, so they are pinned."""
    for trap in (
        "REVERSAL",  # corrections are opposing rows, never edits
        "stock_balances",  # the current position, as against the history
        "IN_TRANSIT",  # stock on a truck, counted at the destination
        "lots.mrp",  # MRP is printed per batch, not held per product
        "is_interstate",  # CGST+SGST or IGST, never both
        "SALE_ISSUE",  # the sales history is ledger rows, not documents
        "Asia/Kolkata",  # the business day is not the UTC day
        "is_active",  # retirement is a flag, not a DELETE
    ):
        assert trap in context, f"the {trap} trap is no longer explained"


def test_the_join_paths_include_the_batch_to_customer_trace(context: str) -> None:
    """The recall question is the one this data model exists to answer, and it
    is four joins deep — the model will not find it unaided."""
    assert "shipment_lines.lot_id" in context
    assert "customers.id" in context


def test_the_briefing_stays_inside_its_token_budget(context: str) -> None:
    """Paid on every question. Growth is fine; unnoticed growth is not."""
    estimated = len(context) // 4
    assert estimated <= TOKEN_BUDGET, (
        f"the briefing is about {estimated} tokens, over the {TOKEN_BUDGET} "
        f"budget. Cut prose before cutting a trap."
    )
