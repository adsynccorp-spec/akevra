# AKEVRA

AKEVRA is a platform for BCBA supervisors overseeing RBTs and trainees in ABA (Applied Behavior Analysis). Access rules are enforced in the database, not only in the API.

This repository covers:

- **Sprint 0** — API, PostgreSQL, organizations, sign-in, RBAC, audit
- **Supervision Setup & Development Plans** — relationships, supervisee intake, versioned IDPs, goals, competencies, milestones
- **Monthly Work & Hours** — monthly cycles, assignments, hours, supervision sessions, BACB compliance calculations
- **Scheduling & Supervision Sessions** — appointments, confirmations, structured preparation forms (versioned), documenting a held appointment
- **Reporting periods & audit readiness** — shared service hours, period status, Supervision Audit, notifications, Settings (rule approval, organization settings)
- **Competency Assessments** — ratings against plan competencies, review, official (locked) record
- **Frontend** (`akevra-frontend/`) — React app for every Relationship Detail tab plus the global Assignments, Hours and Sessions pages

---

## What is already built

- **API + PostgreSQL** — organizations, people, relationships, and an audit trail
- **Separate organizations** — one clinic cannot see another clinic’s records
- **Sign-in** — password, optional authenticator (MFA), session timeout, lockout after failed attempts
- **Roles** — Administrator, Clinical Director, Supervisor (BCBA only), Supervisee. No fifth role.
- **Version 2.3 role model** — Supervisor must be a BCBA. BCaBAs and other credentials cannot be independent Supervisors. BCBAs and BCaBAs live in the Supervisee population.
- **DEC-058** — a BCBA may be assigned as Supervisee for structured professional development, consultation, competency development, leadership development, or case discussion. That assignment does not by itself make the relationship compliance-bearing.
- **Supervisee intake** — exactly one intake record per relationship; a duplicate POST is rejected (`409`)
- **Development plans (IDP)** — versioned. Editing a plan creates a new version; the prior version is kept in full and never overwritten
- **Goals, competencies, milestones** — stored on each IDP version. Active → Achieved/Discontinued requires a documented rationale
- **Role gating** — Supervisee can view the plan and cannot edit it
- **One person, two roles** — the same person can be Supervisor in one relationship and Supervisee in another
- **Workspace at login** — if the same email belongs to more than one organization, the user must pick one first
- **Nothing is permanently deleted** — create and update are logged; delete is blocked

### Monthly Work & Hours

- **Monthly cycles** — one cycle per relationship per month, holding that month's assignments, hours and sessions. The Supervisor opens it and, for fieldwork, sets the month to supervised or concentrated (`cycle.manage`)
- **Assignments** — Supervisor issues (or saves as a **draft**, hidden from the Supervisee until issued); Supervisee submits with files; Supervisor requests revision or completes. Every step is kept in the assignment history. "Requires a file" is enforced on submit
- **Two hours models**
  - **Supervised fieldwork** — the trainee logs their own hours (independent / supervision, restricted / unrestricted). The Supervisor verifies or returns each entry. Single or bulk verify
  - **RBT / BCaBA** — supervision hours come from documented sessions. The Supervisee attests monthly service hours (`service_hours.attest`); they cannot self-log hours
- **Supervision sessions** — date, start/end time, individual / group, meeting method, client observation, topics, feedback, follow-up, files. Can be saved as a draft and completed later. Phone and asynchronous sessions are recorded but do **not** count toward compliance
- **Supervisor notes** — private notes on the Overview tab; Supervisees cannot read them
- **Compliance** — pure calculation functions in `apps/supervision/compliance.py`:
  - RBT: 5% of service hours, 2 contacts a month, 1 individual contact, 1 client observation
  - BCaBA: 5% (2% after 1,000 service hours), biweekly contact, quarterly observation, group ≤ 50%
  - Fieldwork: 20–130 hours a month; supervised 5% / 4 contacts, concentrated 10% / 6 contacts; group ≤ 50%; concentrated hours count ×1.33 toward the total; 60% unrestricted (cumulative); BACB adjustment table. Only **verified** hours count; logged-but-unverified hours are shown separately
  - **Two fieldwork standards**: BACB 2022 (1 observation contact a month) and BACB 2027 (observation by duration: 60 min supervised / 90 min concentrated). The standard follows the trainee's application date, not the calendar. A relationship can **track both** (default) until the standard is determined; choosing one needs a basis, must agree with the application/eligibility year, and records who decided and when (audited). Observed supervision entries carry `observation_minutes`; the Supervisor can add the duration to older entries (`POST /hours/{id}/observation-minutes`)
  - RBT / BCaBA rules are versioned by date (current rules, and the 2027 BACB changes from 2027-01-01)
  - Supervision is relationship-scoped (each supervisor sees their own sessions against the shared service hours) with the overall picture and each supervisor's contribution listed
  - Rules come from the organization's **approved** rule set (Settings → Compliance Rules), falling back to the BACB defaults. Until an Administrator approves, results show **"pending AKEVRA approval"** (see [docs/COMPLIANCE_RULES_FOR_APPROVAL.md](docs/COMPLIANCE_RULES_FOR_APPROVAL.md))

