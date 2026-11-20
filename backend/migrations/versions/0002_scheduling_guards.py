"""scheduling guards (M3)

Strengthens the three triggers on scheduled_sessions (same names as in 0001):
  * an approved schedule is locked like an issued one (reopen to change it)
  * no Saturday or Sunday dates
  * at most center_settings.max_sessions_per_therapy_per_day sessions of one therapy
    type per patient per day (this also prevents duplicate dates)
  * at most center_settings.max_therapists_per_day distinct therapists per patient
    per day, across all of the patient's document periods
Public holidays and center closures come from the holiday calendar, which the
database cannot see; the scheduling services check those.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = (
    "(SELECT p.status FROM periods p JOIN period_therapies pt ON pt.period_id = p.id "
    "WHERE pt.id = {})"
)
_PATIENT = (
    "(SELECT p.patient_id FROM periods p JOIN period_therapies pt ON pt.period_id = p.id "
    "WHERE pt.id = {})"
)
_ISSUED = "document period is issued; reopen it to make changes"
_APPROVED = "the schedule is approved; reopen it to change planned dates"


def _abort(message: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}')"


def _checks(row: str, exclude_self: str) -> str:
    """Window, weekend and daily-limit checks for the session ``row`` (NEW)."""
    patient = _PATIENT.format(f"{row}.period_therapy_id")
    same_day = (
        "FROM scheduled_sessions s JOIN period_therapies pt ON pt.id = s.period_therapy_id "
        f"JOIN periods p ON p.id = pt.period_id WHERE p.patient_id = {patient} "
        f"AND s.session_date = {row}.session_date {exclude_self}"
    )
    return f"""
      {_abort("session date lies outside the document period")}
        WHERE NOT EXISTS (SELECT 1 FROM period_therapies pt JOIN periods p ON p.id = pt.period_id
                          WHERE pt.id = {row}.period_therapy_id
                            AND {row}.session_date BETWEEN p.start_date AND p.end_date);
      {_abort("planned dates cannot fall on a Saturday or Sunday")}
        WHERE strftime('%w', {row}.session_date) IN ('0', '6');
      {_abort("more sessions of this therapy on one day than the center allows")}
        WHERE (SELECT count(*) {same_day} AND pt.therapy_type_id =
                 (SELECT therapy_type_id FROM period_therapies WHERE id = {row}.period_therapy_id))
              >= (SELECT max_sessions_per_therapy_per_day FROM center_settings WHERE id = 1);
      {_abort("more different therapists on one day than the center allows")}
        WHERE (SELECT count(*) FROM (
                 SELECT pt.therapist_id {same_day}
                 UNION
                 SELECT therapist_id FROM period_therapies WHERE id = {row}.period_therapy_id))
              > (SELECT max_therapists_per_day FROM center_settings WHERE id = 1);"""


NEW_TRIGGERS = [
    f"""CREATE TRIGGER scheduled_sessions_check_insert BEFORE INSERT ON scheduled_sessions
    BEGIN
      {_abort(_ISSUED)}
        WHERE {_STATUS.format("NEW.period_therapy_id")} = 'issued';
      {_abort(_APPROVED)}
        WHERE {_STATUS.format("NEW.period_therapy_id")} = 'schedule_approved';
      {_checks("NEW", "")}
    END""",
    f"""CREATE TRIGGER scheduled_sessions_check_update BEFORE UPDATE ON scheduled_sessions
    BEGIN
      {_abort(_ISSUED)}
        WHERE {_STATUS.format("OLD.period_therapy_id")} = 'issued';
      {_abort(_APPROVED)}
        WHERE {_STATUS.format("OLD.period_therapy_id")} = 'schedule_approved';
      {_abort("a planned date cannot move to another therapy")}
        WHERE NEW.period_therapy_id IS NOT OLD.period_therapy_id;
      {_checks("NEW", "AND s.id <> OLD.id")}
    END""",
    f"""CREATE TRIGGER scheduled_sessions_locked_delete BEFORE DELETE ON scheduled_sessions
    BEGIN
      {_abort(_ISSUED)}
        WHERE {_STATUS.format("OLD.period_therapy_id")} = 'issued';
      {_abort(_APPROVED)}
        WHERE {_STATUS.format("OLD.period_therapy_id")} = 'schedule_approved';
    END""",
]

# The definitions created by 0001, restored on downgrade (kept verbatim: migrations are history).
OLD_TRIGGERS = [
    f"""CREATE TRIGGER scheduled_sessions_check_insert BEFORE INSERT ON scheduled_sessions
    BEGIN
      {_abort(_ISSUED)}
        WHERE {_STATUS.format("NEW.period_therapy_id")} = 'issued';
      {_abort("session date lies outside the document period")}
        WHERE NOT EXISTS (SELECT 1 FROM period_therapies pt JOIN periods p ON p.id = pt.period_id
                          WHERE pt.id = NEW.period_therapy_id
                            AND NEW.session_date BETWEEN p.start_date AND p.end_date);
    END""",
    f"""CREATE TRIGGER scheduled_sessions_check_update BEFORE UPDATE ON scheduled_sessions
    BEGIN
      {_abort(_ISSUED)}
        WHERE {_STATUS.format("OLD.period_therapy_id")} = 'issued';
      {_abort("a planned date cannot move to another therapy")}
        WHERE NEW.period_therapy_id IS NOT OLD.period_therapy_id;
      {_abort("session date lies outside the document period")}
        WHERE NOT EXISTS (SELECT 1 FROM period_therapies pt JOIN periods p ON p.id = pt.period_id
                          WHERE pt.id = NEW.period_therapy_id
                            AND NEW.session_date BETWEEN p.start_date AND p.end_date);
    END""",
    f"""CREATE TRIGGER scheduled_sessions_locked_delete BEFORE DELETE ON scheduled_sessions
    WHEN {_STATUS.format("OLD.period_therapy_id")} = 'issued'
    BEGIN {_abort(_ISSUED)}; END""",
]

NAMES = [
    "scheduled_sessions_check_insert",
    "scheduled_sessions_check_update",
    "scheduled_sessions_locked_delete",
]


def upgrade() -> None:
    for name in NAMES:
        op.execute(f"DROP TRIGGER {name}")
    for statement in NEW_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    for name in NAMES:
        op.execute(f"DROP TRIGGER {name}")
    for statement in OLD_TRIGGERS:
        op.execute(statement)
