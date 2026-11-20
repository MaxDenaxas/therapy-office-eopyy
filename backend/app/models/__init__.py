"""SQLAlchemy models. Importing this package registers every table on ``Base.metadata``."""

from .base import Base
from .periods import AuditLog, GeneratedDocument, Period, PeriodTherapy, ScheduledSession
from .prescriptions import Prescription, PrescriptionItem, ReferralPeriod, TherapistAssignment
from .reference import (
    AdminAccount,
    CenterSettings,
    HolidayOverride,
    Patient,
    Therapist,
    TherapyType,
)
from .templates import DocumentTemplate, TemplateConfigurationItem, TemplateVersion

__all__ = [
    "AdminAccount",
    "AuditLog",
    "Base",
    "CenterSettings",
    "DocumentTemplate",
    "GeneratedDocument",
    "HolidayOverride",
    "Patient",
    "Period",
    "PeriodTherapy",
    "Prescription",
    "PrescriptionItem",
    "ReferralPeriod",
    "ScheduledSession",
    "TemplateConfigurationItem",
    "TemplateVersion",
    "Therapist",
    "TherapistAssignment",
    "TherapyType",
]
