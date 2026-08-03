"""Demand forecasting.

WHAT IS BEING PREDICTED
----------------------
Units of one product issued from one location, per day, for the next N days.
That grain is not arbitrary: it is exactly the number the reorder engine needs
to answer "how much cover do I have left", and forecasting anything coarser
(chain-wide, or monthly) would produce a figure nobody can order against.

DEMAND, NOT SALES
-----------------
The ledger records what left the shelf. On a day a branch was out of stock,
that is zero — but the demand was not zero, it walked to the pharmacy across
the road. Training on unadjusted sales teaches the model that running out is
normal and quietly forecasts the stockout forward forever. Days with no
sellable stock are therefore treated as *missing*, not as zero, and filled from
the surrounding days. This is censored-demand correction, and skipping it is
the single most common way a forecast in this domain goes wrong.

THREE METHODS, AND THE DATA PICKS
----------------------------------
    moving_average   the last 28 days, flat
    seasonal_naive   this weekday, averaged over recent weeks
    holt_winters     level + trend + weekly seasonality

Every series is backtested: fit on everything up to a cutoff, predict the held
-out tail, measure the error, and only then choose. A method that cannot beat
the moving average on a given product does not get used for it. This matters
more than picking a cleverer model — a pharmacy has a dozen SKUs behaving in a
dozen ways, and one global choice is wrong for most of them.

WHY NOT A NEURAL NETWORK
------------------------
Two years of daily history per series is roughly 700 points. LSTMs and
transformers need orders of magnitude more before they beat exponential
smoothing, and on data this size they mostly memorise. Holt-Winters is also
inspectable: when a buyer asks why the forecast rose, "the weekly pattern and
the recent level" is an answer. There is no accuracy being sacrificed here for
simplicity — the simple thing is genuinely the better thing at this scale, and
the backtest numbers are reported so anyone can check that claim.
"""

import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from app.core import clock
from app.core.clock import BUSINESS_TZ
from app.models.enums import MovementType, StockStatus
from app.models.masters import Product, Warehouse
from app.models.stock import StockBalance, StockMovement
from app.services import settings as app_settings

#: Postgres does the UTC->IST conversion so the grouping happens in the
#: database rather than in Python over 50,000 rows. Same timezone as
#: app.core.clock, read from it so the two can never drift apart.
BUSINESS_TZ_NAME = str(BUSINESS_TZ)

#: Weekly rhythm. Pharmacies are busier at weekends in residential areas and
#: quieter in commercial ones, and both patterns repeat on 7.
SEASON = 7

#: Holt-Winters needs two full cycles to estimate a season, and a great deal
#: more before the estimate is worth anything. Below this a series gets the
#: simpler methods only.
MIN_DAYS_SEASONAL = 8 * SEASON

#: Below this there is nothing to fit at all; the caller gets a flat mean.
MIN_DAYS = 21

#: How much of the tail to hold out when scoring methods. Four weeks is long
#: enough to include every weekday four times and short enough to leave most
#: of the history for fitting.
BACKTEST_DAYS = 28

#: A stockout run longer than this is not a blip to interpolate over — the
#: product was delisted, or the branch stopped carrying it. Filling it would
#: invent demand that nobody observed.
MAX_FILL_RUN = 14


@dataclass
class Accuracy:
    """How a method did on data it had not seen."""

    method: str
    #: Mean absolute error, in units. The number to quote — it is in the same
    #: unit as the thing being forecast.
    mae: float
    #: Weighted MAPE: total error over total actual. Plain MAPE is unusable
    #: here because a day with 1 unit of actual demand and 2 forecast scores a
    #: 100% error and drowns out a day that was off by 3 units out of 300.
    wape: float
    #: Share of days the forecast landed within 20% of actual.
    hit_rate: float


@dataclass
class Forecast:
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str
    method: str
    #: One entry per day, starting tomorrow.
    daily: list[float]
    start: date
    #: The winning method's held-out accuracy, and every rival's, so the choice
    #: can be audited.
    accuracy: Accuracy
    alternatives: list[Accuracy]
    history_days: int
    #: Days dropped as censored (no sellable stock) and filled.
    stockout_days: int
    #: Average daily demand over the horizon — what reorder actually consumes.
    daily_mean: float
    #: Widening band, because uncertainty compounds with distance.
    lower: list[float]
    upper: list[float]

    @property
    def total(self) -> float:
        return float(sum(self.daily))

    @property
    def confidence(self) -> str:
        """Plain-language reliability, for a screen rather than a paper."""
        if self.history_days < MIN_DAYS_SEASONAL:
            return "low"
        if self.accuracy.wape <= 0.25:
            return "high"
        if self.accuracy.wape <= 0.45:
            return "medium"
        return "low"


# --- loading ----------------------------------------------------------------


