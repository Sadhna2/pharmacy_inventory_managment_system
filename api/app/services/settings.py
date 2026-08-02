"""Reading and writing administrator settings.

HOW THE READ PATH STAYS CHEAP
------------------------------
The anomaly detector asks for six settings while looping over fifty thousand
movements. Six queries per call would be silly and six thousand would be a bug,
so the whole table is loaded once and cached in process, with a version counter
that every write bumps.

The version is also what the forecast cache keys on. Change the horizon or the
backtest window and the fitted models must be thrown away — a cache that
survived a settings change would serve numbers computed under the old rules
while the screen showed the new ones, which is the kind of bug nobody ever
tracks down.

WHY VALUES ARE VALIDATED ON WRITE, NOT ON READ
-----------------------------------------------
Reads happen in the middle of a calculation, where the only sane response to
"z_threshold is the string 'banana'" is to crash. Writes happen at an API
boundary where a human is holding the form and can be told which field is
wrong. So `set_many` is strict and `get` assumes the database is already sane —
and falls back to the default if it somehow is not, because a settings row
should never be able to take the application down.
"""

import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.core.tunables import BY_KEY, DEFAULTS, Tunable
from app.models.settings import AppSetting, FeatureFlag

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_features: dict[str, bool] | None = None
_version = 0


def version() -> int:
    """Bumped on every write. Anything caching derived work should key on it."""
    return _version


def _invalidate() -> None:
    global _cache, _features, _version
    with _lock:
        _cache = None
        _features = None
        _version += 1


def _load(db: Session) -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is None:
            stored = {row.key: row.value for row in db.scalars(select(AppSetting))}
            # Registry first, database second: an unknown key in the database
            # is dropped rather than merged in.
            _cache = {**DEFAULTS, **{k: v for k, v in stored.items() if k in BY_KEY}}
    return _cache


def get(db: Session, key: str) -> Any:
    """Current value, falling back to the shipped default."""
    tunable = BY_KEY.get(key)
    if tunable is None:
        raise KeyError(f"Unknown setting {key!r}")
    value = _load(db).get(key, tunable.default)
    try:
        return tunable.coerce(value)
    except ValueError:
        # A stored value that no longer validates — because the bounds moved in
        # a later release, say. Use the default rather than fail a report.
        return tunable.default


def get_many(db: Session, *keys: str) -> tuple:
    """Several settings in one go, in the order asked for."""
    return tuple(get(db, key) for key in keys)


def all_values(db: Session) -> dict[str, Any]:
    return dict(_load(db))


def overrides(db: Session) -> dict[str, Any]:
    """Only the keys an administrator has actually changed."""
    stored = {row.key: row.value for row in db.scalars(select(AppSetting))}
    return {k: v for k, v in stored.items() if k in BY_KEY}


def set_many(
    db: Session, values: dict[str, Any], *, user_id: int | None = None
) -> dict[str, Any]:
    """Validate and persist a batch of changes.

    All or nothing: a batch with one bad field writes none of it, because a
    settings screen that half-saves leaves the admin unsure what is in effect.

    Setting a value back to its default deletes the row rather than storing it,
    so "unchanged from the default" and "deliberately set to today's default"
    do not silently diverge the next time the default improves.
    """
    unknown = [key for key in values if key not in BY_KEY]
    if unknown:
        raise ValidationError(
            f"Unknown setting{'s' if len(unknown) > 1 else ''}: {', '.join(sorted(unknown))}"
        )

    cleaned: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for key, raw in values.items():
        tunable = BY_KEY[key]
        try:
            cleaned[key] = tunable.coerce(raw)
        except ValueError as exc:
            errors.append({"field": key, "message": f"{tunable.label} {exc}"})
    if errors:
        raise ValidationError("Some settings could not be saved", errors)

    _check_coherent(db, cleaned)

    existing = {row.key: row for row in db.scalars(select(AppSetting))}
    for key, value in cleaned.items():
        if value == BY_KEY[key].default:
            if key in existing:
                db.delete(existing[key])
            continue
        row = existing.get(key)
        if row is None:
            db.add(AppSetting(key=key, value=value, updated_by=user_id))
        else:
            row.value = value
            row.updated_by = user_id

    db.flush()
    _invalidate()
    return cleaned


