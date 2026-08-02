from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    entity_type: str
    entity_id: int | None = None
    actor_user_id: int | None = None
    actor_name: str | None = None
    actor_email: str | None = None
    #: Field-level values captured either side of the change. Null for
    #: actions that create or delete rather than modify.
    before_json: dict | None = None
    after_json: dict | None = None
    ip: str | None = None
    request_id: str | None = None
    created_at: datetime


class AuditActorOut(BaseModel):
    id: int
    full_name: str
    email: str


class AuditFacetsOut(BaseModel):
    """Distinct values present in the log, so the UI can offer real filters
    rather than a hardcoded list that drifts as new actions are added."""

    actions: list[str]
    entity_types: list[str]
    #: Only users who have actually done something. Deliberately not the full
    #: user list — filtering by someone with no entries is a dead end.
    actors: list[AuditActorOut]
