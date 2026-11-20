# Current status

Updated 2026-11-20. Roadmap: `ARCHITECTURE.md` §8. Decisions: `DECISIONS.md`.

## Milestones

- **M0–M4 done and committed** (`aa46d3b` on `main`): scaffolding and repository guard, database, document engine, scheduling, template library.
- **M5.0 Authentication and API foundations: done, awaiting review. Not committed** (changes are in the working tree). A source-level security review found three Medium and several Low findings; the three Medium ones and one Low (re-login) are fixed (see below); the remaining Low findings are open.
- **Next: M5.1 (reference data).** Do not start until explicitly instructed. M5.2–M5.7 follow (see roadmap).

## Latest verified results for M5.0 (run in the implementation session)

| Check | Result |
|---|---|
| Backend pytest, full suite | 817 passed, 0 failed, 0 skipped (after the second round of review fixes; 762 before any review fix) |
| Frontend typecheck / `npm test` / build | OK / 4 passed / OK |
| Spike `test_spike.py` | 27 passed |
| ruff check and format | clean |
| `check_repo.py --all` and `--history` | passed |

The frontend, spike and repository-guard results are from the implementation session (the review fixes touched no frontend or spike file; the spike tests and guard were rerun after the fixes and passed). Before the review fixes, only a comment, an empty `__init__.py`, the FastAPI version floor in `pyproject.toml` and documentation changed; affected subsets were rerun and passed. The documentation-only task that created this file did not rerun any suite.

## Known limitations (M5.0)

- A hand-started `uvicorn --host 0.0.0.0` cannot be refused at startup; non-loopback clients still get 403. Use `python -m app.cli serve`.
- Anyone on the machine can lock the login for up to 15 min by failing it; `admin set-password` clears it.
- Session lifetimes are constants, not settings. Individual failed logins are not audited (lockouts are).
- Database constraint errors give a generic 500 until mapped to 409 (M5.1).
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