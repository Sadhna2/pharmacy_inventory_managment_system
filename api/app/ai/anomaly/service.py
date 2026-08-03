"""Anomaly detection over the stock ledger.

WHAT THIS IS FOR
----------------
A chain of six locations posts a few hundred movements a week. Nobody reads
them. Stock walks out of a branch a dozen strips at a time, an insulin carton
is written off after a fridge failure, someone dispenses at 3am — and every one
of those is a single row in a table with fifty thousand rows in it. This finds
the rows worth looking at.

WHY MEDIAN AND MAD, NOT MEAN AND STANDARD DEVIATION
---------------------------------------------------
The textbook z-score is (x - mean) / sd. It fails here for a specific reason:
the outlier you are hunting is inside the mean and the sd you are measuring it
against. One 400-unit theft drags the mean up and inflates the sd, so the
theft scores 1.9 and passes. Statisticians call this a breakdown point of 0 —
a single bad value ruins the estimator.

The median and the median absolute deviation have a breakdown point of 50%:
half the data can be garbage before they move. So:

    score = 0.6745 * (x - median) / MAD

0.6745 is the constant that makes MAD comparable to a standard deviation for
normally-distributed data, so a threshold of 3.5 keeps its usual "very unusual"
meaning rather than becoming an arbitrary dial.

WHY NOT A MODEL
---------------
An isolation forest or an autoencoder would score these too, and would be
unable to say why. "Branch 3, Alprazolam, 14 units short at a cycle count on 4
July — normally 0" is something a manager can walk downstairs and act on. A
number between 0 and 1 is not. Every finding here carries the baseline it broke
and the row that broke it.

Detectors are deliberately separate rather than one blended score: shrinkage,
a 3am dispense, and a cold-chain write-off are different problems with
different owners, and merging them into one ranked list would bury the small
persistent theft under the big one-off breakage.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models.enums import MovementType
from app.models.masters import Product, Warehouse
from app.models.stock import StockMovement

# --- shipped defaults -------------------------------------------------------
#
# Every one of these is overridable by an administrator under Setup → Settings
# (see app/core/tunables.py). They stay here as the values the system ships
# with and falls back to, and they are what the `Thresholds` object below is
# populated from when nobody has changed anything.

#: Robust-z threshold. 3.5 is the conventional cut for the modified z-score
#: (Iglewicz & Hoaglin); below ~3 a busy pharmacy generates noise all day.
Z_THRESHOLD = 3.5

#: Below this, "unusual" is meaningless — with four observations every value
#: is either the median or an outlier.
MIN_HISTORY = 10

#: Trailing window for consumption. Four weeks holds four of each weekday, so
#: the weekly rhythm is inside the baseline rather than being detected as an
#: anomaly every Saturday, while still being short enough that a season shifts
#: the baseline with it instead of being flagged as one long spike.
BASELINE_DAYS = 28

#: A dispense of two strips is not interesting however unusual it is. Findings
#: under this many units are dropped before scoring.
MIN_UNITS = 5

#: Value floor, in rupees, for write-off and shrinkage findings. Keeps a ₹40
#: breakage off a manager's morning list.
MIN_VALUE = Decimal("500")

#: Materiality: a loss this size is worth a question on its own, without any
#: statistics behind it. Roughly one carton of cold-chain product — the sort of
#: write-off a branch manager should expect to be asked about.
MATERIAL_VALUE = Decimal("5000")

#: Out-of-hours movements this close together are one visit, not several.
SESSION_GAP_MINUTES = 60

#: Movement types that mean "stock left without being sold". These are the
#: shrinkage surface.
LOSS_TYPES = (
    MovementType.CYCLE_COUNT_ADJ,
    MovementType.ADJUSTMENT,
    MovementType.DAMAGE,
    MovementType.SCRAP,
    MovementType.EXPIRY_WRITEOFF,
)

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Thresholds:
    """Everything an administrator can tune, resolved once per run.

    Passed down rather than read inside the detectors: they loop over tens of
    thousands of rows, and a settings lookup per row would be absurd. Building
    it once at the top of `detect()` also means one run cannot straddle a
    settings change halfway through and produce a report computed under two
    different rules.
    """

    z_threshold: float
    min_history: int
    baseline_days: int
    min_units: float
    min_value: Decimal
    material_value: Decimal
    session_gap_minutes: int
    opens: time
    closes: time

    @classmethod
    def load(cls, db: Session) -> "Thresholds":
        from app.core import clock as clock_module
        from app.services import settings as app_settings

        return cls(
            z_threshold=app_settings.get(db, "anomaly.z_threshold"),
            min_history=app_settings.get(db, "anomaly.min_history"),
            baseline_days=app_settings.get(db, "anomaly.baseline_days"),
            min_units=app_settings.get(db, "anomaly.min_units"),
            min_value=Decimal(str(app_settings.get(db, "anomaly.min_value"))),
            material_value=Decimal(str(app_settings.get(db, "anomaly.material_value"))),
            session_gap_minutes=app_settings.get(db, "anomaly.session_gap_minutes"),
            opens=clock_module.parse_hour(app_settings.get(db, "business.opens")),
            closes=clock_module.parse_hour(app_settings.get(db, "business.closes")),
        )

    @property
    def hours_label(self) -> str:
        return f"{self.opens.strftime('%H:%M')}–{self.closes.strftime('%H:%M')}"


@dataclass
class Anomaly:
    """One thing worth looking at.

    `explanation` is the whole point: it must read as a sentence a manager can
    act on without opening the ledger.
    """

    kind: str
    severity: str
    occurred_at: datetime
    product_id: int | None
    product_name: str
    sku: str
    warehouse_id: int
    warehouse_name: str
    quantity: float
    value: float
    score: float
    explanation: str
    #: What the finding was measured against, so the number can be challenged.
    baseline: dict = field(default_factory=dict)
    movement_ids: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identity, so the UI can key rows without a database id.

        Findings are derived, not stored — the same ledger always produces the
        same key for the same finding.
        """
        return f"{self.kind}:{self.warehouse_id}:{self.product_id}:{int(self.occurred_at.timestamp())}"


