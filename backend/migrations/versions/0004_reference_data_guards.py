"""reference data guards (M5.1)

Four triggers, no table changes and no data migration:

  * ``therapy_types_code_frozen``        a therapy type's ``code`` cannot change once any
                                         prescription item, period therapy, template
                                         configuration or therapist assignment uses the type
                                         (stored template field maps name therapies by code).
  * ``therapy_types_keep_active_templates``  a therapy type used by an *active* template cannot
                                         be deactivated.
  * ``therapists_keep_active_templates`` same for therapists.
  * ``therapists_identity_frozen``       the details a template prints (full_name, license_no,
                                         license_authority, license_date) cannot change while an
                                         *active* template uses the therapist (D-27).

The services check the same rules first and answer with an actionable message (naming the
templates to deactivate); these triggers are the backstop for any other writer.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_TEMPLATE_USES_TYPE = (
    "EXISTS (SELECT 1 FROM template_configuration_items i "
    "JOIN document_templates t ON t.id = i.template_id "
    "WHERE i.therapy_type_id = OLD.id AND t.active = 1)"
)
_ACTIVE_TEMPLATE_USES_THERAPIST = (
    "EXISTS (SELECT 1 FROM template_configuration_items i "
    "JOIN document_templates t ON t.id = i.template_id "
    "WHERE i.therapist_id = OLD.id AND t.active = 1)"
)

TRIGGERS = [
    """CREATE TRIGGER therapy_types_code_frozen BEFORE UPDATE OF code ON therapy_types
    WHEN NEW.code IS NOT OLD.code AND (
         EXISTS (SELECT 1 FROM prescription_items WHERE therapy_type_id = OLD.id)
      OR EXISTS (SELECT 1 FROM period_therapies WHERE therapy_type_id = OLD.id)
      OR EXISTS (SELECT 1 FROM template_configuration_items WHERE therapy_type_id = OLD.id)
      OR EXISTS (SELECT 1 FROM therapist_assignments WHERE therapy_type_id = OLD.id))
    BEGIN SELECT RAISE(ABORT, 'therapy type code cannot change once it is used'); END""",
    f"""CREATE TRIGGER therapy_types_keep_active_templates
    BEFORE UPDATE OF active ON therapy_types
    WHEN OLD.active = 1 AND NEW.active = 0 AND {_ACTIVE_TEMPLATE_USES_TYPE}
    BEGIN SELECT RAISE(ABORT, 'a therapy type used by an active template cannot be deactivated; deactivate the template first'); END""",
    f"""CREATE TRIGGER therapists_keep_active_templates
    BEFORE UPDATE OF active ON therapists
    WHEN OLD.active = 1 AND NEW.active = 0 AND {_ACTIVE_TEMPLATE_USES_THERAPIST}
    BEGIN SELECT RAISE(ABORT, 'a therapist used by an active template cannot be deactivated; deactivate the template first'); END""",
    f"""CREATE TRIGGER therapists_identity_frozen
    BEFORE UPDATE OF full_name, license_no, license_authority, license_date ON therapists
    WHEN (NEW.full_name IS NOT OLD.full_name
       OR NEW.license_no IS NOT OLD.license_no
       OR NEW.license_authority IS NOT OLD.license_authority
       OR NEW.license_date IS NOT OLD.license_date)
      AND {_ACTIVE_TEMPLATE_USES_THERAPIST}
    BEGIN SELECT RAISE(ABORT, 'a therapist used by an active template cannot change identity details; deactivate the template first'); END""",
]

NAMES = [
    "therapy_types_code_frozen",
    "therapy_types_keep_active_templates",
    "therapists_keep_active_templates",
    "therapists_identity_frozen",
]


def upgrade() -> None:
    for sql in TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    for name in NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
