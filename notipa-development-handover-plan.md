# Notipa — Development Handover Plan

**Prepared for:** Theo van Stratum / StratumCode
**Date:** 22 July 2026
**Stack:** Django 6.0.7, Python 3.14.x, SQLite (dev) → PostgreSQL (production), Docker throughout
**Status:** Foundation, data model, UI shell, authentication, school-scoping, admin/teacher onboarding (school setup, teacher invites, classes, students, guardians, with full edit and soft-delete for teachers/classes/students/guardians, a class detail view showing its teacher and roster, student/guardian detail views for linking the two together from either direction, an in-app User Manual under Help in the sidebar, and a guardian-facing dashboard/child view), and Announcements (school-wide or class-scoped, draft/publish, guardian read-tracking) are all delivered and validated. Next up: Homework, the next content section, following the write/publish/read pattern Announcements just established.

---

## 1. Purpose of This Document

This is the technical handover plan for Notipa's Phase 1 build (the MVP described in Section 11 of the proposal: 8–12 weeks solo). It exists so that build decisions made now — Docker, the database, the data model, the UI shell, auth, and onboarding — don't have to be revisited or, worse, silently drift as the project grows. It assumes the reader has the proposal for product context and needs the engineering plan.

Three things anchor every decision below:

1. **Docker from the first commit.** Not retrofitted once other things depend on a bare-metal install — this was explicit in the proposal's MVP scope and matters more here than on Theo's other products, because this one is a long-term candidate for open-sourcing and third-party self-hosting (Section 8 of the proposal — Digital Public Good, other countries running their own instances). A containerised setup from day one is what makes that eventually possible without a rewrite.
2. **The database must never be at risk from an application update.** This is the single most important operational property of the whole setup, and it shapes the Docker architecture more than anything else in Sections 2–8.
3. **The permission model is the part not to rush.** The proposal (Section 11) flags this explicitly: a permission-boundary bug on children's data is a serious failure, not a minor bug. Everything from Section 11 onward is designed with that in mind — school-scoped data by construction, non-enumerable IDs, queryset-level enforcement, and role checks that fail closed (403, not a hidden link).

---

## 2. Current State

```
notipa/                        ← outer project directory
├── .venv/
├── notipa/                    ← inner Django package
│   ├── __init__.py
│   ├── asgi.py
│   ├── settings.py            ← replaced, see Sections 7, 11, 13
│   ├── urls.py                ← updated, see Sections 12, 13 — routes to core.urls + auth
│   └── wsgi.py                ← unchanged
├── core/                      ← data model + views app, see Sections 11–15
│   ├── __init__.py
│   ├── admin.py
│   ├── apps.py
│   ├── models.py
│   ├── forms.py                (NEW — Section 14/15 — SchoolForm, SchoolClassForm, StudentForm)
│   ├── middleware.py            (NEW — Section 13 — ActiveSchoolMiddleware)
│   ├── permissions.py           (NEW — Section 13 — role_required, superuser_required, scope_to_school)
│   ├── views.py
│   ├── urls.py
│   ├── tests.py                (stub — no tests written yet)
│   ├── static/core/css/app.css (Section 12)
│   └── migrations/
│       ├── __init__.py
│       └── 0001_initial.py
├── templates/
│   ├── base.html
│   ├── registration/
│   │   └── login.html           (NEW — Section 13)
│   └── core/
│       ├── dashboard.html
│       ├── placeholder.html
│       ├── no_access.html       (NEW — Section 13)
│       ├── school_setup.html    (NEW — Section 15)
│       ├── classes_list.html    (NEW — Section 14)
│       ├── class_form.html      (NEW — Section 14)
│       ├── students_list.html   (NEW — Section 14)
│       └── student_form.html    (NEW — Section 14)
├── .gitignore
├── db.sqlite3                 ← superseded by the /app/data volume, see Section 4
└── manage.py
```

All Docker/settings/dependency files from Section 7 have been delivered and confirmed installing/running. The `core` app's data model, UI shell, auth/scoping layer, and onboarding views have all been delivered and validated, but **the data model migration has not yet been applied to your actual dev database** — that's the first thing in Section 16.

---

## 3. Why the Database Architecture Matters More Than Usual Here

