"""Append-only audit trail.

The table rejects UPDATE and DELETE at the database level, so once written a
line is permanent.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models.identity import AuditLog

# Never record these, even if a caller passes them in.
_REDACTED = {"password", "password_hash", "token", "secret", "refresh_token"}


def _clean(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _REDACTED:
            out[key] = "***"
        elif isinstance(value, dict):
            out[key] = _clean(value)
        else:
            out[key] = str(value) if not isinstance(value, (int, float, bool, type(None), str)) else value
    return out


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    actor_user_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=_clean(before),
        after_json=_clean(after),
        ip=ip,
        user_agent=user_agent,
        request_id=request_id,
    )
    db.add(entry)
    db.flush()
    return entry