### Scheduling & Supervision Sessions

- **Appointments are logistics only** — scheduling, confirming, rescheduling or cancelling never changes a compliance figure. Only a documented session counts
- Supervisor schedules one appointment or a weekly series (`repeat_weeks`); Supervisee confirms (`appointment.confirm`)
- Reschedule (with reason), cancel (with reason), mark held, mark no-show. Every change is kept in the appointment history
- **Supervision Preparation Form** — a structured form per pathway (RBT, BCaBA, BCBA professional development, student fieldwork) defined as data in `prep_templates.py`; questions can be conditional (`show_if`) and are required only when shown. Not compliance-bearing
- Preparation can be filled on an upcoming appointment, or **before any session is scheduled** (standalone); it is attached automatically to the next session scheduled, and both people are told
- **Every save is kept as a version** (`pre_supervision_note_revision`): the original submission and each edit, shown in the appointment history timeline
- Notification times are shown in the **organization's time zone** (Settings), with the zone named
- **Document Session** — a held appointment opens the Log Session screen pre-filled; saving links the session to the appointment. One documented session per appointment; duplicates are rejected

### Reporting periods, shared service hours and audit readiness

- **One service-hours record per supervisee and month**, shared by every relationship (concurrent supervisors never create separate denominators). Supervision is totalled across all of the supervisee's supervisors on the pathway, with each supervisor's contribution listed
- **Missing vs confirmed zero**: no record = Missing (never treated as zero); 0 hours needs a reason (service hold, illness, …). A correction supersedes the record and keeps the original. Saving with nothing changed is rejected (reviews are kept); any real change, notes included, needs each supervisor's review again, and the form warns before completed reviews reset. A correction needs a **reason** (`correction_reason`), and Hours → **History** shows every version of the month: values, what changed, who corrected it, when and why, and each supervisor review with its timestamp
- **Provenance** on service-hours and fieldwork hours records: source type/system/reference, supporting reference, entered by, reviewed by, timestamps
- **Period status**: Open → Ready for Review → Verified/Closed, every change recorded with who and why. Closing stores a snapshot of the inputs, result and rule-set ID; a closed month reports the snapshot and blocks compliance-bearing changes until it is reopened with a reason
- **RBT Requirements Coordinator** as a dated designation per supervisee; **BACB certification numbers** on people
- **Supervision Audit** (Reports & Audit): month-by-month BACB RBT audit-log columns for a supervisee across all supervisors (concluded relationships included), with flags for missing data and a trace to the underlying sessions and service-hours record; CSV export. Pathway-neutral, so the BCaBA log can be mapped from the same rows
- **Service-hour periods**: hours can be entered for a date range (e.g. a pay period), split into the months they cover. Each supervisor **reviews** the shared record separately, after a confirmation showing the month, hours, attestation date and any correction; the record shows per supervisor reviewed / pending
- **Future-dated sessions**: a session can't be completed before its date (organization time zone; save it as a draft instead), and a future-dated completed record never counts toward requirements before its date
- **Session drafts** are labelled *Draft Saved* with *Resume Draft*, never counted, and never shown as documented. The session form warns about very brief entries (e.g. "x") and asks once before completing such a record
- **Recent Activity** (relationship Overview) is dated by when each record was made, shows the session date and documentation date, and each entry opens its record
- Forms name each missing required field instead of silently disabling Save
- **Overall picture**: other agencies are listed separately as service-hours contributors and supervision-only contributors
- **External supervision**: supervision held outside AKEVRA can be recorded per month; an organization can require it before a month is closed (Settings → *Require external reporting*)
- **Supervision Audit** is available to Administrators / Clinical Directors for any supervisee, and to a supervisee for **their own** audit (*My Ongoing Supervision Audit*). From/To month range; months before the first relationship are not flagged
- **Notifications**: in-app bell plus email for scheduling, preparation, assignments, hours, sessions and assessments. Preparation reminders (one per session within 48 h without preparation) run automatically when the bell polls (at most every 2 minutes per organization); `python manage.py send_preparation_reminders` can also be scheduled hourly

