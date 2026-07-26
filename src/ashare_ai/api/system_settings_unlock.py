"""Short-lived step-up authentication for system-setting writes."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status

from ashare_ai.api.auth import AuthContext, verify_password
from ashare_ai.core.config import get_settings

UNLOCK_TTL_SECONDS = 600
_PREFIX = "ashare:system-settings-unlock:"
_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
_ATTEMPTS_LOCK = threading.Lock()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(request: Request, context: AuthContext) -> str:
    peer = request.client.host if request.client else "unknown"
    return f"{peer}:{context.user.user_id}"


def _check_rate_limit(request: Request, context: AuthContext) -> str:
    source = _source(request, context)
    now = time.monotonic()
    cutoff = now - 60
    with _ATTEMPTS_LOCK:
        attempts = _ATTEMPTS[source]
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= get_settings().auth_login_rate_limit_per_minute:
            retry_after = max(1, round(60 - (now - attempts[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="authentication rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
    return source


def _record_failure(source: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS[source].append(time.monotonic())


def _clear_failures(source: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(source, None)


def _redis_client() -> Any:
    import redis

    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def issue_unlock(
    request: Request, context: AuthContext, password: str
) -> tuple[str, datetime]:
    source = _check_rate_limit(request, context)
    if not context.user.enabled or not verify_password(context.user.password_hash, password):
        _record_failure(source)
        raise HTTPException(status_code=401, detail="invalid administrator password")
    _clear_failures(source)
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(seconds=UNLOCK_TTL_SECONDS)
    payload = json.dumps(
        {
            "user_id": context.user.user_id,
            "session_id": context.session.session_id,
        }
    )
    try:
        client = _redis_client()
        client.set(f"{_PREFIX}{_digest(token)}", payload, ex=UNLOCK_TTL_SECONDS)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="settings unlock service unavailable") from exc
    return token, expires_at


def require_settings_unlock(context: AuthContext, token: str | None) -> None:
    if not token or not 32 <= len(token) <= 512:
        raise HTTPException(
            status_code=403,
            detail={"code": "SYSTEM_SETTINGS_LOCKED", "message": "unlock required"},
        )
    try:
        raw = _redis_client().get(f"{_PREFIX}{_digest(token)}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="settings unlock service unavailable") from exc
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=403,
            detail={"code": "SYSTEM_SETTINGS_LOCKED", "message": "unlock expired"},
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or not secrets.compare_digest(
        str(payload.get("user_id", "")), context.user.user_id
    ) or not secrets.compare_digest(
        str(payload.get("session_id", "")), context.session.session_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "SYSTEM_SETTINGS_LOCKED", "message": "unlock does not match session"},
        )
