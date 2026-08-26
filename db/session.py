"""
Engine/session management. Swap DATABASE_URL (see config.py / .env) to point
at Postgres in staging/production -- SQLite is fine for local dev only
(no concurrent writers, no real durability guarantees).
"""

from __future__ import annotations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from db.models import Base


def make_engine(database_url: str):
    if database_url.startswith("sqlite"):
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

    # Postgres (e.g. Supabase via the Supavisor session pooler). On a hosted
    # platform like Railway, an idle DB connection gets silently dropped by the
    # network/pooler; without protection the next query then blocks forever on
    # the dead socket instead of erroring (the "boots fine, queries hang"
    # symptom). Three defenses:
    #   - pool_recycle: retire a pooled connection well before the pooler's idle
    #     cutoff, so we never hand out a stale one.
    #   - TCP keepalives: detect a silently-dropped link at the socket layer.
    #   - connect_timeout: bound how long a new connection may stall, turning an
    #     unreachable DB into a fast, logged error instead of an infinite hang.
    connect_args = {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    return create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=280,
    )


def make_session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Columns added after the first schema shipped. create_all() only creates whole
# tables, never new columns on an existing one, so a dev SQLite file from an
# earlier version would be missing these. Rather than force a DB wipe, add them
# in place. (Production Postgres should use real migrations; ADD COLUMN here is
# idempotent and harmless there.)
# DDL is written to be valid on both SQLite (local dev) and PostgreSQL
# (Supabase in production). `DEFAULT TRUE` is understood by Postgres and by
# SQLite 3.23+ (bundled with modern Python), so no dialect branching is needed.
_ADDED_COLUMNS = {
    "contractors": [
        ("email", "VARCHAR"),
        ("owner_name", "VARCHAR"),
        ("password_hash", "VARCHAR"),
        ("sms_opt_in", "BOOLEAN DEFAULT TRUE"),
    ],
}


def _apply_column_migrations(engine) -> None:
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue  # create_all just built it with every column
        have = {c["name"] for c in insp.get_columns(table)}
        missing = [(name, ddl) for name, ddl in columns if name not in have]
        if not missing:
            continue
        with engine.begin() as conn:
            for name, ddl in missing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db(engine) -> None:
    Base.metadata.create_all(bind=engine)
    _apply_column_migrations(engine)


def get_db_dependency(session_factory):
    """Returns a FastAPI dependency function bound to a specific session factory."""
    def _get_db():
        db: Session = session_factory()
        try:
            yield db
        finally:
            db.close()
    return _get_db
