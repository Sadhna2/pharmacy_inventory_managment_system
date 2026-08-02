"""The registry of everything an administrator can change.

WHY A REGISTRY AND NOT A SETTINGS TABLE WITH FREE-FORM ROWS
------------------------------------------------------------
A bare key/value table lets anyone write `anomaly.z_threshold = "banana"` and
find out at 2am when the exceptions screen 500s. Every tunable here declares
its type, its bounds and its default, so the API can reject a bad value at the
edge and the code that reads it can assume a number.

It also means the defaults live in **one** place. Nothing in `app/ai/` carries
a magic constant any more; each module asks this registry, and the registry is
the same object the settings screen renders itself from. A setting that exists
in the database but not here is ignored — so removing a feature cannot leave a
live row quietly steering something.

WHAT IS DELIBERATELY *NOT* TUNABLE
-----------------------------------
Anything that would let a setting produce a wrong number rather than a
different one. The append-only ledger, the FEFO rule, the GST split and the
separation-of-duties check are not settings and never will be — they are the
guarantees the system exists to make. What is tunable here is judgement:
how cautious to be, how far back to look, how hard to chase a stockout.
"""

from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["bool", "int", "float", "time"]


@dataclass(frozen=True)
class Tunable:
    key: str
    label: str
    #: Why an administrator would touch this, in one sentence they can act on.
    help: str
    kind: Kind
    default: Any
    group: str
    minimum: float | None = None
    maximum: float | None = None
    #: Shown after the input — "days", "units", "₹".
    unit: str | None = None

    def coerce(self, raw: Any) -> Any:
        """Parse and bounds-check a value coming from the API.

        Raises ValueError with a message meant for a human, because it is going
        straight into a form field's error slot.
        """
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.lower() in ("true", "false"):
                return raw.lower() == "true"
            raise ValueError("must be true or false")

        if self.kind == "time":
            text = str(raw).strip()
            parts = text.split(":")
            if len(parts) != 2:
                raise ValueError("must be a time like 06:00")
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError:
                raise ValueError("must be a time like 06:00") from None
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("must be a real time of day")
            return f"{hour:02d}:{minute:02d}"

        try:
            value = int(raw) if self.kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ValueError(
                "must be a whole number" if self.kind == "int" else "must be a number"
            ) from None
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"cannot be below {self.minimum:g}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"cannot be above {self.maximum:g}")
        return value


#: Groups, in the order the settings screen shows them.
#:
#: There is no "features" group here on purpose. Switching a capability on or
#: off is a `feature_flags` row (models/settings.py), not a tunable — the two
#: were briefly both, and a switch that exists in two places is a switch that
#: will eventually disagree with itself.
GROUPS: list[tuple[str, str]] = [
    ("business", "Trading hours"),
    ("reorder", "Replenishment"),
    ("forecast", "Demand forecast"),
    ("anomaly", "Exceptions"),
    ("leadtime", "Supplier lead times"),
]


