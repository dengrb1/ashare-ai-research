from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.storage.models import UserAccount, UserSession

_PASSWORD_HASHER = PasswordHasher()


@dataclass(frozen=True)
class AuthContext:
    user: UserAccount
    session: UserSession


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
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
    if user is None or not user.enabled or not verify_password(user.password_hash, password):
        return None
    if _PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        user.updated_at = datetime.now(UTC)
        db.commit()
    return user


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
    return AuthContext(user=user, session=row)


def clear_auth_cookies(response: Response, settings: Settings | None = None) -> None:
    effective = settings or get_settings()
    response.delete_cookie(effective.session_cookie_name, path="/")
    response.delete_cookie(effective.csrf_cookie_name, path="/")


def require_auth(request: Request, db: Session) -> AuthContext:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    row = db.scalar(select(UserSession).where(UserSession.token_hash == _digest(token)))
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or _aware(row.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired")
    user = db.get(UserAccount, row.user_id)
    if user is None or not user.enabled or user.session_version != row.user_session_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session invalid")
    row.last_seen_at = now
    return AuthContext(user=user, session=row)


def require_csrf(request: Request, context: AuthContext) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
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
