"""Application error hierarchy, mapped to RFC 7807 problem responses.

Services raise these; the global handler in main.py turns them into responses.
Internals never leak to the client — unhandled exceptions become a generic 500
carrying only the request_id.
"""

from typing import Any


class AppError(Exception):
    status_code: int = 400
    title: str = "Request failed"
    error_type: str = "about:blank"

    def __init__(self, detail: str, errors: list[dict[str, Any]] | None = None):
        self.detail = detail
        self.errors = errors or []
        super().__init__(detail)


class ValidationError(AppError):
    status_code = 422
    title = "Validation failed"
    error_type = "/errors/validation"


class NotFoundError(AppError):
    status_code = 404
    title = "Resource not found"
    error_type = "/errors/not-found"


class ConflictError(AppError):
    status_code = 409
    title = "Conflict"
    error_type = "/errors/conflict"


class AuthenticationError(AppError):
    status_code = 401
    title = "Authentication failed"
    error_type = "/errors/authentication"


class PermissionDenied(AppError):
    status_code = 403
    title = "Permission denied"
    error_type = "/errors/permission-denied"


class InsufficientStock(AppError):
    """Raised when a movement or reservation would drive stock negative."""

    status_code = 409
    title = "Insufficient stock"
    error_type = "/errors/insufficient-stock"


class ConcurrencyConflict(AppError):
    status_code = 409
    title = "Concurrent modification"
    error_type = "/errors/concurrency"


class ExternalServiceError(AppError):
    status_code = 502
    title = "Upstream service failed"
    error_type = "/errors/external-service"
