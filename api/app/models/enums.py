"""Domain enumerations.

These are created as native PostgreSQL ENUM types so the database itself
rejects invalid values — see ARCHITECTURE.md §6.4.
"""

import enum


class TrackingMode(str, enum.Enum):
    """How precisely a product's stock is tracked (ARCHITECTURE.md §6.6).

    The modes form a stack: every mode has product+warehouse+bin+quantity,
    and each adds dimensions on top.
    """

    NONE = "NONE"  # quantity only — syringes, cotton, devices
    LOT = "LOT"  # + batch number
    LOT_EXPIRY = "LOT_EXPIRY"  # + batch with expiry date, picked FEFO
    SERIAL = "SERIAL"  # one row per physical unit (schema-only, no UI)


class StockStatus(str, enum.Enum):
    """Physical stock that is not sellable still EXISTS (ARCHITECTURE.md §21.2)."""

    AVAILABLE = "AVAILABLE"
    QUARANTINE = "QUARANTINE"  # received, awaiting QC / recalled
    DAMAGED = "DAMAGED"  # present and countable, not sellable
    IN_TRANSIT = "IN_TRANSIT"  # dispatched, not yet received
    RETURNED_PENDING = "RETURNED_PENDING"


class MovementType(str, enum.Enum):
    OPENING_BALANCE = "OPENING_BALANCE"
    PURCHASE_RECEIPT = "PURCHASE_RECEIPT"
    SALE_ISSUE = "SALE_ISSUE"
    RETURN_IN = "RETURN_IN"
    RETURN_OUT = "RETURN_OUT"
    TRANSFER_DISPATCH = "TRANSFER_DISPATCH"
    TRANSFER_RECEIPT = "TRANSFER_RECEIPT"
    ADJUSTMENT = "ADJUSTMENT"
    DAMAGE = "DAMAGE"
    SCRAP = "SCRAP"
    QC_RELEASE = "QC_RELEASE"
    QC_REJECT = "QC_REJECT"
    CYCLE_COUNT_ADJ = "CYCLE_COUNT_ADJ"
    EXPIRY_WRITEOFF = "EXPIRY_WRITEOFF"
    STATUS_CHANGE = "STATUS_CHANGE"


class DrugSchedule(str, enum.Enum):
    """Indian drug schedules (ARCHITECTURE.md §6.10)."""

    OTC = "OTC"
    G = "G"
    H = "H"  # prescription only
    H1 = "H1"  # prescription + register entry
    X = "X"  # narcotics, strictest


class StorageCondition(str, enum.Enum):
    AMBIENT = "AMBIENT"
    COOL = "COOL"  # 8-15 C
    COLD_CHAIN = "COLD_CHAIN"  # 2-8 C — vaccines, insulin
    FROZEN = "FROZEN"


class SerialStatus(str, enum.Enum):
    IN_STOCK = "IN_STOCK"
    ALLOCATED = "ALLOCATED"
    SHIPPED = "SHIPPED"
    RETURNED = "RETURNED"
    SCRAPPED = "SCRAPPED"


class DocumentStatus(str, enum.Enum):
    """Shared lifecycle for POs, SOs, transfers and adjustments."""

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    IN_TRANSIT = "IN_TRANSIT"
    ALLOCATED = "ALLOCATED"
    PICKED = "PICKED"
    SHIPPED = "SHIPPED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ReservationStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class RecallStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    QUARANTINED = "QUARANTINED"
    CLOSED = "CLOSED"


class SourcingPolicy(str, enum.Enum):
    """How a product should reach a branch (ARCHITECTURE.md §1.3).

    Real pharmacy chains run a hybrid. Bulk chronic medication, cold chain and
    controlled drugs route through the central warehouse — that hub is what
    makes near-expiry redistribution between branches possible at all. Fast
    movers and urgent top-ups go straight from the distributor to the branch,
    because the extra handling day costs more than the pooling saves.

    Declaring it per product turns an implicit assumption into a decision the
    reorder engine can act on.
    """

    VIA_CENTRAL = "VIA_CENTRAL"  # buy centrally, transfer out
    DIRECT = "DIRECT"  # distributor delivers to the branch
    EITHER = "EITHER"  # whichever is cheaper or faster on the day


class DemandArchetype(str, enum.Enum):
    """Shape of a product's demand curve (ARCHITECTURE.md §15).

    Set by the synthetic generator and kept on the product because the
    forecasting page uses it to explain *why* a curve looks the way it does.
    Real deployments would infer this from history rather than declare it.
    """

    CHRONIC = "CHRONIC"  # flat, low variance — best forecast accuracy
    SEASONAL_RESPIRATORY = "SEASONAL_RESPIRATORY"  # sharp annual peak
    MONSOON_FEVER = "MONSOON_FEVER"  # broad mid-year peak, different phase
    ANTIBIOTIC = "ANTIBIOTIC"  # prescription-driven, moderate variance
    COLD_CHAIN = "COLD_CHAIN"  # steady, high value
    CONTROLLED = "CONTROLLED"  # low volume, tightly monitored
    OTC_FMCG = "OTC_FMCG"  # weekend uplift, festival spikes
    DEVICE = "DEVICE"  # flat, tracking_mode NONE
    SLOW_SPECIALTY = "SLOW_SPECIALTY"  # intermittent, Croston-shaped
    NEW_LAUNCH = "NEW_LAUNCH"  # short history — exercises the cold-start gate


class ForecastStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SuggestionStatus(str, enum.Enum):
    """A recommendation is inert until a human acts on it (ARCHITECTURE.md §20)."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"  # converted into a draft PO
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"  # superseded by a newer run