def _issues_query(since: date) -> Select:
    """Daily units issued, per product and location, in business time."""
    local_day = func.date(
        func.timezone(BUSINESS_TZ_NAME, StockMovement.occurred_at)
    ).label("day")
    return (
        select(
            StockMovement.product_id,
            StockMovement.warehouse_id,
            local_day,
            func.sum(func.abs(StockMovement.quantity)).label("units"),
        )
        .where(
            StockMovement.movement_type == MovementType.SALE_ISSUE,
            StockMovement.occurred_at >= datetime.combine(since, time.min),
        )
        .group_by(StockMovement.product_id, StockMovement.warehouse_id, local_day)
    )


def load_series(
    db: Session, *, lookback_days: int = 730
) -> dict[tuple[int, int], dict[date, float]]:
    since = clock.today() - timedelta(days=lookback_days)
    series: dict[tuple[int, int], dict[date, float]] = {}
    for product_id, warehouse_id, day, units in db.execute(_issues_query(since)):
        series.setdefault((product_id, warehouse_id), {})[day] = float(units)
    return series


def _stockout_days(db: Session, product_id: int, warehouse_id: int) -> set[date]:
    """Days the location could not have sold this product.

    Reconstructed from the ledger by replaying movements backwards from
    today's balance. That is more work than reading a snapshot, but a snapshot
    of "what is in stock now" says nothing about what was in stock in March,
    and March is what the model is being trained on.
    """
    on_hand_now = db.scalar(
        select(func.coalesce(func.sum(StockBalance.qty_on_hand), 0)).where(
            StockBalance.product_id == product_id,
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.status == StockStatus.AVAILABLE,
        )
    ) or 0

    local_day = func.date(
        func.timezone(BUSINESS_TZ_NAME, StockMovement.occurred_at)
    ).label("day")
    rows = db.execute(
        select(local_day, func.sum(StockMovement.quantity))
        .where(
            StockMovement.product_id == product_id,
            StockMovement.warehouse_id == warehouse_id,
            StockMovement.status == StockStatus.AVAILABLE,
        )
        .group_by(local_day)
        .order_by(local_day)
    ).all()
    if not rows:
        return set()

    by_day = {row[0]: float(row[1]) for row in rows}
    # Walk back from today: closing balance of day D-1 is closing of D minus
    # D's net movement.
    days = sorted(by_day)
    closing: dict[date, float] = {}
    running = float(on_hand_now)
    for day in reversed(days):
        closing[day] = running
        running -= by_day[day]

    # Zero closing stock means the shelf was empty at the end of that day, so
    # any unmet demand that day is invisible.
    return {day for day, qty in closing.items() if qty <= 0}


def build_daily(
    observations: dict[date, float],
    stockouts: set[date],
    *,
    end: date,
    lookback_days: int,
    max_fill_run: int = MAX_FILL_RUN,
) -> tuple[np.ndarray, list[date], int]:
    """A dense, gap-filled daily array ready to model.

    Returns (values, dates, filled_count). Days with no movement are genuine
    zeros — a pharmacy that sold nothing on Sunday sold nothing. Days that were
    *stocked out* are the exception: those are unobservable, not zero, and get
    the median of the fortnight around them.
    """
    start = end - timedelta(days=lookback_days - 1)
    if observations:
        start = max(start, min(observations))

    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    values = np.array([observations.get(day, 0.0) for day in dates], dtype=float)

    censored = np.array([day in stockouts for day in dates])
    filled = 0
    if censored.any():
        # Long runs are left alone: a fortnight with no stock is a decision,
        # not an outage, and inventing demand across it would be fiction.
        runs = _runs(censored)
        for lo, hi in runs:
            if hi - lo > max_fill_run:
                continue
            window = np.concatenate(
                [values[max(0, lo - 7) : lo], values[hi : hi + 7]]
            )
            healthy = window[window > 0]
            if healthy.size == 0:
                continue
            values[lo:hi] = float(np.median(healthy))
            filled += hi - lo

    return values, dates, filled


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous [start, end) spans where mask is True."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


# --- methods ----------------------------------------------------------------
#
# Each takes a training array and a horizon, and returns `horizon` predictions.
# Same signature throughout so the backtest can treat them interchangeably.


def _moving_average(train: np.ndarray, horizon: int) -> np.ndarray:
    window = train[-BACKTEST_DAYS:] if train.size >= BACKTEST_DAYS else train
    return np.repeat(float(window.mean()) if window.size else 0.0, horizon)


def _seasonal_naive(train: np.ndarray, horizon: int) -> np.ndarray:
    """Each future weekday gets the mean of that weekday recently.

    The mean of the last few same-weekdays rather than the single last one:
    one bad Tuesday should not become every Tuesday.
    """
    if train.size < SEASON:
        return _moving_average(train, horizon)
    out = np.empty(horizon)
    for i in range(horizon):
        offset = (i % SEASON) + 1
        # Positions in train that share this weekday, most recent first.
        same = train[-offset::-SEASON][:4]
        out[i] = float(same.mean()) if same.size else float(train.mean())
    return out