### Settings

- **Compliance Rules** (Administrator) — review each pathway's BACB default parameters per version, adjust values if needed, and approve from a date with a note. Stored as the organization's own rule set (who/when/note); a new approval supersedes the previous one, which stays on file
- **Organization settings** — time zone (used in notifications and emails, default `America/New_York`) and *Require external reporting*. Changes are audited

### Competency Assessments

- Supervisor rates a competency from the current development plan (`assessment.manage`)
- Flow: **Draft → Ready for Review → Official**. Review needs a rating; Official needs documented evidence. It can be returned from review to draft
- An Official assessment is locked — it can no longer be edited. The UI shows it as **Finalized**
- One unfinished (draft / in review) assessment per competency, date and goal; repeated Save Draft clicks don't create copies
- Summary counts follow the four-level scale: Needs Training, Developing, Performs With Support, Independent
- Supervisee sees Official assessments only (`assessment.view`); drafts and assessments under review are hidden

Browse every endpoint in **Swagger**: http://127.0.0.1:8000/api/docs/

---

## Run it on your machine

You need Python 3.12+ and PostgreSQL.

```bash
createdb akevra_sprint0

cd akevra
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py seed_sprint0
python manage.py runserver
```

Then open:

- Swagger UI — http://127.0.0.1:8000/api/docs/
- OpenAPI schema — http://127.0.0.1:8000/api/schema/
- Health check — http://127.0.0.1:8000/api/v1/health

```bash
python manage.py test apps
```

The suite covers Sprint 0, Supervision Setup, Monthly Work & Hours (`test_monthly_work.py`, including BACB calculation cases), Scheduling (`test_scheduling.py`) and Competency Assessments (`test_assessments.py`).

`migrate` is required on a fresh clone and after every pull. It applies the supervision migrations and the `core` migrations that install the database row-level security rules for the new tables. On a database that was already seeded for Sprint 0, `seed_sprint0` is safe to run again: it adds `casey@akevra.test` if missing and backfills DEC-058 purpose on BCBA-as-supervisee rows.

---

## Try the API (Swagger)

1. Start the server (steps above)
2. Open **http://127.0.0.1:8000/api/docs/**
3. Call `POST /api/v1/auth/login` with a demo account
4. Copy the `token` from the response
5. Click **Authorize**, paste the token, then call the other endpoints

Demo password for every account: **`Sprint0!Akevra`**

| Email | What it demonstrates |
|---|---|
| `jordan@akevra.test` | Supervisor of Alex **and** Supervisee of Morgan (start here for milestone 2) |
| `alex@akevra.test` | Supervisee — view-only on the development plan |
| `casey@akevra.test` | BCaBA in the Supervisee population — cannot be assigned as Supervisor |
| `director@akevra.test` | Clinical Director |
| `admin@akevra.test` | Must choose an organization (Northshore ABA or Summit Behavioral) |
| `settings.admin@akevra.test` | Administrator **not** on any relationship |
| `party.admin@akevra.test` | Administrator who **is** named on one relationship |
| `mfa@akevra.test` | MFA — TOTP secret `JBSWY3DPEHPK3PXP` |
| `qa.bcba@akevra.test` | **QA Sandbox** Supervisor (Blake) — create and edit freely here |
| `qa.rbt@akevra.test` | QA Sandbox Supervisee (Sky) — RBT ongoing supervision |
| `qa.rbt2@akevra.test` | QA Sandbox trainee (Jamie) — logs fieldwork hours |
| `qa.admin@akevra.test` | QA Sandbox Administrator |

The QA Sandbox is a separate organization, so testing there never touches the Northshore / Summit reference data.

