"""The one root-type check the store's JSON documents are read through.

A manifest or an attribution file is an object. Nothing constrains what a
repository actually serves, and a root of any other JSON type reaches a caller
that immediately treats it as a dict. A populated list is the sharp case: it is
truthy, so an emptiness test in the caller passes it straight through and the
first .get raises instead.
"""

import json
from typing import Any, cast

from loguru import logger as log


def json_object(raw: str, what: str) -> "dict[str, Any] | None":
    """The JSON object `raw` holds, or None when it holds anything else.

    `what` names the document in the log line, so a bad file can be traced
    back to the repository that served it. Decode errors are the caller's to
    handle; this decides the root type only.
    """
    root = json.loads(raw)
    if isinstance(root, dict):
        return cast(dict[str, Any], root)
    log.error(f"{what} holds a JSON {type(root).__name__}, not an object")
    return None
