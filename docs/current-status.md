# Current status

Updated 2026-11-20. Roadmap: `ARCHITECTURE.md` §8. Decisions: `DECISIONS.md`.

## Milestones

- **M0–M4 committed** (`aa46d3b`); **M5.0 committed** (`33e4958`): authentication and API foundations, after two rounds of security review.
- **M5.1 Reference data (therapy types and therapists): implemented, awaiting review. Not committed.** Details: `ARCHITECTURE.md` §5.12, D-34/D-35, P-54–P-56.
- **Next: M5.2 (patients).** Do not start until explicitly instructed. M5.3–M5.7 follow (see roadmap).

## Latest verified results (M5.1, run in the implementation session)

| Check | Result |
|---|---|
| Backend pytest, full suite | 1016 passed, 0 failed, 0 skipped (822 before M5.1 + 194 new, incl. the audit follow-ups) |
| ruff check and format | clean |
| `check_repo.py --all` and `--history` | passed |
| Spike `test_spike.py` | 27 passed |
| Frontend | not rerun (no frontend file changed in M5.1); M5.0 results: typecheck/build OK, 4 tests passed |

## M5.1 in short

- Migration `0004_reference_data_guards`: four triggers, no table change. API: `/api/therapy-types` and `/api/therapists` (list, create, get, patch, activate, deactivate; no DELETE), all protected.
- A therapy type or therapist used by an active template cannot be deactivated; therapist identity fields cannot change while an active template uses them; a therapy type's `code` is immutable once referenced; template matching refuses inactive types/therapists.
- Duplicate therapist names / licence numbers and duplicate therapy-type names are warnings.
- Open points for later milestones: whether M5.3/M6 services must also refuse inactive therapists/types (planned: yes); Q-05 (therapist change mid-period); U-05 (several EOPYY codes per therapy type, additive mapping table if needed). The permanent OCR/AI requirement is recorded as D-35.

## Required before the F2 template-library screens: a Template Management API

A **protected Template Management API must be implemented before the F2 template-library frontend screens.** It should support listing templates, registering templates, uploading new template versions, activating templates and deactivating templates, built on the existing `app/services/templates.py` lifecycle (no parallel workflow), on `protected_router()`, with the same conventions as M5.1 (JSON bodies rejecting unknown fields, stable error codes, audit entries with ids only, upload size/type limits from P-30). It is **not** part of M5.1 and is not yet scheduled as its own M5.x step: it must be planned before F2.

Until then there is **no HTTP or command-line way to manage templates**: `python -m app.cli` has only the `db`, `admin` and `serve` commands. Templates are managed by calling the Python services directly (`register_template`, `add_version`, `activate_template`, `deactivate_template` in `app/services/templates.py`; the tests under `backend/tests/templates/` show the calls). The M5.1 errors that say "deactivate the template first" therefore describe the Python workflow until that API exists.

## Accepted raw-database inconsistency (M5.1 audit)

The database does not stop a raw write from switching a template back on while one of its therapists or therapy types is inactive (there is deliberately **no fifth trigger**). The authoritative paths refuse it: `activate_template` ("… is inactive") and `match_template` / `match_for_period` / `match_for_patient` (`NoMatchingTemplate`). Documented by a test, `test_a_raw_reactivation_of_a_template_with_an_inactive_record_is_allowed_but_never_matched`.

## Known limitations (M5.0)

- A hand-started `uvicorn --host 0.0.0.0` cannot be refused at startup; non-loopback clients still get 403. Use `python -m app.cli serve`.
- Anyone on the machine can lock the login for up to 15 min by failing it; `admin set-password` clears it.
- Session lifetimes are constants, not settings. Individual failed logins are not audited (lockouts are).
- Database constraint errors outside the reference-data routes still give a generic 500 (M5.1 maps them for its own routes only).
- No login screen yet (F1). Response-time equality for unknown usernames is not measured by a test.

## Open questions still relevant

Q-03 (fewer dates than authorized: blocks issuing), Q-01/Q-06 (before real use), U-01–U-04 (before prescriptions, M5.4–M5.5). See `DECISIONS.md`.

## M5.0 review fixes (applied, uncommitted)

1. **Server log privacy:** an outermost `CrashGuard` (`app/errors.py`) answers unexpected errors itself, so uvicorn never logs the traceback and exception message; logs carry the exception type and a request id. Verified with a real uvicorn subprocess.
2. **No write lock for authenticated reads:** session checks run read-only; the once-a-minute `last_seen_at` refresh is a separate best-effort write (200 ms lock wait, skipped when busy).
3. **Route protection:** the structural test inspects every route, not only `/api`.
4. **Failed re-login keeps the session:** the old session is revoked only after the new one is created.

Still open (review Low findings, intentionally not fixed): interactive docs on by default (`THERAPY_ENVIRONMENT` defaults to `development`); CLI tracebacks on EOF/Ctrl-C and on a concurrent `admin create`; length check before NFC normalisation in `verify_password`.

## Required before M7 downloads or any WebSocket support

**Convert `app/security.py` from `BaseHTTPMiddleware` to a pure ASGI middleware before implementing M7 document downloads or any WebSocket support.** Two reasons:

- **WebSockets:** Starlette's `BaseHTTPMiddleware` passes every non-HTTP scope straight through (`scope["type"] != "http"`), so a WebSocket would skip the loopback-client, Host and Origin checks as well as the security headers. The application is and stays **HTTP-only**; the route-protection test reports any route without HTTP methods (the allow-list for them is empty). Supporting a WebSocket means rebuilding those checks for the `websocket` scope first.
- **Streaming:** `BaseHTTPMiddleware` ends a *streamed* response normally when its generator fails mid-way, so a client cannot tell the body was cut. No endpoint streams today. Issued PDFs/DOCX must not be streamed through this middleware; after the conversion this must be re-verified with a test. (A length-declared `FileResponse` may make truncation detectable, but that has not been verified.)