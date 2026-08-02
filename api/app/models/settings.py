"""Runtime feature switches and administrator-tunable numbers.

Every AI capability in the brief is independently switchable, for two reasons:

  - **Demo safety.** If a feature misbehaves in front of an audience it can be
    turned off in one click and the rest of the application is unaffected.
  - **Honest scope.** Features that are planned but not built are rows here
    with `is_implemented = False`, so the system states plainly what exists
    rather than shipping a dead menu item.

A flag is enforced server-side (see `services/features.py`), not merely used to
hide a button — turning something off must actually close the endpoint.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Grouping for the settings screen: "AI", "OPERATIONS", "COMPLIANCE".
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="AI")

    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # False for capabilities that are designed but not yet built. The toggle is
    # shown disabled rather than hidden, so the roadmap is visible and honest.
    is_implemented: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class AppSetting(Base):
    """One administrator override of a value declared in `core/tunables.py`.

    Only rows that differ from the shipped default exist. That is not a space
    optimisation: it means an improved default reaches every installation that
    never touched the setting, and a row here is always a decision somebody
    made deliberately and can be asked about.

    The registry, not this table, is the source of truth for what a setting
    means. A row whose key is not in the registry is ignored — deleting a
    feature cannot leave a live value quietly steering something.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