*(Unchanged — repeated here because it's still the load-bearing decision.)*

The specific risk being designed against: a docker-compose setup that treats the database as just another part of the application — inside the same container, or on a bind mount that gets wiped or overwritten on rebuild — will lose data the moment you redeploy. For a communication tool holding attendance-adjacent records, permission-slip responses, and guardian contact details for children, that's not an acceptable failure mode even in early pilot use.

The fix is structural, not a setting to remember:

- **The database lives in its own service, on a named Docker volume, decoupled from the application image.**
- **Application updates rebuild and replace the `web` container only.** The `db` service (production) or the SQLite data volume (development) is never touched by that process.
- **Nobody runs `docker compose down -v` as part of a normal deploy.** The `-v` flag is what destroys volumes — it should be nowhere in the standard update workflow, documented as such, and treated as a deliberate, rare, manual action (e.g. deliberately resetting a dev environment).

This is implemented below for both environments.

---

## 4. Development Environment (SQLite)

Even in development, the SQLite file is **not** kept inside the bind-mounted code directory. If it were, an easy mistake — cleaning the working directory, a bad `git clean`, a container rebuild with the wrong mount — could take it out along with the code. Instead:

- `db.sqlite3` lives at `/app/data/db.sqlite3` inside the container.
- `/app/data` is a **named Docker volume** (`notipa_dev_db`), separate from the code bind mount.
- The code itself (`.:/app`) is bind-mounted for live reload during development, but `/app/data` is not part of that bind mount — it's an independent volume that survives rebuilds, `docker compose up --build`, and even `docker compose down` (without `-v`).
- `settings.py` creates `BASE_DIR / 'data'` automatically if it doesn't exist, so the very first `docker compose up` works with no manual setup step.

## 5. Production Environment (PostgreSQL)

- PostgreSQL runs as its **own service** (`db`), from the official `postgres` image — not bundled into the Django image.
- Its data directory (`/var/lib/postgresql/data`) is mounted to a **named volume** (`notipa_prod_db`).
- The `web` service depends on `db` but has no write access to its volume and no ability to remove it.
- Deploying a new version of the app means: build a new `web` image, stop the old `web` container, start the new one. The `db` container is not recreated, not rebuilt, and not touched — it keeps running (or, at most, restarts against the same volume if the host reboots).

## 6. Switching Between SQLite and PostgreSQL

Handled entirely through the `DJANGO_ENV` environment variable, read in `settings.py`. When `DJANGO_ENV=production`, `DATABASES` points at PostgreSQL using `DB_*` variables; otherwise it uses SQLite on the `/app/data` volume. No code differs between environments — only which `.env` file is loaded (`.env` for development, `.env.prod` for production).

---

## 7. Delivered Files — Foundation (Docker, Settings, Dependencies)

All of the following have been handed over as complete, ready-to-save files and placed in the project root (alongside `manage.py`), except `settings.py` which replaces `notipa/settings.py`:

| File | Location | Status |
|---|---|---|
| `Dockerfile` | project root | Delivered |
| `entrypoint.sh` | project root | Delivered |
| `docker-compose.yml` (dev) | project root | Delivered |
| `docker-compose.prod.yml` (production) | project root | Delivered |
| `.env.example` | project root | Delivered |
| `.dockerignore` | project root | Delivered |
| `settings.py` | `notipa/settings.py` | Delivered, updated in Section 11 (data model) and Section 13 (auth/scoping) |
| `requirements.txt` | project root | Delivered, installs cleanly |

The full contents of `Dockerfile`, `entrypoint.sh`, both compose files, `.env.example`, `.dockerignore`, and `requirements.txt` are unchanged since the previous handover and are not repeated here — see your existing copies in the project root.

---

## 8. Deployment / Update Workflow (the part that protects the database)

**Development:**
```
docker compose up --build
```
Rebuilds only the `web` image. `notipa_dev_db` is untouched.

**Production update (new release):**
```
docker compose -f docker-compose.prod.yml build web
docker compose -f docker-compose.prod.yml up -d --no-deps web
```
`--no-deps` is the key flag — it stops Compose from recreating the `db` service as a side effect of bringing `web` up. The `db` container is not recreated, not rebuilt, and not touched — it keeps running against `notipa_prod_db` throughout.

**Never run, as part of a normal update:**
```
docker compose down -v          # destroys volumes, including the db
docker compose down --volumes   # same
docker volume rm notipa_prod_db # obviously
```
Worth putting this explicitly in whatever runbook or README ends up covering deploys, precisely because `docker compose down -v` is a common copy-pasted "clean slate" habit that's fine in development and destructive in production.

**Backups (production):** still not designed — the one open item carried over across every handover revision so far, and it should exist before real guardian/student data is in the system, not after. Worth a short follow-up plan (e.g. scheduled `pg_dump` from the `db` container to off-host storage) before real usage starts, not before Phase 1 development — no urgency while it's still local dev.

---

## 9. Phase 1 Build Sequence (from proposal Section 11, sequenced for solo execution)

1. **Foundation — done.** Docker, env-driven settings, and dependencies all in place and confirmed installing/running.
2. **Data model — done.** School/class/student/guardian multi-tenant schema with UUID primary keys, delivered and validated as the `core` app. See Section 11.
3. **Admin and teacher onboarding flows — done.** Auth, school-scoping, in-app school setup (superusers only), teacher/admin invites, and Classes/Students list+create views are all real and working. See Sections 13–16. Still open within this item: guardian invites (SMS-based, proposal Section 4.2 — a separate, later mechanism, deliberately out of scope here since it needs SMS infrastructure this app doesn't have yet).
4. **Announcements with read tracking — done.** See Section 23.
5. **Homework posting.** ← next. (Model exists — `Homework`. Same placeholder situation as Fee Notices/Permission Slips below.)
6. **Fee due-date notices** (Track 2 / private-school only — informational at MVP stage). (Model exists — `FeeNotice`. Same placeholder situation.)
7. **Permission slips with response tracking.** (Model exists — `PermissionSlip`/`PermissionSlipResponse`. Same placeholder situation.)
8. **PWA install + web push notifications** — last in the sequence since it depends on the rest of the data model existing first.

Estimate carried over from the proposal: 8–12 weeks solo. Having the data model, UI shell, auth/scoping layer, and onboarding pattern already built and proven ahead of items 4–7 should make each of those meaningfully faster than a from-scratch estimate would suggest — every one of them follows the same shape now established by Classes/Students (a scoped list view, a role-restricted create view, a school-scoped ModelForm).

---

## 10. Not Yet Built

Being explicit about what exists and doesn't, so partial progress on a section isn't confused with that section being done:

- **No guardian invite flow.** The SMS-based, no-app-store-account guardian invite described in proposal Section 4.2 doesn't exist yet — the `User.phone_number` field and passwordless-friendly design are in place, but nothing sends an SMS or issues a guardian a way to log in. Standard username/password login (Section 13) covers admins/teachers for now.
- **Homework, Fee Notices, and Permission Slips are still placeholder pages**, not real views — see Section 12.2. The models behind all of them already exist and are tested (Section 11.2). Settings is also still a placeholder. Announcements (previously in this list too) is now real — see Section 23.
- **File storage is local filesystem**, not yet the S3-compatible object storage the proposal specifies for production (Section 5) — fine for dev, needs revisiting before Homework/StudentRecord attachments are used with real files at any scale.
- **`role_required` exists but is under-exercised.** It's used by Classes/Students (admin/teacher only) and tested directly, but none of the still-placeholder sections have had their real role restrictions decided yet (e.g. should guardians see Announcements read-only? Almost certainly yes — but that decision hasn't been made concrete in code yet).

---

## 11. Data Model (`core` app) — Delivered

Delivered as complete, ready-to-save files, validated before being committed — same standard as Section 7. Validation method: migrations were generated and applied against a disposable SQLite database outside the project directory, then exercised with real object creation across every model and relationship (School → SchoolMembership → SchoolClass → Student → GuardianLink → Announcement/AnnouncementRead → FeeNotice → PermissionSlip/PermissionSlipResponse → StudentRecord) to confirm the schema behaves as designed before it ever touched your actual project files. `manage.py check` and `manage.py makemigrations --check --dry-run core` both pass clean against your real Django 6.0.7 project.

### 11.1 Files delivered

| File | Location | Status |
|---|---|---|
| `models.py` | `core/models.py` | Delivered, validated |
| `admin.py` | `core/admin.py` | Delivered, validated |
| `apps.py` | `core/apps.py` | Delivered (standard Django app config, no custom changes needed) |
| `0001_initial.py` | `core/migrations/0001_initial.py` | Delivered, validated — **not yet applied to your dev database**, see Section 16 |
| `tests.py` | `core/` | Delivered as a standard empty stub at first — now holds the teacher-invite test suite, see Section 16.3 |
| `settings.py` | `notipa/settings.py` | Updated — see 11.4 |

### 11.2 Models

- **`User`** (custom, replaces Django's default) — shared by admins, teachers, and guardians. Email is optional; `phone_number` is the identifier that matters, since guardians are commonly invited via SMS/link with no email address at all (proposal Section 4.2). Which role a user holds is determined per-school, not on this record.
- **`School`** — the tenant. `country`, `default_language`, `timezone`, `tier` (free/paid — Track 1 vs Track 2), `academic_year_start_month`.
- **`SchoolMembership`** — links a `User` to a `School` with a role (admin/teacher/guardian). This is the data the permission-scoping layer (Section 13) filters on; the same person can hold different roles at different schools, or more than one role at the same school.
- **`SchoolClass`** — a class/section within a school (named `SchoolClass`, not `Class`, to avoid shadowing the Python keyword). Has a `homeroom_teacher` and optional `additional_teachers`, both scoped to `SchoolMembership` rows with role `teacher`.
- **`Student`** — belongs to a school and optionally a class.
- **`GuardianLink`** — through-model connecting guardians (`User`) to `Student`, with a `relationship` field (mother/father/grandparent/guardian/other) supporting multi-guardian households (proposal Section 4.1/4.2), and `is_primary_contact` for future SMS-fallback routing.
- **`Announcement`** / **`AnnouncementRead`** — school-wide or class-scoped announcement, with per-guardian read tracking.
- **`Homework`** — per-class, optional due date and file attachment.
- **`FeeNotice`** — informational fee due-date notice, Track 2 only, explicitly no payment processing (proposal Section 4.3).
- **`PermissionSlip`** / **`PermissionSlipResponse`** — event requiring guardian acknowledgement, tracked per student.
- **`StudentRecord`** — teacher-authored note or attached report, guardian-scoped by default (`visible_to_guardians`).

### 11.3 Design decisions worth knowing about

- **Row-level multi-tenancy.** Every tenant-scoped model carries a `school` foreign key (directly, or transitively via `student`/`school_class`), matching proposal Section 5's stated approach — shared database, not schema-per-tenant.
- **Roles are per-school, not global.** `SchoolMembership` rather than Django's built-in permission groups, because the same person can be a teacher at one school and a guardian at another (or both, at the same school).
- **UUID primary keys, on every model.** This was a deliberate change requested after the first version of this app was delivered — the original delivery used Django's default auto-incrementing integer IDs, then all primary keys were switched to UUIDs (`models.UUIDField(primary_key=True, default=uuid.uuid4)`, via a shared abstract `UUIDModel` base class) before any real data existed. Two reasons: non-enumerable IDs matter specifically because this app holds children's and guardians' data (a URL like `/students/104/` shouldn't let anyone guess `/students/105/`), and UUIDs avoid primary-key collisions if data ever needs to move between separate self-hosted instances down the line (proposal Section 8's Digital Public Good model).
- **File attachments use local storage for now.** `Homework.attachment` and `StudentRecord.attachment` are plain `FileField`s. Proposal Section 5 specifies S3-compatible object storage (DigitalOcean Spaces) for production; swapping the storage backend later is a `STORAGES['default']` change in settings, not a model change, so this was left as local storage until it's actually needed.

### 11.4 What changed in `settings.py` for the data model

```python
INSTALLED_APPS = [
    ...
    'core',
]

AUTH_USER_MODEL = 'core.User'
```

`AUTH_USER_MODEL` has to be set before the first migration exists — it can't be changed later without a rebuild — so this was the only correct moment to make that call.

Also added, to support the `FileField` attachments on `Homework` and `StudentRecord`:

```python
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
```

---

## 12. UI Shell (CSS + app frame + dashboard) — Delivered

Theo supplied a hand-rolled CSS design system (originally written for a different product, "KnowYourKit") to use as Notipa's visual base — paper-white/verified-green palette, Space Grotesk + IBM Plex type, a distinctive "tag with eyelet" chip motif for status, no CSS framework. This was integrated into the Django project as the actual frontend, not a mockup — Django templates throughout, no React, no SPA, per proposal Section 5.

### 12.1 Files delivered

| File | Location | Status |
|---|---|---|
| `app.css` | `core/static/core/css/app.css` | Delivered — Theo's CSS, rebranded from "KnowYourKit" to "Notipa" in the header comment only; all design tokens and component styles kept as supplied, except one tweak — see 12.4 |
| `base.html` | `templates/base.html` | Delivered — the app shell: sidebar with grouped navigation, topbar with breadcrumb + school switcher + logout, main content area, messages framework wired in (Section 13) |
| `dashboard.html` | `templates/core/dashboard.html` | Delivered — real page; renders live, school-scoped counts and a recent-announcements list, plus a "not linked to a school" state (Section 13/15) |
| `placeholder.html` | `templates/core/placeholder.html` | Delivered — reusable "not built yet" page for sections without real views yet |
| `urls.py` | `notipa/urls.py` | Updated — includes `core.urls` at the site root and `django.contrib.auth.urls` under `/accounts/` (Section 13) |

### 12.2 What's real vs. placeholder right now

- **Real:** the sidebar/topbar shell, the Dashboard (school-scoped), the login page, and — as of Sections 14/15 — Classes, Students, and (superusers only) School Setup.
- **Placeholder:** Announcements, Homework, Fee Notices, Permission Slips, and Settings still route to real URLs and render a proper "not built yet" page (the CSS's `.empty-state` pattern) rather than a 404. Each placeholder's message states which Phase 1 build-sequence item (Section 9) will replace it.

### 12.3 Sidebar navigation structure

- **Overview** — Dashboard
- **Communication** — Announcements, Homework, Fee Notices, Permission Slips
- **People** — Students, Classes
- **Admin** — Settings, and (superusers only) Set Up a School

### 12.4 One design tweak made after initial delivery

Theo's original CSS used `--line-strong` (a near-black, `#12181A`) at 2px for the sidebar's right border and the topbar's bottom border — visually heavier than intended once seen in the running app. Both were changed to `--line` (the soft grey already used for cards and tables, `#DBDFD5`) at 1.5px, so the shell's structural edges recede rather than frame the layout. `--line-strong` was deliberately left in place for the secondary button border and the modal border. Flagged in case Theo wants those softened too, or wants the original restored — both are one-line CSS edits.

### 12.5 Validation performed

Every route rendered through Django's test client against a disposable database copy before being written into the real project; `manage.py collectstatic` run against the real production storage backend (`WhiteNoiseMiddleware`/`CompressedManifestStaticFilesStorage`) with no errors.

---

## 13. Authentication + Queryset-Level School Scoping — Delivered

This is the proposal's "part not to rush" (Section 1) made concrete. Two things had to exist together, because scoping only means something once requests carry an authenticated user: standard login, and a middleware that resolves *which* school a logged-in user is acting as.

### 13.1 Files delivered

| File | Location | Status |
|---|---|---|
| `middleware.py` | `core/middleware.py` | Delivered — `ActiveSchoolMiddleware` |
| `permissions.py` | `core/permissions.py` | Delivered — `role_required()`, `superuser_required`, `scope_to_school()` |
| `login.html` | `templates/registration/login.html` | Delivered — standalone auth page, no sidebar (nothing to show one for yet) |
| `no_access.html` | `templates/core/no_access.html` | Delivered — 403 page for role-restricted sections |
| `settings.py` | `notipa/settings.py` | Updated — see 13.3 |
| `urls.py` | `notipa/urls.py`, `core/urls.py` | Updated — `/accounts/login/`, `/accounts/logout/`, `core:switch_school` |

### 13.2 How it works

`ActiveSchoolMiddleware` runs after `AuthenticationMiddleware` and, for an authenticated user, looks up their active (`is_active=True`) `SchoolMembership` rows and attaches `request.school`, `request.membership`, and `request.memberships` to every request. Which school is "active" for a user with more than one membership is stored in `request.session['active_school_id']` and changed via a school-switcher `<select>` in the topbar (only shown when a user has more than one membership) — `core:switch_school` only ever honours a school ID that already appears in the user's own `request.memberships`, never trusting the raw POSTed value, so it can't be used to view another school's data by guessing IDs.

Every tenant-scoped view then filters through `scope_to_school(queryset, request)` rather than querying models directly — this is the actual enforcement, not just a convention. `role_required(*roles)` is a decorator for restricting a view to specific `SchoolMembership.Role` values on the user's *active* membership; `superuser_required` is the equivalent for platform-operator actions (currently just creating a new School — Section 15).

Every existing view (`dashboard`, `placeholder`, `classes_list`/`class_new`, `students_list`/`student_new`, `school_setup`) now requires login. `classes_list`/`class_new`/`students_list`/`student_new` additionally require the active membership to be admin or teacher — guardians get a real 403, not a hidden sidebar link.

### 13.3 What changed in `settings.py` for auth

```python
MIDDLEWARE = [
    ...
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    ...
    'core.middleware.ActiveSchoolMiddleware',   # after AuthenticationMiddleware
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'core:dashboard'
LOGOUT_REDIRECT_URL = 'login'
```

This is standard Django username/password login — not the SMS-based passwordless guardian flow from proposal Section 4.2, which is separate, later work (Section 10). This covers the login/logout plumbing every role needs regardless of how guardians eventually authenticate.

### 13.4 Validation performed

Built two schools, two admin users, a teacher, a guardian, and a user with no membership at all, then confirmed via Django's test client against a disposable database:

- Unauthenticated requests to the dashboard and to Students both redirect to `/accounts/login/`.
- Each admin's dashboard shows only their own school's announcement — never the other school's.
- A user with no `SchoolMembership` sees the explicit "not linked to a school" state, not someone else's data and not an error.
- `scope_to_school()` returns the correct count for each school independently.
- Logout works and immediately re-locks the dashboard.
- A user with memberships at two schools sees a working school switcher; switching updates the active school; **switching to a school ID they don't belong to is silently ignored** (the adversarial case that actually matters here).
- A guardian hitting `/classes/` or `/students/` directly gets a real 403, not a 404 or a silently empty page.

---

## 14. Classes & Students Onboarding Views — Delivered

The first real content behind the "People" sidebar section, and the first views built on top of the Section 13 scoping layer — chosen first because Announcements/Homework/Fee Notices/Permission Slips all need classes and a roster to exist before they mean anything.

### 14.1 Files delivered

| File | Location | Status |
|---|---|---|
| `forms.py` | `core/forms.py` | Delivered — `SchoolClassForm`, `StudentForm` (plus `SchoolForm`, see Section 15) |
| `classes_list.html` | `templates/core/classes_list.html` | Delivered — table of classes, scoped to `request.school` |
| `class_form.html` | `templates/core/class_form.html` | Delivered — create form |
| `students_list.html` | `templates/core/students_list.html` | Delivered — table of students, scoped to `request.school` |
| `student_form.html` | `templates/core/student_form.html` | Delivered — create form |
| `views.py` | `core/views.py` | Updated — `classes_list`, `class_new`, `students_list`, `student_new` |
| `urls.py` | `core/urls.py` | Updated — `core:classes`, `core:class_new`, `core:students`, `core:student_new` replace the old placeholder routes |

### 14.2 Design decisions worth knowing about

- **Both forms take `school` as an explicit constructor argument, never from POST data.** `SchoolClassForm(request.POST, school=request.school)` — the school a new class or student belongs to is always the requesting user's active school, never something a client could override by tampering with form fields.
- **Dropdowns are pre-filtered to the same school.** `SchoolClassForm`'s `homeroom_teacher` field only lists `SchoolMembership` rows with role `teacher` at `request.school`; `StudentForm`'s `school_class` field only lists `SchoolClass` rows at `request.school`. This was specifically tested: an admin at School B POSTing a School A teacher's ID as `homeroom_teacher` gets a rejected form, not a saved record.
- **Restricted to admin/teacher via `role_required`.** Guardians have no product reason to browse a full roster.
- **Success/error feedback uses Django's messages framework**, now wired into `base.html` and rendered with the CSS's `.alert` component — "Class created," "Student added," etc.

### 14.3 Validation performed

Full end-to-end flow via Django's test client against a disposable database: admin logs in, creates a class, confirms it's scoped to their school; the homeroom-teacher dropdown only shows their own school's teachers; creates a student assigned to that class; both list pages show the new records; a second admin at a second school sees none of it; a cross-school homeroom-teacher assignment attempt is rejected by form validation; a guardian is blocked with a 403 from both pages.

---

## 15. In-App School Setup for Superusers — Delivered

**The problem this fixes:** a freshly created superuser (via `createsuperuser`) had no `School` and no `SchoolMembership` — the only way to create the first school was `/admin/`, which is exactly the "creating records directly in the database" experience Theo flagged as not acceptable for day-to-day use. Django admin remains available for edge cases, but it's no longer required for the basic bootstrap step.

### 15.1 Files delivered

| File | Location | Status |
|---|---|---|
| `school_setup.html` | `templates/core/school_setup.html` | Delivered — form page |
| `forms.py` | `core/forms.py` | Updated — added `SchoolForm` |
| `views.py` | `core/views.py` | Updated — added `school_setup` |
| `urls.py` | `core/urls.py` | Updated — added `core:school_setup` |
| `permissions.py` | `core/permissions.py` | Updated — added `superuser_required` |
| `base.html`, `dashboard.html` | `templates/base.html`, `templates/core/dashboard.html` | Updated — sidebar entry point (superusers only) and the "not linked to a school" empty state now link here instead of `/admin/` |

### 15.2 How it works

`core:school_setup` is restricted to `request.user.is_superuser` via the new `superuser_required` decorator — creating a new tenant is a platform-operator action, one level above any school-scoped role, not something a regular school admin should be able to trigger. Submitting the form creates the `School` **and** an admin `SchoolMembership` for the submitting superuser in one step, then sets that school as their active school in the session — so they land on a working, scoped dashboard immediately rather than having to separately go create a `SchoolMembership` by hand afterward. The entry point appears twice: as a call-to-action on the dashboard's empty state (for the very first school) and as a permanent sidebar link under Admin (for adding further schools later).

### 15.3 Validation performed

Fresh superuser with no school sees the empty state and the "Set Up a School" button; a regular (non-superuser) orphan account does not see that button and gets a real 403 if they hit the URL directly; submitting the form creates the school, auto-creates the admin membership, and the very next dashboard load shows the real scoped view with no manual step in between; the sidebar link persists afterward for creating a second school.

---

## 16. Teacher/Admin Invite Onboarding — Delivered

**The problem this fixes:** classes reference a `homeroom_teacher`, but until now there was no in-app way to create the `SchoolMembership` rows a teacher needs — only `/admin/` could do it (Section 10). This closes that gap and fixes the build-sequence ordering issue Theo flagged: a school needs to be able to add teachers *before* classes are fully usable, not after.

### 16.1 Files delivered

| File | Location | Status |
|---|---|---|
| `forms.py` | `core/forms.py` | Updated — added `TeacherForm` |
| `views.py` | `core/views.py` | Updated — added `teachers_list`, `teacher_new` |
| `urls.py` | `core/urls.py` | Updated — added `core:teachers`, `core:teacher_new` |
| `teachers_list.html` | `templates/core/teachers_list.html` | Delivered — table of teachers/admins, scoped to `request.school` |
| `teacher_form.html` | `templates/core/teacher_form.html` | Delivered — create form |
| `base.html` | `templates/base.html` | Updated — "Teachers" sidebar link under People, visible to admins/superusers only |
| `tests.py` | `core/tests.py` | Delivered — 8 tests covering this flow (previously an empty stub) |

### 16.2 Design decisions worth knowing about

- **Admin-only, not admin/teacher.** Unlike Classes/Students (`role_required(ADMIN, TEACHER)`), `teachers_list`/`teacher_new` use `role_required(ADMIN)` only. Adding staff is a higher-privilege action than adding a student or class — a teacher shouldn't be able to grant themselves or anyone else admin access to the school.
- **`TeacherForm` creates a `User` and a `SchoolMembership` in one step**, scoped to `school` passed explicitly by the view — same "never trust a school id from POST" pattern as `SchoolClassForm`/`StudentForm` (Section 14.2). The role (teacher or admin) is a form field, so this one flow also covers adding a second admin.
- **No invite email/SMS infrastructure exists yet** (Section 10), so the admin sets an initial password directly in the form and passes it to the new teacher out of band — a deliberate, temporary trade-off called out in both the form's docstring and the template copy, not an oversight.
- **Duplicate usernames are rejected** with a message pointing at `/admin/` for the edge case of attaching an *existing* user to a second school — reusing an account across schools is out of scope for this first pass.

### 16.3 Validation performed

8 automated tests in `core/tests.py` (`TeacherInviteTests`), run against a disposable in-memory database: an admin can create a teacher and the new account can log in immediately; mismatched passwords and duplicate usernames are both rejected without creating a record; a teacher (non-admin) gets a 403 from both the list and create views; an admin at one school cannot see another school's staff in the list view; an anonymous request is redirected to login. All 8 pass.

---

## 17. Edit & Soft-Delete for Teachers, Classes, and Students — Delivered

**The problem this fixes:** Teachers, Classes, and Students could be created but not edited or removed — a typo in a student's name, a teacher who leaves, or a class that's discontinued had no in-app fix short of `/admin/`. This adds edit views for all three, plus soft delete (archive/revoke) so records disappear from day-to-day use without erasing history — a hard delete on a `Student` would cascade through `GuardianLink`, `FeeNotice`, `PermissionSlipResponse`, and `StudentRecord` (Section 11.2), which is exactly the kind of accidental data loss this avoids.

### 17.1 Files delivered

| File | Location | Status |
|---|---|---|
| `forms.py` | `core/forms.py` | Updated — added `TeacherEditForm`; `SchoolClassForm`/`StudentForm` reused as-is for edits (they already accept an `instance`) |
| `views.py` | `core/views.py` | Updated — added `teacher_edit`/`teacher_revoke`/`teacher_restore`, `class_edit`/`class_archive`/`class_restore`, `student_edit`/`student_archive`/`student_restore`; list views now filter to `is_active=True` by default with a `?show=archived` toggle |
| `urls.py` | `core/urls.py` | Updated — `<uuid:pk>/edit/`, `.../archive/` (or `.../revoke/` for teachers), `.../restore/` routes for all three |
| `class_form.html`, `student_form.html` | `templates/core/` | Updated — now double as edit forms (title/button text adapts) and show an Archive/Restore panel when editing an existing record |
| `teacher_edit_form.html` | `templates/core/teacher_edit_form.html` | Delivered — separate from the create form; no password fields, username shown read-only |
| `classes_list.html`, `students_list.html`, `teachers_list.html` | `templates/core/` | Updated — Edit links, a Status column, and (Classes/Students) a "Show archived" toggle |
| `tests.py` | `core/tests.py` | Updated — 11 new tests (`TeacherEditRevokeTests`, `ClassEditArchiveTests`, `StudentEditArchiveTests`), 19 total |

### 17.2 Design decisions worth knowing about

- **Soft delete reuses the `is_active` field every model already had** (`Student.is_active`, `SchoolClass.is_active`, `SchoolMembership.is_active`) rather than adding a new "deleted" flag or `deleted_at` timestamp — that field already existed specifically for this ("revoke access without deleting history" — Section 11.2), it was just never wired up to a button. No migration was needed.
- **Teacher soft-delete is called "revoke," not "archive,"** matching the existing `SchoolMembership.is_active` help text and the language the dashboard/handover plan already used for it. Classes and Students use "archive"/"restore" instead, since "revoke" doesn't make sense for them.
- **Archived/revoked records are never truly hidden.** `class_edit`/`student_edit`/`teacher_edit` can still be opened by URL (scoped to the same school) to restore them, and revoked teachers still show up in `teachers_list` with a Status column — same reasoning as Section 16.2's "no dead ends." Classes/Students default the list view to active-only with a one-click `?show=archived` toggle instead of showing everything at once, since teachers/admins browse those rosters far more often day-to-day.
- **Editing a teacher never touches username or password.** `TeacherEditForm` is a separate form from `TeacherForm`, not the same form with conditional fields — changing a login identifier or resetting a password are both out of scope for this pass (Section 17's docstring in `forms.py` explains why).
- **A teacher's role can be changed in place** (teacher ↔ admin) via the same edit form, with a validation check that rejects the change if the same user already holds a `SchoolMembership` with the target role at that school (the `unique_user_school_role` constraint would otherwise raise an `IntegrityError` instead of a clean form error).
- **Archiving a class or student never cascades.** A `SchoolClass` going inactive doesn't touch its existing `Student.school_class` references (already `on_delete=SET_NULL`, unaffected by a soft delete) or its `Announcement`/`Homework`/`PermissionSlip` history; it only drops out of the default list and out of `SchoolClassForm`'s/`StudentForm`'s active-only dropdowns.

### 17.3 Validation performed

11 new automated tests, run alongside the existing 8 (19 total, all passing) against a disposable in-memory database: an admin can edit a teacher's name/contact/role; changing a teacher's role to one they already hold at that school is rejected with a form error, not a database exception; an admin at one school gets a 404 (not a 403) editing another school's teacher/class/student, so a guessed URL doesn't even confirm the record exists; revoking a teacher's access is confirmed by that account then getting a 403 from `teachers_list`, and restoring reverses it; archiving a class removes it from the default `classes_list` but it reappears with `?show=archived`, and an archived class is confirmed absent from `StudentForm`'s `school_class` dropdown; the same edit/archive/restore/cross-school-404 pattern is confirmed for Students.

---

## 18. Class Detail View (Teacher + Roster) — Delivered

**The problem this fixes:** `classes_list` could show a row per class (name, year, homeroom teacher, student count) but nothing about *which* students, and no way to see a class's homeroom teacher and any co-teachers together with its full roster on one page — you had to cross-reference the Students list's Class column by eye.

### 18.1 Files delivered

| File | Location | Status |
|---|---|---|
| `views.py` | `core/views.py` | Updated — added `class_detail` |
| `urls.py` | `core/urls.py` | Updated — added `core:class_detail` at `classes/<uuid:pk>/` |
| `class_detail.html` | `templates/core/class_detail.html` | Delivered — homeroom + co-teachers, then the class roster with the same archived-toggle pattern as `students_list` |
| `classes_list.html`, `students_list.html` | `templates/core/` | Updated — class names are now links to `class_detail`; `classes_list` also gained a "View" button alongside "Edit" |
| `tests.py` | `core/tests.py` | Updated — 4 new tests (`ClassDetailTests`), 23 total |

### 18.2 Design decisions worth knowing about

- **Reuses `_get_class_or_404`'s scoping, not a new lookup.** Same "never trust a URL id without checking it belongs to `request.school`" pattern as every other detail/edit view here — a 404, not a 403, for another school's class.
- **Roster defaults to active students, with the same `?show=archived` toggle** used on `students_list` and the other list views, rather than always showing everyone — an archived student showing up unexplained in a class roster would be more confusing here than in the main Students list, where the Status column gives it context immediately.
- **Restricted to admin/teacher, same as `classes_list`/`students_list`** — guardians have no product reason to browse a class roster or teacher list; `role_required` (not just a hidden link) enforces it, confirmed by a 403 test.

### 18.3 Validation performed

4 new automated tests (23 total, all passing): the page shows the homeroom teacher's name and only active students by default; `?show=archived` brings the archived ones back in; an admin at one school gets a 404 for another school's class; a guardian gets a 403.

---

## 19. Guardian Setup — Delivered

**The problem this fixes:** `Student.guardians` and `GuardianLink` (Section 11.2) existed in the schema from the start, but there was no way to create a guardian *account* in-app — only `/admin/`, and even that couldn't create the SchoolMembership a guardian needs to be scoped to a school. This is the prerequisite for the next step (actually linking a guardian to a Student via `GuardianLink`), the same way Section 16's teacher invite flow was the prerequisite for classes having real homeroom teachers.

### 19.1 Files delivered

| File | Location | Status |
|---|---|---|
| `forms.py` | `core/forms.py` | Updated — added `GuardianForm`, `GuardianEditForm` |
| `views.py` | `core/views.py` | Updated — added `guardians_list`, `guardian_new`, `guardian_edit`, `guardian_revoke`, `guardian_restore` |
| `urls.py` | `core/urls.py` | Updated — added `core:guardians`, `core:guardian_new`, `core:guardian_edit`, `core:guardian_revoke`, `core:guardian_restore` |
| `guardians_list.html` | `templates/core/guardians_list.html` | Delivered — table of guardians, scoped to `request.school`, with a Linked Students count column (0 for everyone until `GuardianLink` creation is built) |
| `guardian_form.html`, `guardian_edit_form.html` | `templates/core/` | Delivered — create and edit forms, following the Teacher forms' shape |
| `base.html` | `templates/base.html` | Updated — "Guardians" sidebar link under People, visible to admin and teacher (not gated to admin-only, unlike Teachers) |
| `tests.py` | `core/tests.py` | Updated — 10 new tests (`GuardianInviteTests`, `GuardianEditRevokeTests`), 33 total |

### 19.2 Design decisions worth knowing about

- **Admin *or* teacher, unlike Teachers (admin-only).** Adding a guardian is routine roster work done whenever a family enrolls — closer to adding a Student than to adding staff — so it uses `role_required(ADMIN, TEACHER)`, the same restriction as Classes/Students, not the tighter admin-only restriction on `teacher_new`/`teacher_edit`.
- **Still a username/password account, not the SMS-based passwordless invite the proposal describes (Section 4.2).** That flow needs SMS-sending infrastructure this project doesn't have yet (Section 10) — same deliberate, temporary trade-off already made for `TeacherForm`, called out in `GuardianForm`'s docstring. `phone_number` is required here (optional on `TeacherForm`) because it's the field the eventual SMS flow will actually key off of, worth capturing correctly from day one.
- **Soft delete reuses the same `SchoolMembership.is_active` flag**, called "revoke"/"restore" like Teachers (not "archive," which is what Classes/Students use) — same reasoning as Section 17.2: it already existed for exactly this, no migration needed.
- **No role field on `GuardianEditForm`.** Unlike `TeacherEditForm`, a guardian membership isn't promoted to teacher/admin through this form — that would be a deliberate, separate action via the Teacher invite flow if it were ever actually needed, not a checkbox buried in guardian editing.
- **This is a foundation step, not the full guardian experience.** Creating a `GuardianLink` (attaching a guardian to a specific `Student`, with a relationship type and primary-contact flag) is the next piece of work — see Section 20 below — and guardian-facing views (a read-only Announcements feed, etc.) don't exist yet either.

### 19.3 Validation performed

10 new automated tests (33 total, all passing): an admin can create a guardian and the new account can log in immediately; a teacher (not just an admin) can also create one; a missing phone number and a duplicate username are both rejected without creating a record; a guardian account itself gets a 403 trying to create another guardian; an admin only sees their own school's guardians in the list; editing updates name/phone/email; an admin at one school gets a 404 editing another school's guardian membership; revoke/restore round-trips correctly.

---

## 20. Guardian–Student Linking — Delivered

**The problem this fixes:** Section 19 made it possible to create a guardian *account*, but nothing connected one to a specific student — `core.models.GuardianLink` (relationship type, primary-contact flag) existed in the schema from day one (Section 11.2) but had no UI. This also introduces the first single-student page, `student_detail`, the same role `class_detail` (Section 18) fills for classes. It also covers the reverse direction: linking a student to a guardian starting from the *guardian's* own page, since Student.guardians and User.students are both many-to-many (a student can have several guardians, a guardian can have several students, e.g. siblings) and only being able to start from the student side made adding several children to one guardian more tedious than it needed to be.

### 20.1 Files delivered

| File | Location | Status |
|---|---|---|
| `forms.py` | `core/forms.py` | Updated — added `GuardianLinkForm` (student → guardian) and `StudentLinkForm` (guardian → student), both wrapping the same `GuardianLink` model |
| `views.py` | `core/views.py` | Updated — added `student_detail`, `guardian_link_add`, `guardian_link_remove`, `guardian_detail`, `student_link_add`, `student_link_remove` |
| `urls.py` | `core/urls.py` | Updated — `core:student_detail` at `students/<uuid:pk>/` with `core:guardian_link_add`/`core:guardian_link_remove` nested under it; `core:guardian_detail` at `guardians/<uuid:pk>/` with `core:student_link_add`/`core:student_link_remove` nested under it |
| `student_detail.html` | `templates/core/student_detail.html` | Delivered — student summary, linked-guardians table (relationship, contact, primary flag, a Remove button per row, guardian name links to `guardian_detail`), and a form to link another guardian |
| `guardian_detail.html` | `templates/core/guardian_detail.html` | Delivered — the mirror: guardian summary, linked-students table (class, relationship, primary flag, Remove button, student name links to `student_detail`), and a form to link another student |
| `students_list.html`, `class_detail.html`, `guardians_list.html` | `templates/core/` | Updated — student and guardian names are now links to their respective detail pages |
| `tests.py` | `core/tests.py` | Updated — 14 new tests (`GuardianLinkTests`, `StudentLinkTests`), 47 total |

### 20.2 Design decisions worth knowing about

- **Both directions write the same `GuardianLink` row** — `GuardianLinkForm` and `StudentLinkForm` are two different forms (different field the user picks from, different dropdown scoping) over the same model, not two different kinds of link. `test_linking_from_either_side_produces_same_record` confirms a link made via `student_link_add` shows up correctly when viewed from `student_detail`.
- **The guardian dropdown (student → guardian direction) is scoped three ways at once**: same school as the student, an active GUARDIAN membership (a revoked guardian can't be newly linked), and not already linked to this student. **The student dropdown (guardian → student direction) mirrors this**: same school as the guardian, not archived (`is_active=True` — linking a guardian to an archived student is almost always a mistake), and not already linked to this guardian. Both are enforced in the form's queryset, not just template/JS, so a tampered POST is still caught by `clean()` and rejected with a form error rather than a database `IntegrityError` (the model already has a `unique_guardian_student` constraint).
- **Removing a link deletes the `GuardianLink` row outright — not a soft delete.** Unlike Student/Class/Teacher/Guardian, `GuardianLink` is a pure join record with no independent identity worth preserving; deleting it doesn't touch the guardian's account, the student's record, or either one's history. `is_active` archiving is for the *entities* (people, classes) this project cares about not losing; this is just the connection between two of them. Removal is scoped through the parent either way: `guardian_link_remove` resolves the student first then filters the link to it; `student_link_remove` resolves the guardian membership first then filters the link to that guardian — so pairing a real link id with the wrong (inaccessible, or simply unrelated) parent id 404s instead of removing anything, confirmed by a dedicated test on each side.
- **Only one primary contact per student, enforced in both forms' `save()`.** Marking a new link as primary demotes any existing primary link for that student, regardless of which side the link was created from — the exact scenario the flag exists to prevent (`GuardianLink` docstring, Section 11.2) is two guardians both flagged primary with no way to tell who gets SMS fallback first.
- **Every list that shows an entity now links to that entity's detail page**: `students_list`/`class_detail`'s roster link to `student_detail`; `guardians_list` and `student_detail`'s guardian table link to `guardian_detail`; `classes_list`/`students_list` already linked to `class_detail` (Section 18). Cross-navigation between a student and their guardians works both ways now.

### 20.3 Validation performed

14 new automated tests (47 total, all passing) split across `GuardianLinkTests` (student → guardian direction) and `StudentLinkTests` (guardian → student direction, the newly added half): the guardian dropdown excludes another school's guardians, a revoked guardian, and guardians already linked to this student; the student dropdown excludes another school's students, archived students, and students already linked to this guardian; linking from either direction sets the relationship and primary-contact flag correctly and is confirmed visible from the *other* entity's detail page; marking a second link primary demotes the first, from either direction; a bypassed POST for an already-linked pair is rejected without creating a duplicate row, from either direction; an admin at one school gets a 404 viewing another school's student or guardian detail page; removing a link deletes only the link (the guardian's `User`/`SchoolMembership` and the student's own record are confirmed to still exist); pairing a real link id with an unrelated parent id in either removal URL 404s rather than deleting anything.

---

## 21. In-App User Manual — Delivered

**The problem this fixes:** everything built so far (Sections 14–20) was only documented in this handover plan — a file a school admin or teacher using the actual product would never see. There was no answer inside the app itself to "how do I add a teacher" or "how do I link a guardian to a student."

### 21.1 Files delivered

| File | Location | Status |
|---|---|---|
| `views.py` | `core/views.py` | Updated — added `wiki` |
| `urls.py` | `core/urls.py` | Updated — added `core:wiki` at `wiki/` |
| `wiki.html` | `templates/core/wiki.html` | Delivered — static, single-page manual: roles/scoping, school setup, Teachers, Guardians, Classes, Students, linking guardians and students (both directions), and the archive/revoke soft-delete pattern used throughout, with a jump-to-section table of contents |
| `base.html` | `templates/base.html` | Updated — new "Help" sidebar section with a "User Manual" link |
| `tests.py` | `core/tests.py` | Updated — 5 new tests (`WikiTests`), 52 total |

### 21.2 Design decisions worth knowing about

- **Static content, not a database-backed wiki.** The request was for "a wiki or user manual" — given how small and stable this app's documented surface still is, a single template is far less to maintain than a CMS-style editable-pages feature would be, and it costs nothing to convert later if the content outgrows one page. Worth revisiting if the manual grows to the point where non-developers need to edit it themselves.
- **No role restriction, unlike every other real view in this app.** `wiki` is just `@login_required` — no `role_required`, no school-scoping. An admin reading how Guardians work and a teacher reading how Teachers work are both fine; there's nothing here that differs per school or needs hiding from anyone. It's also reachable by a user with no active school membership at all (an "orphan" account), confirmed by a dedicated test, since that's exactly the person most likely to need it.
- **Written for the person using the product, not the person building it.** The content describes what to click and what each field means, in plain language — it deliberately doesn't mention model names, view names, or anything from this handover plan's own vocabulary (`SchoolMembership`, `role_required`, etc.), since that's internal implementation detail with no bearing on how to use the app.
- **The "editing and archiving" section explains the soft-delete pattern once, generically**, rather than repeating the same archive/restore explanation four times across Teachers/Guardians/Classes/Students — each of those sections links back to it implicitly by using consistent terminology ("Archive," "Revoke Access," "Restore").

### 21.3 Validation performed

5 new automated tests (52 total, all passing): an admin, a teacher, a guardian, and a user with no school membership at all can each load the page successfully; an anonymous request is redirected to login.

---

## 22. Guardian-Facing Views — Delivered

**The problem this fixes:** a guardian account could log in (Section 19) and be linked to a student (Section 20), but had nowhere to actually go — the ordinary dashboard's school-wide counts aren't theirs to see, and every People page (Students/Classes/Teachers/Guardians) is admin/teacher-only, so a guardian clicking any of them got a 403. This gives a guardian something to land on: their own children, and a read-only page per child.

### 22.1 Files delivered

| File | Location | Status |
|---|---|---|
| `views.py` | `core/views.py` | Updated — `dashboard` now branches to `_guardian_dashboard` for guardian accounts; added `my_child_detail` |
| `urls.py` | `core/urls.py` | Updated — added `core:my_child_detail` at `my-children/<uuid:pk>/` |
| `guardian_dashboard.html` | `templates/core/guardian_dashboard.html` | Delivered — a card list of the guardian's own linked children (class, homeroom teacher, a "Primary contact" tag where applicable), each linking to `my_child_detail` |
| `my_child_detail.html` | `templates/core/my_child_detail.html` | Delivered — read-only: the child's class and homeroom teacher's name/email, plus an honest "coming soon" note for Announcements/Homework/Fee Notices/Permission Slips, none of which are real views yet |
| `base.html` | `templates/base.html` | Updated — the entire "People" sidebar section (Students/Classes/Guardians/Teachers) and the entire "Admin" sidebar section (Settings, Set Up a School) are now hidden for anyone who isn't admin/teacher (People) or admin/superuser (Admin) — previously only the Teachers link was gated |
| `tests.py` | `core/tests.py` | Updated — 12 new tests (`GuardianDashboardTests`), 64 total |

### 22.2 Design decisions worth knowing about

- **Look but don't touch.** `my_child_detail` is deliberately read-only — no edit, no archive, no adding/removing guardian links from this page. `student_detail` (the admin/teacher equivalent) already does all of that; a guardian doesn't get to unlink themselves or another guardian from their own child, or edit the child's record, from this view or any other one right now.
- **Scoped through the `GuardianLink`, not just the school.** `role_required(GUARDIAN)` alone would let any guardian at a school view *any* student at that school by guessing an id — `my_child_detail`'s lookup filters on `guardian=request.user` as well, so a guardian can only reach children they're actually linked to. Confirmed by a test where one guardian gets a 404 trying to view another guardian's child, despite being at the same school.
- **Deliberately thin, because there's not much real data to show yet.** Announcements, Homework, Fee Notices, and Permission Slips are all still placeholder pages (Section 10) — there's no guardian-relevant content behind them yet. Rather than build a guardian dashboard that promises more than exists, this only surfaces what's actually real right now: which children are linked to the guardian, and each child's class/teacher — plus an explicit "coming soon" note instead of silently omitting the rest.
- **The People sidebar section is now hidden for guardians entirely**, not just individually 403'd per page. Previously only the Teachers link was gated (admin-only); Students/Classes/Guardians were visible to everyone including guardians, who'd hit a 403 clicking any of them. Hiding the whole section is a small but real polish fix — no more dead links in the guardian's own sidebar.
- **The Admin sidebar section (Settings, Set Up a School) is now hidden for anyone who isn't an admin or superuser** — a follow-up fix within this same section, caught immediately after the first pass shipped: guardians (and teachers) could still see "Settings" even though it's an admin-level placeholder, and the section label itself read "Admin" while showing to non-admins. "Set Up a School" keeps its own superuser check nested inside, so a superuser with no admin membership at any school still sees it.
- **The regular admin/teacher dashboard is unchanged.** `dashboard`'s guardian branch only triggers for `request.membership.role == GUARDIAN`; admins and teachers still see the same school-wide counts and recent-announcements list as before, confirmed by a regression test.

### 22.3 Validation performed

12 new automated tests (64 total, all passing): an admin still sees the ordinary dashboard (not the guardian one); a guardian sees only their own linked children on their dashboard, not another guardian's child at the same school; a guardian with no linked children yet sees an explicit empty state rather than a blank page; a guardian can open their own child's detail page and see the homeroom teacher's name; a guardian gets a 404 (not a 403) trying to view a child they aren't linked to; an admin gets a 403 trying to use the guardian-only `my_child_detail` view even for a student they can otherwise see; the People sidebar section is confirmed absent from a guardian's rendered page and present on an admin's; the Admin sidebar section is confirmed absent for both a guardian and a teacher, present for an admin, and "Set Up a School" specifically still shows for a superuser account with no school membership at all.

---

## 23. Announcements — Delivered

**The problem this fixes:** Announcements was Phase 1 build sequence item 4 (Section 9) and had a real model (`Announcement`/`AnnouncementRead`, Section 11.2) since the very beginning, but only a placeholder page — no way to actually write, publish, or read one. It's also the first feature guardians get real content in, turning `my_child_detail`'s "coming soon" note (Section 22) into something real.

### 23.1 Files delivered

| File | Location | Status |
|---|---|---|
| `forms.py` | `core/forms.py` | Updated — added `AnnouncementForm` |
| `views.py` | `core/views.py` | Updated — added `announcements_list` (dispatches by role, same pattern as `dashboard`), `announcement_new`, `announcement_edit`, `announcement_publish`, `announcement_unpublish`, `announcement_delete`, `_guardian_announcements`, `announcement_mark_read`, `_relevant_class_ids_for_guardian`; `my_child_detail` now also fetches that child's recent announcements |
| `urls.py` | `core/urls.py` | Updated — the old `announcements` placeholder route now points at `announcements_list`; added `announcement_new`/`announcement_edit`/`announcement_publish`/`announcement_unpublish`/`announcement_delete`/`announcement_mark_read` |
| `announcements_list.html` | `templates/core/announcements_list.html` | Delivered — admin/teacher view: every announcement (drafts included) with Edit/Publish-or-Unpublish/Delete per row |
| `announcement_form.html` | `templates/core/announcement_form.html` | Delivered — create/edit form, doubles as both since `AnnouncementForm` accepts an `instance` |
| `guardian_announcements.html` | `templates/core/guardian_announcements.html` | Delivered — guardian view: published, relevant announcements only, with a "Mark as read" button per unread one |
| `my_child_detail.html` | `templates/core/my_child_detail.html` | Updated — replaced the announcements portion of the old "coming soon" note with a real Recent Announcements list for that specific child |
| `wiki.html` | `templates/core/wiki.html` | Updated — new "Announcements" section, and "What a guardian sees" updated to describe the real guardian Announcements page instead of listing it as not-yet-built |
| `tests.py` | `core/tests.py` | Updated — 14 new tests (`AnnouncementCRUDTests`, `GuardianAnnouncementVisibilityTests`), 78 total |

### 23.2 Design decisions worth knowing about

- **Draft/publish is a separate action from saving, on purpose.** `announcement_new`/`announcement_edit` never touch `published_at` — only the dedicated `announcement_publish`/`announcement_unpublish` views do. Writing and editing a post always lands back as (or stays) a draft; making it visible to guardians is a deliberate second step, so a half-finished draft can't accidentally reach families because of a single misclick on a form that both saves and publishes at once.
- **No soft delete here, unlike everywhere else in this app.** `Announcement` has no `is_active` field, and unlike a Teacher, Guardian, Class, or Student, nothing else in the schema holds a reference to an announcement worth protecting — deleting one also cascades to its `AnnouncementRead` rows, and that's fine, a read receipt for a post that no longer exists doesn't need to stick around. `announcement_delete` is a real, permanent delete, with a confirm prompt in the template. Worth knowing about if that assumption ever changes (e.g. if announcements start getting referenced elsewhere).
- **`announcements_list` (the single URL, `core:announcements`) branches by role**, same pattern as `dashboard` (Section 22): admin/teacher get every announcement including drafts with management controls; a guardian gets `_guardian_announcements` instead — published only, filtered to school-wide plus whichever classes their own linked children are actually in. Two different templates behind one URL name, so every "View all" / sidebar link just works regardless of who clicks it.
- **A guardian's visibility rule is re-derived, not trusted, everywhere it matters.** `_relevant_class_ids_for_guardian` (looking up which `SchoolClass` ids the guardian's own children belong to) is called fresh in both `_guardian_announcements` and `announcement_mark_read` — the mark-read view doesn't just trust that a posted announcement id was one the guardian was actually shown; it re-applies the same published/school-wide-or-my-class filter before allowing the read receipt, so a guardian can't mark an unrelated class's post (or an unpublished draft) as read by guessing its id in a POST.
- **Class-scoped announcements use the class's guardians, not the whole school's.** A guardian whose child is in Grade 5 never sees a Grade 4-only announcement, confirmed by a dedicated test with two classes and two announcements.

### 23.3 Validation performed

14 new automated tests (78 total, all passing): an admin/teacher creating an announcement always saves it as a draft (`published_at` is `None`) with `author` set correctly; a guardian gets a 403 trying to create one; publish/unpublish round-trips `published_at`; editing and deleting both work, and an admin at one school gets a 404 trying to edit another school's announcement; the staff list shows drafts. On the guardian side: a guardian sees exactly the school-wide-plus-own-class announcements and nothing else (confirmed against a second class's announcement they should *not* see); drafts never appear; marking a visible announcement as read creates the `AnnouncementRead` row; marking an unrelated class's announcement or a draft as read both 404 instead of succeeding; `my_child_detail` shows the same filtered set for that specific child.

---

## 24. Immediate Next Steps

1. **Apply the migration for real.** The `0001_initial` migration has been validated against disposable databases repeatedly but has not yet touched your actual dev SQLite file:
   ```
   docker compose up --build
   docker compose exec web python manage.py migrate
   docker compose exec web python manage.py createsuperuser
   ```
   Then log in at `/accounts/login/`, use **Set Up a School** (Section 15) to create your first school, and confirm the dashboard, Classes (including the class detail view), Students (including the student detail view and guardian linking from both directions), Teachers, Guardians (including the guardian detail view), the guardian-facing dashboard/child view, Announcements (draft/publish/edit/delete, and the guardian read-tracking view), and the User Manual — all work end-to-end against your real database. Worth specifically testing as a guardian account, not just as an admin.
2. **Build Homework** (Section 9, item 5) — the next content section; the `Homework` model already exists (Section 11.2), and Announcements just established the admin/teacher-write, guardian-read UI pattern (list + form + guardian-filtered view) every remaining section should follow.
3. **Decide on real SMS-based guardian invites** (Section 10) — the passwordless flow per proposal Section 4.2, replacing the username/password interim from Section 19.
4. **Keep the User Manual (Section 21) current** as new sections get built — it'll go stale the same way this handover plan would if sections were added without updating it.
5. **Sketch a backup plan** for the production Postgres volume before it holds real data (Section 8) — still open, still not urgent while everything is local dev.
