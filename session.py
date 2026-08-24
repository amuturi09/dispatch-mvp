"""
Engine/session management. Swap DATABASE_URL (see config.py / .env) to point
at Postgres in staging/production -- SQLite is fine for local dev only
(no concurrent writers, no real durability guarantees).
"""

from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from db.models import Base


def make_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


def make_session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db(engine) -> None:
    Base.metadata.create_all(bind=engine)


def get_db_dependency(session_factory):
    """Returns a FastAPI dependency function bound to a specific session factory."""
    def _get_db():
        db: Session = session_factory()
        try:
            yield db
        finally:
            db.close()
    return _get_db
