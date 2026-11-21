# CLAUDE.md

Guide for Claude Code sessions. Keep it short; details live in `docs/`.

## Project

Therapy Office: a local-first application for **one** Greek special-therapy center. The administrator (the only user) registers patients and prescriptions, plans therapy dates, and generates the monthly **EOPYY attestation documents** (DOCX + PDF) from predefined DOCX templates, preserving layout, signatures and stamps. Therapists have **no accounts**. Current center: three therapies, one therapist each, seven templates (one per combination, matched **exactly**) — all of this is data, never hard-coded.

## Stack (verified in `backend/pyproject.toml`, `frontend/package.json`)

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLite + SQLAlchemy 2.x + Alembic, argon2-cffi, python-docx + lxml, pypdf, `holidays`, LibreOffice headless (PDF). Tests: pytest (`httpx2` test client), ruff.
- Frontend: React 19, Vite, Tailwind 4, TypeScript, Vitest. Only a shell exists so far.

## Architecture (full detail: `docs/ARCHITECTURE.md`)

- `backend/app/models`, `migrations/`: schema, constraints and immutability triggers (the database enforces the rules, not only services).
- `app/documents/`: field-map driven DOCX rendering, byte-level integrity checks, PDF conversion. `app/storage.py`: content-addressed read-only file store.
- `app/scheduling/` (pure rules + solver) and `app/services/` (scheduling, templates): the caller owns the transaction.
- `app/auth/`, `app/api/`, `app/security.py`, `app/errors.py`: authentication, routes, request filter, privacy-safe errors (M5.0, `ARCHITECTURE.md` §5.10). `app/services/reference.py`, `app/api/reference.py`, `app/schemas/`: therapy types and therapists (M5.1, §5.12).
- Every API router except the public allow-list in `app/main.py` must be built with `protected_router()`; `tests/auth/test_route_protection.py` fails otherwise. Routes get a DB session through `DbSession`.

## Rules

- **Never modify `spike/`** (frozen) unless explicitly told to.
- **No real data in the repo**: no real patient data, AMKA, templates, signatures, stamps, therapist licence details. Tests use synthetic fixtures built in code. Real files stay outside Git (`THERAPY_DATA_DIR` is refused inside a repo).
- Do not weaken database constraints, security checks, privacy protections or the repository guard without explicit approval. No passwords, tokens, AMKAs or names in logs, audit entries or error bodies; validation errors never echo values.
- No cloud infrastructure, no multi-user/therapist authentication, no LAN access, no unnecessary dependencies or infrastructure.
- Planned session dates are **not** proof that sessions occurred. Never claim EOPYY compliance. Quantities come only from the prescription, entered by the administrator.
- Issued documents are immutable (snapshot + template version kept; corrections are new versions).

## Workflow

Inspect before changing; follow existing conventions; make focused changes; add/update tests; run the checks below and report exact counts; do not fix unrelated issues. **Never commit or push without explicit approval after review.** Never discard or overwrite uncommitted user changes.

Checks (from `README.md` and the repo):

```bash
cd backend && .venv/Scripts/python.exe -m pytest        # full suite takes ~6 min
cd backend && ruff check . && ruff format --check .
cd frontend && npm run typecheck && npm test && npm run build
python tools/check_repo.py --staged     # repository safety guard (also run by the pre-commit hook)
python tools/check_repo.py --all
python tools/check_repo.py --history
cd spike && ../backend/.venv/Scripts/python.exe -m pytest test_spike.py
```

Environment notes: system `python` lacks the dependencies, use `backend/.venv`. If Vitest fails with "reading 'config'", `ELECTRON_RUN_AS_NODE` is set: run frontend commands from a normal shell. Long Bash heredocs may fail; write a script file instead. The hook is enabled with `git config core.hooksPath .githooks`.

## Git

Verify the branch and `git status` at session start; do not assume. At the time of writing: branch `main`, last commit `33e4958` (M5.0); M5.1 is uncommitted in the working tree.

## Status

M0–M5.0 done and committed. M5.1 (therapy types and therapists) implemented, **awaiting review and commit**. M5.2–M5.7 remain; **do not start M5.2 until told to.** See `docs/current-status.md`.

## What to read

- Any milestone work: `docs/current-status.md`, then the milestone table in `docs/ARCHITECTURE.md` §8.
- Product or architecture change: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (decisions D-, proposals P-, open questions Q-/U-), `docs/PRODUCT_REQUIREMENTS.md`.
- Security or auth: `ARCHITECTURE.md` §5.10, D-29–D-33 / P-46–P-52, then `app/auth/`, `app/security.py`, `app/api/deps.py`.
- Documents/templates/scheduling: the matching `ARCHITECTURE.md` section (§5) and the module's tests.
- Setup and commands: `README.md`. Do not load everything by default.
