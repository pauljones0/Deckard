"""Validate object roots for remote store JSON documents."""

import json
from typing import Any, cast

from loguru import logger as log


def json_object(raw: str, what: str) -> "dict[str, Any] | None":
    """Return the decoded object root, or log `what` and return None.
    Let the caller handle decode errors."""
    root = json.loads(raw)
    if isinstance(root, dict):
        return cast(dict[str, Any], root)
    log.error(f"{what} holds a JSON {type(root).__name__}, not an object")
    return None
