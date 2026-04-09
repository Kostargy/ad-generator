# Everdries_ad_generator

Behold My Awesome Project!

[![Built with Cookiecutter Django](https://img.shields.io/badge/built%20with-Cookiecutter%20Django-ff69b4.svg?logo=cookiecutter)](https://github.com/cookiecutter/cookiecutter-django/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

License: MIT

## Settings

Moved to [settings](https://cookiecutter-django.readthedocs.io/en/latest/1-getting-started/settings.html).

## Running locally

Local dev uses SQLite (no Postgres needed). Celery defaults to a Redis broker at `redis://localhost:6379/0`, but you can skip Redis entirely by running tasks in eager mode (see below).

### Prerequisites

- Python 3.12 (see `.python-version`)
- [uv](https://github.com/astral-sh/uv) for dependency management
- Redis — only if you want to run a real Celery worker. Skip if you use eager mode.

### First-time setup

```bash
# 1. Install dependencies into the project's virtualenv
uv sync

# 2. Apply migrations (creates db.sqlite3 in the project root)
uv run python manage.py migrate

# 3. Create a superuser so you can log in to the admin
uv run python manage.py createsuperuser
```

### Configure API keys

The app reads API keys from the database (`APISettings` singleton), not the environment. After your first login, visit `/admin/campaigns/apisettings/` (or the in-app Settings page) and fill in:

- `gemini_api_key`
- `openai_api_key` (optional — only used as a fallback)
- `gemini_model` (defaults to a Gemini image preview model)

### Run the dev server

```bash
uv run python manage.py runserver
```

Open http://localhost:8000 and sign in with the superuser you just created.

### Image generation: pick one of two modes

Image generation runs as a Celery task. You have two options for local dev:

**Option A — Eager mode (simplest, no Redis, no worker):**

Run the dev server with `CELERY_TASK_ALWAYS_EAGER=true` so tasks execute synchronously inside the request/handler:

```bash
CELERY_TASK_ALWAYS_EAGER=true uv run python manage.py runserver
```

The trade-off: clicking "Generate" blocks the request until the whole batch finishes (can be several minutes). Fine for poking at the app; painful for real generation runs.

**Option B — Real Celery worker (matches production):**

Start Redis (e.g. `brew services start redis` on macOS), then in a second terminal:

```bash
uv run celery -A config worker -l info --concurrency=2
```

Leave the dev server running normally in the first terminal. Generations kicked off from the UI will be picked up by this worker.

### Useful commands

```bash
# Open a Django shell
uv run python manage.py shell

# Make migrations after model changes
uv run python manage.py makemigrations

# Run tests
uv run pytest
```

## Basic Commands

### Setting Up Your Users

- To create a **normal user account**, just go to Sign Up and fill out the form. Once you submit it, you'll see a "Verify Your E-mail Address" page. Go to your console to see a simulated email verification message. Copy the link into your browser. Now the user's email should be verified and ready to go.

- To create a **superuser account**, use this command:

      uv run python manage.py createsuperuser

For convenience, you can keep your normal user logged in on Chrome and your superuser logged in on Firefox (or similar), so that you can see how the site behaves for both kinds of users.

### Type checks

Running type checks with mypy:

    uv run mypy everdries_ad_generator

### Test coverage

To run the tests, check your test coverage, and generate an HTML coverage report:

    uv run coverage run -m pytest
    uv run coverage html
    uv run open htmlcov/index.html

#### Running tests with pytest

    uv run pytest

### Live reloading and Sass CSS compilation

Moved to [Live reloading and SASS compilation](https://cookiecutter-django.readthedocs.io/en/latest/2-local-development/developing-locally.html#using-webpack-or-gulp).

## Deployment

The following details how to deploy this application.
