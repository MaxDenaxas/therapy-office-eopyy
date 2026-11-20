# Therapy Office

Local application for a single therapy center that prepares the monthly EOPYY attestation documents from predefined DOCX templates. Status: **milestone M4** (database, document engine, scheduling, template library; no API workflows or screens yet).

Read first: [`docs/PRODUCT_REQUIREMENTS.md`](docs/PRODUCT_REQUIREMENTS.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Prerequisites

- Python **3.12**
- Node.js **22.22+ / 24.15+** (Node 24.12 also works but npm prints engine warnings for `jsdom`)
- Git
- LibreOffice for PDF generation (tests marked `libreoffice` are skipped when it is missing). Found in PATH or the standard install folder; otherwise set `THERAPY_SOFFICE_PATH`.

## One-time setup

```bash
git config core.hooksPath .githooks     # enables the pre-commit safety check
```

Optionally create `.private-denylist` (git-ignored) in the repository root with one term per line (real patient or therapist names). The safety check then refuses any commit containing them, ignoring case and accents.

## Backend

```bash
cd backend
py -3.12 -m venv .venv                      # Windows; elsewhere: python3.12 -m venv .venv
.venv\Scripts\activate                      # Windows; elsewhere: source .venv/bin/activate
python -m pip install -e ".[dev]"

pytest                                      # tests
ruff check . && ruff format --check .       # lint

# run (development, auto-reload); health check at http://127.0.0.1:8000/api/health
uvicorn app.main:create_app --factory --reload
```

Configuration is read from `THERAPY_*` environment variables or a git-ignored `backend/.env` (see `backend/.env.example`). `THERAPY_DATA_DIR` must be **outside** this repository; the application refuses to start otherwise.

### Database (SQLite at `THERAPY_DATA_DIR/therapy.db`)

```bash
python -m app.cli db init       # create a fresh database (refuses if one already exists)
python -m app.cli db upgrade    # apply pending migrations
python -m app.cli db status     # path, current and latest schema revision
python -m app.cli db check      # integrity, foreign-key and revision check
```

Migrations live in `backend/migrations/` (Alembic). Plain `alembic current` / `alembic history` also work from `backend/`.

## Frontend

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173 (proxies /api to the backend on :8000)
npm test             # Vitest
npm run typecheck    # tsc --noEmit
npm run build        # type-check + production build into frontend/dist
```

To serve the built frontend from the backend (one process, one port):

```bash
# PowerShell:  $env:THERAPY_FRONTEND_DIST = "..\frontend\dist"
uvicorn app.main:create_app --factory       # http://127.0.0.1:8000
```

> If `npm test` fails with `Cannot read properties of undefined (reading 'config')` inside a VS Code extension or agent terminal, the shell has `ELECTRON_RUN_AS_NODE=1` set. Run the tests from a normal terminal (or unset that variable).

## Repository safety

`python tools/check_repo.py --staged | --all | --history` blocks DOCX/PDF/database files, private directories, `.env` files, unknown binaries, AMKA-like numbers (11 digits) and denylisted terms. It runs automatically on commit once the hook is enabled. Real patient data, real templates, signatures, stamps and therapist license details must never be committed.

## Center configuration and templates

The center provides three therapies (speech therapy, psychotherapy, occupational therapy), one therapist each, and uses seven templates — one per non-empty combination. A document's template is chosen by **exact** match of its therapy/therapist combination. Therapies, therapists and templates are entered by the administrator; nothing about this center is hard-coded.

During development the seven templates are **synthetic TEST ONLY** documents (`backend/tests/fixtures/center_templates.py`: fictional therapists, crossed-out stamps, a visible TEST ONLY line in every generated page). They will be replaced by the center's real templates, which never enter Git.

## Test fixtures

`backend/tests/fixtures/eopyy_builder.py` builds a synthetic EOPYY-style DOCX entirely from code (fake names, generated placeholder images). Tests never read private files.

## The spike

`spike/` is a frozen, independently testable prototype of the DOCX-to-PDF pipeline. It needs private templates that are not in Git, so its tests cannot run on a fresh clone. It is left unchanged.
