"""Document periods, their therapies, planned dates, issued documents, audit log.

Separation (D-08): prescribed quantity lives on ``referral_periods`` and is read
through ``period_therapies.referral_period_id`` (never copied, never typed in);
planned dates are ``scheduled_sessions``; what was issued is the immutable
``generated_documents.snapshot``. Nothing here records that a session was performed.

One attestation prints one date range, so every therapy of a document period must
have exactly the period's window (trigger). Therapies with different windows go
into separate document periods; overlapping periods of one patient are allowed.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import (
    Base,
    boolean,
    date_order,
    iso_date,
    json_text,
    not_blank,
    one_of,
    sha256_hex,
    timestamp_column,
)


class Period(Base):
    __tablename__ = "periods"
    __table_args__ = (
        UniqueConstraint("patient_id", "start_date", "end_date"),
        iso_date("start_date"),
        iso_date("end_date"),
        date_order("start_date", "end_date"),
        one_of("status", "draft", "schedule_approved", "issued"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="draft")
    created_at: Mapped[datetime] = timestamp_column()


class PeriodTherapy(Base):
    __tablename__ = "period_therapies"
    __table_args__ = (
        UniqueConstraint("period_id", "therapy_type_id"),
        boolean("home_visit", nullable=True),  # NULL = not chosen yet (R-PER-2)
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id"), nullable=False)
    therapy_type_id: Mapped[int] = mapped_column(ForeignKey("therapy_types.id"), nullable=False)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), nullable=False)
    # UNIQUE: a monthly referral can be documented only once (P-16)
    referral_period_id: Mapped[int] = mapped_column(
        ForeignKey("referral_periods.id"), nullable=False, unique=True
    )
    home_visit: Mapped[bool | None] = mapped_column(Boolean)


class ScheduledSession(Base):
    """A planned date. Not evidence that a session took place (D-08)."""

    __tablename__ = "scheduled_sessions"
    __table_args__ = (
        iso_date("session_date"),
        one_of("origin", "proposed", "manual"),
        Index(None, "period_therapy_id", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_therapy_id: Mapped[int] = mapped_column(
        ForeignKey("period_therapies.id"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class GeneratedDocument(Base):
    """An issued document. Append-only: no UPDATE, no DELETE (triggers)."""

    __tablename__ = "generated_documents"
    __table_args__ = (
        UniqueConstraint("period_id", "version"),
        CheckConstraint("version > 0", name="version_positive"),
        iso_date("issue_date"),
        sha256_hex("docx_sha256"),
        sha256_hex("pdf_sha256"),
        sha256_hex("snapshot_sha256"),
        json_text("snapshot"),
        not_blank("docx_path"),
        not_blank("pdf_path"),
        not_blank("app_version"),
        one_of("reason", "initial", "correction"),
        CheckConstraint(
            "(version = 1 AND reason = 'initial') OR "
            "(version > 1 AND reason = 'correction' AND "
            "coalesce(length(trim(correction_note)), 0) > 0)",
            name="reason_matches_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_version_id: Mapped[int] = mapped_column(
        ForeignKey("template_versions.id"), nullable=False
    )
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    issued_at: Mapped[datetime] = timestamp_column()
    docx_path: Mapped[str] = mapped_column(String(500), nullable=False)
    docx_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_path: Mapped[str] = mapped_column(String(500), nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    libreoffice_version: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    correction_note: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Append-only. ``details`` holds IDs, never AMKA or names."""

    __tablename__ = "audit_log"
    __table_args__ = (
        not_blank("action"),
        not_blank("entity"),
        json_text("details", nullable=True),
        Index(None, "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = timestamp_column()
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[str | None] = mapped_column(Text)  # JSON
