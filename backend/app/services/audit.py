"""The one way services write audit entries (extracted unchanged from M5.1's ``_audit``).

``details`` holds ids, counts and field *names* only — never AMKA, names or other submitted
values (ARCHITECTURE.md §5.10). The audit log is append-only (triggers in migration 0001).
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(session: Session, action: str, entity: str, entity_id: int, **details) -> None:
    session.add(
        AuditLog(
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=json.dumps(details, sort_keys=True, default=str) if details else None,
        )
    )


__all__ = ["record_audit"]
