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
    allowed_containers: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    def allowed_list(self) -> List[str]:
        try:
            v: Any = json.loads(self.allowed_containers or "[]")
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
        return []

    def set_allowed_list(self, items: List[str]) -> None:
        self.allowed_containers = json.dumps([str(x) for x in items])
