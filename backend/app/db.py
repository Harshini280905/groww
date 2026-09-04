"""SQLAlchemy engine, session factory, declarative Base.

SQLite by default (zero-install, WAL mode enabled at first connection so
the single-writer lock doesn't bite the demo). Swap to Postgres via the
DATABASE_URL env var — no code changes needed:

    export DATABASE_URL=postgresql+psycopg://user:pass@host/db

The push-based cache design in the blueprint keeps hot-path DB writes low
(one PriceTick + one SignificantEvent per symbol per poll cycle), so SQLite
is genuinely fine for a hackathon. The Postgres story is a documented
production swap-in, not a fake claim.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./watchlist.db")

_engine_kwargs: dict = {"echo": False, "future": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a session, ensures it's closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
