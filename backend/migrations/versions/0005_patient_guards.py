"""patient guards (M5.2)

One trigger, no table changes and no data migration:

  * ``patients_no_delete``  a patient row can never be deleted — referenced or not. Patients are
                            deactivated (``active = 0``) and can be reactivated; their prescriptions,
                            periods and issued documents keep pointing at them (D-14, Q-06).

Foreign keys (RESTRICT) already refused deleting a *referenced* patient; this closes the gap for
an unreferenced one. The service has no delete path at all; the trigger is the backstop for any
other writer.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS = [
    """CREATE TRIGGER patients_no_delete BEFORE DELETE ON patients
    BEGIN SELECT RAISE(ABORT, 'patients are deactivated, never deleted'); END""",
]

NAMES = ["patients_no_delete"]


def upgrade() -> None:
    for sql in TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    for name in NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
