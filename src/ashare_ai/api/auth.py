from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.storage.models import UserAccount, UserSession

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("timing-defense-not-a-real-password")
_PASSWORD_VERIFY_SEMAPHORE = threading.BoundedSemaphore(2)
_AUTH_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
_AUTH_ATTEMPTS_LOCK = threading.Lock()


@dataclass(frozen=True)
class AuthContext:
    user: UserAccount
    session: UserSession
    scheme: str = "COOKIE"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def hash_password(password: str) -> str:
    # Existing hashes and bootstrap credentials remain backward compatible;
    # all public create/reset schemas require 12 and production startup requires 14.
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        with _PASSWORD_VERIFY_SEMAPHORE:
            return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def bootstrap_admin(db: Session, settings: Settings | None = None) -> UserAccount | None:
    effective = settings or get_settings()
    username = effective.admin_username
    password = effective.admin_password
    if not username or not password:
        return None
    normalized = username.strip().casefold()
    existing = db.scalar(select(UserAccount).where(UserAccount.username == normalized))
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    admin = UserAccount(
        username=normalized,
        password_hash=hash_password(password),
        role="ADMIN",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def authenticate(db: Session, username: str, password: str) -> UserAccount | None:
    user = db.scalar(select(UserAccount).where(UserAccount.username == username.strip().casefold()))
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    valid_password = verify_password(password_hash, password)
    if user is None or not user.enabled or not valid_password:
        return None
    if _PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        user.updated_at = datetime.now(UTC)
        db.commit()
    return user


def _auth_source(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        trusted_proxy = ipaddress.ip_address(peer).is_private
    except ValueError:
        trusted_proxy = False
    if trusted_proxy:
        # Use the proxy-adjacent (right-most) address. The first X-Forwarded-For
        # value is attacker-controlled unless every upstream proxy overwrites it.
        forwarded = request.headers.get("x-forwarded-for", "")
        for candidate in reversed(forwarded.split(",")):
            try:
                if candidate.strip():
                    return str(ipaddress.ip_address(candidate.strip()))
            except ValueError:
                continue
    return peer


def check_auth_rate_limit(
    request: Request,
    username: str,
    settings: Settings | None = None,
    *,
    now: float | None = None,
) -> str:
    """Rate-limit password attempts by source and account for one API process."""
    effective = settings or get_settings()
    account = hashlib.sha256(username.strip().casefold().encode("utf-8")).hexdigest()[:16]
    source = f"{_auth_source(request)}:{account}"
    current = time.monotonic() if now is None else now
    cutoff = current - 60
    with _AUTH_ATTEMPTS_LOCK:
        attempts = _AUTH_ATTEMPTS[source]
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= effective.auth_login_rate_limit_per_minute:
            retry_after = max(1, round(60 - (current - attempts[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="authentication rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
    return source


def record_auth_failure(source: str, *, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _AUTH_ATTEMPTS_LOCK:
        if len(_AUTH_ATTEMPTS) >= 10_000 and source not in _AUTH_ATTEMPTS:
            cutoff = current - 60
            expired = [
                key
                for key, values in _AUTH_ATTEMPTS.items()
                if not values or values[-1] <= cutoff
            ]
            for key in expired:
                _AUTH_ATTEMPTS.pop(key, None)
            if len(_AUTH_ATTEMPTS) >= 10_000:
                _AUTH_ATTEMPTS.pop(next(iter(_AUTH_ATTEMPTS)))
        _AUTH_ATTEMPTS[source].append(current)


def clear_auth_failures(source: str) -> None:
    with _AUTH_ATTEMPTS_LOCK:
        _AUTH_ATTEMPTS.pop(source, None)


def create_session(
    db: Session,
    user: UserAccount,
    response: Response,
    request: Request,
    settings: Settings | None = None,
) -> AuthContext:
    effective = settings or get_settings()
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = UserSession(
        user_id=user.user_id,
        token_hash=_digest(token),
        csrf_hash=_digest(csrf),
        session_type="WEB",
        user_session_version=user.session_version,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=effective.session_ttl_hours),
        user_agent=request.headers.get("user-agent", "")[:512] or None,
        client_ip=request.client.host if request.client else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    response.set_cookie(
        effective.session_cookie_name,
        token,
        httponly=True,
        secure=effective.cookie_secure,
        samesite="lax",
        max_age=effective.session_ttl_hours * 3600,
        path="/",
    )
    response.set_cookie(
        effective.csrf_cookie_name,
        csrf,
        httponly=False,
        secure=effective.cookie_secure,
        samesite="lax",
        max_age=effective.session_ttl_hours * 3600,
        path="/",
    )
    return AuthContext(user=user, session=row, scheme="COOKIE")


def create_token_session(
    db: Session,
    user: UserAccount,
    request: Request,
    settings: Settings | None = None,
) -> TokenPair:
    effective = settings or get_settings()
    access_token = secrets.token_urlsafe(48)
    refresh_token = secrets.token_urlsafe(64)
    access_seconds = effective.access_token_ttl_minutes * 60
    refresh_seconds = effective.refresh_token_ttl_days * 24 * 3600
    now = datetime.now(UTC)
    db.add(
        UserSession(
            user_id=user.user_id,
            token_hash=_digest(access_token),
            csrf_hash=_digest(""),
            session_type="APP",
            refresh_token_hash=_digest(refresh_token),
            refresh_expires_at=now + timedelta(seconds=refresh_seconds),
            user_session_version=user.session_version,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=access_seconds),
            user_agent=request.headers.get("user-agent", "")[:512] or None,
            client_ip=request.client.host if request.client else None,
        )
    )
    db.commit()
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_in=access_seconds,
        refresh_expires_in=refresh_seconds,
    )


def rotate_refresh_token(
    db: Session,
    refresh_token: str,
    settings: Settings | None = None,
) -> TokenPair:
    effective = settings or get_settings()
    row = db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == _digest(refresh_token),
            UserSession.session_type == "APP",
        ).with_for_update()
    )
    now = datetime.now(UTC)
    if (
        row is None
        or row.revoked_at is not None
        or row.refresh_expires_at is None
        or _aware(row.refresh_expires_at) <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token invalid"
        )
    user = db.get(UserAccount, row.user_id)
    if user is None or not user.enabled or user.session_version != row.user_session_version:
        row.revoked_at = now
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session invalid")
    access_token = secrets.token_urlsafe(48)
    next_refresh_token = secrets.token_urlsafe(64)
    access_seconds = effective.access_token_ttl_minutes * 60
    refresh_seconds = effective.refresh_token_ttl_days * 24 * 3600
    row.token_hash = _digest(access_token)
    row.refresh_token_hash = _digest(next_refresh_token)
    row.expires_at = now + timedelta(seconds=access_seconds)
    row.refresh_expires_at = now + timedelta(seconds=refresh_seconds)
    row.last_seen_at = now
    db.commit()
    return TokenPair(
        access_token=access_token,
        refresh_token=next_refresh_token,
        access_expires_in=access_seconds,
        refresh_expires_in=refresh_seconds,
    )


def revoke_refresh_token(db: Session, refresh_token: str) -> None:
    row = db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == _digest(refresh_token),
            UserSession.session_type == "APP",
        )
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()


def clear_auth_cookies(response: Response, settings: Settings | None = None) -> None:
    effective = settings or get_settings()
    response.delete_cookie(effective.session_cookie_name, path="/")
    response.delete_cookie(effective.csrf_cookie_name, path="/")


def require_auth(request: Request, db: Session) -> AuthContext:
    settings = get_settings()
    authorization = request.headers.get("authorization", "")
    scheme = "COOKIE"
    token: str | None
    if authorization:
        prefix, separator, credential = authorization.partition(" ")
        if prefix.casefold() != "bearer" or not separator or not credential.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authorization header"
            )
        token = credential.strip()
        scheme = "BEARER"
    else:
        token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    statement = select(UserSession).where(UserSession.token_hash == _digest(token))
    statement = statement.where(
        UserSession.session_type == ("APP" if scheme == "BEARER" else "WEB")
    )
    row = db.scalar(statement)
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or _aware(row.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired")
    user = db.get(UserAccount, row.user_id)
    if user is None or not user.enabled or user.session_version != row.user_session_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session invalid")
    row.last_seen_at = now
    return AuthContext(user=user, session=row, scheme=scheme)


def require_csrf(request: Request, context: AuthContext) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if context.scheme == "BEARER":
        return
    settings = get_settings()
    cookie = request.cookies.get(settings.csrf_cookie_name, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")
    if not hmac.compare_digest(_digest(header), context.session.csrf_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")


def revoke_session(db: Session, context: AuthContext) -> None:
    context.session.revoked_at = datetime.now(UTC)
    db.commit()


def invalidate_user_sessions(db: Session, user: UserAccount) -> None:
    now = datetime.now(UTC)
    user.session_version += 1
    user.updated_at = now
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def require_admin(context: AuthContext) -> None:
    if context.user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="administrator required")