---

## Supervision Setup APIs

| Method | Path | Who | What it does |
|---|---|---|---|
| GET | `/api/v1/eligible-parties` | signed-in workspace user | BCBAs as Supervisors; everyone (including BCBAs and BCaBAs) as Supervisees |
| GET | `/api/v1/relationships` | party or Clinical Director | list visible relationships |
| POST | `/api/v1/relationships` | BCBA supervisor (or admin self-party) | create a relationship (Supervisor must be BCBA) |
| GET | `/api/v1/relationships/{id}` | party or Clinical Director | one relationship |
| GET / POST | `/api/v1/relationships/{id}/intake` | GET: party; POST: Supervisor | one intake per relationship; duplicate POST → `409` |
| GET / POST | `/api/v1/relationships/{id}/development-plans` | GET: `idp.view`; POST: `idp.manage` | list versions / create first IDP |
| GET | `/api/v1/development-plans/{id}` | `idp.view` | retrieve one version **in full** (goals, competencies, milestones) |
| PATCH | `/api/v1/development-plans/{id}` | `idp.manage` | edit **current** version → creates a new version; prior version is preserved |
| POST | `/api/v1/milestones/{id}/transition` | `idp.manage` | Active → Achieved/Discontinued; rationale required |

## Monthly Work & Hours APIs

`{id}` under `/relationships/` is the relationship UUID. Lists are filtered to what the caller may see (drafts never reach the Supervisee).

| Method | Path | Who | What it does |
|---|---|---|---|
| GET / POST | `/api/v1/relationships/{id}/cycles` | GET: `cycle.view`; POST: `cycle.manage` | list / open monthly cycles |
| GET / PATCH | `/api/v1/cycles/{id}` | GET: `cycle.view`; PATCH: `cycle.manage` | one cycle with everything recorded that month; PATCH sets a fieldwork month to supervised / concentrated |
| GET | `/api/v1/relationships/{id}/compliance` | `compliance.view` | monthly and cumulative results, the rules used, and whether they are approved |
| GET / POST | `/api/v1/relationships/{id}/assignments` | POST: `assignment.create` | list / issue an assignment (`draft: true` saves a draft) |
| GET / PATCH | `/api/v1/assignments/{id}` | party | one assignment with history and files; edit a draft |
| POST | `/api/v1/assignments/{id}/issue` | `assignment.create` | issue a draft |
| POST | `/api/v1/assignments/{id}/submit` | `assignment.submit` | Supervisee submits (multipart, `files`) |
| POST | `/api/v1/assignments/{id}/request-revision` · `/complete` | `assignment.review` | review outcome |
| GET | `/api/v1/assignment-attachments/{id}/download` | party | authenticated file download |
| GET / POST | `/api/v1/relationships/{id}/hours` | POST: `hours.log` (fieldwork only) | list / log hours |
| PATCH | `/api/v1/hours/{id}` | `hours.log` | the trainee corrects a pending or returned entry; it goes back for verification |
| POST | `/api/v1/hours/{id}/verify` · `/return` | `hours.verify` | Supervisor verifies or returns |
| PUT | `/api/v1/relationships/{id}/service-hours` | `service_hours.attest` | RBT / BCaBA monthly service hours (`year`, `month`, `hours`) |
| GET / POST | `/api/v1/relationships/{id}/sessions` | POST: `session.document` | list / document a session (`draft: true`, `appointment_id`, multipart `files`) |
| GET / PATCH | `/api/v1/sessions/{id}` | `session.view` | one session; edit or `complete` a draft |
| GET | `/api/v1/session-attachments/{id}/download` | party | authenticated file download |
| GET / POST | `/api/v1/relationships/{id}/notes` | Supervisor only | private supervisor notes |
| GET | `/api/v1/assignments` · `/hours` · `/sessions` | signed-in user | the same records across all of the caller's relationships (global pages) |

## Scheduling APIs (logistics only)

