"""Prescriptions, prescribed therapies, monthly referral periods, therapist assignments.

Three levels (D-19, D-20, P-01):
  prescriptions        overall validity, numbers, layout
  prescription_items   one prescribed therapy, its monthly quantity
  referral_periods     one window of one therapy, with its authorized quantity

Each therapy has its **own** referral windows; nothing assumes therapies share them.
The only quantity columns in the schema are ``monthly_quantity`` and
``authorized_quantity``; both are NOT NULL, > 0 and have no default, so no
session count can exist without an explicit value taken from a prescription.
Rules that span tables (no overlap, inside validity, read-only once used) are
SQLite triggers created by the initial migration.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, date_order, iso_date, not_blank, one_of, timestamp_column


class Prescription(Base):
    __tablename__ = "prescriptions"
    __table_args__ = (
        iso_date("valid_from"),
        iso_date("valid_to"),
        date_order("valid_from", "valid_to"),
        iso_date("issued_on", nullable=True),
        iso_date("decision_date", nullable=True),
        iso_date("examination_date", nullable=True),
        not_blank("prescription_no", nullable=True),
        not_blank("layout"),
        one_of("source", "manual", "extracted"),
        one_of("status", "draft", "confirmed"),
        Index(None, "patient_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False)
    prescription_no: Mapped[str | None] = mapped_column(String(32), unique=True)
    issued_on: Mapped[date | None] = mapped_column(Date)
    decision_no: Mapped[str | None] = mapped_column(String(64))
    decision_date: Mapped[date | None] = mapped_column(Date)
    examination_date: Mapped[date | None] = mapped_column(Date)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    doctor_name: Mapped[str | None] = mapped_column(String(200))
    # e.g. "eopyy_ekpy_monthly_v1"; no default — the layout must be stated (D-22)
    layout: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = timestamp_column()

    items: Mapped[list[PrescriptionItem]] = relationship(back_populates="prescription")


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"
    __table_args__ = (
        UniqueConstraint("prescription_id", "therapy_type_id"),
        CheckConstraint("monthly_quantity > 0", name="monthly_quantity_positive"),
        CheckConstraint(
            "printed_total_quantity IS NULL OR printed_total_quantity >= 0",
            name="printed_total_nonneg",
        ),
        CheckConstraint(
            "printed_duration_months IS NULL OR printed_duration_months > 0",
            name="printed_duration_positive",
        ),
        CheckConstraint(
            "eopyy_code IS NULL OR (length(eopyy_code) > 0 AND eopyy_code NOT GLOB '*[^0-9]*')",
            name="eopyy_code_digits",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), nullable=False)
    therapy_type_id: Mapped[int] = mapped_column(ForeignKey("therapy_types.id"), nullable=False)
    eopyy_code: Mapped[str | None] = mapped_column(String(16))  # as printed
    monthly_quantity: Mapped[int] = mapped_column(Integer, nullable=False)  # no default, ever
    printed_total_quantity: Mapped[int | None] = mapped_column(Integer)  # as printed (U-01)
    printed_duration_months: Mapped[int | None] = mapped_column(Integer)  # as printed (U-01)

    prescription: Mapped[Prescription] = relationship(back_populates="items")
    referral_periods: Mapped[list[ReferralPeriod]] = relationship(back_populates="item")


class ReferralPeriod(Base):
    __tablename__ = "referral_periods"
    __table_args__ = (
        UniqueConstraint("prescription_item_id", "sequence_no"),
        UniqueConstraint("prescription_item_id", "valid_from"),
        iso_date("valid_from"),
        iso_date("valid_to"),
        date_order("valid_from", "valid_to"),
        CheckConstraint("sequence_no > 0", name="sequence_positive"),
        CheckConstraint("authorized_quantity > 0", name="authorized_quantity_positive"),
        not_blank("referral_no", nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_item_id: Mapped[int] = mapped_column(
        ForeignKey("prescription_items.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    referral_no: Mapped[str | None] = mapped_column(String(32))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    authorized_quantity: Mapped[int] = mapped_column(Integer, nullable=False)  # no default, ever

    item: Mapped[PrescriptionItem] = relationship(back_populates="referral_periods")


class TherapistAssignment(Base):
    """Who delivers which therapy for a patient. History is kept: rows are closed
    (``valid_to``), never rewritten. At most one open row per patient + therapy."""

    __tablename__ = "therapist_assignments"
    __table_args__ = (
        iso_date("valid_from"),
        iso_date("valid_to", nullable=True),
        date_order("valid_from", "valid_to"),
        Index(
            "uq_therapist_assignments_one_open",
            "patient_id",
            "therapy_type_id",
            unique=True,
            sqlite_where=text("valid_to IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), nullable=False)
    therapy_type_id: Mapped[int] = mapped_column(ForeignKey("therapy_types.id"), nullable=False)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = timestamp_column()
