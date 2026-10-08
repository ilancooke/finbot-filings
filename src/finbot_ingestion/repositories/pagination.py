"""Bounded repository pages; tokens are opaque, scoped SDK continuations."""

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_token: str | None = None
