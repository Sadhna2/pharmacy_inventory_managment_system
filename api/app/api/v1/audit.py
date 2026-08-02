"""Read access to the audit trail.

The log has been written on every state change since day one but had no way
out of the database. Without this endpoint the answer to "who cancelled that
order, and what did it look like before?" is a psql session.

Read-only by construction: there is no POST, PATCH or DELETE here, and the
table itself revokes UPDATE and DELETE at the database level. Entries are
created only as a side effect of the action they describe.
"""

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.identity import AuditLog, User
from app.schemas.audit import AuditActorOut, AuditFacetsOut, AuditLogOut
from app.schemas.common import Page, PageParams, paginate

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=Page[AuditLogOut])
def list_audit(
    action: str | None = Query(None, description="Exact action, e.g. po.approve"),
    entity_type: str | None = Query(None, description="e.g. purchase_order"),
    entity_id: int | None = None,
    actor_user_id: int | None = None,
    date_from: date | None = Query(None, description="Inclusive"),
    date_to: date | None = Query(None, description="Inclusive"),
    page: int = 1,
    size: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("audit.view")),
) -> Page[AuditLogOut]:
    """Newest first. Every filter is optional and they compose.

    `entity_type` + `entity_id` together give the full history of one record —
    which is the question people actually ask.
    """
    actor = User.__table__.alias("actor")
    stmt = (
        select(AuditLog, actor.c.full_name, actor.c.email)
        .outerjoin(actor, actor.c.id == AuditLog.actor_user_id)
    )

    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor_user_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        # Inclusive of the whole end day: a user picking "to 5 Aug" means the
        # end of the 5th, not midnight at its start.
        stmt = stmt.where(
            AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min)
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    params = PageParams(page=page, size=size)
    rows = db.execute(
        stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset(params.offset)
        .limit(params.size)
    ).all()

    items = [
        AuditLogOut(
            id=log.id,
            action=log.action,
            entity_type=log.entity_type,
            entity_id=log.entity_id,
            actor_user_id=log.actor_user_id,
            actor_name=name,
            actor_email=email,
            before_json=log.before_json,
            after_json=log.after_json,
            ip=str(log.ip) if log.ip else None,
            request_id=log.request_id,
            created_at=log.created_at,
        )
        for log, name, email in rows
    ]
    return paginate(items, total, params)


@router.get("/facets", response_model=AuditFacetsOut)
def audit_facets(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("audit.view")),
) -> AuditFacetsOut:
    """The action and entity names actually present in the log.

    Derived rather than hardcoded, so a filter list cannot drift out of date
    as Layer 2 adds new audited actions.
    """
    actions = db.scalars(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    ).all()
    entities = db.scalars(
        select(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type)
    ).all()
    actors = db.execute(
        select(User.id, User.full_name, User.email)
        .join(AuditLog, AuditLog.actor_user_id == User.id)
        .distinct()
        .order_by(User.full_name)
    ).all()
    return AuditFacetsOut(
        actions=list(actions),
        entity_types=list(entities),
        actors=[
            AuditActorOut(id=uid, full_name=name, email=email)
            for uid, name, email in actors
        ],
    )
