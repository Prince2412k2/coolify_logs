from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # JSON-encoded list of Coolify project IDs (strings) this key may read.
    # An empty list means the key is effectively disabled (rejected by validation
    # at create/update time, but legacy/manually-edited rows still behave safely).
    allowed_projects: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
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
