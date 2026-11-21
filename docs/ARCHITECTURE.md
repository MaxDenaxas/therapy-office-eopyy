# Architecture

Status: living document — updated 2026-11-20. Items marked open or awaiting review below are still unresolved. Requirements are referenced by ID from `PRODUCT_REQUIREMENTS.md`; decisions by ID from `DECISIONS.md`.

## 1. Overview

A local web application on one center computer: a FastAPI process serves the API and the built React frontend on `127.0.0.1`. Data lives in one SQLite database plus a file store, both outside the Git repository.

```
Browser (React) ──HTTP──▶ FastAPI ─┬─ services ─┬─ scheduling/   (pure)
                                   │            ├─ documents/    (pure-ish: DOCX + PDF)
                                   │            └─ prescriptions/ (manual now; extractor later)
                                   ├─ SQLite (SQLAlchemy 2.0, Alembic)
                                   ├─ file store (templates, issued documents, previews)
                                   └─ LibreOffice headless (one conversion at a time)
```

## 2. Technology

| Area | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2 |
| Database | SQLite (WAL, `foreign_keys=ON`), SQLAlchemy 2.0, Alembic in batch mode; immutability triggers in migrations |
| Documents | python-docx + lxml (ported from the spike) |
| PDF | LibreOffice headless; pypdf for page-count/font checks in production (PyMuPDF only in dev/tests: AGPL) |
| Holidays | `holidays` package (Greece, incl. Orthodox Easter), pinned, plus DB overrides |
| Auth | One admin account (created by CLI only), Argon2id (`argon2-cffi`), server-side sessions stored as SHA-256 hashes, CSRF synchronizer token (§5.10) |
| Frontend | React + TypeScript + Vite + Tailwind CSS (M0: React 19, Vite 8, Tailwind 4, TypeScript 7). TanStack Query and React Router are added in F1, when the first real screens need them. |
| Tests | Backend: pytest (+ `httpx2` for the FastAPI test client), hypothesis for scheduler properties (M3). Frontend: Vitest + Testing Library. Lint: ruff (backend), `tsc --noEmit` (frontend). |

Not used: Redis, Docker, task queues, a separate PDF service, multi-tenant features (D-03, D-04).

## 3. Repository layout

Status after M5.0: everything shown with an *(Mx)* tag exists (M0–M4 committed, M5.0 awaiting review); entries marked "later" or "future" do not exist yet. See `current-status.md`.

```
therapy-office/
├── README.md                # setup and run commands (M0)
├── docs/                    # this documentation
├── spike/                   # FROZEN prototype + acceptance harness (unchanged)
├── tools/check_repo.py      # repository safety check: DOCX/PDF/DB/AMKA/denylist (M0)
├── .githooks/pre-commit     # runs the safety check; enable with `git config core.hooksPath .githooks` (M0)
├── backend/
│   ├── pyproject.toml  .env.example                                   (M0)
│   ├── alembic.ini  migrations/   # Alembic: env.py + versions/0001 (M1), 0002 (M3), 0003_admin_sessions (M5.0)
│   ├── app/
│   │   ├── main.py  config.py                                         (M0: app factory, THERAPY_* settings)
│   │   ├── api/health.py                                              (M0)
│   │   ├── api/deps.py  api/auth.py                                   (M5.0: lazy database, request sessions; login/logout/session)
│   │   ├── auth/                                                      (M5.0: passwords, service, dependencies)
│   │   ├── security.py  errors.py                                     (M5.0: request filter + headers; privacy-safe errors)
│   │   ├── db.py  migrate.py  cli.py                                  (M1: engine, migrations, `db` commands; M5.0: `admin`, `serve`)
│   │   ├── models/                                                    (M1: SQLAlchemy models)
│   │   ├── storage.py                                                 (M2: content-addressed, read-only file store)
│   │   ├── services/scheduling.py                                     (M3: propose / edit / status / approve / reopen)
│   │   ├── services/reference.py  api/reference.py  schemas/   (M5.1: therapy types and therapists)
│   │   ├── services/patients.py  api/patients.py  schemas/patients.py  text.py  services/audit.py  (M5.2)
│   │   ├── services/templates.py                                      (M4: register / versions / activate / match)
│   │   ├── schemas/                                                   # later
│   │   ├── scheduling/      # (M3) pure: model, calendar, rules, solver, flow
│   │   ├── documents/       # (M2) errors, field_map, package, locate, render, verify, validate,
│   │   │                    #      pdf, pipeline; mapping (M4: mapping assistant)
│   │   └── prescriptions/   # input model, validation, layouts.py (referral pre-fill rules); extractor interface (future)
│   └── tests/
│       ├── fixtures/eopyy_builder.py   # synthetic EOPYY-style DOCX built in code (M0)
│       └── unit/ integration/          (M0)
└── frontend/                # Vite + React + TypeScript + Tailwind + Vitest (M0: shell only)
    └── src/  api/ components/ test/   # features/{patients,prescriptions,therapists,template-library,periods,documents,settings} later
```