def _check_coherent(db: Session, changes: dict[str, Any]) -> None:
    """Cross-field rules — the ones a per-field validator cannot see.

    Each of these is a combination that is individually legal and jointly
    meaningless, and each would surface later as a confusing empty screen
    rather than as an error anybody could connect to what they typed.
    """
    merged = {**_load(db), **changes}
    errors: list[dict[str, str]] = []

    opens, closes = str(merged["business.opens"]), str(merged["business.closes"])
    if opens >= closes:
        errors.append(
            {
                "field": "business.closes",
                "message": "Counter closes must be later than it opens. "
                           "Overnight trading is not modelled.",
            }
        )

    if merged["forecast.backtest_days"] >= merged["forecast.lookback_days"]:
        errors.append(
            {
                "field": "forecast.backtest_days",
                "message": "The held-out window has to be shorter than the "
                           "history it is held out of, or there is nothing "
                           "left to fit on.",
            }
        )

    if merged["anomaly.min_value"] > merged["anomaly.material_value"]:
        errors.append(
            {
                "field": "anomaly.material_value",
                "message": "This is below the value at which write-offs are "
                           "ignored entirely, so it would never fire.",
            }
        )

    z = ("reorder.service_standard", "reorder.service_high", "reorder.service_critical")
    if not merged[z[0]] <= merged[z[1]] <= merged[z[2]]:
        errors.append(
            {
                "field": "reorder.service_critical",
                "message": "Critical drugs must be protected at least as hard "
                           "as prescription ones, and those at least as hard "
                           "as general lines.",
            }
        )

    if errors:
        raise ValidationError("Some settings contradict each other", errors)


def reset(db: Session, keys: list[str] | None = None) -> None:
    """Drop overrides and go back to the shipped defaults."""
    rows = db.scalars(select(AppSetting)).all()
    for row in rows:
        if keys is None or row.key in keys:
            db.delete(row)
    db.flush()
    _invalidate()


# --- feature flags ----------------------------------------------------------


def features(db: Session) -> dict[str, bool]:
    """Which capabilities are switched on, by flag key.

    A capability is on only if it is both enabled *and* implemented. The second
    half matters: `feature_flags` deliberately carries rows for things that are
    designed and not built, so the settings screen can show an honest roadmap
    instead of the system pretending a dead menu item works.
    """
    global _features
    if _features is not None:
        return _features
    with _lock:
        if _features is None:
            _features = {
                flag.key: bool(flag.is_enabled and flag.is_implemented)
                for flag in db.scalars(select(FeatureFlag))
            }
    return _features


def set_feature(
    db: Session, key: str, enabled: bool, *, user_id: int | None = None
) -> FeatureFlag:
    flag = db.get(FeatureFlag, key)
    if flag is None:
        raise ValidationError(f"Unknown feature {key!r}")
    if enabled and not flag.is_implemented:
        raise ValidationError(
            f"{flag.label} is not built yet, so it cannot be switched on."
        )
    flag.is_enabled = enabled
    flag.updated_by = user_id
    db.flush()
    _invalidate()
    return flag


def describe(db: Session) -> list[dict[str, Any]]:
    """Every tunable with its current value, default and bounds.

    Shaped for the settings screen, which renders itself entirely from this —
    adding a tunable to the registry puts a correctly-labelled, correctly-
    validated field on the page with no frontend change at all.
    """
    current = _load(db)
    changed = set(overrides(db))

    def row(tunable: Tunable) -> dict[str, Any]:
        return {
            "key": tunable.key,
            "label": tunable.label,
            "help": tunable.help,
            "kind": tunable.kind,
            "group": tunable.group,
            "unit": tunable.unit,
            "minimum": tunable.minimum,
            "maximum": tunable.maximum,
            "default": tunable.default,
            "value": current.get(tunable.key, tunable.default),
            "is_overridden": tunable.key in changed,
        }

    return [row(t) for t in BY_KEY.values()]