TUNABLES: list[Tunable] = [
    # --- business hours ----------------------------------------------------
    Tunable(
        key="business.opens",
        label="Counter opens",
        help="Local time. Stock moving outside these hours is flagged as an "
             "exception, so set them wider than your real hours rather than "
             "narrower — an early delivery should not be an alert.",
        kind="time", default="06:00", group="business",
    ),
    Tunable(
        key="business.closes",
        label="Counter closes",
        help="Local time. Deliveries and inbound transfers are never flagged, "
             "however late they arrive.",
        kind="time", default="23:00", group="business",
    ),

    # --- reorder -----------------------------------------------------------
    Tunable(
        key="reorder.review_period_days",
        label="Review period",
        help="How often somebody actually looks at this screen and places "
             "orders. Orders are sized to cover the gap until the next look, "
             "so setting this shorter than reality means running out between "
             "reviews.",
        kind="int", default=7, minimum=1, maximum=60, unit="days", group="reorder",
    ),
    Tunable(
        key="reorder.service_critical",
        label="Service level — critical drugs",
        help="Cold chain, Schedule H1 and X. 2.33 is a 99% service level: nine "
             "times in a thousand order cycles the shelf empties early. Raising "
             "it buys certainty with cash tied up in stock.",
        kind="float", default=2.33, minimum=1.0, maximum=3.5, group="reorder",
    ),
    Tunable(
        key="reorder.service_high",
        label="Service level — prescription drugs",
        help="Schedule H and anything prescription-only. 1.88 is 97%.",
        kind="float", default=1.88, minimum=1.0, maximum=3.5, group="reorder",
    ),
    Tunable(
        key="reorder.service_standard",
        label="Service level — everything else",
        help="OTC and general lines. 1.65 is 95%.",
        kind="float", default=1.65, minimum=1.0, maximum=3.5, group="reorder",
    ),
    Tunable(
        key="reorder.max_cover_days",
        label="Never order more than",
        help="A hard ceiling on the resulting stock position, whatever the "
             "arithmetic says. Stops a slow supplier with a large minimum "
             "order from recommending eight months of something that expires "
             "in six.",
        kind="int", default=120, minimum=14, maximum=365, unit="days", group="reorder",
    ),
    Tunable(
        key="reorder.min_daily_demand",
        label="Ignore products selling less than",
        help="Below this the arithmetic stops meaning anything — a product "
             "selling three a month has a lead-time demand under one unit and "
             "any safety stock is a rounding artefact. Order those by eye.",
        kind="float", default=0.2, minimum=0.0, maximum=50.0,
        unit="units/day", group="reorder",
    ),

    # --- forecast ----------------------------------------------------------
    Tunable(
        key="forecast.horizon_days",
        label="Forecast horizon",
        help="How far ahead to predict by default. Longer is not better — "
             "error compounds, and replenishment only needs to see past the "
             "lead time plus the review period.",
        kind="int", default=30, minimum=7, maximum=90, unit="days", group="forecast",
    ),
    Tunable(
        key="forecast.lookback_days",
        label="History to learn from",
        help="Shorter reacts faster to a branch whose demand has genuinely "
             "changed; longer is steadier and captures a full season.",
        kind="int", default=730, minimum=60, maximum=1825, unit="days",
        group="forecast",
    ),
    Tunable(
        key="forecast.backtest_days",
        label="Held-out test window",
        help="How much of the recent past to hide from the model, then score "
             "it against. This is what picks the method per product, so it "
             "should be long enough to include every weekday a few times.",
        kind="int", default=28, minimum=7, maximum=90, unit="days",
        group="forecast",
    ),
    Tunable(
        key="forecast.max_fill_run_days",
        label="Fill stockouts shorter than",
        help="A day with no sellable stock is treated as unknown demand, not "
             "zero, and filled from the days around it. A run longer than this "
             "is a decision to stop stocking the product, and inventing demand "
             "across it would be fiction.",
        kind="int", default=14, minimum=1, maximum=90, unit="days",
        group="forecast",
    ),

    # --- anomaly -----------------------------------------------------------
    Tunable(
        key="anomaly.z_threshold",
        label="Sensitivity",
        help="How far from normal a day has to be before it is worth a look. "
             "3.5 is the conventional cut. Lower finds more and cries wolf "
             "more; higher only surfaces the obvious.",
        kind="float", default=3.5, minimum=2.0, maximum=8.0, group="anomaly",
    ),
    Tunable(
        key="anomaly.baseline_days",
        label="Compare against the last",
        help="The window a day is judged against. Four weeks holds four of "
             "each weekday, so the weekly rhythm is inside the baseline rather "
             "than being flagged every Saturday — and a season shifts the "
             "baseline with it instead of showing up as one long spike.",
        kind="int", default=28, minimum=7, maximum=180, unit="days", group="anomaly",
    ),
    Tunable(
        key="anomaly.min_units",
        label="Ignore movements under",
        help="A dispense of two strips is not interesting however unusual it "
             "is.",
        kind="int", default=5, minimum=1, maximum=1000, unit="units",
        group="anomaly",
    ),
    Tunable(
        key="anomaly.min_value",
        label="Ignore write-offs under",
        help="Keeps a small breakage off a manager's morning list.",
        kind="float", default=500.0, minimum=0.0, maximum=100000.0, unit="₹",
        group="anomaly",
    ),
    Tunable(
        key="anomaly.material_value",
        label="Always flag write-offs over",
        help="A loss this size is worth a question on its own, with no "
             "statistics behind it — which matters most at a branch that "
             "writes off so little there is no pattern to compare against.",
        kind="float", default=5000.0, minimum=0.0, maximum=1000000.0, unit="₹",
        group="anomaly",
    ),
    Tunable(
        key="anomaly.session_gap_minutes",
        label="Group out-of-hours activity within",
        help="One recall writes a dozen movements in the same minute. "
             "Movements this close together are treated as one visit and "
             "reported as one finding.",
        kind="int", default=60, minimum=1, maximum=480, unit="minutes",
        group="anomaly",
    ),
    Tunable(
        key="anomaly.min_history",
        label="Need at least",
        help="Observations before a product or location can be judged at all. "
             "Below this, 'unusual' is meaningless — with four data points "
             "every value is either the median or an outlier.",
        kind="int", default=10, minimum=3, maximum=200,
        unit="observations", group="anomaly",
    ),

    # --- lead time ---------------------------------------------------------
    Tunable(
        key="leadtime.lookback_days",
        label="Measure deliveries from the last",
        help="Shorter reacts faster to a distributor who has recently changed; "
             "longer is steadier.",
        kind="int", default=730, minimum=30, maximum=3650, unit="days",
        group="leadtime",
    ),
    Tunable(
        key="leadtime.min_sample",
        label="Call a supplier measured after",
        help="Below this, percentiles are noise dressed up as insight. Such "
             "suppliers are still listed, but flagged as unproven and their "
             "quoted lead time is used for ordering instead.",
        kind="int", default=5, minimum=2, maximum=100, unit="deliveries",
        group="leadtime",
    ),
]

BY_KEY: dict[str, Tunable] = {t.key: t for t in TUNABLES}
DEFAULTS: dict[str, Any] = {t.key: t.default for t in TUNABLES}
