# Notipa — Notify Parent

Notipa is a school-to-parent communication platform: a single place for a school's admins and teachers to manage classes and students, keep guardians (parents, grandparents, or other caregivers) linked to the right children, and post announcements — built with a public-school-friendly free tier and a licensed tier for private schools in mind.

It's a multi-tenant Django app: every school is its own isolated tenant, a person's role (admin, teacher, or guardian) is scoped per school rather than global, and guardians are designed to be reachable without needing an app-store account (phone-number-first, SMS/link invites planned).

## Status

Actively under development. Delivered and tested so far:

- **Schools** — in-app setup for the platform's first admin (superusers only), with support for more than one school per install.
- **Teachers & Admins** — invite, edit, and revoke/restore staff accounts, scoped per school.
- **Guardians** — invite, edit, and revoke/restore guardian accounts.
- **Classes** — create, edit, and archive/restore classes, with a homeroom teacher and a class detail page showing the full roster.
- **Students** — create, edit, and archive/restore students, with a student detail page.
- **Guardian ↔ Student linking** — link guardians to students (and vice versa) from either side, with relationship type and a primary-contact flag.
- **Announcements** — school-wide or class-scoped, with a draft/publish workflow for staff and a read-tracked, filtered view for guardians.
- **Guardian-facing dashboard** — a guardian sees only their own linked children and the announcements relevant to them, never the full school roster.
- **In-app User Manual** — built-in documentation for the above, linked from the sidebar.

Still to come: Homework, Fee Notices, Permission Slips, SMS-based guardian invites, and PWA/push notifications. See `notipa-development-handover-plan.md` for the detailed build log and roadmap.

## Tech stack

- **Backend:** Django 6, Python 3.14
- **Database:** SQLite in development, PostgreSQL in production
- **Static files:** WhiteNoise
- **App server:** Gunicorn (production)
- **Containerization:** Docker / Docker Compose

## Getting started

The project is designed to run in Docker.

```bash
cp env.example.txt .env
# edit .env and set a real SECRET_KEY, at minimum

docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Then visit `http://localhost:8000/`, log in with the superuser account you just created, and use **Set Up a School** to create your first school.

### Running without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp env.example.txt .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

### Environment variables

See `env.example.txt` for the full list. The important ones:

| Variable | Purpose |
|---|---|
| `DJANGO_ENV` | `development` or `production` — controls which settings apply (e.g. SQLite vs PostgreSQL, `runserver` vs Gunicorn). |
| `SECRET_KEY` | Django's secret key. Always set your own; never use the default outside local development. |
| `DEBUG` | Defaults to `True` in development, `False` in production. |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Standard Django host/origin allowlists. |
| `DB_*` | PostgreSQL connection settings, only read when `DJANGO_ENV=production`. |

### Running tests

```bash
python manage.py test core
```

## Project structure

```
core/                   The whole application (models, views, forms, templates' logic, tests)
notipa/                 Django project settings, URLs, WSGI entrypoint
templates/               Shared base template and page templates
notipa-development-handover-plan.md   Detailed build log, design decisions, and roadmap
```

## License

Notipa is open source software, licensed under the [MIT License](LICENSE). You're free to use, modify, and distribute it, including for commercial purposes, as long as the original copyright and license notice are kept.
