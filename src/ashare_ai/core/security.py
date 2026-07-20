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
_DATABASE_MESSAGE = re.compile(
    r"(?i)(?:\[sql:|sqlalchemy|psycopg|duplicate key value|unique constraint)"
)


def safe_error_text(message: str, *, maximum_length: int = 500) -> str:
    """Sanitize persisted text before returning it through a public API."""
    normalized = " ".join(message.split())
    if _DATABASE_MESSAGE.search(normalized):
        return "数据库操作失败，未写入不完整结果"
    normalized = _CREDENTIAL.sub(lambda match: f"{match.group(1)}=[REDACTED]", normalized)
    normalized = _BEARER.sub("Bearer [REDACTED]", normalized)
    normalized = _URL.sub("[remote endpoint]", normalized)
    normalized = _WINDOWS_PATH.sub("[server path]", normalized)
    normalized = _UNIX_PATH.sub("[server path]", normalized)
    return normalized[:maximum_length]


def safe_error_message(error: BaseException, *, maximum_length: int = 500) -> str:
    """Return a useful failure reason without credentials, endpoints, or server paths."""
    return safe_error_text(str(error) or type(error).__name__, maximum_length=maximum_length)
