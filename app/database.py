import os
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def get_db_path() -> str:
    return os.getenv("DB_PATH", "/data/db.sqlite")


def _sqlite_url(path: str) -> str:
    # Three slashes means absolute path.
    return f"sqlite:///{path}"


def ensure_db_parent_dir(db_path: str) -> None:
    parent = Path(db_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def create_engine_and_sessionmaker(db_path: str):
    ensure_db_parent_dir(db_path)
    engine = create_engine(
        _sqlite_url(db_path),
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
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
