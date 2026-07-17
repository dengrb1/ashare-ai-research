from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ashare_ai.api.auth import AuthContext, require_auth, require_csrf
from ashare_ai.storage.database import SessionLocal


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_auth_context(request: Request, db: Annotated[Session, Depends(get_db)]) -> AuthContext:
    return require_auth(request, db)


def get_write_context(
    request: Request,
    context: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthContext:
    require_csrf(request, context)
    return context
