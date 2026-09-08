"""DB plumbing shared by persistence + telemetry. Postgres or Oracle.

Two decisions:
- `PortableJSON`: JSONB on Postgres, JSON-as-text (CLOB/VARCHAR) elsewhere/Oracle.
  Models always bind/read plain dict/list.
- DB is optional: empty `db_url` -> no engine, `session_scope()` yields None,
  callers treat as "persistence off" and no-op. Lets it run locally without a DB.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Text, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class PortableJSON(TypeDecorator):
    """JSON column: JSONB on Postgres, JSON-as-text (CLOB/VARCHAR) elsewhere.

    Python side always binds/reads dict/list.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value  # JSONB adapts dict/list natively
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.loads(value) if isinstance(value, str) else value


# Engine + session factory. None when no db_url (persistence disabled).
_engine = (
    create_engine(settings.db_url, echo=settings.db_echo, future=True)
    if settings.db_url
    else None
)
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False) if _engine else None


def init_db() -> None:
    """Create tables from `Base` only when db_auto_create is set (dev). No-op otherwise.

    Prod default (db_auto_create=False): schema is externally managed, so we never
    issue DDL -- ship db/postgresql.sql / db/oracle.sql to the DBA (or the prefixed
    db/*.generated.sql when TABLE_PREFIX / *_TABLE overrides are set).
    """
    if _engine is None:
        log.info("No db_url configured; persistence and telemetry disabled.")
        return
    if not settings.db_auto_create:
        log.info("db_auto_create off; schema externally managed, skipping create_all.")
        return
    Base.metadata.create_all(_engine)


@contextmanager
def session_scope() -> Iterator[Session | None]:
    """Transactional Session, or None when disabled. Commit on success, rollback on error.

    Best-effort: callers guard the None case so a DB issue never breaks a request.
    """
    if _SessionLocal is None:
        yield None
        return
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection() -> bool | None:
    """Ping the DB for the readiness probe: True healthy, False unreachable, None
    when no DB is configured (nothing to wait on). `select(1)` is portable --
    SQLAlchemy renders `SELECT 1 FROM DUAL` on Oracle automatically.
    """
    if _engine is None:
        return None
    try:
        with _engine.connect() as conn:
            conn.execute(select(1))
        return True
    except Exception as exc:
        log.warning("DB readiness check failed: %s", exc)
        return False
