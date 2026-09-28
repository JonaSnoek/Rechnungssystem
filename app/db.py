"""Database engine, session handling and migration bootstrap."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, scoped_session, sessionmaker

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: scoped_session | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    @event.listens_for(engine, "connect")
    def _sqlite_regexp(dbapi_connection, _record):  # pragma: no cover - driver hook
        import re

        dbapi_connection.create_function("regexp", 2, lambda p, s: bool(re.search(p, s)))


def create_db_engine(url: str, echo: bool = False) -> Engine:
    kwargs: dict = {"echo": echo, "future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        engine = create_engine(url, **kwargs)
        _configure_sqlite(engine)
    else:
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
        engine = create_engine(url, **kwargs)
    return engine


def init_engine(url: str, echo: bool = False) -> Engine:
    global _engine, _session_factory
    _engine = create_db_engine(url, echo=echo)
    _session_factory = scoped_session(
        sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    )
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Datenbank-Engine ist nicht initialisiert")
    return _engine


def get_session() -> Session:
    if _session_factory is None:
        raise RuntimeError("Datenbank-Session ist nicht initialisiert")
    return _session_factory()


def remove_session() -> None:
    if _session_factory is not None:
        _session_factory.remove()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        remove_session()


def raw_connection():
    return get_engine().connect()


def table_exists(name: str) -> bool:
    try:
        with get_engine().connect() as conn:
            return bool(conn.execute(text(_exists_sql(name))).scalar())
    except Exception:
        return False


def _exists_sql(table: str) -> str:
    dialect = get_engine().dialect.name
    if dialect == "sqlite":
        return (
            "SELECT count(*) FROM sqlite_master "
            f"WHERE type='table' AND name='{table}'"
        )
    return (
        "SELECT count(*) FROM information_schema.tables "
        f"WHERE table_name = '{table}'"
    )
