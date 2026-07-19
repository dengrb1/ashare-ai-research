from __future__ import annotations

import re

_CREDENTIAL = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)"
    r"\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL = re.compile(r"(?i)https?://[^\s\"'<>]+")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\)[^\s\"'<>]+")
_UNIX_PATH = re.compile(r"(?<![\w])/(?:app|data|home|opt|tmp|usr|var)/[^\s\"'<>]+")


def safe_error_message(error: BaseException, *, maximum_length: int = 500) -> str:
    """Return a useful failure reason without credentials, endpoints, or server paths."""
    message = " ".join(str(error).split()) or type(error).__name__
    message = _CREDENTIAL.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    message = _BEARER.sub("Bearer [REDACTED]", message)
    message = _URL.sub("[remote endpoint]", message)
    message = _WINDOWS_PATH.sub("[server path]", message)
    message = _UNIX_PATH.sub("[server path]", message)
    return message[:maximum_length]