| Method | Path | Who | What it does |
|---|---|---|---|
| GET / POST | `/api/v1/relationships/{id}/appointments` | POST: `appointment.manage` | list / schedule (`repeat_weeks` for a weekly series) |
| GET | `/api/v1/appointments` | signed-in user | upcoming and past appointments across relationships |
| GET / PATCH | `/api/v1/appointments/{id}` | party; PATCH: `appointment.manage` | one appointment with history; reschedule (with `reason`) |
| POST | `/api/v1/appointments/{id}/confirm` | `appointment.confirm` | Supervisee confirms |
| POST | `/api/v1/appointments/{id}/cancel` · `/complete` · `/no-show` | `appointment.manage` | cancel (with `reason`), mark held, mark no-show |
| PUT | `/api/v1/appointments/{id}/prep-note` | `prep.submit` (Supervisee) | save structured answers on an upcoming appointment; each save adds a version |
| GET / PUT | `/api/v1/relationships/{id}/preparation` | GET: party; PUT: Supervisee | the pathway's form template, waiting / linked preparations; PUT saves preparation before a session is scheduled |

None of these change a compliance figure. To count, a held appointment must be documented as a session (`POST .../sessions` with `appointment_id`).

## Reporting Period, Audit & Settings APIs

| Method | Path | Who | What it does |
|---|---|---|---|
| POST | `/api/v1/cycles/{id}/status` | `cycle.manage` | Open → Ready for Review → Closed (closing stores a snapshot), reopen with a reason |
| GET | `/api/v1/supervisees/{id}/service-hours` | party | shared monthly service-hours records with corrections (internal / external / confirmed zero with reason); entered via `PUT /relationships/{id}/service-hours` |
| POST | `/api/v1/relationships/{id}/service-hours/periods` | `service_hours.attest` | service hours entered for a date range, split by month |
| POST | `/api/v1/service-hours/{id}/review` | Supervisor | this supervisor's review of the shared record |
| GET / POST | `/api/v1/supervisees/{id}/external-supervision` | GET: party; POST: the supervisee or Admin / Clinical Director | supervision held outside AKEVRA, per month |
| GET / POST · PATCH | `/api/v1/supervisees/{id}/coordinators` · `/coordinators/{id}` | Administrator / Clinical Director | RBT Requirements Coordinator designations (dated) |
| PATCH | `/api/v1/people/{id}/certification` | Administrator or the person | BACB certification number |
| GET | `/api/v1/supervisees/{id}/audit-log` | Admin / Clinical Director, or the supervisee for themselves | month-by-month audit log (`start`, `end`; `export=csv`) |
| GET | `/api/v1/audit/people` | signed-in user | supervisees the caller may audit (only themselves for a supervisee) |
| GET | `/api/v1/compliance-rules` | signed-in user | rule catalog per pathway, rules in force, approval history |
| POST | `/api/v1/compliance-rules/approve` | Administrator | approve a pathway's rules from a date, with optional overrides and a note |
| GET / PATCH | `/api/v1/organization/settings` | GET: workspace user; PATCH: Administrator | `time_zone`, `require_external_reporting` |
| GET | `/api/v1/notifications` | signed-in user | the bell (also triggers due preparation reminders) |
| POST | `/api/v1/notifications/{id}/read` · `/notifications/read-all` | signed-in user | mark read |
| GET | `/api/v1/fieldwork-standard/trainees` | `fieldwork_standard.history.view` | fieldwork trainees whose standard history the caller may read (Administrators: all, without opening the relationship) |
| GET | `/api/v1/relationships/{id}/fieldwork-standard/history` | `fieldwork_standard.history.view` (Administrator, Clinical Director, the parties) | every change to the trainee's governing fieldwork standard: previous and new selection, basis, application year, note, who, when |
| POST | `/api/v1/relationships/{id}/prior-hours/review` | `relationship.edit` | mark prior post-certification hours (entered at setup) reviewed |
| GET / POST | `/api/v1/hours/{id}/observation-minutes` | GET: `cycle.view`; POST: `hours.verify` | observed minutes on an observed entry and every correction to it; POST records or corrects them (BACB 2027). Not recorded counts as 0 |

## Competency Assessment APIs

| Method | Path | Who | What it does |
|---|---|---|---|
| GET / POST | `/api/v1/relationships/{id}/assessments` | GET: `assessment.view`; POST: `assessment.manage` | list with a summary / create a draft against a plan competency |
| GET / PATCH | `/api/v1/assessments/{id}` | as above | one assessment; edit until it is Official |
| POST | `/api/v1/assessments/{id}/submit` | `assessment.manage` | Draft → Ready for Review (needs a rating) |
| POST | `/api/v1/assessments/{id}/return` | `assessment.manage` | Ready for Review → Draft |
| POST | `/api/v1/assessments/{id}/finalize` | `assessment.manage` | → Official (needs evidence); locked afterwards |

