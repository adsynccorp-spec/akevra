# AKEVRA

AKEVRA is a platform for BCBA supervisors overseeing RBTs and trainees in ABA (Applied Behavior Analysis). Access rules are enforced in the database, not only in the API.

This repository covers:

- **Sprint 0** — API, PostgreSQL, organizations, sign-in, RBAC, audit
- **Supervision Setup & Development Plans** — relationships, supervisee intake, versioned IDPs, goals, competencies, milestones

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
python manage.py test apps.core.tests apps.supervision.tests
```

`migrate` is required on a fresh clone (it applies `supervision.0002` and `core.0003`). On a database that was already seeded for Sprint 0, `seed_sprint0` is safe to run again: it adds `casey@akevra.test` if missing and backfills DEC-058 purpose on BCBA-as-supervisee rows.

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

After first deploy, run once via Railway Console:

```
python manage.py migrate
python manage.py seed_sprint0
```

Swagger: `https://your-app.up.railway.app/api/docs/`

---

## Project layout

```
akevra/
  apps/        accounts, organizations, rbac, supervision, audit, core
  config/      Django settings and URLs
  manage.py
```

Supervision Setup lives in `apps/supervision/` (models, services, views, tests). Sprint 0 acceptance tests remain in `apps/core/tests.py`.

---

## Not yet built

Monthly hours, session notes, competency *assessments* (ratings/approvals), compliance calculations, reports, and the AI assistant are for later sprints. The tables exist; those workflows are not built yet.
