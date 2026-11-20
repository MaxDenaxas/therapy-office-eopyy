"""Declarative base and small helpers for SQLite-portable constraints.

Conventions (docs/ARCHITECTURE.md §4): integer primary keys; dates stored as ISO
text and checked with ``col = date(col)``; booleans as 0/1 with a CHECK; JSON as
text with ``json_valid``; status values as text with a CHECK; foreign keys
RESTRICT (the default) — referenced rows are deactivated, never deleted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def timestamp_column() -> Mapped[datetime]:
    return mapped_column(DateTime, nullable=False, server_default=func.current_timestamp())


# --- CHECK helpers ---------------------------------------------------------------
def iso_date(col: str, name: str | None = None, nullable: bool = False) -> CheckConstraint:
    """Valid ISO date text. ``IS`` (not ``=``): date('16/09/2025') is NULL, and a plain
    comparison with NULL would let the bad value through. NULL itself passes, so
    nullability is decided by the column's NOT NULL alone."""
    del nullable  # same expression either way (kept for readability at call sites)
    return CheckConstraint(f"{col} IS date({col})", name=name or f"{col}_iso_date")


def boolean(col: str, nullable: bool = False) -> CheckConstraint:
    expr = f"{col} IN (0, 1)" + (f" OR {col} IS NULL" if nullable else "")
    return CheckConstraint(expr, name=f"{col}_bool")


def one_of(col: str, *values: str) -> CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{col} IN ({listed})", name=f"{col}_allowed")


def not_blank(col: str, nullable: bool = False) -> CheckConstraint:
    expr = f"length(trim({col})) > 0"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_not_blank")


def json_text(col: str, nullable: bool = False) -> CheckConstraint:
    expr = f"json_valid({col})"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_json")


def date_order(start: str, end: str) -> CheckConstraint:
    return CheckConstraint(f"{end} IS NULL OR {end} >= {start}", name=f"{end}_after_{start}")


def sha256_hex(col: str) -> CheckConstraint:
    return CheckConstraint(
        f"length({col}) = 64 AND {col} NOT GLOB '*[^0-9a-f]*'", name=f"{col}_sha256"
    )