### Path `id` is the relationship UUID

Intake and development-plan list/create take the **relationship** `id`, not a person id.

1. Call `GET /api/v1/relationships` while logged in as Jordan
2. Copy the row’s **top-level** `id`
3. Do **not** use `supervisor.id` or `supervisee.id`

If Swagger says `Value must be a Guid`, there is usually a trailing space in the path field, or a person id was pasted. Paste the relationship UUID with no spaces.

Example seeded pair (Jordan supervises Alex, RBT ongoing):

- Use the `id` from the relationship list for `/intake` and `/development-plans`
- Alex’s person id will 404 or fail Swagger validation — that is expected

---

## How to verify milestone 2 in Swagger

Authorize as **`jordan@akevra.test`** first.

### 1. Version 2.3 role model

1. `GET /api/v1/eligible-parties`
   - Supervisors are BCBA only
   - Casey (`bcaba`) and Alex (`rbt`) are **not** in `supervisors`
   - Casey, Alex, **and** Jordan (BCBA) are in `supervisees`
2. `POST /api/v1/relationships` with Casey as `supervisor_id` → **400**, `code: supervisor_must_be_bcba`

### 2. Exactly one intake

1. `GET /api/v1/relationships` → copy Jordan→Alex top-level `id`
2. `POST /api/v1/relationships/{id}/intake`

```json
{
  "captured_on": "2026-09-16",
  "background": "RBT joining ongoing supervision",
  "notes": "Initial intake"
}
```

3. First success is **201**. The same POST again is **409**, `code: intake_duplicate`
4. `GET` the same path returns that one record

If intake already exists from an earlier demo, the first POST is already **409** — that still demonstrates the rule. For a clean create, make a new Jordan→Casey relationship (`supervision_track: bcaba_ongoing_supervision`) and intake that id.

### 3. IDP versioning

1. `POST /api/v1/relationships/{id}/development-plans`

```json
{
  "summary": "Version 1 summary",
  "goals": [{ "title": "Independent data collection", "description": "Collect IOA without prompts" }],
  "competencies": [{ "name": "Session documentation", "target_level": "independent", "current_level": "prompted" }],
  "milestones": [{ "title": "Complete first documented month", "status": "active" }]
}
```

2. Copy the plan `id` (version 1)
3. `PATCH /api/v1/development-plans/{v1_id}` with a new summary / extra goal → response is a **new** `id`, `version_number: 2`, `is_current: true`
4. `GET /api/v1/development-plans/{v1_id}` → still `"Version 1 summary"`, `is_current: false`, `record_status: superseded`, original goals intact

If a plan already exists, `GET /api/v1/relationships/{id}/development-plans` lists every version. Create another first plan on a new relationship (Jordan→Casey) if you want to repeat the create step.

### 4. Milestone rationale

Use the milestone `id` from the **current** plan (`is_current: true`). After an edit, that is version 2’s milestone, not version 1’s. Superseded versions cannot be transitioned.

1. `POST /api/v1/milestones/{id}/transition` with `{ "status": "achieved" }` → **400**, `code: rationale_required`
2. Same call with `"rationale": "IOA met criterion across three consecutive sessions."` → **200**

### 5. Supervisee view-only

1. Login as `alex@akevra.test` and Authorize with Alex’s token
2. `GET /api/v1/development-plans/{current_plan_id}` → **200**, `can_manage: false`
3. `PATCH` the same plan → **403**, `code: idp_view_only`

### 6. DEC-058 (BCBA as Supervisee)

As Jordan, `GET /api/v1/relationships` and find **Morgan → Jordan**:

- `supervisee.credential_type`: `bcba`
- `supervision_track`: `bcba_professional_development`
- `supervisee_purpose`: `structured_professional_development`
- `is_compliance_bearing`: **false**
- Jordan’s `your_role` on that row: `supervisee`
- `idp.manage`: false, `idp.view`: true

Roles remain four: Administrator, Clinical Director, Supervisor, Supervisee.