@dataclass
class _Row:
    """A movement, flattened. Loaded once and shared by every detector."""

    id: int
    movement_type: MovementType
    product_id: int
    warehouse_id: int
    quantity: Decimal
    unit_cost: Decimal | None
    occurred_at: datetime
    notes: str | None

    @property
    def value(self) -> Decimal:
        return abs(self.quantity) * (self.unit_cost or Decimal(0))


def _robust_z(
    value: float, values: list[float], min_history: int = MIN_HISTORY
) -> tuple[float, float, float]:
    """(score, median, MAD) for `value` against `values`.

    Returns a score of 0 when MAD is 0 — a series that never varies (a product
    dispensed in fives every single day) would otherwise score every deviation
    as infinite. Falling back to the mean-based z here was tempting and wrong:
    it reintroduces exactly the contamination this function exists to avoid.
    """
    if len(values) < min_history:
        return 0.0, 0.0, 0.0
    med = median(values)
    mad = median([abs(v - med) for v in values])
    if mad == 0:
        return 0.0, med, 0.0
    return 0.6745 * (value - med) / mad, med, mad


def _severity(score: float, threshold: float = Z_THRESHOLD) -> str:
    """Banded relative to the threshold, not against fixed numbers.

    An administrator who drops the sensitivity to 2.5 wants *more* findings,
    not the same findings all relabelled "low" because the old 4.5 cut still
    applies. The bands move with the dial.
    """
    if score >= threshold * 1.7:
        return "high"
    if score >= threshold * 1.3:
        return "medium"
    return "low"


