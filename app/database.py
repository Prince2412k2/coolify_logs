import os
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def get_db_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def get_db_path() -> str:
    return os.getenv("DB_PATH", "/data/db.sqlite")


def _sqlite_url(path: str) -> str:
    return f"sqlite:///{path}"


def ensure_db_parent_dir(db_path: str) -> None:
    parent = Path(db_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def create_engine_and_sessionmaker(db_url: str = "", db_path: str = ""):
    if not db_url:
        db_path = db_path or get_db_path()
        db_url = _sqlite_url(db_path)
        ensure_db_parent_dir(db_path)

    if db_url.startswith("sqlite"):
        engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    else:
        # Defaults (5+10) are too tight for a gateway with many concurrent WS
        # viewers. Sessions are still expected to be short-lived (auth lookup
        # only), but headroom protects against bursty connects.
        engine = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=40,
            pool_recycle=1800,
        )

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, SessionLocal


def get_db(request: Request) -> Session:
    SessionLocal = request.app.state.SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