def _holt_winters(train: np.ndarray, horizon: int) -> np.ndarray:
    """Additive level, trend and weekly season.

    Additive rather than multiplicative because a branch can legitimately sell
    zero of something on a quiet day, and multiplicative seasonality is
    undefined at zero. Damped trend: an undamped one extrapolates a fortnight
    of growth into a year of it, which is how forecasts end up ordering three
    months of paracetamol.
    """
    if train.size < MIN_DAYS_SEASONAL:
        return _seasonal_naive(train, horizon)
    try:
        model = ExponentialSmoothing(
            train,
            trend="add",
            damped_trend=True,
            seasonal="add",
            seasonal_periods=SEASON,
            initialization_method="estimated",
        ).fit(optimized=True)
        forecast = np.asarray(model.forecast(horizon), dtype=float)
    except Exception:
        # Optimisation can fail to converge on a degenerate series. Falling
        # back beats propagating an exception into a report endpoint.
        return _seasonal_naive(train, horizon)
    if not np.all(np.isfinite(forecast)):
        return _seasonal_naive(train, horizon)
    return forecast


METHODS = {
    "moving_average": _moving_average,
    "seasonal_naive": _seasonal_naive,
    "holt_winters": _holt_winters,
}


# --- scoring ----------------------------------------------------------------


def score(actual: np.ndarray, predicted: np.ndarray, method: str) -> Accuracy:
    predicted = np.clip(predicted, 0, None)
    error = np.abs(actual - predicted)
    total = float(actual.sum())
    within = (
        error <= np.maximum(actual * 0.2, 1.0)
    )  # ±20%, with a 1-unit floor so tiny days are not unwinnable
    return Accuracy(
        method=method,
        mae=round(float(error.mean()), 2),
        wape=round(float(error.sum() / total) if total > 0 else 0.0, 3),
        hit_rate=round(float(within.mean()), 3),
    )


def backtest(
    values: np.ndarray, holdout_days: int = BACKTEST_DAYS
) -> tuple[str, list[Accuracy]]:
    """Fit every method on the head, score it on the tail, rank them.

    The held-out tail is never seen during fitting, so these numbers are an
    honest estimate of next month rather than a description of last month.
    """
    holdout = min(holdout_days, max(7, values.size // 4))
    train, test = values[:-holdout], values[-holdout:]

    results = [
        score(test, METHODS[name](train, holdout), name)
        for name in METHODS
        if train.size >= MIN_DAYS
    ]
    if not results:
        return "moving_average", []

    # Ranked on WAPE, tie-broken on MAE. Deliberately not on hit rate: a method
    # can hit the ±20% band often while being wildly wrong on the busy days,
    # and the busy days are the ones that empty a shelf.
    results.sort(key=lambda a: (a.wape, a.mae))
    return results[0].method, results


def _band(daily: np.ndarray, residual_scale: float) -> tuple[list[float], list[float]]:
    """A widening interval around the point forecast.

    Grows with the square root of the horizon, which is what an error that
    accumulates independently day to day does. Not a formal prediction
    interval — it is a stated approximation, and honest about being one.
    """
    horizon = np.arange(1, daily.size + 1)
    spread = residual_scale * np.sqrt(horizon)
    return (
        [round(float(v), 1) for v in np.clip(daily - spread, 0, None)],
        [round(float(v), 1) for v in daily + spread],
    )


# --- entry points -----------------------------------------------------------


def forecast_one(
    db: Session,
    product: Product,
    warehouse: Warehouse,
    observations: dict[date, float],
    *,
    horizon: int = 30,
    lookback_days: int = 730,
    backtest_days: int = BACKTEST_DAYS,
    max_fill_run: int = MAX_FILL_RUN,
) -> Forecast | None:
    stockouts = _stockout_days(db, product.id, warehouse.id)
    values, dates, filled = build_daily(
        observations, stockouts, end=clock.today(),
        lookback_days=lookback_days, max_fill_run=max_fill_run,
    )
    if values.size < MIN_DAYS or values.sum() == 0:
        return None

    method, results = backtest(values, backtest_days)
    daily = np.clip(METHODS[method](values, horizon), 0, None)

    chosen = next(
        (r for r in results if r.method == method),
        Accuracy(method=method, mae=0.0, wape=0.0, hit_rate=0.0),
    )
    return Forecast(
        product_id=product.id,
        sku=product.sku,
        product_name=product.name,
        warehouse_id=warehouse.id,
        warehouse_name=warehouse.name,
        method=method,
        daily=[round(float(v), 1) for v in daily],
        start=clock.today() + timedelta(days=1),
        accuracy=chosen,
        alternatives=[r for r in results if r.method != method],
        history_days=len(dates),
        stockout_days=filled,
        daily_mean=round(float(daily.mean()), 2),
        **dict(
            zip(
                ("lower", "upper"),
                # Scale the band on the winning method's own held-out error,
                # so a series that is genuinely hard to predict gets a wide
                # band and says so.
                _band(daily, chosen.mae or float(values.std())),
                strict=True,
            )
        ),
    )


#: Fitted forecasts, keyed by the request AND by the ledger's high-water mark.
#:
#: Fitting sixty series takes about twelve seconds, which is fine for a nightly
#: job and far too slow for a page load. A time-based TTL would have to choose
#: between serving stale numbers and refitting for nothing; keying on
#: MAX(stock_movements.id) does neither. The ledger is append-only, so that id
#: only ever moves forward, and it moves exactly when a forecast could have
#: changed. One cheap index scan per request buys an exact invalidation.
_CACHE: dict[tuple, tuple[tuple[int, int], list[Forecast]]] = {}
_CACHE_LIMIT = 32

#: One fit at a time, process-wide.
#:
#: Without this, three people opening the forecast screen on a cold cache run
#: three identical thirty-second fits at once on a two-core box, and all three
#: get slower. The lock makes the second and third wait for the first and then
#: read its result. Held across the fit rather than around the dictionary
#: because the fit is the expensive part; the API runs one uvicorn worker, so
#: process-wide is chain-wide here.
_FIT_LOCK = threading.Lock()


def _ledger_version(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.max(StockMovement.id), 0))) or 0)


