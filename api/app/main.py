import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DataError

from app.ai.anomaly import router as anomaly_router
from app.ai.forecasting import router as forecasting_router
from app.ai.intake import router as intake_router
from app.ai.leadtime import router as leadtime_router
from app.ai.reorder import router as reorder_router
from app.api.v1 import audit, auth, masters, operations, products, stock, users
from app.api.v1 import settings as settings_api
from app.core import api_docs
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.db.session import engine

configure_logging()
log = get_logger("api")

def _warm_forecast_cache() -> None:
    """Fit the default forecasts once, off the request path.

    Fitting sixty Holt-Winters models takes about twelve seconds locally and
    twice that on a 2 GB t4g. Without this, whoever opens the forecast screen
    first pays for all of it. The result is cached against the ledger's
    high-water mark, so this is the same work that request would have done —
    only nobody is watching a spinner while it happens.

    Runs in a plain thread rather than a task: it is CPU-bound numpy, and
    awaiting it would block the event loop for the whole fit. Failures are
    logged and swallowed — a cold cache is slow, but a container that refuses
    to start because a forecast failed is down.
    """
    try:
        from app.ai.forecasting import service as forecasting
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            count = len(forecasting.forecast_all(db, horizon=30))
        log.info("forecast_cache_warmed", series=count)
    except Exception as exc:  # noqa: BLE001 — startup must never be fatal here
        log.warning("forecast_cache_warm_failed", error=str(exc))


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(
        target=_warm_forecast_cache, name="forecast-warmup", daemon=True
    ).start()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Pharmacy Inventory Management System",
    description=(
        "Multi-branch pharmacy chain inventory with an append-only stock ledger, "
        "batch/expiry tracking with FEFO, GST, and RBAC.\n\n"
        "**Every endpoint below says which screen uses it** — the tags are named "
        "after the sections in the product's sidebar, and each operation ends "
        "with a *Used by* line naming the screen and describing what it "
        "computes. Nothing here is unreachable from the interface.\n\n"
        "Two things worth knowing before reading:\n\n"
        "* **`stock_movements` is append-only.** A database trigger rejects "
        "`UPDATE` and `DELETE` on it, so there is no endpoint that edits or "
        "removes a posting — corrections are reversing entries. Every quantity "
        "the product shows is derived from this table.\n"
        "* **The AI endpoints derive, they do not store.** The analysis tags "
        "read the ledger and compute an answer per request; invoice intake "
        "produces a draft for a person to check and has no code path to the "
        "ledger at all."
    ),
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,  # required for the httpOnly refresh cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id to every request, response and log line."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _problem(
    request: Request, status: int, type_: str, title: str, detail: str,
    errors: list | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": type_,
            "title": title,
            "status": status,
            "detail": detail,
            "instance": str(request.url.path),
            "request_id": getattr(request.state, "request_id", None),
            "errors": errors or [],
        },
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _problem(
        request, exc.status_code, exc.error_type, exc.title, exc.detail, exc.errors
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(p) for p in e["loc"][1:]),
            # Pydantic prefixes anything a validator raises as ValueError with
            # "Value error, ". That is a fact about pydantic, and it is read by
            # someone filling in a form who has never heard of it.
            "message": e["msg"].removeprefix("Value error, "),
        }
        for e in exc.errors()
    ]

    # A rule that spans two fields — a GSTIN against the state it must open
    # with — belongs to the model, not to either field, so pydantic gives it no
    # location and the form has nothing to hang it under. "One or more fields
    # are invalid" is then the only thing the user is told, which is exactly
    # the message that leaves someone re-reading a form for the mistake.
    #
    # So when the whole failure is location-less, its own words become the
    # detail. Anything with a location keeps the summary, because those are
    # already rendered against the field they name.
    unplaced = [e["message"] for e in errors if not e["field"]]
    detail = (
        unplaced[0]
        if len(unplaced) == len(errors) == 1
        else "One or more fields are invalid"
    )

    return _problem(
        request, 422, "/errors/validation", "Validation failed", detail, errors
    )


@app.exception_handler(DataError)
async def data_error_handler(request: Request, exc: DataError) -> JSONResponse:
    """A value the database could not accept is the caller's problem, not ours.

    The one that actually happens: an id above 2147483647. Every `{id}` path
    here is a Python `int`, which has no upper bound, so it sails through
    validation and Postgres rejects the comparison against an `integer`
    column — surfacing as a bare 500, the same answer the API gives when it is
    genuinely broken. Anyone probing ids would read that as "found something
    and crashed it", which is the opposite of the truth.

    422 rather than 404 because this is not "no such row": the value is not a
    usable identifier at all, and the same fault arrives through numeric
    filters where 404 would make no sense.

    Scoped to DataError deliberately. Its siblings — IntegrityError, the
    connection failures — are ours, and must keep reaching the handler below
    that logs a trace.
    """
    log.warning(
        "database_rejected_value",
        path=request.url.path,
        request_id=getattr(request.state, "request_id", None),
        error=type(exc.orig).__name__ if exc.orig else type(exc).__name__,
    )
    return _problem(
        request,
        422,
        "/errors/validation",
        "Validation failed",
        "A value in this request is outside the range the system can store.",
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log the full trace, return nothing internal to the client.
    log.exception(
        "unhandled_exception",
        path=request.url.path,
        request_id=getattr(request.state, "request_id", None),
    )
    return _problem(
        request,
        500,
        "/errors/internal",
        "Internal server error",
        "Something went wrong. Quote the request_id when reporting this.",
    )


@app.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready() -> dict[str, str]:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(products.router, prefix=API_PREFIX)
app.include_router(masters.router, prefix=API_PREFIX)
app.include_router(stock.router, prefix=API_PREFIX)
app.include_router(stock.lots_router, prefix=API_PREFIX)

# Layer 1: operations
app.include_router(operations.po_router, prefix=API_PREFIX)
app.include_router(operations.grn_router, prefix=API_PREFIX)
app.include_router(operations.so_router, prefix=API_PREFIX)
app.include_router(operations.tr_router, prefix=API_PREFIX)
app.include_router(operations.adj_router, prefix=API_PREFIX)
app.include_router(operations.rc_router, prefix=API_PREFIX)

# Compliance: read-only view of the trail every action above writes.
app.include_router(audit.router, prefix=API_PREFIX)

# Administration
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(users.roles_router, prefix=API_PREFIX)
app.include_router(settings_api.router, prefix=API_PREFIX)

# Layer 2: analysis over the history the layers above wrote. Every router here
# is read-only and derives its answers from the ledger — nothing is stored.
app.include_router(leadtime_router.router, prefix=API_PREFIX)
app.include_router(anomaly_router.router, prefix=API_PREFIX)
app.include_router(forecasting_router.router, prefix=API_PREFIX)
app.include_router(reorder_router.router, prefix=API_PREFIX)
# Reads a photographed supplier invoice into a draft goods receipt. Creates
# nothing — the draft is submitted through the existing grn_router above, by a
# person, which is what keeps the ledger's only writer unchanged.
app.include_router(intake_router.router, prefix=API_PREFIX)


def custom_openapi() -> dict:
    """Build the schema once, then tell each operation where it is used.

    Registered after every router, because `api_docs.annotate` walks the
    finished path table — a route included later would not be reached.
    """
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = api_docs.annotate(
        get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=api_docs.TAG_GROUPS,
        )
    )
    return app.openapi_schema


app.openapi = custom_openapi
