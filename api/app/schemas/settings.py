"""Schemas for the administrator settings screen."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TunableOut(BaseModel):
    key: str
    label: str
    help: str
    kind: str = Field(description="bool | int | float | time")
    group: str
    unit: str | None
    minimum: float | None
    maximum: float | None
    default: Any
    value: Any
    is_overridden: bool = Field(
        description="True when an administrator has changed it from the default"
    )


class FeatureOut(BaseModel):
    key: str
    label: str
    description: str
    is_enabled: bool
    is_implemented: bool = Field(
        description="False for capabilities that are designed but not built. "
                    "The toggle is shown, disabled, rather than hidden."
    )
    updated_at: datetime | None = None


class SettingsGroupOut(BaseModel):
    key: str
    label: str
    tunables: list[TunableOut]


class SettingsOut(BaseModel):
    features: list[FeatureOut]
    groups: list[SettingsGroupOut]


class SettingsUpdateIn(BaseModel):
    """A batch of changes. All or nothing — a half-saved settings screen leaves
    an administrator unsure what is actually in effect."""

    values: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)


class FeatureStateOut(BaseModel):
    """What the frontend needs to decide which navigation entries to show.

    Available to any signed-in user, unlike the rest of this module: hiding a
    dead menu item is not a secret, and the endpoints enforce the same switches
    server-side regardless.
    """

    enabled: dict[str, bool]
