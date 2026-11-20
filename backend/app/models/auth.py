"""Server-side administrator sessions (M5.0).

Only SHA-256 hashes of the session token and of the CSRF token are stored; the
tokens themselves exist only in the browser (cookie / memory). A stolen database
copy therefore cannot be replayed as a login. Times are naive UTC.

Expiry is checked twice, independently: idle (``last_seen_at`` + idle timeout)
and absolute (``expires_at``, fixed at creation). A revoked session keeps its
row until cleanup so a revocation can never be undone by a later touch.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, sha256_hex


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    __table_args__ = (
        sha256_hex("token_hash"),
        sha256_hex("csrf_hash"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint("last_seen_at >= created_at", name="last_seen_after_created"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
        ),
        Index(None, "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_account.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
