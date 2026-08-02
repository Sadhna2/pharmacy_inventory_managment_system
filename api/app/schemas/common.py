from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int
    pages: int


class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    size: int = Field(50, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def paginate(items: list[T], total: int, params: PageParams) -> Page[T]:
    return Page(
        items=items,
        total=total,
        page=params.page,
        size=params.size,
        pages=max(1, -(-total // params.size)),  # ceiling division
    )


class Message(BaseModel):
    message: str


class ProblemDetail(BaseModel):
    """RFC 7807 problem response."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
    errors: list[dict] = []
