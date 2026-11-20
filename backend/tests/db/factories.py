"""Tiny helpers that insert valid rows. All data is invented.

AMKA values are fakes the repository guard accepts (repeated digits or 12345678901).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import (
    DocumentTemplate,
    GeneratedDocument,
    Patient,
    Period,
    PeriodTherapy,
    Prescription,
    PrescriptionItem,
    ReferralPeriod,
    ScheduledSession,
    TemplateConfigurationItem,
    TemplateVersion,
    Therapist,
    TherapyType,
)

LAYOUT = "eopyy_ekpy_monthly_v1"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def add(session: Session, obj):
    session.add(obj)
    session.flush()
    return obj


def patient(session: Session, amka: str = "11111111111", **kw) -> Patient:
    values = {"first_name": "ΓΕΩΡΓΙΟΣ", "last_name": "ΔΟΚΙΜΑΣΤΙΚΟΣ", "father_name": "ΝΙΚΟΛΑΟΥ"}
    return add(session, Patient(amka=amka, **{**values, **kw}))


def therapist(session: Session, name: str = "Θεραπεύτρια Δοκιμής", **kw) -> Therapist:
    return add(session, Therapist(full_name=name, **kw))


def therapy_type(
    session: Session, code: str, label: str, eopyy_code: str | None = None, **kw
) -> TherapyType:
    return add(
        session,
        TherapyType(
            code=code, name_el=label.rstrip(":"), template_label=label, eopyy_code=eopyy_code, **kw
        ),
    )


def prescription(
    session: Session,
    pat: Patient,
    valid_from: date,
    valid_to: date,
    status: str = "confirmed",
    **kw,
) -> Prescription:
    return add(
        session,
        Prescription(
            patient_id=pat.id,
            valid_from=valid_from,
            valid_to=valid_to,
            layout=LAYOUT,
            status=status,
            **kw,
        ),
    )


def item(
    session: Session, rx: Prescription, tt: TherapyType, monthly_quantity: int, **kw
) -> PrescriptionItem:
    return add(
        session,
        PrescriptionItem(
            prescription_id=rx.id, therapy_type_id=tt.id, monthly_quantity=monthly_quantity, **kw
        ),
    )


def referral(
    session: Session,
    it: PrescriptionItem,
    seq: int,
    valid_from: date,
    valid_to: date,
    quantity: int,
    **kw,
) -> ReferralPeriod:
    return add(
        session,
        ReferralPeriod(
            prescription_item_id=it.id,
            sequence_no=seq,
            valid_from=valid_from,
            valid_to=valid_to,
            authorized_quantity=quantity,
            **kw,
        ),
    )


def monthly_windows(start: date, count: int) -> list[tuple[date, date]]:
    """16/09–15/10, 16/10–15/11, ... (end = day before the same day next month)."""
    windows = []
    current = start
    for _ in range(count):
        month = current.month % 12 + 1
        year = current.year + (current.month == 12)
        nxt = current.replace(year=year, month=month)
        windows.append((current, nxt - timedelta(days=1)))
        current = nxt
    return windows


def period(session: Session, pat: Patient, start: date, end: date, **kw) -> Period:
    return add(session, Period(patient_id=pat.id, start_date=start, end_date=end, **kw))


def period_therapy(
    session: Session,
    per: Period,
    ref: ReferralPeriod,
    thr: Therapist,
    therapy_type_id: int | None = None,
    **kw,
) -> PeriodTherapy:
    if therapy_type_id is None:
        therapy_type_id = session.get(PrescriptionItem, ref.prescription_item_id).therapy_type_id
    return add(
        session,
        PeriodTherapy(
            period_id=per.id,
            referral_period_id=ref.id,
            therapist_id=thr.id,
            therapy_type_id=therapy_type_id,
            **kw,
        ),
    )


def session_date(
    session: Session, pt: PeriodTherapy, day: date, origin: str = "proposed"
) -> ScheduledSession:
    return add(session, ScheduledSession(period_therapy_id=pt.id, session_date=day, origin=origin))


def template(
    session: Session, name: str, key: str, config: list[tuple[int, int]] = ()
) -> DocumentTemplate:
    tpl = add(session, DocumentTemplate(name=name, configuration_key=key))
    for therapy_type_id, therapist_id in config:
        add(
            session,
            TemplateConfigurationItem(
                template_id=tpl.id, therapy_type_id=therapy_type_id, therapist_id=therapist_id
            ),
        )
    return tpl


def version(
    session: Session, tpl: DocumentTemplate, no: int = 1, status: str = "valid"
) -> TemplateVersion:
    return add(
        session,
        TemplateVersion(
            template_id=tpl.id,
            version_no=no,
            original_filename=f"{tpl.name}.docx",
            sha256=sha(f"{tpl.name}-{no}"),
            storage_path=f"templates/{sha(tpl.name + str(no))}.docx",
            size_bytes=1234,
            field_map=json.dumps({"blocks": []}),
            field_map_schema_version=1,
            validation_status=status,
            validation_report=json.dumps({"errors": []}),
        ),
    )


def document(
    session: Session, per: Period, tv: TemplateVersion, version_no: int = 1, **kw
) -> GeneratedDocument:
    values = {
        "reason": "initial" if version_no == 1 else "correction",
        "correction_note": None if version_no == 1 else "διόρθωση ημερομηνίας",
        "issue_date": per.end_date + timedelta(days=1),
        "docx_path": f"documents/{per.id}-v{version_no}.docx",
        "docx_sha256": sha("docx"),
        "pdf_path": f"documents/{per.id}-v{version_no}.pdf",
        "pdf_sha256": sha("pdf"),
        "snapshot": json.dumps({"period": per.id}),
        "snapshot_sha256": sha("snap"),
        "app_version": "0.1.0",
    }
    return add(
        session,
        GeneratedDocument(
            period_id=per.id, version=version_no, template_version_id=tv.id, **{**values, **kw}
        ),
    )