To create another BCBA-as-Supervisee row, POST a relationship with `supervision_track: bcba_professional_development` and a purpose such as `consultation`. A BCBA supervisee on an RBT track is rejected.

---

## Where the data is

Postgres database: **`akevra_sprint0`**

| Table | What it holds |
|---|---|
| `organization` | Clinics |
| `login_identity` | Sign-in emails |
| `user_account` | People inside an organization |
| `organization_role_grant` | Administrator / Clinical Director |
| `supervisory_relationship` | Supervisor ↔ Supervisee pairs |
| `supervisee_intake` | One intake record per relationship |
| `development_plan` | Versioned IDP (prior versions preserved) |
| `development_plan_goal` | Goals on a specific IDP version |
| `development_plan_competency` | Competencies on a specific IDP version |
| `development_plan_milestone` | Milestones on a specific IDP version |
| `monthly_cycle` | One month of work per relationship |
| `assignment` / `assignment_event` / `assignment_attachment` | Assignments, their history, and submitted files |
| `hours_entry` | Fieldwork hours logged by the trainee |
| `service_hours_attestation` | RBT / BCaBA monthly service hours |
| `supervision_session` / `session_attachment` | Documented supervision sessions and their files |
| `relationship_note` | Private supervisor notes |
| `appointment` / `appointment_event` | Scheduled sessions and their history |
| `pre_supervision_note` / `pre_supervision_note_revision` | Supervisee preparation (structured answers) and every saved version |
| `competency_assessment` | Competency ratings (Draft → Ready for Review → Official) |
| `compliance_rule_set` / `compliance_rule_parameter` | Approved compliance rules per organization (empty until approved in Settings) |
| `monthly_cycle_event` | Period status history (who, when, why) |
| `service_hours_record` / `service_hours_review` / `service_hours_period_entry` | Shared monthly service hours, each supervisor's review, date-range entries |
| `external_supervision_record` | Supervision held outside AKEVRA |
| `requirements_coordinator_designation` | RBT Requirements Coordinator (dated) |
| `notification` | In-app notifications (the bell) |
| `audit_event` | Who changed what, and when |

---

## Deploy on Render + Neon (current staging)

> **Note:** Staging currently runs on **Render** (`https://akevra.onrender.com`) + **Neon**; Railway (below) is the earlier setup. Production will be AWS ECS/Fargate + RDS PostgreSQL + S3 in the applicable deployment milestone.

Neon gives **one** connection string. Put that whole string in Render as `DATABASE_URL`. Do not split it into `DB_NAME` / `DB_USER` unless you want to.

**Neon:** Dashboard → Connection details → copy the URI. Use the **direct** host (no `-pooler` in the hostname).

**Render Web Service**

| Setting | Value |
|---|---|
| Root Directory | `akevra` if the GitHub repo is the Golf app; leave empty if the repo is only AKEVRA |
| Build command | `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate` |
| Start command | `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120` |
| Health check path | `/api/v1/health` |
| Region | the same region as the Neon database |

**Environment variables (Render → Environment):**

| Key | Value |
|---|---|
| `DATABASE_URL` | paste Neon connection string (the long `postgresql://...` line) |
| `SECRET_KEY` | any long random string |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | `.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://YOUR-SERVICE.onrender.com` |
| `MFA_ENCRYPTION_KEY` | Fernet key (same command as below) |
| `CORS_ORIGINS` / `FRONTEND_URL` | the frontend URL, e.g. `https://akevra-frontend.vercel.app` |
| `MEDIA_ROOT` | `/opt/render/project/src/media` (or a Render Disk mount path so uploads survive redeploys) |

**Stability:** the free plan sleeps after ~15 minutes idle, and the first request then takes 30–60 s (users see timeouts). For client testing use the Starter plan, or ping `/api/v1/health` every 5 minutes (e.g. UptimeRobot).

Migrations run in the build command. After the first successful deploy, open Render **Shell** once and run:

```
python manage.py seed_sprint0
```

Swagger: `https://YOUR-SERVICE.onrender.com/api/docs/`

---

## Staging Deployment (Railway + Neon)

This is only for the testing and staging phases.

The production deployment will be on AWS ECS/Fargate + RDS PostgreSQL + S3 to be completed in the applicable deployment milestone.

