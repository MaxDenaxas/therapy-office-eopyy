"""DOCX templates: the logical template, its therapy/therapist configuration and
immutable file versions.

Database guarantees (triggers in the initial migration):
  * template_versions can never be updated or deleted
  * a template can only be active with a current version that belongs to it and is valid
  * a template's configuration is frozen once it has a version
  * at most one active template per configuration_key (partial unique index)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, boolean, json_text, not_blank, one_of, sha256_hex, timestamp_column


class DocumentTemplate(Base):
    __tablename__ = "document_templates"
    __table_args__ = (
        not_blank("name"),
        not_blank("configuration_key"),
        boolean("active"),
        Index(
            "uq_document_templates_one_active_per_configuration",
            "configuration_key",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    # canonical sorted "therapy_type_id:therapist_id" pairs, e.g. "1:3,4:2" (computed by the
    # service from template_configuration_items)
    configuration_key: Mapped[str] = mapped_column(String(500), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("template_versions.id", use_alter=True)
    )
    created_at: Mapped[datetime] = timestamp_column()


class TemplateConfigurationItem(Base):
    __tablename__ = "template_configuration_items"

    template_id: Mapped[int] = mapped_column(ForeignKey("document_templates.id"), primary_key=True)
    therapy_type_id: Mapped[int] = mapped_column(ForeignKey("therapy_types.id"), primary_key=True)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), nullable=False)


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version_no"),
        CheckConstraint("version_no > 0", name="version_positive"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("field_map_schema_version > 0", name="field_map_schema_positive"),
        sha256_hex("sha256"),
        not_blank("original_filename"),
        not_blank("storage_path"),
        json_text("field_map"),
        json_text("validation_report", nullable=True),
        one_of("validation_status", "valid", "invalid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("document_templates.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    field_map: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    field_map_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # validated before insert; the row is immutable afterwards
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    validation_report: Mapped[str | None] = mapped_column(Text)  # JSON
    preview_pdf_path: Mapped[str | None] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = timestamp_column()
