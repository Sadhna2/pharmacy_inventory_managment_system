import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.ai.anomaly import router as anomaly_router
from app.ai.forecasting import router as forecasting_router
from app.ai.leadtime import router as leadtime_router
from app.ai.reorder import router as reorder_router
from app.api.v1 import audit, auth, masters, operations, products, stock, users
from app.api.v1 import settings as settings_api
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
        "batch/expiry tracking with FEFO, GST, and RBAC."
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
        {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
        for e in exc.errors()
    ]
    return _problem(
        request,
        422,
        "/errors/validation",
        "Validation failed",
        "One or more fields are invalid",
        errors,
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
