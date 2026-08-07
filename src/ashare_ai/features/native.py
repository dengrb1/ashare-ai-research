from __future__ import annotations

import os
from functools import lru_cache
from importlib import import_module
from typing import Any


@lru_cache(maxsize=1)
def _load_native_module() -> Any | None:
    try:
        return import_module("ashare_ai_core")
    except ImportError:
        return None


def native_technical_metrics(
    closes: list[float], volumes: list[float]
) -> (
    tuple[float | None, float | None, float | None, float | None, float | None, float | None]
    | None
):
    """Return Rust metrics when enabled and installed, otherwise signal Python fallback."""
    mode = os.getenv("ASHARE_NATIVE_TECHNICAL", "auto").strip().lower()
    if mode not in {"auto", "on", "off"}:
        raise ValueError("ASHARE_NATIVE_TECHNICAL must be auto, on, or off")
    if mode == "off":
        return None
    module = _load_native_module()
    if module is None:
        if mode == "on":
            raise RuntimeError(
                "ASHARE_NATIVE_TECHNICAL=on requires the optional ashare_ai_core extension"
            )
        return None
    result = module.calculate_technical_metrics(closes, volumes)
    if not isinstance(result, tuple) or len(result) != 6:
        raise RuntimeError("ashare_ai_core returned an invalid technical metrics payload")
    return result
