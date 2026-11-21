"""Therapy types and therapists (M5.1). Every route requires the administrator session and
the CSRF token (``protected_router``). There is deliberately no DELETE: referenced records are
deactivated, never removed (D-14, Q-06).

Errors: 404 ``not_found``; 409 with a stable code (``duplicate_code``, ``duplicate_eopyy_code``,
``code_locked``, ``in_use_by_active_template``, ``identity_locked``, ``constraint_violation``)
and an actionable ``message``; 422 for invalid input (no submitted value is echoed).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

from app.api.deps import DbSession
from app.auth.dependencies import protected_router
from app.errors import ApiError
from app.schemas.reference import (
    TherapistCreate,
    TherapistList,
    TherapistResponse,
    TherapistUpdate,
    TherapyTypeCreate,
    TherapyTypeList,
    TherapyTypeResponse,
    TherapyTypeUpdate,
    WarningOut,
)
from app.services import reference as service

therapy_types = protected_router(prefix="/therapy-types", tags=["therapy types"])
therapists = protected_router(prefix="/therapists", tags=["therapists"])

# SQLite integers are 64-bit: a larger id could never exist and would fail to bind (a 500)
RecordId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _http(exc: service.ReferenceDataError) -> ApiError:
    if exc.code == "not_found":
        status = 404
    elif exc.code in ("invalid_value", "invalid_field"):
        status = 422
    else:
        status = 409
    return ApiError(status, exc.code, message=exc.message, **exc.details)


def _warnings(saved: service.Saved) -> list[WarningOut]:
    return [WarningOut(code=w.code, ids=list(w.ids)) for w in saved.warnings]


# ---------------------------------------------------------------- therapy types
@therapy_types.get("", response_model=TherapyTypeList)
def list_therapy_types(db: DbSession, active: bool | None = None):
    return {"items": service.list_therapy_types(db, active)}


@therapy_types.post("", status_code=201, response_model=TherapyTypeResponse)
def create_therapy_type(body: TherapyTypeCreate, db: DbSession):
    try:
        saved = service.create_therapy_type(db, body.model_dump())
    except service.ReferenceDataError as exc:
        raise _http(exc) from None
    return {"item": saved.item, "warnings": _warnings(saved)}


@therapy_types.get("/{type_id}", response_model=TherapyTypeResponse)
def get_therapy_type(type_id: RecordId, db: DbSession):
    try:
        return {"item": service.get_therapy_type(db, type_id), "warnings": []}
    except service.ReferenceDataError as exc:
        raise _http(exc) from None


@therapy_types.patch("/{type_id}", response_model=TherapyTypeResponse)
def update_therapy_type(type_id: RecordId, body: TherapyTypeUpdate, db: DbSession):
    try:
        saved = service.update_therapy_type(db, type_id, body.changes())
    except service.ReferenceDataError as exc:
        raise _http(exc) from None
    return {"item": saved.item, "warnings": _warnings(saved)}


@therapy_types.post("/{type_id}/deactivate", response_model=TherapyTypeResponse)
def deactivate_therapy_type(type_id: RecordId, db: DbSession):
    try:
        return {"item": service.set_therapy_type_active(db, type_id, False), "warnings": []}
    except service.ReferenceDataError as exc:
        raise _http(exc) from None


@therapy_types.post("/{type_id}/activate", response_model=TherapyTypeResponse)
def activate_therapy_type(type_id: RecordId, db: DbSession):
    try:
        return {"item": service.set_therapy_type_active(db, type_id, True), "warnings": []}
    except service.ReferenceDataError as exc:
        raise _http(exc) from None


# ---------------------------------------------------------------- therapists
@therapists.get("", response_model=TherapistList)
def list_therapists(db: DbSession, active: bool | None = None):
    return {"items": service.list_therapists(db, active)}


@therapists.post("", status_code=201, response_model=TherapistResponse)
def create_therapist(body: TherapistCreate, db: DbSession):
    try:
        saved = service.create_therapist(db, body.model_dump())
    except service.ReferenceDataError as exc:
        raise _http(exc) from None
    return {"item": saved.item, "warnings": _warnings(saved)}


@therapists.get("/{therapist_id}", response_model=TherapistResponse)
def get_therapist(therapist_id: RecordId, db: DbSession):
    try:
        return {"item": service.get_therapist(db, therapist_id), "warnings": []}
    except service.ReferenceDataError as exc:
        raise _http(exc) from None


@therapists.patch("/{therapist_id}", response_model=TherapistResponse)
def update_therapist(therapist_id: RecordId, body: TherapistUpdate, db: DbSession):
    try:
        saved = service.update_therapist(db, therapist_id, body.changes())
    except service.ReferenceDataError as exc:
        raise _http(exc) from None
    return {"item": saved.item, "warnings": _warnings(saved)}


@therapists.post("/{therapist_id}/deactivate", response_model=TherapistResponse)
def deactivate_therapist(therapist_id: RecordId, db: DbSession):
    try:
        return {"item": service.set_therapist_active(db, therapist_id, False), "warnings": []}
    except service.ReferenceDataError as exc:
        raise _http(exc) from None


@therapists.post("/{therapist_id}/activate", response_model=TherapistResponse)
def activate_therapist(therapist_id: RecordId, db: DbSession):
    try:
        return {"item": service.set_therapist_active(db, therapist_id, True), "warnings": []}
    except service.ReferenceDataError as exc:
        raise _http(exc) from None
