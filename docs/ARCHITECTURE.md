# Architecture

Status: draft for review (2026-11-20). Requirements are referenced by ID from `PRODUCT_REQUIREMENTS.md`; decisions by ID from `DECISIONS.md`.

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
| Auth | One admin account, argon2id password hash, server-side session cookie |
| Frontend | React + TypeScript + Vite + Tailwind CSS (M0: React 19, Vite 8, Tailwind 4, TypeScript 7). TanStack Query and React Router are added in F1, when the first real screens need them. |
| Tests | Backend: pytest (+ `httpx2` for the FastAPI test client), hypothesis for scheduler properties (M3). Frontend: Vitest + Testing Library. Lint: ruff (backend), `tsc --noEmit` (frontend). |

Not used: Redis, Docker, task queues, a separate PDF service, multi-tenant features (D-03, D-04).

## 3. Repository layout

Status after M0: items marked *(M0)* exist; the rest arrive with their milestone.

```
therapy-office/
├── README.md                # setup and run commands (M0)
├── docs/                    # this documentation
├── spike/                   # FROZEN prototype + acceptance harness (unchanged)
├── tools/check_repo.py      # repository safety check: DOCX/PDF/DB/AMKA/denylist (M0)
├── .githooks/pre-commit     # runs the safety check; enable with `git config core.hooksPath .githooks` (M0)
├── backend/
│   ├── pyproject.toml  .env.example                                   (M0)
│   ├── alembic.ini  migrations/   # Alembic: env.py + versions/0001_initial_schema.py (M1)
│   ├── app/
│   │   ├── main.py  config.py                                         (M0: app factory, THERAPY_* settings)
│   │   ├── api/health.py                                              (M0)
│   │   ├── db.py  migrate.py  cli.py                                  (M1: engine, migrations, `db` commands)
│   │   ├── models/                                                    (M1: SQLAlchemy models)
│   │   ├── storage.py                                                 (M2: content-addressed, read-only file store)
│   │   ├── security.py                                                # later
│   │   ├── services/scheduling.py                                     (M3: propose / edit / status / approve / reopen)
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
| `admin_account` | The single operator | `id = 1` (CHECK), username, password_hash, failed_login_count, locked_until, last_login_at |
| `patients` | Personal data entered once | first_name, last_name, father_name, `amka` UNIQUE (11 digits), notes, active |
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
Content-addressed, read-only template files (`templates/<sha256>.docx`); issued files read-only with hashes in the DB; files served only through authenticated endpoints. 34 SQLite triggers (migration `0001`) block UPDATE/DELETE on template versions, issued documents and the audit log, edits to issued periods, overlapping or out-of-validity referral periods, changes to prescription data once used, mismatched period therapies, and inconsistent template activation. Every connection runs with `foreign_keys=ON`, WAL, `synchronous=FULL` and `BEGIN IMMEDIATE` (P-25). `python -m app.cli db check` runs `PRAGMA integrity_check`, `foreign_key_check` and a revision check (file re-hashing arrives with storage in M4/M8). `cli verify-integrity` re-hashes files and runs `PRAGMA integrity_check`.

### 5.8 Repository safety (implemented in M0)
- `.gitignore` uses root-anchored patterns (`/templates/`, `/storage/`, `/spike/templates/` …), so a source folder that merely shares a name (e.g. `frontend/src/features/templates/`) is never ignored by accident. `*.docx`, `*.pdf` and database files are ignored everywhere: test documents are built in code (`eopyy_builder.py`), so no document ever needs to be committed.
- `tools/check_repo.py` (standard library only) runs from the `pre-commit` hook and on demand (`--staged`, `--all`, `--history`). It blocks document/database files, private directories, `.env` files, unknown binaries and **AMKA-like numbers** (11 digits; obvious fakes such as `00000000000` and `12345678901` are allowed, and a line can be marked `amka-ok`). An optional, git-ignored `.private-denylist` holds real names or other terms to block; matching ignores case and accents (Greek capitals carry no accents).
- The check is a safety net, not a substitute for care; it fails closed (no working Python → commit blocked).

### 5.9 Security and backups
Bound to `127.0.0.1`; one password-protected admin; BitLocker on the host recommended; AMKA masked in lists; no personal data in logs. `cli backup` (SQLite online backup + file store, encrypted, retention) scheduled via Windows Task Scheduler; restore is documented and tested. Upload checks: `.docx` only, size limit, zip-bomb guard, no external entities.

### 5.10 Future: prescription-text extraction (R-RX-6, not built)
`prescriptions/extraction.py` defines one interface: `extract(text) -> ProposedPrescription`, returning exactly the fields of manual entry — prescription number, decision number/date, validity, per therapy the EOPYY code, monthly quantity, printed total/duration, and the monthly referral windows and numbers — each with a "needs review" flag, plus the detected `layout`. Unknown layouts return nothing rather than a guess. The MVP ships no implementation. The flow is designed so extraction only ever produces a **draft**: proposed values go into `prescription_extractions`, the administrator reviews and corrects them in the same form used for manual entry, and saving creates an ordinary `prescriptions` row with `source = extracted` and a link to the extraction. No other part of the system changes. Whether an extractor may run locally only or call an external service is an open privacy decision (Q-07).

## 6. Reuse of the spike

The spike stays untouched and keeps its own 27 tests. Its modules are **ported** (not imported) into `backend/app/documents/`; parity tests (`tests/documents/test_spike_parity.py`) run the spike's own modules, unmodified, on the same synthetic fixtures: outputs are identical part by part, the change logs agree, each engine's verification accepts the other's output, and template validation agrees. Intentional differences are tested explicitly (P-33). `store.py` informs the schema and triggers only. `make_templates.py` is not reused because it depends on the private template; production tests use a fixture built entirely in code.

## 7. Testing

Unit tests for pure modules (scheduler incl. property tests and golden holiday lists 2025–2027; document engine incl. tamper detection); integration tests for services against a temporary SQLite + file store; one LibreOffice end-to-end test; parity tests against the spike. A fresh clone must run all tests without any private file. Tests that need LibreOffice are marked `libreoffice` and skipped when it is not installed; PDF failure modes are covered with a fake `soffice` so they run everywhere. After M0 this holds for the backend (`pytest`: configuration, health API, synthetic fixture builder, repository guard), the frontend (`vitest`) and the spike's own tests only if its private templates are regenerated locally (they are not part of a fresh clone — see section 6).

## 8. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | **Done, awaiting review.** Repo hygiene + scaffolding: anchored `.gitignore`, pre-commit data guard, backend/frontend skeletons, config, synthetic fixture builder | Fresh clone runs `pytest`, `ruff`, `npm test` and `npm run build` with no private files (see `README.md`) |
| M1 | **Done, awaiting review.** DB foundation: models, migrations, triggers, `db init/upgrade/status/check` | Constraint/trigger tests pass; migration round-trip; failed migration rolls back; fresh database initialises |
| M2 | **Done, awaiting review.** Document engine port + gates + file store | Parity tests and LibreOffice end-to-end pass |
| M3 | **Done, awaiting review.** Scheduling engine + services + database guards | Rule, randomised and holiday tests pass; deterministic |
| M4 | **Done, awaiting review.** Template library backend + mapping assistant | Upload → map → validate → activate; duplicates refused; versions kept; exact matching |
| M5 | Admin login + audit log | Login, lockout, audit entries, no personal data in logs |
| M6 | Patients, prescriptions (manual) with monthly referral periods + pre-fill rule, therapists, assignments, matching | Registration flow incl. no-match handling; pre-fill reproduces the example prescription's six windows (16/09–15/10 … 16/02–15/03); month-arithmetic edge cases tested |
| M7 | Document periods from referral windows + schedule proposal/edit/approve + home visits | Workflow and lock tests; misaligned windows reported; a referral period cannot be documented twice |
| M8 | Preview, issue, correction, history, integrity check | End-to-end: register → prescribe → period → schedule → preview → issue → correct |
| F1–F3 | Frontend: management screens; template library; monthly flow + history | Administrator completes the monthly flow in a few steps |
| M9 | Backup/restore, deployment notes, acceptance with a real center template | Restore test passes; real template preserved |
