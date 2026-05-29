from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # JSON-encoded list of Coolify project IDs (strings) this key may read.
    # An empty list means the key is effectively disabled.
    allowed_projects: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # When true, this key can additionally call /api/admin/* routes — but only
    # against projects in allowed_projects. There is no "global super-admin"
    # scope by design; grant projects explicitly.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    def allowed_project_list(self) -> List[str]:
        try:
            v: Any = json.loads(self.allowed_projects or "[]")
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
        return []

    def set_allowed_projects(self, items: List[str]) -> None:
        self.allowed_projects = json.dumps(sorted({str(x) for x in items}))


class AdminAudit(Base):
    """One row per admin write operation. Append-only; FIFO-trimmed at 5000."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_name: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)  # "ok" / "error"
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