Data directory (`THERAPY_DATA_DIR`; default `%PROGRAMDATA%\TherapyOffice` on Windows, `~/.local/share/therapy-office` elsewhere): `therapy.db`, `templates/`, `documents/`, `previews/`, `backups/`. The application **refuses to start** if this directory is inside a Git repository (D-05).

## 4. Data model

Conventions: integer primary keys; ISO dates; UTC timestamps; JSON as text with `json_valid` checks; foreign keys `RESTRICT`; referenced rows are deactivated, never deleted.

| Table | Purpose | Key fields and constraints |
|---|---|---|
| `admin_account` | The single operator | `id = 1` (CHECK), username, password_hash (Argon2id), failed_login_count, locked_until, last_login_at; created only by the CLI, never seeded |
| `admin_sessions` | Server-side login sessions (M5.0, migration `0003`) | admin_id → admin_account, `token_hash` (UNIQUE) and `csrf_hash` — SHA-256 hex (CHECK), never the tokens; created_at, last_seen_at, `expires_at` (absolute limit), revoked_at; trigger `admin_sessions_fixed`: only `last_seen_at` may change, and `revoked_at` once |
| `patients` | Personal data entered once | first_name, last_name, father_name, `amka` UNIQUE (11 digits), notes, active, created_at, updated_at; trigger `patients_no_delete` (`0005`): never deleted, only deactivated (§5.14) |
| `therapists` | Therapist data (no login) | full_name, specialty, license_no, license_authority, license_date, active |
| `therapy_types` | Therapy catalogue | `code` UNIQUE, name_el, `eopyy_code` UNIQUE NULL (e.g. 530000010), `eopyy_description` (e.g. "ΑΓΩΓΗ ΛΟΓΟΥ - ΛΟΓΟΘΕΡΑΠΕΙΑ"), `template_label` (e.g. "Λογοθεραπεία:"), supports_home_visit |
| `prescriptions` | One prescription ("Γνωμάτευση") as approved | patient_id, `prescription_no` (barcode no., UNIQUE NULL), issued_on, decision_no, decision_date, examination_date, **valid_from, valid_to** (overall validity), doctor_name?, `layout` (e.g. `eopyy_ekpy_monthly_v1`), `source` (`manual`/`extracted`), extraction_id?, status (`draft`/`confirmed`), notes |
| `prescription_items` | One prescribed therapy | prescription_id, therapy_type_id, eopyy_code (as printed), **`monthly_quantity`** (> 0, NOT NULL, no default), `printed_total_quantity` NULL, `printed_duration_months` NULL (both kept as printed, not interpreted); UNIQUE(prescription_id, therapy_type_id) |
| `referral_periods` | One monthly referral ("Μηνιαίο Παραπεμπτικό") of a prescribed therapy | prescription_item_id, sequence_no, `referral_no` NULL, **valid_from, valid_to**, **`authorized_quantity`** (> 0; pre-filled = monthly_quantity, stored explicitly); UNIQUE(prescription_item_id, sequence_no); UNIQUE(prescription_item_id, valid_from); service checks: inside the prescription's validity, no overlaps |
| `prescription_extractions` | *Future (R-RX-6), **not created in M1***: input text + proposed values for review | input_text (deleted once reviewed — P-15), extractor, extractor_version, proposed (JSON), created_at |
| `therapist_assignments` | Who delivers which therapy | patient_id, therapy_type_id, therapist_id, valid_from, valid_to (NULL = current); partial UNIQUE(patient_id, therapy_type_id) WHERE valid_to IS NULL |
| `document_templates` | Logical template | name UNIQUE, `configuration_key`, active, current_version_id; partial UNIQUE(configuration_key) WHERE active |
| `template_configuration_items` | The combination a template represents | (template_id, therapy_type_id) PK, therapist_id |
| `template_versions` | Immutable uploaded file + mapping | template_id, version_no, sha256 (64 lowercase hex), storage_path, size_bytes, field_map (JSON), field_map_schema_version, validation_status (`valid`/`invalid`, decided before insert), validation_report, preview_pdf_path, uploaded_at; UNIQUE(template_id, version_no); no UPDATE/DELETE |
| `periods` | Document period: one monthly referral window for one patient | patient_id, start_date, end_date (≥ start), status (`draft` → `schedule_approved` → `issued`); UNIQUE(patient_id, start_date, end_date). Periods of one patient **may overlap** (therapies with independent windows, P-22). `issued` only via a generated document; window fixed once therapies are attached |
| `period_therapies` | Snapshot per therapy for the document period | period_id, therapy_type_id, therapist_id, **`referral_period_id`** (UNIQUE: a referral is documented once), home_visit (NULL = not chosen); UNIQUE(period_id, therapy_type_id). **No quantity column**: the quantity is read through `referral_period_id` (P-23). Trigger: the referral must belong to a confirmed prescription of the same patient and therapy type and have exactly the period's window; links cannot be rewired; locked when issued |
| `scheduled_sessions` | Planned dates | period_therapy_id, session_date, origin (`proposed`/`manual`); locked when issued |
| `generated_documents` | Issued documents only | period_id, version, template_version_id, issue_date, docx/pdf path + sha256, `snapshot` (JSON) + sha256, app/LibreOffice version, reason (`initial`/`correction`), note; UNIQUE(period_id, version); no UPDATE/DELETE |
| `center_settings` | Typed single row | ada, representative_name, max_therapists_per_day = 2, max_sessions_per_therapy_per_day = 1, prefer_gap_days = 1 |
| `holiday_overrides` | Center-specific days | date UNIQUE, kind (`closed`/`open`), name |
| `audit_log` | Append-only action log | at, action, entity, entity_id, details (JSON, IDs only — no AMKA or names) |

