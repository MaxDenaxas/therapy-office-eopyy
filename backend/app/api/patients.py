"""Patients (M5.2). Every route requires the administrator session and the CSRF token
(``protected_router``). There is deliberately no DELETE: patients are deactivated, never removed.

Privacy: lists and search results carry only ``amka_masked``; the full AMKA appears only in the
detail of one patient. Personal search values (names, AMKA) are accepted **only** in the JSON body
of ``POST /api/patients/search`` — the GET routes accept a fixed set of non-personal query
parameters and refuse any other (``unknown_query_parameter``, names never echoed). Lists and
searches return active patients unless ``active=false`` asks for the inactive ones. The search runs
in a read-only transaction (``ReadDbSession``): it never takes the SQLite write lock.

Errors: 404 ``not_found``; 409 ``duplicate_amka`` (with the other patient's id and active flag),
``amka_change_needs_confirmation`` (with the number of issued documents), ``constraint_violation``;
422 ``validation_error``, ``invalid_value``, ``invalid_field``, ``invalid_query``,
``unknown_query_parameter``. No submitted value is ever echoed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Path, Query

from app.api.deps import DbSession, ReadDbSession, allow_query, get_clock
from app.auth.dependencies import protected_router
from app.errors import ApiError
from app.schemas.patients import (
    AmkaCorrection,
    EmptyBody,
    PatientCreate,
    PatientDetail,
    PatientDetailResponse,
    PatientPage,
    PatientResponse,
    PatientSearch,
    PatientSummary,
    PatientUpdate,
    PatientWarningOut,
    UsageOut,
)
from app.services import patients as service

patients = protected_router(prefix="/patients", tags=["patients"])

RecordId = Annotated[int, Path(ge=1, le=2**63 - 1)]
Clock = Annotated[Callable[[], datetime], Depends(get_clock)]
NO_QUERY = [Depends(allow_query())]


def _http(exc: service.PatientError) -> ApiError:
    if exc.code == "not_found":
        status = 404
    elif exc.code in ("invalid_value", "invalid_field", "invalid_query"):
        status = 422
    else:
        status = 409
    return ApiError(status, exc.code, message=exc.message, **exc.details)


def _saved(saved: service.Saved) -> dict:
    return {
        "item": PatientDetail.model_validate(saved.item),
        "warnings": [
            PatientWarningOut(code=w.code, ids=list(w.ids), count=w.count) for w in saved.warnings
        ],
    }


def _page(page: service.Page) -> dict:
    return {
        "items": [PatientSummary.of(row) for row in page.items],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


@patients.get(
    "",
    response_model=PatientPage,
    dependencies=[Depends(allow_query("active", "offset", "limit"))],
)
def list_patients(
    db: DbSession,
    active: bool = True,  # omitted: active patients only
    offset: Annotated[int, Query(ge=0, le=service.MAX_OFFSET)] = 0,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = service.DEFAULT_LIMIT,
):
    return _page(service.list_patients(db, active, offset, limit))


@patients.post("/search", response_model=PatientPage, dependencies=NO_QUERY)
def search_patients(body: PatientSearch, db: ReadDbSession):
    try:
        page = service.search_patients(
            db,
            query=body.query,
            amka=body.amka,
            active=body.active,
            offset=body.offset,
            limit=body.limit,
        )
    except service.PatientError as exc:
        raise _http(exc) from None
    return _page(page)


@patients.post("", status_code=201, response_model=PatientResponse, dependencies=NO_QUERY)
def create_patient(body: PatientCreate, db: DbSession, clock: Clock):
    try:
        return _saved(service.create_patient(db, body.model_dump(), clock()))
    except service.PatientError as exc:
        raise _http(exc) from None


@patients.get("/{patient_id}", response_model=PatientDetailResponse, dependencies=NO_QUERY)
def get_patient(patient_id: RecordId, db: DbSession):
    try:
        row = service.get_patient(db, patient_id)
        usage = service.patient_usage(db, patient_id)
    except service.PatientError as exc:
        raise _http(exc) from None
    return {"item": PatientDetail.model_validate(row), "usage": UsageOut(**usage.__dict__)}


@patients.patch("/{patient_id}", response_model=PatientResponse, dependencies=NO_QUERY)
def update_patient(patient_id: RecordId, body: PatientUpdate, db: DbSession, clock: Clock):
    try:
        return _saved(service.update_patient(db, patient_id, body.changes(), clock()))
    except service.PatientError as exc:
        raise _http(exc) from None


@patients.post("/{patient_id}/amka", response_model=PatientResponse, dependencies=NO_QUERY)
def correct_amka(patient_id: RecordId, body: AmkaCorrection, db: DbSession, clock: Clock):
    try:
        saved = service.correct_amka(
            db, patient_id, body.amka, clock(), body.acknowledge_issued_documents
        )
    except service.PatientError as exc:
        raise _http(exc) from None
    return _saved(saved)


@patients.post("/{patient_id}/deactivate", response_model=PatientResponse, dependencies=NO_QUERY)
def deactivate_patient(
    patient_id: RecordId, db: DbSession, clock: Clock, body: EmptyBody | None = None
):
    try:
        return _saved(service.set_patient_active(db, patient_id, False, clock()))
    except service.PatientError as exc:
        raise _http(exc) from None


@patients.post("/{patient_id}/activate", response_model=PatientResponse, dependencies=NO_QUERY)
def activate_patient(
    patient_id: RecordId, db: DbSession, clock: Clock, body: EmptyBody | None = None
):
    try:
        return _saved(service.set_patient_active(db, patient_id, True, clock()))
    except service.PatientError as exc:
        raise _http(exc) from None