**Neon (PostgreSQL):** Create a project at [neon.tech](https://neon.tech). Copy the direct connection URI (no `-pooler` in hostname).

**Railway (Application Server):**

| Setting | Value |
|---|---|
| Builder | Dockerfile |
| Build command | *(handled by Dockerfile)* |
| Start command | `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2` |
| Healthcheck path | `/api/v1/health` |

**Environment variables (Railway → Variables):**

| Key | Value |
|---|---|
| `DATABASE_URL` | Neon connection string |
| `SECRET_KEY` | long random string |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | `your-app.up.railway.app` |
| `CSRF_TRUSTED_ORIGINS` | `https://your-app.up.railway.app` |
| `MFA_ENCRYPTION_KEY` | generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `CORS_ORIGINS` | the frontend URL, e.g. `https://akevra-frontend.vercel.app` |
| `FRONTEND_URL` | the same frontend URL (used in invitation and password-reset emails) |
| `MEDIA_ROOT` | path of a persistent volume for uploaded files, e.g. `/data/media` |
| `ASSIGNMENT_ATTACHMENT_MAX_MB` | optional upload limit per file (default `10`) |

After first deploy, run once via Railway Console:

```
python manage.py migrate
python manage.py seed_sprint0
```

Run `python manage.py migrate` again after every deploy that adds migrations.

**Uploaded files:** assignment and session files are stored on disk at `MEDIA_ROOT`. The Railway container disk is wiped on every deploy, so attach a Railway **Volume** and set `MEDIA_ROOT` to its mount path. Without it, uploaded files are lost on redeploy (the database records stay). Production moves files to S3.

**Compliance rule approval:** an Administrator approves the rules in the app under **Settings → Compliance Rules** (per pathway, from a date, with a note). The command below is still available for a platform-wide approval:

```
python manage.py approve_compliance_rules --name "AKEVRA Approved" --effective-from 2026-01-01
```

Change a single value with `--set TRACK:KEY=VALUE` (e.g. `--set rbt_ongoing_supervision:min_supervision_percent=5`).

**Frontend (Vercel):** project root `akevra-frontend`, build `npm run build`, output `dist`, env `VITE_API_URL`. Details in `akevra-frontend/README.md`.

Swagger: `https://your-app.up.railway.app/api/docs/`

---

## Project layout

```
akevra02/
  apps/             accounts, organizations, rbac, supervision, audit, core
  config/           Django settings and URLs
  docs/             compliance rules for AKEVRA approval
  akevra-frontend/  React app (Vite)
  manage.py
```

| Area | Backend files (`apps/supervision/`) |
|---|---|
| Supervision Setup | `models.py`, `services.py`, `views.py`, `tests.py` |
| Monthly Work & Hours | `monthly.py`, `monthly_views.py`, `test_monthly_work.py` |
| Compliance calculations | `compliance.py` (pure functions + versioned BACB defaults) |
| Scheduling & preparation | `scheduling.py`, `scheduling_views.py`, `prep_templates.py`, `test_scheduling.py` |
| Competency Assessments | `assessments.py`, `assessment_views.py`, `test_assessments.py` |
| Reporting periods, service hours, audit | `periods.py`, `period_views.py`, `audit_log.py`, `test_periods.py` |
| Settings (rule approval, organization) | `rules.py`, `settings_views.py` |
| Notifications | `notifications.py`, `notification_views.py`, `management/commands/send_preparation_reminders.py` |
| QA rounds | `test_qa_round.py`, `test_qa_round2.py` |

Role permissions are in `apps/rbac/matrix.py`. Row-level security SQL for each table is in `apps/core/sql/`. Sprint 0 acceptance tests remain in `apps/core/tests.py`.

---

## Not yet built

- The **AI Teaching Assistant** — later sprint
- Frontend placeholder pages: Active / Closed relationships, the global Compliance page, Support
- **Compliance reminders** beyond preparation reminders (the `compliance_reminder` table exists)

Waiting on the client:

- **Approval of the compliance rules** in Settings → Compliance Rules — until then, results show "pending AKEVRA approval"
- The client's own **Supervision Preparation Form** questions (the shared Google Form links require sign-in); they slot into `prep_templates.py`
- **Staging plan** — Render Starter (or a health-check ping) so the server does not sleep during testing
- **File storage on staging** — a persistent disk (`MEDIA_ROOT`) or S3-type storage
