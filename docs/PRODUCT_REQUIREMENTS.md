# Product Requirements

Status: draft for review (2026-11-20).
This file says *what* the application must do. `ARCHITECTURE.md` says *how*; `DECISIONS.md` records *why* and what is still open.

## 1. Purpose

Generate the monthly EOPYY therapy attestation document ("Έντυπο Βεβαίωσης Θεραπειών Ειδικής Αγωγής") for patients of a single special-therapy center, from predefined DOCX templates, without altering the templates' layout, signatures, stamps or fixed text.

## 2. Users

| User | Access |
|---|---|
| Administrator (the center's operator) | Everything: patients, prescriptions, therapists, templates, schedules, documents, settings. One account. |
| Therapists | **No account, no access.** They exist only as data (name, license details) and inside the templates. |

## 3. Core concepts — kept strictly separate

| Concept | Meaning | Comes from |
|---|---|---|
| **Prescription** ("Γνωμάτευση Παροχών ΕΚΠΥ") | The doctor's prescription as approved: overall validity (e.g. 16/09/2025–15/03/2026) and, per therapy, the EOPYY service code and the **monthly quantity** (e.g. speech 8/month, child psychotherapy 6/month). | Entered by the administrator (MVP); later also extracted from prescription text and reviewed by the administrator. |
| **Monthly referral period** ("Μηνιαίο Παραπεμπτικό") | One month of a prescribed therapy: its own validity window (e.g. 16/10–15/11), referral number and authorized quantity. A prescription lists several per therapy (page 2 of the prescription). | Pre-filled from the prescription, checked/corrected by the administrator. |
| **Therapist assignment** | Which therapist delivers each prescribed therapy. | Administrator. |
| **Document period** | The date range one document covers: one monthly referral window shared by the patient's therapies (e.g. 16/10–15/11). Not tied to calendar months. | Derived from the referral periods. |
| **Schedule** | Planned session dates: proposed by the application, adjusted and approved by the administrator. | Scheduler + administrator. |
| **Issued document** | The attestation exactly as issued: dates, template version, snapshot of all data used. | Generated; immutable. |

**A schedule is a plan.** The application does not record, verify or claim that sessions were performed. Any statement in the document that sessions took place is made by its signatories, not by the application. What the EOPYY document must legally evidence has to be verified before production use (section 7).

## 4. Functional requirements

### Patients
- **R-PAT-1** Register a patient: first name, last name, father's name, AMKA (11 digits, unique), optional notes.
- **R-PAT-2** Patient details are entered once and reused for every document. Edits never alter documents already issued.

### Prescriptions
- **R-RX-1** Record a prescription: prescription number, issue date, committee decision number and date, overall validity (from–to), optional prescribing doctor's name and notes. For each prescribed therapy: the EOPYY service code (e.g. 530000010), the therapy type, and the **monthly quantity**. The values printed as "total quantity" and "treatment duration (months)" are recorded as printed, for reference.
- **R-RX-2** Record each therapy's **monthly referral periods**: validity window, referral number (if known) and authorized quantity. The application pre-fills them from the prescription (consecutive one-month windows from the validity start, quantity = monthly quantity); the administrator checks them against the prescription and corrects them before saving. Windows must lie within the prescription's validity and must not overlap.
- **R-RX-3** Session quantities come **only** from a prescription's referral periods. The application has no default quantities and never invents them; the administrator enters what the approved prescription states.
- **R-RX-4** A renewed or changed prescription is recorded as a new prescription; earlier prescriptions and documents based on them are kept unchanged.
- **R-RX-5** Store only the prescription data the application needs. Not stored: address, phone, e-mail, date of birth, diagnosis, doctor's AMKA/AFM.
- **R-RX-6 (future, not MVP)** The administrator pastes the prescription text; the application proposes the fields of R-RX-1 and R-RX-2; the administrator reviews and corrects every field before anything is saved. Nothing extracted is used without that review. Other prescription layouts must be supportable without changing the data model.

### Therapists and assignments
- **R-THR-1** Maintain therapists: name, specialty, license number, issuing authority, license date. Deactivate instead of delete.
- **R-ASG-1** Assign exactly one therapist to each prescribed therapy of a patient. Changes apply to future periods only.

### Templates
- **R-TPL-1** Upload DOCX templates (about 6–7, possibly more). Each already contains its fixed text, therapist details, signatures, stamps, images and layout.
- **R-TPL-2** Give each template a name and define the exact therapy/therapist combination it represents (one pair per section). The center provides speech therapy, psychotherapy and occupational therapy (one therapist each) and keeps seven templates, one per non-empty combination (D-28).
- **R-TPL-3** Validate a template before it can be activated (all fields locatable, therapist details present and consistent, no unmapped dynamic content, dry-run render preserves everything).
- **R-TPL-4** Preview, activate and deactivate templates.
- **R-TPL-5** Replace a template with a new version; earlier versions and the documents issued with them stay unchanged. Files can never be overwritten.
- **R-TPL-6** The template is selected automatically by **exact** match of the document's therapy/therapist combination; all its sections are filled. A larger or smaller template, another therapist or a therapy the center does not provide never matches; no match, or more than one active match, is reported as an error — never guessed.
- **R-TPL-8** Until the center's real templates are registered, development and testing use synthetic templates with fictional therapists and an unmistakable TEST ONLY marking; documents produced from them must never be submitted.
- **R-TPL-7** The application suggests where the fields of a template are (for the EOPYY form family); the administrator confirms which therapy each section is and whether ΑΔΑ / representative come from the center settings or are printed as in the template. A template whose printed therapist details (licence number, issuing authority, licence date, name if printed) do not match the configured therapists cannot be activated; signatures and stamps are checked visually by the administrator in the preview.

### Periods and scheduling
- **R-PER-1** Create a document period for a patient by choosing a monthly referral window (e.g. 16/10/2025–15/11/2025). The period includes every prescribed therapy that has a referral period with exactly that window; its quantity is that referral period's authorized quantity, and its therapist comes from the current assignment. Periods follow the referral windows, not calendar months.
- **R-PER-1a** A monthly referral period can belong to only one document period. Therapies of the same prescription may have different referral windows; therapies whose windows differ are documented in **separate** document periods (one attestation prints one date range), never merged (see `DECISIONS.md`, P-22 and U-04).
- **R-PER-2** Choose home visit ΝΑΙ/ΟΧΙ per therapy per period, where the template has that field. Required before a document can be created.
- **R-SCH-1** Propose dates for each therapy: exactly the authorized quantity of its referral period, within the period's window, no Saturdays or Sundays, no Greek public holidays (plus center-specific closures).
- **R-SCH-2** At most two **distinct therapists** per patient per day (one therapist with two therapies counts once); at most one session of the same therapy per day (configurable).
- **R-SCH-3** Prefer at least one calendar day between sessions of the same therapy; spread sessions as evenly as possible. These are preferences, reported when not met.
- **R-SCH-4** If the hard rules cannot all be satisfied, report the conflicts. Never output an invalid schedule silently.
- **R-SCH-5** The administrator can move, add or remove dates; every edit is re-checked against the rules. The administrator approves the schedule.
- **R-SCH-6** Planned dates are a plan, not attendance. A schedule with fewer dates than authorized is kept and shown as **incomplete**; approving it requires an explicit confirmation that is recorded. A therapy with no dates can never be approved (D-24). Whether an incomplete schedule may be issued is open (Q-03).
- **R-SCH-7** An approved schedule is locked; it must be reopened before dates change. Approval re-checks the rules against the current calendar and closures.

### Documents
- **R-DOC-1** "Create document" is available once the period's end date has passed.
- **R-DOC-2** The document is generated from: stored patient data, the document period's window, its therapies with their referral periods and authorized quantities, the approved schedule dates, home-visit choices, center settings (ΑΔΑ, representative), and **today's date as the issue date**, using the automatically selected template. The issued document records which prescription and which referral periods it covers.
- **R-DOC-2a** Generation fails with a clear error if any therapy of the period has no session dates (D-24), or if a center field the template maps as dynamic (ΑΔΑ, representative) has no value in the center settings (D-25). Template text is kept only for fields mapped as static.
- **R-DOC-3** DOCX and PDF are generated; the administrator previews the PDF before issuing. The issued document is exactly the previewed one.
- **R-DOC-4** Issued documents are immutable. A correction creates a new version; the earlier version remains. Each document records the exact template version and a snapshot of all data used.
- **R-DOC-5** Issued documents can be listed per patient, opened, downloaded (PDF/DOCX) and printed.

### Settings
- **R-SET-1** Center settings: ΑΔΑ, representative name, scheduling limits. Each template mapping states whether its ΑΔΑ / representative are taken from these settings (dynamic) or printed as in the template (static).
- **R-SET-2** Center-specific holiday overrides (extra closures, or a public holiday the center works).

## 5. Non-functional requirements

- **Fidelity:** generated documents keep the template's layout, fonts, tables, images, signatures and stamps; only mapped fields change.
- **Local-first:** runs on one center computer; no internet service required.
- **Privacy:** password-protected administrator login; data and files stored outside the Git repository; real patient data, real templates, signatures, stamps and license details are never committed.
- **Integrity and history:** template versions and issued documents cannot be modified or deleted; important actions are audit-logged.
- **Recoverability:** regular encrypted backups with a tested restore.
- **Language:** Greek user interface and documents.

## 6. Out of scope for the MVP

Therapist accounts or access; per-session confirmation or completion tracking; multi-center support; therapist conflicts across patients; AI/OCR prescription extraction (designed for, not built); electronic submission to EOPYY; remote/online access.

## 7. Must be verified before production use

1. The exact EOPYY form and issuance requirements: who signs, whether listed dates must be performed sessions, deadlines, retention period.
2. Acceptance with at least one real center template (with real signatures and stamps; patient data sanitized).
3. The prescription interpretation in `DECISIONS.md` (U-01 to U-09), in particular the meaning of "total quantity" and "treatment duration", and which document (request or approved decision) is authoritative.
