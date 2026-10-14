# AKEVRA

AKEVRA is a platform for BCBA supervisors overseeing RBTs and trainees in ABA (Applied Behavior Analysis). Access rules are enforced in the database, not only in the API.

This repository covers:

- **Sprint 0** — API, PostgreSQL, organizations, sign-in, RBAC, audit
- **Supervision Setup & Development Plans** — relationships, supervisee intake, versioned IDPs, goals, competencies, milestones
- **Monthly Work & Hours** — monthly cycles, assignments, hours, supervision sessions, BACB compliance calculations
- **Scheduling & Supervision Sessions** — appointments, confirmations, prep notes, documenting a held appointment
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
  - Fieldwork: 20–130 hours a month; supervised 5% / 4 contacts, concentrated 10% / 6 contacts; 1 observation; group ≤ 50%; concentrated hours count ×1.33 toward the total; 60% unrestricted (cumulative); BACB adjustment table
  - Rules are versioned by date (2022 rules, and the 2027 BACB changes from 2027-01-01). A fieldwork relationship stays on the rules in force when it started
  - Results show **"pending AKEVRA approval"** until the rules are approved (see [docs/COMPLIANCE_RULES_FOR_APPROVAL.md](docs/COMPLIANCE_RULES_FOR_APPROVAL.md))

### Scheduling & Supervision Sessions

- **Appointments are logistics only** — scheduling, confirming, rescheduling or cancelling never changes a compliance figure. Only a documented session counts
- Supervisor schedules one appointment or a weekly series (`repeat_weeks`); Supervisee confirms (`appointment.confirm`)
- Reschedule (with reason), cancel (with reason), mark held, mark no-show. Every change is kept in the appointment history
- **Prep notes** — the Supervisee writes a pre-supervision note on an upcoming appointment (`prep.submit`); the Supervisor reads it. Not compliance-bearing
- **Document Session** — a held appointment opens the Log Session screen pre-filled; saving links the session to the appointment

### Competency Assessments

- Supervisor rates a competency from the current development plan (`assessment.manage`)
- Flow: **Draft → Ready for Review → Official**. Review needs a rating; Official needs documented evidence. It can be returned from review to draft
- An Official assessment is locked — it can no longer be edited
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
| PUT | `/api/v1/appointments/{id}/prep-note` | `prep.submit` (Supervisee) | save the prep note on an upcoming appointment (returned on the appointment) |

None of these change a compliance figure. To count, a held appointment must be documented as a session (`POST .../sessions` with `appointment_id`).

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
| `pre_supervision_note` | Supervisee prep notes on appointments |
| `competency_assessment` | Competency ratings (Draft → Ready for Review → Official) |
| `compliance_rule_set` / `compliance_rule_parameter` | Approved compliance rules (empty until AKEVRA approves) |
| `audit_event` | Who changed what, and when |

---

## Deploy on Render (free) + Neon (free Postgres)

> **Note:** This documents a low-cost development/staging setup. Development and staging use **Railway** (application server) + **Neon** (managed PostgreSQL). Production will be AWS ECS/Fargate + RDS PostgreSQL + S3 in the applicable deployment milestone.

Neon gives **one** connection string. Put that whole string in Render as `DATABASE_URL`. Do not split it into `DB_NAME` / `DB_USER` unless you want to.

**Neon:** Dashboard → Connection details → copy the URI. Use the **direct** host (no `-pooler` in the hostname).

**Render Web Service**

| Setting | Value |
|---|---|
| Root Directory | `akevra` if the GitHub repo is the Golf app; leave empty if the repo is only AKEVRA |
| Build command | `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate` |
| Start command | `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT` |

**Environment variables (Render → Environment):**

| Key | Value |
|---|---|
| `DATABASE_URL` | paste Neon connection string (the long `postgresql://...` line) |
| `SECRET_KEY` | any long random string |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | `.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://YOUR-SERVICE.onrender.com` |

After the first successful deploy, open Render **Shell** once and run:

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

**Compliance rule approval:** after AKEVRA signs off [docs/COMPLIANCE_RULES_FOR_APPROVAL.md](docs/COMPLIANCE_RULES_FOR_APPROVAL.md), run once:

```
python manage.py approve_compliance_rules --name "AKEVRA Approved" --effective-from 2026-01-01
```

Change a single value with `--set TRACK:KEY=VALUE` (e.g. `--set rbt_ongoing_supervision:min_supervision_percent=5`), or later in the Django admin under Compliance rule sets.

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
| Scheduling | `scheduling.py`, `scheduling_views.py`, `test_scheduling.py` |
| Competency Assessments | `assessments.py`, `assessment_views.py`, `test_assessments.py` |

Role permissions are in `apps/rbac/matrix.py`. Row-level security SQL for each table is in `apps/core/sql/`. Sprint 0 acceptance tests remain in `apps/core/tests.py`.

---

## Not yet built

- **Reports & Audit** export and the **AI Teaching Assistant** — later sprints
- Frontend placeholder pages: Active / Closed relationships, the global Compliance page, Settings, Support
- **Compliance reminders** — the table exists; scheduled reminder emails are not sent yet

Waiting on the client:

- **AKEVRA approval of the compliance rules** — until then, results show "pending AKEVRA approval" (see the approval command above)
- **File storage on staging** — attach a Railway Volume (`MEDIA_ROOT`) or move uploads to S3-type storage
