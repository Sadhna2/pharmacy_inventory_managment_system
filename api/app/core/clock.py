"""Business time.

Every timestamp in the ledger is stored as UTC — that is not negotiable, and
nothing here changes it. But almost every *question* about a timestamp is a
local one. "Was this dispensed after closing?" and "how much did we sell on
Tuesday?" are about the clock on the wall in the shop, and 20:00 IST is 14:30
UTC, which sits on the previous day for a chunk of the world.

The chain operates only in India, which has a single timezone and no daylight
saving, so this is one constant rather than a per-warehouse setting. If the
business ever crosses a border, `local()` becomes a lookup on the warehouse and
every caller here already asks the right question.
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

#: When a counter is open. Generous at both ends: metro pharmacies run long
#: days, and a delivery signed in at 07:30 or a stock count finished at 22:30
#: is ordinary, not suspicious.
#:
#: These are the shipped defaults. An administrator sets the real hours under
#: Setup → Settings, and callers pass them in — this module deliberately does
#: not read the database, because the seed generator uses it too and a seeder
#: that depends on runtime configuration is a seeder nobody can reproduce.
OPENS = time(6, 0)
CLOSES = time(23, 0)


def parse_hour(text: str) -> time:
    """"06:00" → time(6, 0). The storage format for a trading-hour setting."""
    hour, _, minute = text.partition(":")
    return time(int(hour), int(minute or 0))


def local(moment: datetime) -> datetime:
    """A UTC instant as the clock read in the shop.

    Naive datetimes are assumed to be UTC, which is what the database returns
    for a column declared without a timezone and what every writer in this
    codebase intends.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(BUSINESS_TZ)
    return moment.astimezone(BUSINESS_TZ)


def local_date(moment: datetime) -> date:
    """The business day a movement belongs to."""
    return local(moment).date()


def today() -> date:
    """Today, as the shop reckons it. Use this, never `date.today()`.

    `date.today()` reads the host clock, and the host is not the business. A
    developer's laptop is on IST while the production container is on UTC, so
    between 18:30 and midnight India time the two disagree about what day it
    is — and every date-sensitive answer in the product shifts with them: which
    batches count as expiring within 30 days, which deliveries were late, what
    date lands on a new order, where a forecast horizon starts.

    That is not a display quirk. A pharmacy in Mumbai closing at 22:00 would
    have its evening's work filed under tomorrow, and the same query would give
    a different answer depending on which machine ran it. The chain operates in
    one timezone, so the business day is one thing, and this is it.
    """
    return local_date(datetime.now(UTC))


def at_local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Build the UTC instant for a wall-clock time on a business day.

    The inverse of `local`, and the only correct way to write a timestamp that
    is supposed to mean "3pm at the branch".
    """
    return datetime.combine(
        day, time(hour, minute, second), tzinfo=BUSINESS_TZ
    ).astimezone(ZoneInfo("UTC"))


def is_after_hours(
    moment: datetime, opens: time = OPENS, closes: time = CLOSES
) -> bool:
    wall = local(moment).time()
    return not (opens <= wall <= closes)
