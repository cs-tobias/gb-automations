from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from gb_automations.db import Base


class SyncCursor(Base):
    """Per-source position marker for incremental syncs.

    Placeholder for the skeleton — real models land when we port the Apps Script logic.
    """

    __tablename__ = "sync_cursors"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_value: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
