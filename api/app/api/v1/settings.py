"""Administrator settings.

Two things live behind one screen: which capabilities are switched on, and the
numbers behind them. Both are gated on `settings.manage`, which only an
administrator holds — a manager can read every analysis screen but cannot
change how cautious the safety stock is, because that decision has a cash cost
somebody owns.

Every change is audited with its before and after value. "The forecast started
recommending twice as much last Tuesday" has to be answerable, and the answer
is usually that somebody moved a number.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_permission
from app.core.tunables import GROUPS
from app.db.session import get_db
from app.models.identity import User
from app.models.settings import FeatureFlag
from app.schemas.settings import (
    FeatureOut,
    FeatureStateOut,
    SettingsGroupOut,
    SettingsOut,
    SettingsUpdateIn,
    TunableOut,
)
from app.services import audit
from app.services import settings as settings_service

router = APIRouter(prefix="/settings", tags=["settings"])

MANAGE = require_permission("settings.manage")


def _features(db: Session) -> list[FeatureOut]:
    flags = db.scalars(
        select(FeatureFlag).order_by(FeatureFlag.sort_order, FeatureFlag.key)
    ).all()
    return [
        FeatureOut(
            key=flag.key,
            label=flag.label,
            description=flag.description,
            is_enabled=flag.is_enabled,
            is_implemented=flag.is_implemented,
            updated_at=flag.updated_at,
        )
        for flag in flags
    ]


def _snapshot(db: Session) -> SettingsOut:
    described = settings_service.describe(db)
    by_group: dict[str, list[TunableOut]] = {}
    for row in described:
        by_group.setdefault(row["group"], []).append(TunableOut(**row))

    return SettingsOut(
        features=_features(db),
        groups=[
            SettingsGroupOut(key=key, label=label, tunables=by_group[key])
            for key, label in GROUPS
            if by_group.get(key)
        ],
    )


@router.get("", response_model=SettingsOut)
def read_settings(
    db: Session = Depends(get_db),
    _: User = Depends(MANAGE),
) -> SettingsOut:
    """Everything the settings screen renders itself from.

    Each tunable carries its own label, help text, type and bounds, so adding
    one to the registry puts a correctly validated field on the page with no
    frontend change at all.
    """
    return _snapshot(db)


@router.patch("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
) -> SettingsOut:
    """Apply a batch of changes and return the new state.

    Returning the whole snapshot rather than an acknowledgement is deliberate:
    setting a value back to its default deletes the override, and a couple of
    cross-field rules can reject a combination that each field would accept on
    its own. The client should render what the server actually holds, not what
    it hoped it sent.
    """
    before = settings_service.all_values(db)

    if payload.values:
        applied = settings_service.set_many(db, payload.values, user_id=user.id)
        changed = {
            key: {"from": before.get(key), "to": value}
            for key, value in applied.items()
            if before.get(key) != value
        }
        if changed:
            audit.record(
                db,
                action="settings.update",
                entity_type="app_settings",
                actor_user_id=user.id,
                before={k: v["from"] for k, v in changed.items()},
                after={k: v["to"] for k, v in changed.items()},
            )

    for key, enabled in payload.features.items():
        flag = settings_service.set_feature(db, key, enabled, user_id=user.id)
        audit.record(
            db,
            action="settings.feature_toggle",
            entity_type="feature_flag",
            actor_user_id=user.id,
            before={"key": key, "is_enabled": not enabled},
            after={"key": key, "is_enabled": flag.is_enabled, "label": flag.label},
        )

    db.commit()
    return _snapshot(db)


@router.post("/reset", response_model=SettingsOut)
def reset_settings(
    db: Session = Depends(get_db),
    user: User = Depends(MANAGE),
) -> SettingsOut:
    """Drop every override and go back to the shipped defaults.

    Feature switches are left alone — "reset the numbers" should not silently
    turn a capability back on that somebody deliberately switched off.
    """
    before = settings_service.overrides(db)
    settings_service.reset(db)
    if before:
        audit.record(
            db,
            action="settings.reset",
            entity_type="app_settings",
            actor_user_id=user.id,
            before=before,
            after={},
        )
    db.commit()
    return _snapshot(db)


@router.get("/features", response_model=FeatureStateOut)
def read_features(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> FeatureStateOut:
    """Which capabilities are live, for anyone signed in.

    The navigation needs this to avoid offering a screen that would 404. It is
    not a security boundary — every gated endpoint checks the same switch
    itself — so it needs no more than authentication.
    """
    return FeatureStateOut(enabled=settings_service.features(db))
