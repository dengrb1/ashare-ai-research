from __future__ import annotations

import re

from ashare_ai.core.contracts import CanonicalSymbol, Exchange

_CANONICAL_SUFFIXES = {
    "SH": Exchange.SH,
    "XSHG": Exchange.SH,
    "SSE": Exchange.SH,
    "SZ": Exchange.SZ,
    "XSHE": Exchange.SZ,
    "SZSE": Exchange.SZ,
    "BJ": Exchange.BJ,
    "BSE": Exchange.BJ,
}


def infer_exchange(code: str) -> Exchange:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"security code must contain exactly six digits: {code!r}")
    if code.startswith(("4", "8", "92")):
        return Exchange.BJ
    if code.startswith(("5", "6", "9")):
        return Exchange.SH
    if code.startswith(("0", "1", "2", "3")):
        return Exchange.SZ
    raise ValueError(f"cannot infer exchange for security code: {code}")


def normalize_symbol(value: str | int, exchange: str | Exchange | None = None) -> CanonicalSymbol:
    raw = str(value).strip().upper()
    explicit_exchange: Exchange | None = None

    match = re.fullmatch(r"(?:([A-Z]+)[.\-]?)?(\d{1,6})(?:[.\-]?([A-Z]+))?", raw)
    if match is None:
        raise ValueError(f"unsupported security symbol format: {value!r}")

    prefix, digits, suffix = match.groups()
    code = digits.zfill(6)
    labels = [label for label in (prefix, suffix) if label]
    for label in labels:
        parsed = _CANONICAL_SUFFIXES.get(label)
        if parsed is None:
            raise ValueError(f"unknown exchange label: {label}")
        if explicit_exchange is not None and parsed != explicit_exchange:
            raise ValueError(f"conflicting exchange labels in symbol: {value!r}")
        explicit_exchange = parsed

    if exchange is not None:
        exchange_label = exchange.value if isinstance(exchange, Exchange) else str(exchange).upper()
        supplied = _CANONICAL_SUFFIXES.get(exchange_label)
        if supplied is None:
            raise ValueError(f"unknown exchange: {exchange!r}")
        if explicit_exchange is not None and supplied != explicit_exchange:
            raise ValueError("explicit exchange conflicts with symbol exchange")
        explicit_exchange = supplied

    resolved = explicit_exchange or infer_exchange(code)
    return f"{code}.{resolved.value}"
