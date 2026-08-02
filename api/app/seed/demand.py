"""The demand model behind the synthetic history.

Separated from the generator because this file is the part that has to be
*right*. If demand is flat random noise, every forecast in Layer 2 scores the
same and the whole exercise proves nothing. Structure has to exist in the data
before a model can be credited with finding it.

Four effects, each chosen because it is real and each detectable:

  seasonality   Fever and antibiotic demand climbs through the monsoon
                (Jun–Sep). Respiratory demand climbs Oct–Feb, when Indian
                cities are cold and the air is at its worst. ORS peaks in the
                Apr–Jun heat. Chronic medicines — metformin, statins,
                antihypertensives — barely move, because they are repeat
                prescriptions taken every day of the year.

  weekday       Mondays and Saturdays are busy; Sundays are quiet.

  branch        A hospital-facing branch dispenses in bulk and skews to
                injectables and antibiotics. A residential branch sells
                chronic repeats. A commercial one peaks on weekdays.

  trend         Slow growth over two years, which is what a chain that is
                opening branches looks like.

Chronic products are deliberately given LOW variance and seasonal ones HIGH.
That contrast is the point: a forecast should be near-exact on metformin and
should have to work for paracetamol, and a benchmark that cannot tell those
apart is not measuring anything.
"""

import math
from datetime import date

#: Per-SKU demand character.
#:
#: base            mean units/day at a mid-size branch
#: seasonal_peak   month (1-12) of highest demand; None for flat products
#: amplitude       peak-to-mean swing, as a fraction of base
#: cv              coefficient of variation — day-to-day noise
#: trend           annual growth
PROFILES: dict[str, dict] = {
    # --- chronic repeats: flat, predictable, low noise --------------------
    "MET-500": {"base": 22, "seasonal_peak": None, "amplitude": 0.05, "cv": 0.18, "trend": 0.12},
    "AML-5":   {"base": 16, "seasonal_peak": None, "amplitude": 0.04, "cv": 0.17, "trend": 0.10},
    "ATO-10":  {"base": 14, "seasonal_peak": None, "amplitude": 0.05, "cv": 0.19, "trend": 0.14},
    "INS-GLA": {"base": 3, "seasonal_peak": None, "amplitude": 0.06, "cv": 0.30, "trend": 0.18},
    "VITD3":   {"base": 7, "seasonal_peak": 1, "amplitude": 0.30, "cv": 0.35, "trend": 0.20},

    # --- monsoon fever complex: strong seasonality ------------------------
    "PAR-650": {"base": 48, "seasonal_peak": 8, "amplitude": 0.85, "cv": 0.34, "trend": 0.08},
    "AMOX-500": {"base": 18, "seasonal_peak": 8, "amplitude": 0.70, "cv": 0.38, "trend": 0.06},

    # --- winter/pollution respiratory -------------------------------------
    "CET-10":  {"base": 26, "seasonal_peak": 12, "amplitude": 0.75, "cv": 0.36, "trend": 0.09},

    # --- summer dehydration ------------------------------------------------
    "ORS-21":  {"base": 20, "seasonal_peak": 5, "amplitude": 0.95, "cv": 0.40, "trend": 0.11},

    # --- controlled substance: low volume, tightly dispensed --------------
    "ALP-025": {"base": 4, "seasonal_peak": None, "amplitude": 0.08, "cv": 0.45, "trend": 0.04},

    # --- consumables: track footfall rather than any illness --------------
    "SYR-5ML": {"base": 40, "seasonal_peak": 8, "amplitude": 0.35, "cv": 0.28, "trend": 0.15},
    "COT-100": {"base": 9, "seasonal_peak": None, "amplitude": 0.12, "cv": 0.32, "trend": 0.10},
}

#: How each location's demand differs. `mix` scales specific SKUs on top of
#: the branch's overall size.
BRANCH_PROFILES: dict[str, dict] = {
    # A warehouse supplies branches; it barely retails, and it is shut at
    # the weekend.
    "central": {"scale": 0.15, "weekend": 0.2, "mix": {}},
    # Institutional volumes, and wards do not close on a Sunday.
    "hospital": {
        "scale": 1.6,
        "weekend": 0.85,
        "mix": {"AMOX-500": 1.8, "INS-GLA": 2.2, "SYR-5ML": 2.6, "PAR-650": 1.3},
    },
    # People collect their repeat prescriptions at the weekend.
    "residential": {
        "scale": 1.1,
        "weekend": 1.15,
        "mix": {"MET-500": 1.5, "AML-5": 1.5, "ATO-10": 1.4, "VITD3": 1.3},
    },
    # An office district empties at the weekend.
    "commercial": {
        "scale": 0.9,
        "weekend": 0.45,
        "mix": {"PAR-650": 1.4, "CET-10": 1.3, "ALP-025": 1.2},
    },
    "standard": {"scale": 1.0, "weekend": 0.9, "mix": {}},
}

#: Sunday=6. Indian pharmacies are busiest at the start of the week and on
#: Saturday; Sunday is half a day in most of the country.
WEEKDAY_FACTOR = [1.18, 1.02, 0.98, 0.97, 1.04, 1.12, 0.62]


def seasonal_factor(profile: dict, day: date) -> float:
    """A cosine peaking in `seasonal_peak`, flat when there is no peak.

    A cosine rather than a step per month: demand for fever medicine does not
    jump on 1 June and drop on 30 September, it climbs and falls. A model
    fitted on step functions would learn an artefact of the generator.
    """
    peak = profile["seasonal_peak"]
    if peak is None:
        return 1.0
    # Day-of-year position relative to the peak month's midpoint.
    peak_doy = (peak - 1) * 30.4 + 15
    offset = (day.timetuple().tm_yday - peak_doy) / 365.25
    return 1.0 + profile["amplitude"] * math.cos(2 * math.pi * offset)


def trend_factor(profile: dict, day: date, start: date) -> float:
    years = (day - start).days / 365.25
    return (1.0 + profile["trend"]) ** years


def expected_demand(
    sku: str, branch_kind: str, day: date, start: date
) -> float:
    """Mean units for one SKU at one location on one day, before noise."""
    profile = PROFILES[sku]
    branch = BRANCH_PROFILES.get(branch_kind, BRANCH_PROFILES["standard"])

    weekday = day.weekday()
    weekday_mult = WEEKDAY_FACTOR[weekday]
    if weekday >= 5:
        weekday_mult *= branch["weekend"]

    return (
        profile["base"]
        * branch["scale"]
        * branch["mix"].get(sku, 1.0)
        * seasonal_factor(profile, day)
        * trend_factor(profile, day, start)
        * weekday_mult
    )


def branch_kind(name: str, is_central: bool) -> str:
    """Classify a seeded warehouse from its name.

    The seed labels branches "(Hospital)", "(Residential)", "(Commercial)" —
    reading that rather than hardcoding ids keeps this working if someone adds
    a branch.
    """
    if is_central:
        return "central"
    lowered = name.lower()
    for kind in ("hospital", "residential", "commercial"):
        if kind in lowered:
            return kind
    return "standard"
