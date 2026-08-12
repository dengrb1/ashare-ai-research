from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.config import get_settings


def build_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
    elif backend == "postgresql":
        # A database that is down must fail fast instead of hanging every
        # process that touches this engine.  psycopg's default is to wait
        # forever for the server handshake, which wedges a worker on its first
        # settings/energy-saving query when postgres is unreachable — the
        # worker never starts and its log stays empty.  The doctor already
        # uses the same 3-second budget.
        kwargs["connect_args"] = {"connect_timeout": 3}
    return create_engine(url, **kwargs)


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
