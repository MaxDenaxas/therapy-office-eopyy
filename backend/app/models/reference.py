"""People, catalogues and single-row settings."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, boolean, iso_date, not_blank, one_of, timestamp_column


class AdminAccount(Base):
    """The one operator (D-02). ``id = 1`` makes a second row impossible."""

    __tablename__ = "admin_account"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        not_blank("username"),
        not_blank("password_hash"),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # argon2id (M5)
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = timestamp_column()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class CenterSettings(Base):
    """Typed single row (``id = 1``), seeded by the initial migration."""

    __tablename__ = "center_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        not_blank("ada", nullable=True),
        not_blank("representative_name", nullable=True),
        CheckConstraint("max_therapists_per_day BETWEEN 1 AND 5", name="max_therapists_range"),
        CheckConstraint(
            "max_sessions_per_therapy_per_day BETWEEN 1 AND 3", name="max_sessions_range"
        ),
        CheckConstraint("prefer_gap_days BETWEEN 0 AND 7", name="prefer_gap_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    ada: Mapped[str | None] = mapped_column(String(64))
    representative_name: Mapped[str | None] = mapped_column(String(200))
    max_therapists_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("2")
    )
    max_sessions_per_therapy_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    prefer_gap_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = timestamp_column()


class HolidayOverride(Base):
    __tablename__ = "holiday_overrides"
    __table_args__ = (iso_date("day"), one_of("kind", "closed", "open"), not_blank("name"))

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class Therapist(Base):
    """Therapist data only — therapists never log in (D-03)."""

    __tablename__ = "therapists"
    __table_args__ = (
        not_blank("full_name"),
        iso_date("license_date", nullable=True),
        boolean("active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(200))
    license_no: Mapped[str | None] = mapped_column(String(64))
    license_authority: Mapped[str | None] = mapped_column(String(200))
    license_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = timestamp_column()


class TherapyType(Base):
    __tablename__ = "therapy_types"
    __table_args__ = (
        not_blank("code"),
        not_blank("name_el"),
        not_blank("template_label"),
        CheckConstraint(
            "eopyy_code IS NULL OR (length(eopyy_code) > 0 AND eopyy_code NOT GLOB '*[^0-9]*')",
            name="eopyy_code_digits",
        ),
        boolean("supports_home_visit"),
        boolean("active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name_el: Mapped[str] = mapped_column(String(200), nullable=False)
    eopyy_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    eopyy_description: Mapped[str | None] = mapped_column(String(200))
    template_label: Mapped[str] = mapped_column(String(100), nullable=False)
    supports_home_visit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (
        not_blank("first_name"),
        not_blank("last_name"),
        not_blank("father_name"),
        CheckConstraint("length(amka) = 11 AND amka NOT GLOB '*[^0-9]*'", name="amka_format"),
        boolean("active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    father_name: Mapped[str] = mapped_column(String(100), nullable=False)
    amka: Mapped[str] = mapped_column(String(11), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()
