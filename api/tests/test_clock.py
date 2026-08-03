"""The business day is one thing, wherever the code happens to be running.

The only pure-unit file in the suite — everything else here talks to a live
API, because everything else is only true end-to-end. This is true in
isolation, and it needs to be, because it is the thing that makes a laptop and
a container agree about what day it is.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core import clock


def test_business_day_rolls_over_at_indian_midnight() -> None:
    """18:30 UTC is already tomorrow in Mumbai, and the ledger must know it.

    This is the exact case that made a deployment differ from a laptop. The
    production container runs on UTC; a developer runs on IST. For the five
    and a half hours after 18:30 UTC they disagree about the date, and every
    date-sensitive answer in the product moves with it — which batches count
    as expiring within 30 days, whether a delivery was late, what date lands
    on a new order.
    """
    just_before = datetime(2026, 8, 3, 18, 29, tzinfo=UTC)
    just_after = datetime(2026, 8, 3, 18, 31, tzinfo=UTC)

    assert clock.local_date(just_before) == date(2026, 8, 3)
    assert clock.local_date(just_after) == date(2026, 8, 4)
    # And the naive host view — what `date.today()` would have said on a UTC
    # box — is still on the 3rd for both. That gap is the bug.
    assert just_after.date() == date(2026, 8, 3)


@pytest.mark.parametrize(
    "host_tz", ["UTC", "Asia/Kolkata", "America/Los_Angeles", "Pacific/Kiritimati"]
)
def test_today_ignores_the_host_timezone(host_tz: str, monkeypatch) -> None:
    """`clock.today()` answers for the shop, not for the machine.

    Kiritimati is UTC+14 and Los Angeles is UTC-8 — a 22-hour spread, so on any
    given instant at least one of them is on a different calendar day from
    India. All four must still produce the Indian date.
    """
    monkeypatch.setenv("TZ", host_tz)
    expected = datetime.now(UTC).astimezone(ZoneInfo("Asia/Kolkata")).date()
    assert clock.today() == expected


def test_at_local_is_the_inverse_of_local() -> None:
    """A timestamp written as "3pm at the branch" reads back as 3pm."""
    written = clock.at_local(date(2026, 8, 3), 15)
    assert written.tzinfo is not None
    assert clock.local(written).hour == 15
    assert clock.local_date(written) == date(2026, 8, 3)
