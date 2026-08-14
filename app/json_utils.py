from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def json_object(value: Any) -> dict[str, Any]:
    """Return a JSON object as a plain dict regardless of DB codec behavior."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        if not value.strip():
            return {}
        decoded = json.loads(value)
        if isinstance(decoded, Mapping):
            return dict(decoded)
        raise ValueError(f"expected JSON object, got {type(decoded).__name__}")
    raise TypeError(f"expected JSON object/dict, got {type(value).__name__}")
