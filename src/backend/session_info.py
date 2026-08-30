"""Read desktop and session identifiers without importing globals or GTK.
XDG_CURRENT_DESKTOP is colon-separated and ordered most-specific first."""
import os


def desktop_components() -> list[str]:
    """The lowercased components of XDG_CURRENT_DESKTOP, most specific
    first. Empty when the variable is unset or blank."""
    raw = os.getenv("XDG_CURRENT_DESKTOP") or ""
    return [part.strip().lower() for part in raw.split(":") if part.strip()]


def session_type() -> str | None:
    """The lowercased XDG_SESSION_TYPE ("wayland", "x11", "tty"), or None
    when unset or blank."""
    value = os.getenv("XDG_SESSION_TYPE")
    if value is None:
        return None
    return value.strip().lower() or None