Distinct concepts map to distinct tables: prescription validity (`prescriptions`), monthly quantity (`prescription_items`), each month's window and authorized quantity (`referral_periods.authorized_quantity`, read through `period_therapies.referral_period_id`), planned dates (`scheduled_sessions`), and what was issued (`generated_documents.snapshot`, which includes the prescription number, decision number, referral numbers, windows and quantities). Nothing in the model represents "session performed" (D-08).

Chain: `prescriptions` 1─* `prescription_items` 1─* `referral_periods` 1─0..1 `period_therapies` *─1 `periods` 1─* `generated_documents`.

## 5. Key mechanisms

### 5.1 Template library and matching (R-TPL-*, implemented in M4)
`backend/app/services/templates.py`: `register_template` (inactive template + configuration pairs; same-combination templates reported), `add_version` (file stored and inspected, map bound to the file's hash, engine validation + configuration checks, immutable version recorded as `valid` or `invalid` with its report; an identical file is refused — as a second version or for another template), `activate_template` (re-verifies the stored file's hash and re-runs every check against today's data; refuses if another template is already active for the same combination), `deactivate_template`, `match_template` / `match_for_period` / `match_for_patient`. **Matching is exact (D-28):** the requested (therapy, therapist) pairs must equal a template's configuration; every section of the matched template is filled. The current center has three therapies and seven templates (one per combination); during development they are synthetic TEST ONLY templates (`backend/tests/fixtures/center_templates.py`), to be replaced by the real ones. This is data, not code: any other set of therapies and templates works the same way. Configuration checks beyond the engine: the map's sections are exactly the configured therapies; every printed therapist detail (licence number, issuing authority, licence date, name if printed) equals the therapist's record (D-27); therapy types and therapists active; sections without images and dynamic center fields without a center setting are warned. `backend/app/documents/mapping.py` suggests a field map for the EOPYY form family from its printed labels; headings are linked to therapies and center-field modes chosen by the administrator; anything unrecognised becomes a note, never a guess.

- `configuration_key` = sorted `therapy_type_id:therapist_id` pairs, e.g. `1:3,4:2`.
- Match = exact equality against **active** templates with a **valid** current version. Zero → error naming the missing combination. Two active templates with the same key cannot exist (partial unique index); activation of a duplicate is refused with the clashing template's name. The matcher still checks defensively.
- Run at registration/assignment change (to warn early) and **authoritatively at document creation, on the period's own snapshot** (`period_therapies`), so a later assignment change cannot put an old period on the wrong template.

### 5.2 Prescriptions and monthly referral periods (R-RX-*, R-PER-1)
- **Referral periods are stored rows, not computed on the fly.** That keeps any future layout (other durations, uneven months, quantities that change per month) representable without schema changes.
- **Pre-fill rule** for layout `eopyy_ekpy_monthly_v1` (the only one known): for each therapy, consecutive windows from the prescription's `valid_from`, each ending the day before the same day of the following month (16/09–15/10, 16/10–15/11, …), until `valid_to`; `authorized_quantity` = `monthly_quantity`. If the windows do not end exactly on `valid_to`, the pre-fill is flagged for review instead of guessed. Pre-fill rules live in `prescriptions/layouts.py`, one function per layout; a new layout adds a rule, not a table.
- The administrator compares the pre-filled windows with page 2 of the prescription, enters referral numbers if wanted, and corrects anything before saving (status `draft` → `confirmed`). Only confirmed prescriptions feed periods.
- **Creating a document period:** the administrator picks a window from the patient's confirmed referral periods; the service collects all referral periods with exactly that window (one per therapy), takes each therapist from the current assignment, and links each to its referral period (the quantity is read from there, never copied). Therapies whose windows differ go into **separate document periods**, which may overlap; they are never merged into one document (P-22, U-04).
- Month arithmetic is done by a small, tested helper (clamping to the last day of short months); edge cases are always shown to the administrator for confirmation.

### 5.3 Assignment and prescription changes
Never edited in place: the old assignment gets `valid_to`, a new row is created; a new prescription is a new record. Draft periods are not changed automatically — the UI shows the difference and offers "re-sync" until the period is issued. Issued documents are unaffected.

### 5.4 Scheduling (R-SCH-*, implemented in M3)
`backend/app/scheduling/` is pure (no database, no API); `backend/app/services/scheduling.py` connects it to the database.

- **Calendar** (`calendar.py`): working days = weekdays − Greek public holidays (`holidays` package: incl. Clean Monday, Good Friday, Easter Monday, Whit Monday, Labour Day moved out of Holy Week) − `closed` overrides + `open` overrides. An `open` override can only cancel a public holiday; Saturdays and Sundays are never working days.
- **Rules** (`rules.py`, used for proposals, manual edits and approval alike): hard — working day, inside the referral window, ≤ authorized quantity, ≤ `max_sessions_per_therapy_per_day` of a therapy per day (no duplicates), ≤ `max_therapists_per_day` distinct therapists per patient per day across **all** the patient's periods (one therapist with two therapies counts once); soft — `SHORT_GAP` (closer than `prefer_gap_days`), `SHARED_DAY` (several therapies on one day). Limits come from `center_settings`.
- **Solver** (`solver.py`): input checks and per-therapy capacity checks (`INSUFFICIENT_DAYS`, listing excluded holidays and full days) → for each therapy an exact dynamic programme picks exactly the authorized quantity of valid slots, keeping fixed (manual) dates, minimising `1000 × missing gap days + 40 × other therapies already on the day + (distance from evenly spaced targets)²` → all placement orders (≤ 4 therapies; rotations beyond) are tried and the cheapest wins → if none works, a max-flow check proves `THERAPIST_LIMIT` (or `NO_SCHEDULE_FOUND`) or yields a valid schedule flagged `SPACING_NOT_OPTIMISED`. The result is re-checked by `rules.py`; an invalid schedule is never returned. No randomness: identical input → identical output, whatever the input order.
- **Services**: `propose_schedule` (draft periods only; replaces earlier *proposed* dates, keeps *manual* ones, treats the patient's other periods as fixed), `replace_dates` (manual edit; hard-rule violations refused; fewer dates accepted and reported incomplete), `schedule_status` (planned vs authorized, violations, warnings), `approve_schedule` (re-checks against today's calendar; zero dates never; incomplete only with `allow_incomplete=True`, audited), `reopen_schedule`. The caller owns the transaction; failures leave the database unchanged.
- **Database guards** (migration `0002`): no weekend dates; per-day limits read from `center_settings`; dates locked while the schedule is approved (as well as when issued). Holidays and closures are checked by the services (the database cannot see the holiday calendar).

### 5.5 Document engine (implemented in M2, ported from the spike)
`backend/app/documents/` — independent of the API and the database; plain files in, plain files out.

| Module | Responsibility |
|---|---|
| `field_map.py` | Versioned, typed field map (schema 1): a **locator** per field (body / header / footer, optional table cell, paragraph, **run span**), the text the span must currently hold (`expect` pattern or `between` anchors), per-field date formats, the therapy blocks with fixed therapist details and the home-visit boxes. Bound to the template's SHA-256. |
| `package.py` | SHA-256 hashing and safe DOCX inspection before python-docx opens a file: ZIP integrity, size/ratio limits, required parts, plain Word content type (no macros/templates), no DOCTYPE/ENTITY, no external references except hyperlinks. |
| `locate.py` | Resolve a locator to runs; a span may cover several runs only if they share formatting (revision ids ignored); each run must hold exactly one text node; writing puts the value in the first run and empties the rest — runs are never added or removed. |
| `render.py` | Fill one template — every section must receive dates (D-28): patient, period, issue date, ΑΔΑ/representative (dynamic fields need a value; static fields keep the template text and refuse one — D-25), session dates per therapy, home-visit tick; remove the sample watermark from the output only. Refuses inconsistent data (a therapy with zero dates — D-24, dates outside the period, therapies the template does not contain, missing ΝΑΙ/ΟΧΙ). Writes atomically; never touches the template. |
| `verify.py` | Accept a generated DOCX only if every part equals the template except the recorded text edits (and the watermark), images and their placement are byte-identical, every field reads back as intended, and no marker or template sample text survives. |
| `validate.py` | Activation gate for a template + map: every location exists and holds what the map expects, every body table is mapped, therapist details present, home-visit boxes empty, static center fields not blank, plus a dry run through `render` + `verify`; warnings for unmapped dates, text boxes, content controls and Word fields. |
| `pdf.py` | LibreOffice headless, one conversion at a time, private profile, neutral file names, process-tree kill on timeout; PDF validation with pypdf (header/EOF, readable, page count, all fonts embedded, expected texts). |
| `pipeline.py` | `generate_document`: render → verify → convert → validate; all or nothing. |

`backend/app/storage.py` — `FileStore` under `THERAPY_DATA_DIR`: `templates/<sha256>.docx` (content-addressed), `documents/`, `previews/<id>/`; atomic writes, read-only files, no overwrite, hash re-check on read, no path escapes.

### 5.6 Preview and issuance (R-DOC-*)
1. Preconditions: period ended; schedule approved; home visits chosen; template matched.
2. Preview: render into `previews/<uuid>/`, verify, convert, apply gates, show PDF with warnings (e.g. soft schedule preferences not met).
3. Issue: re-check that the inputs (hash of the snapshot, incl. issue date) are unchanged since the preview, then **promote the previewed files** into `documents/` (read-only), insert the immutable row, set the period to `issued`.
4. Correction: reopen the period (audited) → edit → new preview → version + 1.

### 5.7 Storage and integrity
Content-addressed, read-only template files (`templates/<sha256>.docx`); issued files read-only with hashes in the DB; files served only through authenticated endpoints. 34 SQLite triggers (migration `0001`; three of them strengthened in `0002`) block UPDATE/DELETE on template versions, issued documents and the audit log, edits to issued periods, overlapping or out-of-validity referral periods, changes to prescription data once used, mismatched period therapies, and inconsistent template activation; `admin_sessions_fixed` (`0003`) keeps a session's identity fixed and its revocation final; four reference-data guards (`0004`, §5.12) protect therapy-type codes and therapists used by active templates; `patients_no_delete` (`0005`, §5.14) makes patient rows undeletable. Every connection runs with `foreign_keys=ON`, WAL, `synchronous=FULL` and `BEGIN IMMEDIATE` (P-25). `python -m app.cli db check` runs `PRAGMA integrity_check`, `foreign_key_check` and a revision check (file re-hashing arrives with storage in M4/M7). `cli verify-integrity` re-hashes files and runs `PRAGMA integrity_check`.

### 5.8 Repository safety (implemented in M0)
- `.gitignore` uses root-anchored patterns (`/templates/`, `/storage/`, `/spike/templates/` …), so a source folder that merely shares a name (e.g. `frontend/src/features/templates/`) is never ignored by accident. `*.docx`, `*.pdf` and database files are ignored everywhere: test documents are built in code (`eopyy_builder.py`), so no document ever needs to be committed.
- `tools/check_repo.py` (standard library only) runs from the `pre-commit` hook and on demand (`--staged`, `--all`, `--history`). It blocks document/database files, private directories, `.env` files, unknown binaries and **AMKA-like numbers** (11 digits; obvious fakes such as `00000000000` and `12345678901` are allowed, and a line can be marked `amka-ok`). An optional, git-ignored `.private-denylist` holds real names or other terms to block; matching ignores case and accents (Greek capitals carry no accents).
- The check is a safety net, not a substitute for care; it fails closed (no working Python → commit blocked).

### 5.9 Security and backups
Bound to loopback only (enforced, §5.10); one password-protected admin; BitLocker on the host recommended; AMKA masked in lists; no personal data in logs. `cli backup` (SQLite online backup + file store, encrypted, retention) scheduled via Windows Task Scheduler; restore is documented and tested. Upload checks: `.docx` only, size limit, zip-bomb guard, no external entities.

### 5.10 Authentication and API foundations (M5.0)
- **Administrator:** exactly one account, created and re-passworded **only** by `python -m app.cli admin create | set-password` (no web setup route, no default credentials). Password policy: 12–128 characters after NFC normalisation, not equal to the username. Argon2id with argon2-cffi's RFC 9106 defaults; weaker stored hashes are upgraded at the next login. `set-password` clears the lock and revokes every session. `admin status` never prints the hash.
- **Login** (`POST /api/auth/login`): one generic `401 invalid_credentials` for an unknown username, a wrong password or no administrator; an unknown username still costs one Argon2 verification. **Backoff** on the single account (whatever username was typed): from the 5th consecutive failure the login is locked for min(30 s × 2^(n−5), 15 min); while locked every attempt, even correct, gets `429 login_locked` + `Retry-After` without a hash check and without counting. Success resets the counter. A session presented with a *successful* login is revoked and replaced (no session fixation) — after the password was verified, in the same transaction, and *before* the session cap is enforced, so a re-login with 10 live sessions never evicts an unrelated one; a failed, locked or invalid login leaves it, and the browser's cookie, untouched.
- **Sessions:** 256-bit random token in the `therapy_session` cookie (HttpOnly, SameSite=Strict, Path=/, no Domain, Max-Age 12 h, `Secure` automatically over HTTPS). Only SHA-256 hashes of the token and of the CSRF token are stored. A session is valid only if not revoked, **and** before `expires_at` (12 h after login), **and** less than 30 min after `last_seen_at` — the two limits are checked independently. Validation happens in a **read-only** transaction (it never waits for a writer); `last_seen_at` is then refreshed at most once a minute in a separate short write that waits at most 200 ms and is *skipped* — never failed — if the database is busy (all checks still apply). At most 10 live sessions (least recently used revoked). Revoked, expired and idle rows are deleted at login, logout and password change.
- **CSRF and request filter** (`app/security.py`, every request): the TCP peer must be loopback (403); the Host header must be `127.0.0.1`, `localhost` or `[::1]` (400; DNS rebinding); unsafe methods need `Origin` equal to the request's own origin and a `Sec-Fetch-Site`, if sent, of `same-origin` (403); unsafe `/api` bodies must be JSON (415). Protected unsafe requests also need `X-CSRF-Token` = HMAC-SHA256(session token), compared by hash in constant time (403 `csrf_failed`). `GET /api/auth/session` returns it again after a page reload. No CORS anywhere; the Vite dev proxy keeps the browser's Host (`changeOrigin: false`), so development is same-origin too.
- **Protection by default:** public routes are an exact allow-list in `app/main.py` (`GET /api/health`, `POST /api/auth/login`, `GET /api/auth/session`). Every other router is built with `protected_router()`, whose router-level dependency requires the session and CSRF. `tests/auth/test_route_protection.py` walks the effective dependency tree (router and include levels, recursively) of **every** route of the application, under `/api` or not, and fails for any gap; the only exemptions are the public allow-list, the development docs and the static mount named `frontend`. It is itself tested against deliberately unprotected routes (with and without the `/api` prefix), a second static mount and WebSocket routes: the application is HTTP-only, so any route without HTTP methods is reported (the allow-list for them is empty).
- **Fail closed:** the web app never creates the database. A missing or not fully migrated database answers 503 on everything that needs it; no administrator means every protected route answers 401. Settings refuse any non-loopback `THERAPY_HOST` (no LAN access until a validated TLS setup exists); `python -m app.cli serve` binds to that host only.
- **Transactions:** one session per request (`DbSession`). GET/HEAD use a deferred `BEGIN` with `PRAGMA query_only=ON`; other methods use `BEGIN IMMEDIATE`. The dependency is `scope="function"`, so the commit happens before the response is sent and a failed commit becomes a 500; any exception, including an `HTTPException`, rolls the whole request back. Session validation runs before the route, in its own read-only transaction plus the optional best-effort `last_seen_at` write (see Sessions). A failed login is *returned* (not raised) so its counter is committed.
- **Privacy:** error bodies are `{"code": …}`; validation errors list only location and message (never the submitted value); unexpected errors return `internal_error` + a request id and are logged by exception type only — an outermost `CrashGuard` middleware (`app/errors.py`) answers them itself so that nothing propagates to uvicorn, which would otherwise log the traceback *with the exception message*; if the response had already started the connection is aborted with a message-free `ResponseAborted`; a bare process-control exception (KeyboardInterrupt, SystemExit, cancellation) propagates untouched, and a `BaseExceptionGroup` mixing ordinary and process-control exceptions is reduced to one process-control leaf (ordinary members logged by type only and dropped, no context chain) so that no message reaches uvicorn; SQL errors never contain bound values (`hide_parameters=True`). Audit entries carry ids and counts only (`admin.created`, `admin.password_changed`, `auth.login`, `auth.logout`, `auth.lockout`, `auth.sessions_revoked`). Headers: `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, COOP/CORP `same-origin`, `Permissions-Policy`; `/api` adds `Cache-Control: no-store` and `CSP default-src 'none'`; the frontend gets a `'self'`-only CSP.

### 5.12 Reference data: therapy types and therapists (M5.1)
- **Tables unchanged:** `therapy_types` (`code` unique and immutable once used, `name_el`, `template_label`, optional unique `eopyy_code`, `eopyy_description`, `supports_home_visit`, `active`) and `therapists` (`full_name`, optional `specialty`, `license_no`, `license_authority`, `license_date`, `active`). No global therapist–therapy eligibility table (Q3): eligibility is derived from active templates; patient-level history stays in `therapist_assignments` (M5.3), the per-document therapist in `period_therapies`, the printed one in the issued snapshot.
- **API** (all on `protected_router()`, JSON bodies reject unknown fields, **no DELETE**): `GET|POST /api/therapy-types`, `GET|PATCH /api/therapy-types/{id}`, `POST …/{id}/activate|deactivate`; the same five for `/api/therapists`. Lists take `?active=true|false`. Create/update return `{item, warnings}`; errors are `404 not_found`, `409` with a stable `code` and an actionable `message`, `422` for invalid input (values never echoed). Path ids are bounded to SQLite's integer range.
- **Rules (service `services/reference.py`, backed by migration `0004_reference_data_guards`, four triggers):** a therapy type's `code` cannot change once a prescription item, period therapy, template configuration or assignment uses it; a therapy type or therapist used by an **active** template cannot be deactivated; a therapist's identity details (name, licence number, authority, date — what templates print, D-27) cannot change while an active template uses them. The error lists the templates (id and name): deactivate the template, make the change, then upload and activate a corrected version through the template library. Duplicate names and licence numbers are **warnings** (`duplicate_name`, `duplicate_full_name`, `duplicate_license_no`, with the ids of the other records), never errors; `eopyy_code` stays unique where present.
- **Matching:** `match_template` additionally refuses a template whose therapy types or therapists are inactive, even if the template is active.
- **Audit:** `therapy_type.created|updated|activated|deactivated`, `therapist.…` — entity and id, and for updates the changed *field names* only.
- **Future extraction (§5.13):** the printed EOPYY code stays on `prescription_items.eopyy_code`; `therapy_types.eopyy_code` is a mapping aid, and several codes per therapy type could later be an additive mapping table.

### 5.14 Patients (M5.2)
- **Schema:** table unchanged. Migration `0005_patient_guards` adds one trigger, `patients_no_delete` (no DELETE, referenced or not). No database length limits (SQLite ignores `VARCHAR(n)`; the rules live in `app/text.py`, used by the schemas *and* the service).
- **Text rules (`app/text.py`):** names — NFC; refuse Cc, Cf, U+2028/U+2029 and every whitespace other than U+0020 (tabs/newlines/NBSP are refused, never converted); trim and collapse ordinary spaces; 1–100 characters counted after normalisation; at least one letter (Unicode category L*, any script — a name of only punctuation, digits, symbols, combining marks or private-use characters is refused). Lone surrogates (Cs) are refused in names and notes (they cannot be stored as UTF-8; without this they caused a 500). Known limitation (Decision W): names containing U+200C ZERO WIDTH NON-JOINER or U+200D ZERO WIDTH JOINER — used in some Persian and Indic spellings — are currently rejected with other Cf format characters; supporting them between letters would require a separate explicit design decision. Notes — NFC; LF and TAB allowed, other Cc/Cf and U+2028/U+2029 refused; trimmed; blank → NULL; ≤ 2,000 characters. AMKA — exactly 11 ASCII digits after trimming ordinary spaces; no checksum, nothing repaired. The patient schemas do not use `str_strip_whitespace` (it would strip tabs/newlines silently) and refuse unknown body fields without naming them (a key could be an AMKA).
- **Search fold:** `therapy_fold` = NFD → casefold → NFD → drop marks → non-letters/digits to spaces → collapse. Registered on every connection as the SQLite function `therapy_fold` (deterministic); never used in a trigger, CHECK or index. Latin and Greek letters stay distinct (no transliteration). M5.1 keeps its own fold unchanged.
- **API** (`protected_router`, no DELETE): `GET /api/patients` (only `active`, `offset`, `limit`) and `POST /api/patients/search` (`{query | amka, active, offset, limit}`) return `{items, total, offset, limit}` of `PatientSummary` — **active patients only when `active` is omitted**; `active=true` active only, `active=false` inactive only (inactive patients stay findable; "all patients" is not expressible through the API, only for internal service callers with `active=None`) — — **no `amka` field, only `amka_masked`** (`•••••••` + last 4 digits). `POST /api/patients`, `GET|PATCH /api/patients/{id}` (PATCH: names and notes; the AMKA is refused), `POST …/{id}/amka` (`{amka, acknowledge_issued_documents}`), `POST …/{id}/activate|deactivate`. The detail (`{item, usage}`) is the only place showing the full AMKA. Unknown or repeated query parameters → `422 unknown_query_parameter`, names never echoed (`allow_query`).
- **Search:** each query token (≤ 5, query ≤ 100 characters) must match the start of a word in first, last or father's name: `instr(' ' || fold(first) || ' ' || fold(last) || ' ' || fold(father), ' ' || token) > 0`, all ANDed (`instr` is literal: `% _ \` need no escaping). A query without letters or digits is `422 invalid_query`. `amka` is an exact match. Order: folded last, first, father name, id; `offset`/`limit` (≤ 100) + `total`. Notes are never searched.
- **Read-only POST:** the search uses `ReadDbSession` (`get_read_db`: the read factory, deferred `BEGIN` + `query_only`, always rolled back) and never takes the write lock; CSRF, Origin, session checks are unchanged. A structural test forbids any route from using `get_read_db` and `get_db` together. Authentication's optional `last_seen_at` refresh is already best-effort (200 ms, then skipped); a lock-contention test with a stale `last_seen_at` proves the search answers promptly while another connection holds `BEGIN IMMEDIATE`.
- **Rules:** AMKA unique across active and inactive patients (`409 duplicate_amka` with the other id and active flag, never the value). Same folded first+last+father name → warning `possible_duplicate`. Deactivation always allowed; warns about open assignments, draft prescriptions and unissued periods; changes nothing else; existing periods can still be issued. Repeating activate/deactivate or a no-op PATCH writes nothing. Inactive patients can be edited and corrected; M5.3, M5.4 and M6 must refuse *new* assignments, prescriptions and periods for them. AMKA correction with issued documents requires `acknowledge_issued_documents` (`409 amka_change_needs_confirmation` otherwise) and warns `issued_documents_show_previous_details`; issued documents keep the old details in their snapshot; no AMKA history is kept. `updated_at` (injected clock, whole seconds) moves only on a real change.
- **Audit:** `patient.created`, `patient.updated {fields}`, `patient.amka_corrected {issued_documents}`, `patient.activated`, `patient.deactivated` — ids, field names and counts only. Reads and searches are not audited. `services/audit.py` (`record_audit`) is the shared writer (extracted unchanged from M5.1).
- **Logs:** `app.cli serve` runs uvicorn with `access_log=False` (request lines would include query strings). A hand-started uvicorn must use `--no-access-log` and never `--log-level trace` (uvicorn's trace logger prints whole request scopes, cookies included). The frontend must never put personal data in any URL (API or its own routes); search goes through the POST body.
- **For M7:** the preview's input hash must include the patient fields, so an edit between preview and issue forces a new preview.

### 5.13 Future: prescription-text extraction (R-RX-6, not built)
`prescriptions/extraction.py` defines one interface: `extract(text) -> ProposedPrescription`, returning exactly the fields of manual entry — prescription number, decision number/date, validity, per therapy the EOPYY code, monthly quantity, printed total/duration, and the monthly referral windows and numbers — each with a "needs review" flag, plus the detected `layout`. Unknown layouts return nothing rather than a guess. The MVP ships no implementation. The flow is designed so extraction only ever produces a **draft**: proposed values go into `prescription_extractions`, the administrator reviews and corrects them in the same form used for manual entry, and saving creates an ordinary `prescriptions` row with `source = extracted` and a link to the extraction. No other part of the system changes. Whether an extractor may run locally only or call an external service is an open privacy decision (Q-07).

## 6. Reuse of the spike

The spike stays untouched and keeps its own 27 tests. Its modules are **ported** (not imported) into `backend/app/documents/`; parity tests (`tests/documents/test_spike_parity.py`) run the spike's own modules, unmodified, on the same synthetic fixtures: outputs are identical part by part, the change logs agree, each engine's verification accepts the other's output, and template validation agrees. Intentional differences are tested explicitly (P-33). `store.py` informs the schema and triggers only. `make_templates.py` is not reused because it depends on the private template; production tests use a fixture built entirely in code.

## 7. Testing

Unit tests for pure modules (scheduler incl. property tests and golden holiday lists 2025–2027; document engine incl. tamper detection); integration tests for services against a temporary SQLite + file store; one LibreOffice end-to-end test; parity tests against the spike. A fresh clone must run all tests without any private file. Tests that need LibreOffice are marked `libreoffice` and skipped when it is not installed; PDF failure modes are covered with a fake `soffice` so they run everywhere. After M0 this holds for the backend (`pytest`: configuration, health API, synthetic fixture builder, repository guard), the frontend (`vitest`) and the spike's own tests only if its private templates are regenerated locally (they are not part of a fresh clone — see section 6).

## 8. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | **Done (committed).** Repo hygiene + scaffolding: anchored `.gitignore`, pre-commit data guard, backend/frontend skeletons, config, synthetic fixture builder | Fresh clone runs `pytest`, `ruff`, `npm test` and `npm run build` with no private files (see `README.md`) |
| M1 | **Done (committed).** DB foundation: models, migrations, triggers, `db init/upgrade/status/check` | Constraint/trigger tests pass; migration round-trip; failed migration rolls back; fresh database initialises |
| M2 | **Done (committed).** Document engine port + gates + file store | Parity tests and LibreOffice end-to-end pass |
| M3 | **Done (committed).** Scheduling engine + services + database guards | Rule, randomised and holiday tests pass; deterministic |
| M4 | **Done (committed).** Template library backend + mapping assistant | Upload → map → validate → activate; duplicates refused; versions kept; exact matching |
| M5 | Administration and prescriptions, in steps (no step changes an earlier one's guarantees): | |
| M5.0 | **Done, awaiting review.** Authentication and API foundations: CLI-only administrator, Argon2id, server-side sessions (`0003_admin_sessions`), login backoff, CSRF + Origin/Host checks, request-scoped transactions, fail-closed guards, security headers, privacy-safe errors (§5.10) | Every API route is protected or on an exact public allow-list (structural test); nothing secret or personal in logs, audit entries or error bodies |
| M5.1 | **Done, awaiting review.** Reference data (§5.12): therapy types (code, name, template label, optional editable EOPYY code — nothing seeded) and therapists; deactivate, never delete | Duplicates answer 409; deactivated records kept |
| M5.2 | **Implemented, awaiting review.** Patients (§5.14): create, update, list, search (POST body only, read-only transaction), retrieve, AMKA correction; AMKA exactly 11 ASCII digits; masked in lists; deactivate only (`0005_patient_guards`) | A list never contains a full AMKA |
| M5.3 | Therapist assignments with history; advisory exact-template warning | Reassignment keeps history |
| M5.4 | Prescriptions and referral periods (draft): explicit quantities and windows; read-only window suggestions | The server never fills in a quantity |
| M5.5 | Confirmation and lifecycle: confirm (warnings for missing numbers), unconfirm if unused, delete drafts only | "Confirmed" never implies EOPYY approval |
| M5.6 | Patient history and referral-period queries for M6 | — |
| M5.7 | Hardening: audit completeness, privacy tests, docs | — |
| M6 | Document periods from referral windows + schedule proposal/edit/approve + home visits | Workflow and lock tests; misaligned windows reported; a referral period cannot be documented twice |
| M7 | Preview, issue, correction, history, integrity check | End-to-end: register → prescribe → period → schedule → preview → issue → correct |
| F1–F3 | Frontend: management screens; template library (**F2 needs a protected Template Management API first**: list, register, upload version, activate, deactivate — on `services/templates.py`; see `current-status.md`); monthly flow + history | Administrator completes the monthly flow in a few steps |
| M8 | Backup/restore, deployment notes, acceptance with a real center template | Restore test passes; real template preserved |
