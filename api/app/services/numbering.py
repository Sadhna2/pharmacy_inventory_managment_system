"""Gap-free document numbering: PO-000001, GRN-000001, ...

Uses a row lock rather than a Postgres SEQUENCE because sequences intentionally
leave gaps on rollback, and finance teams ask about missing invoice numbers.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.documents import DocumentSequence


def next_number(db: Session, prefix: str, *, width: int = 6) -> str:
    row = db.scalar(
        select(DocumentSequence)
        .where(DocumentSequence.prefix == prefix)
        .with_for_update()
    )
    if row is None:
        row = DocumentSequence(prefix=prefix, last_value=0)
        db.add(row)
        db.flush()

    row.last_value += 1
    db.flush()
    return f"{prefix}-{row.last_value:0{width}d}"
