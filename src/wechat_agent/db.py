from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


@lru_cache(maxsize=8)
def _engine_for_url(db_url: str) -> Engine:
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(db_url, future=True, connect_args=connect_args)


def get_engine(settings: Settings | None = None) -> Engine:
    active_settings = settings or get_settings()
    return _engine_for_url(active_settings.db_url)


@lru_cache(maxsize=8)
def _sessionmaker_for_url(db_url: str) -> sessionmaker[Session]:
    engine = _engine_for_url(db_url)
    return sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False, future=True)


def _ensure_sqlite_parent(db_url: str) -> None:
    if not db_url.startswith("sqlite:///"):
        return
    path = db_url.removeprefix("sqlite:///")
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)


def init_db(settings: Settings | None = None) -> None:
    active_settings = settings or get_settings()
    _ensure_sqlite_parent(active_settings.db_url)

    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine(active_settings))


@contextmanager
def session_scope(settings: Settings | None = None):
    active_settings = settings or get_settings()
    SessionLocal = _sessionmaker_for_url(active_settings.db_url)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