def _load(db: Session, since: date) -> tuple[list[_Row], dict[int, Product], dict[int, str]]:
    rows = [
        _Row(*r)
        for r in db.execute(
            select(
                StockMovement.id,
                StockMovement.movement_type,
                StockMovement.product_id,
                StockMovement.warehouse_id,
                StockMovement.quantity,
                StockMovement.unit_cost,
                StockMovement.occurred_at,
                StockMovement.notes,
            )
            .where(StockMovement.occurred_at >= datetime.combine(since, time.min))
            .order_by(StockMovement.occurred_at)
        )
    ]
    products = {p.id: p for p in db.scalars(select(Product))}
    warehouses = dict(db.execute(select(Warehouse.id, Warehouse.name)).all())
    return rows, products, warehouses


# --- detectors --------------------------------------------------------------
#
# Each takes the loaded rows and returns findings. They share no state, so a
# detector can be added or removed without touching the others.


def _detect_consumption(
    rows: list[_Row], products: dict[int, Product], warehouses: dict[int, str],
    cfg: Thresholds,
) -> list[Anomaly]:
    """Days where a branch issued far more (or less) of something than usual.

    Aggregated to a day per (product, location) before scoring. Scoring
    individual movements instead would flag every large single dispense to a
    hospital, which is a customer, not an anomaly.

    THE BASELINE IS THE LAST FOUR WEEKS, NOT THE LAST TWO YEARS
    -----------------------------------------------------------
    Paracetamol sells five times more in the monsoon than in February. Scored
    against a whole-year median, every single day of the monsoon is a 5-sigma
    event and the screen fills up with the weather. That is not a detector, it
    is a calendar.

    A trailing 28-day window fixes it without any seasonality model: by July
    the baseline is already July, so what survives is a day that broke from the
    week around it. Trailing rather than centred, because a detector that peeks
    at the following fortnight cannot be run on today.

    NEAR-ZERO SERIES ARE SKIPPED, NOT SCORED
    ----------------------------------------
    A product a location sells twice a month has a median of zero, and every
    sale is then infinitely unusual. Ratios like "76× the usual 3" come from
    exactly this and mean nothing. Those series are dropped: sporadic demand is
    real, but it cannot be monitored this way and pretending otherwise buries
    the findings that count.
    """
    daily: dict[tuple[int, int], dict[date, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    ids: dict[tuple[int, int, date], list[int]] = defaultdict(list)

    for row in rows:
        if row.movement_type is not MovementType.SALE_ISSUE:
            continue
        # Local day, not UTC day: a 9pm IST sale is 15:30 UTC, but an 11pm one
        # is 17:30 UTC on a date that is still today — grouping on UTC would
        # split each trading day in the wrong place and flatten the spikes.
        day = clock.local_date(row.occurred_at)
        key = (row.product_id, row.warehouse_id)
        daily[key][day] += float(abs(row.quantity))
        ids[(row.product_id, row.warehouse_id, day)].append(row.id)

    out: list[Anomaly] = []
    for (product_id, warehouse_id), by_day in daily.items():
        if len(by_day) < cfg.min_history:
            continue
        for day, units in sorted(by_day.items()):
            if units < cfg.min_units:
                continue
            # Days with no sale are real zeros, not missing data. Leaving them
            # out would pull the median up and hide genuine collapses.
            window = [
                by_day.get(day - timedelta(days=back), 0.0)
                for back in range(1, cfg.baseline_days + 1)
            ]
            score, med, _ = _robust_z(units, window, cfg.min_history)
            if abs(score) < cfg.z_threshold or med < cfg.min_units:
                continue

            product = products.get(product_id)
            direction = "more" if score > 0 else "less"
            multiple = units / med
            out.append(
                Anomaly(
                    kind="consumption",
                    severity=_severity(abs(score), cfg.z_threshold),
                    occurred_at=datetime.combine(day, time(12, 0)),
                    product_id=product_id,
                    product_name=product.name if product else f"Product {product_id}",
                    sku=product.sku if product else "",
                    warehouse_id=warehouse_id,
                    warehouse_name=warehouses.get(warehouse_id, ""),
                    quantity=round(units, 2),
                    value=0.0,
                    score=round(abs(score), 1),
                    explanation=(
                        f"{units:,.0f} units issued — {multiple:.1f}× the "
                        f"{med:,.0f} a day this branch had been running."
                        if score > 0
                        else f"Only {units:,.0f} units issued, against "
                        f"{med:,.0f} a day over the previous four weeks."
                    ),
                    baseline={
                        "typical_units": round(med, 1),
                        "days_measured": cfg.baseline_days,
                        "direction": direction,
                    },
                    movement_ids=ids[(product_id, warehouse_id, day)][:20],
                )
            )
    return out


def _detect_shrinkage(
    rows: list[_Row], products: dict[int, Product], warehouses: dict[int, str],
    cfg: Thresholds,
) -> list[Anomaly]:
    """Stock written off without a sale behind it.

    TWO WAYS IN, AND THE SECOND ONE MATTERS MOST
    --------------------------------------------
    The statistical path scores each write-off against that location's own
    history — per location rather than chain-wide, because a busy central
    warehouse legitimately writes off more than a branch counter, and pooling
    them would flag the warehouse every week and never flag the branch.

    But a branch that writes off five things a year has no distribution to
    score against, and that is exactly the branch where a single ₹7,000 carton
    of insulin going in the bin should be a question. Demanding ten prior
    write-offs before anyone is allowed to notice one is backwards, so a second
    path flags any loss above a materiality threshold regardless of history —
    which is how a controller reads a ledger anyway: materiality first,
    variance second.
    """
    losses = [
        row
        for row in rows
        if row.movement_type in LOSS_TYPES and row.quantity < 0
    ]
    by_warehouse: dict[int, list[_Row]] = defaultdict(list)
    for row in losses:
        by_warehouse[row.warehouse_id].append(row)

    out: list[Anomaly] = []
    for warehouse_id, warehouse_losses in by_warehouse.items():
        has_baseline = len(warehouse_losses) >= cfg.min_history
        for row in warehouse_losses:
            if row.value < cfg.min_value or abs(row.quantity) < cfg.min_units:
                continue

            score, med = 0.0, 0.0
            if has_baseline:
                others = [float(r.value) for r in warehouse_losses if r.id != row.id]
                score, med, _ = _robust_z(float(row.value), others, cfg.min_history)

            material = row.value >= cfg.material_value
            if score < cfg.z_threshold and not material:
                continue

            product = products.get(row.product_id)
            counted = row.movement_type is MovementType.CYCLE_COUNT_ADJ
            written = row.movement_type.value.replace("_", " ").lower()
            if score >= cfg.z_threshold:
                because = (
                    f"this location's typical write-off is ₹{med:,.0f}."
                )
                severity = _severity(score, cfg.z_threshold)
            else:
                because = (
                    "large enough to be worth signing off on its own"
                    + ("." if has_baseline else ", and this location has too few "
                       "write-offs to compare it against.")
                )
                # Materiality alone is a medium: it is unusual in money, not
                # necessarily unusual in behaviour.
                severity = "high" if row.value >= cfg.material_value * 3 else "medium"

            out.append(
                Anomaly(
                    # A count variance is missing stock; a damage write-off is
                    # destroyed stock. Same maths, different conversation.
                    kind="shrinkage" if counted else "write_off",
                    severity=severity,
                    occurred_at=row.occurred_at,
                    product_id=row.product_id,
                    product_name=product.name if product else "",
                    sku=product.sku if product else "",
                    warehouse_id=warehouse_id,
                    warehouse_name=warehouses.get(warehouse_id, ""),
                    quantity=float(abs(row.quantity)),
                    value=round(float(row.value), 2),
                    score=round(score, 1),
                    explanation=(
                        f"{abs(row.quantity):,.0f} units written off as {written} "
                        f"(₹{row.value:,.0f}) — {because}"
                        + (f" Note: {row.notes}" if row.notes else "")
                    ),
                    baseline={
                        "typical_value": round(med, 2) if has_baseline else None,
                        "write_offs_measured": len(warehouse_losses) - 1,
                        "movement_type": row.movement_type.value,
                        "flagged_by": "variance" if score >= cfg.z_threshold else "materiality",
                    },
                    movement_ids=[row.id],
                )
            )
    return out


def _after_hours_finding(
    session: list[_Row], products: dict[int, Product], warehouses: dict[int, str],
    cfg: Thresholds,
) -> Anomaly:
    """One out-of-hours session, described as a person would describe it."""
    first, last = session[0], session[-1]
    started = clock.local(first.occurred_at).strftime("%H:%M")
    ended = clock.local(last.occurred_at).strftime("%H:%M")
    units = sum(abs(r.quantity) for r in session)
    value = sum(r.value for r in session)
    skus = {r.product_id for r in session}

    # The product fields describe the session's single product where there is
    # one, and stay empty where there isn't — better than naming an arbitrary
    # member of a mixed batch and implying the rest were the same.
    single = products.get(first.product_id) if len(skus) == 1 else None

    if len(session) == 1:
        what = f"{units:,.0f} units moved at {started}"
    else:
        what = (
            f"{len(session)} movements between {started} and {ended} "
            f"({units:,.0f} units across {len(skus)} product"
            f"{'s' if len(skus) > 1 else ''})"
        )
    note = first.notes if len({r.notes for r in session}) == 1 and first.notes else None

    return Anomaly(
        kind="after_hours",
        severity="high",
        occurred_at=first.occurred_at,
        product_id=single.id if single else None,
        product_name=single.name if single else "",
        sku=single.sku if single else "",
        warehouse_id=first.warehouse_id,
        warehouse_name=warehouses.get(first.warehouse_id, ""),
        quantity=float(units),
        value=round(float(value), 2),
        # Fixed score: this detector is a rule, and pretending it produced a
        # z-score would be dishonest about how it decided.
        score=0.0,
        explanation=(
            f"{what} — outside {cfg.hours_label}."
            + (f" Note: {note}" if note else "")
        ),
        baseline={
            "hours": cfg.hours_label,
            "movements": len(session),
            "types": sorted({r.movement_type.value for r in session}),
        },
        movement_ids=[r.id for r in session][:50],
    )


def _detect_after_hours(
    rows: list[_Row], products: dict[int, Product], warehouses: dict[int, str],
    cfg: Thresholds,
) -> list[Anomaly]:
    """Stock moving when the shop is shut.

    Not a statistic: one 3am dispense is worth a question regardless of how
    many others there were. Receipts are excluded — deliveries genuinely
    arrive before opening, and flagging every early lorry would train people
    to ignore this list.

    CLUSTERED BY SESSION, NOT BY ROW
    -------------------------------
    One operator action can write a dozen movements: a batch recall quarantines
    every affected lot, then scraps them, and a transfer writes a dispatch and
    a receipt. Emitting a finding per row turns "somebody was in the warehouse
    at 1am" into thirty identical alerts and makes the screen useless. A run of
    after-hours movements at one location with no {GAP_MINUTES}-minute break in
    it is one session, and one finding.
    """
    candidates = [
        row
        for row in rows
        if row.movement_type
        not in (
            MovementType.PURCHASE_RECEIPT,
            MovementType.TRANSFER_RECEIPT,
            MovementType.OPENING_BALANCE,
        )
        and abs(row.quantity) >= cfg.min_units
        and clock.is_after_hours(row.occurred_at, cfg.opens, cfg.closes)
    ]

    by_warehouse: dict[int, list[_Row]] = defaultdict(list)
    for row in candidates:
        by_warehouse[row.warehouse_id].append(row)

    out: list[Anomaly] = []
    for warehouse_rows in by_warehouse.values():
        warehouse_rows.sort(key=lambda r: r.occurred_at)
        session: list[_Row] = []
        for row in warehouse_rows:
            gap = (
                (row.occurred_at - session[-1].occurred_at).total_seconds() / 60
                if session
                else 0
            )
            if session and gap > cfg.session_gap_minutes:
                out.append(_after_hours_finding(session, products, warehouses, cfg))
                session = []
            session.append(row)
        if session:
            out.append(_after_hours_finding(session, products, warehouses, cfg))
    return out


def _detect_repeat_losses(
    rows: list[_Row], products: dict[int, Product], warehouses: dict[int, str],
    cfg: Thresholds,
) -> list[Anomaly]:
    """The same product going missing at the same branch, again and again.

    Individually each variance is small enough to sign off. This is the
    detector that catches the person taking eight strips a month for a year —
    the pattern is the finding, and no single-row detector can see it.
    """
    counts: dict[tuple[int, int], list[_Row]] = defaultdict(list)
    for row in rows:
        if row.movement_type is MovementType.CYCLE_COUNT_ADJ and row.quantity < 0:
            counts[(row.product_id, row.warehouse_id)].append(row)

    out: list[Anomaly] = []
    for (product_id, warehouse_id), events in counts.items():
        if len(events) < 3:
            continue
        product = products.get(product_id)
        total_units = sum(abs(e.quantity) for e in events)
        total_value = sum(e.value for e in events)
        span = (events[-1].occurred_at.date() - events[0].occurred_at.date()).days or 1
        out.append(
            Anomaly(
                kind="repeat_loss",
                severity="high" if len(events) >= 3 and total_value >= cfg.min_value else "medium",
                occurred_at=events[-1].occurred_at,
                product_id=product_id,
                product_name=product.name if product else "",
                sku=product.sku if product else "",
                warehouse_id=warehouse_id,
                warehouse_name=warehouses.get(warehouse_id, ""),
                quantity=float(total_units),
                value=round(float(total_value), 2),
                score=float(len(events)),
                explanation=(
                    f"{len(events)} unexplained count variances on the same product "
                    f"at this branch over {span} days — {total_units:,.0f} units, "
                    f"₹{total_value:,.0f} in total."
                ),
                baseline={
                    "occurrences": len(events),
                    "span_days": span,
                    "first_seen": events[0].occurred_at.date().isoformat(),
                },
                movement_ids=[e.id for e in events],
            )
        )
    return out


DETECTORS = (
    _detect_consumption,
    _detect_shrinkage,
    _detect_after_hours,
    _detect_repeat_losses,
)


def detect(
    db: Session,
    *,
    lookback_days: int = 90,
    kinds: list[str] | None = None,
    warehouse_id: int | None = None,
    min_severity: str = "low",
) -> list[Anomaly]:
    """Run every detector and return findings, most serious first.

    Nothing is written. Findings are recomputed from the ledger on each call,
    which means a corrected movement makes its finding disappear on the next
    load rather than leaving a stale alert someone has to dismiss.
    """
    cfg = Thresholds.load(db)
    since = clock.today() - timedelta(days=lookback_days)
    rows, products, warehouses = _load(db, since)

    findings: list[Anomaly] = []
    for detector in DETECTORS:
        findings.extend(detector(rows, products, warehouses, cfg))

    if warehouse_id is not None:
        findings = [f for f in findings if f.warehouse_id == warehouse_id]
    if kinds:
        wanted = set(kinds)
        findings = [f for f in findings if f.kind in wanted]

    cutoff = SEVERITY_ORDER.get(min_severity, 2)
    findings = [f for f in findings if SEVERITY_ORDER[f.severity] <= cutoff]

    # Severity first, then recency — an old high beats a fresh low, because the
    # question this list answers is "what should I deal with", not "what
    # happened last".
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], -f.occurred_at.timestamp()))
    return findings


def summarise(findings: list[Anomaly]) -> dict:
    """Counts and exposure, for the header strip above the list."""
    by_kind: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)
    for finding in findings:
        by_kind[finding.kind] += 1
        by_severity[finding.severity] += 1
    return {
        "total": len(findings),
        "high": by_severity.get("high", 0),
        "medium": by_severity.get("medium", 0),
        "low": by_severity.get("low", 0),
        "by_kind": dict(by_kind),
        # Only losses carry a value; a consumption spike is not money lost.
        "value_at_risk": round(
            sum(f.value for f in findings if f.kind in ("shrinkage", "write_off", "repeat_loss")),
            2,
        ),
    }
