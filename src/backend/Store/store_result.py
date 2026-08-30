"""Typed success and failure channel for store read boundaries.
Callers must narrow Err before Ok.value access; neither arm defines truthiness."""
from __future__ import annotations

import enum
from dataclasses import dataclass


class StoreFetchError(Exception):
    """A fetch failure with its URL and short detail."""

    def __init__(self, url: str, detail: str) -> None:
        super().__init__(f"{detail} ({url})")
        self.url = url
        self.detail = detail


class ErrReason(enum.Enum):
    """Why a read-boundary call failed, at the granularity a caller acts on."""

    NO_CONNECTION = "no_connection"   # no store reachable / catalog fetch failed
    INVALID_ASSET = "invalid_asset"   # unsafe id, missing url, staged-manifest mismatch
    INSTALL_FAILED = "install_failed"  # unresolved repository/ref or missing/failed Git


@dataclass(frozen=True)
class Ok[T]:
    """A successful result with its payload."""

    value: T


@dataclass(frozen=True)
class Err:
    """A failed result with no payload."""

    reason: ErrReason
    detail: str = ""


type StoreResult[T] = Ok[T] | Err