def forecast_all(
    db: Session,
    *,
    horizon: int | None = None,
    lookback_days: int | None = None,
    warehouse_id: int | None = None,
    product_id: int | None = None,
) -> list[Forecast]:
    """Every (product, location) pair with enough history to be worth fitting."""
    horizon = horizon or app_settings.get(db, "forecast.horizon_days")
    lookback_days = lookback_days or app_settings.get(db, "forecast.lookback_days")
    backtest_days = app_settings.get(db, "forecast.backtest_days")
    max_fill_run = app_settings.get(db, "forecast.max_fill_run_days")

    key = (horizon, lookback_days, backtest_days, max_fill_run, warehouse_id, product_id)
    # The settings version is half the cache key. Changing the horizon or the
    # held-out window has to throw the fitted models away — a cache that
    # survived it would serve numbers computed under the old rules while the
    # screen showed the new ones.
    version = (_ledger_version(db), app_settings.version())
    cached = _CACHE.get(key)
    if cached and cached[0] == version:
        return cached[1]

    with _FIT_LOCK:
        # Re-check inside the lock: whoever held it may have been fitting
        # exactly this, in which case there is nothing left to do.
        cached = _CACHE.get(key)
        if cached and cached[0] == version:
            return cached[1]
        return _fit_all(
            db,
            key=key,
            version=version,
            horizon=horizon,
            lookback_days=lookback_days,
            backtest_days=backtest_days,
            max_fill_run=max_fill_run,
            warehouse_id=warehouse_id,
            product_id=product_id,
        )


def _fit_all(
    db: Session,
    *,
    key: tuple,
    version: tuple[int, int],
    horizon: int,
    lookback_days: int,
    backtest_days: int,
    max_fill_run: int,
    warehouse_id: int | None,
    product_id: int | None,
) -> list[Forecast]:
    series = load_series(db, lookback_days=lookback_days)
    products = {p.id: p for p in db.scalars(select(Product))}
    warehouses = {w.id: w for w in db.scalars(select(Warehouse))}

    out: list[Forecast] = []
    for (pid, wid), observations in series.items():
        if warehouse_id is not None and wid != warehouse_id:
            continue
        if product_id is not None and pid != product_id:
            continue
        product, warehouse = products.get(pid), warehouses.get(wid)
        if product is None or warehouse is None:
            continue
        result = forecast_one(
            db, product, warehouse, observations,
            horizon=horizon, lookback_days=lookback_days,
            backtest_days=backtest_days, max_fill_run=max_fill_run,
        )
        if result:
            out.append(result)

    out.sort(key=lambda f: -f.total)

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = (version, out)
    return out


def daily_demand(
    db: Session, product_id: int, warehouse_id: int, *, horizon: int | None = None
) -> float:
    """Just the number the reorder engine needs: expected units per day.

    Falls back to the trailing mean when there is not enough history to fit —
    a rough figure beats no recommendation, and the caller is told which it
    got via the forecast's `confidence`.
    """
    forecasts = forecast_all(
        db, horizon=horizon, warehouse_id=warehouse_id, product_id=product_id
    )
    return forecasts[0].daily_mean if forecasts else 0.0
