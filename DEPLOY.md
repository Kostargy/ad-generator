# Heroku Deployment Guide

This project deploys to Heroku as a 2-process app:

| Process | What it does |
|---|---|
| `web`    | Django served by gunicorn |
| `worker` | Celery worker that runs image generation tasks |
| `release`| One-shot migrations on every deploy |

The Procfile and `app.json` are already configured. This guide walks through a fresh deploy and gives you the day-to-day commands you'll need.

---

## 1. Prerequisites

- [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli) installed and `heroku login` done
- A Heroku account with a verified payment method (no free tier)
- Git installed; you're in the project directory and on the branch you want to deploy

---

## 2. First-time deploy (cheat sheet)

Run from `everdries_ad_generator/`:

```bash
# 1. Create the app
heroku create your-app-name --stack heroku-24

# 2. Buildpack (heroku/python auto-detects uv.lock — no separate uv buildpack needed)
heroku buildpacks:add heroku/python

# 3. Provision Postgres + Redis
heroku addons:create heroku-postgresql:essential-0
heroku addons:create heroku-redis:mini

# 4. Required Django config vars
heroku config:set \
  DJANGO_SETTINGS_MODULE=config.settings.production \
  DJANGO_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \
  DJANGO_ALLOWED_HOSTS=".herokuapp.com" \
  DJANGO_SECURE_SSL_REDIRECT=True \
  WEB_CONCURRENCY=2

# 5. Push code (release phase auto-runs migrations)
git push heroku main

# 6. Scale the worker dyno (web is auto-scaled to 1)
heroku ps:scale worker=1

# 7. Create a Django admin user
heroku run python manage.py createsuperuser

# 8. Open the app
heroku open
```

After the app loads, log into `/admin/`, go to **Campaigns → API Settings**, and configure:

- `primary_provider` (gemini or openai)
- `gemini_api_key`
- `openai_api_key`
- `gemini_model`
- `critic_model`

---

## 3. Updating after first deploy

```bash
git add -A
git commit -m "your change"
git push heroku main
```

The `release` phase in `Procfile` re-runs migrations on every deploy. The web dyno restarts automatically; the worker dyno restarts too.

---

## 4. Day-to-day commands

### Logs

```bash
heroku logs --tail                       # all dynos, live
heroku logs --tail --dyno web            # only web
heroku logs --tail --dyno worker         # only Celery worker
heroku logs --num 200                    # last 200 lines (no tail)
```

### Dynos

```bash
heroku ps                                # show running dynos
heroku ps:scale web=1 worker=1           # set dyno counts
heroku ps:scale worker=0                 # stop the worker (saves $)
heroku restart                           # restart everything
heroku restart worker                    # restart only the worker
```

### Config vars

```bash
heroku config                            # show all
heroku config:get DJANGO_SECRET_KEY      # show one
heroku config:set KEY=value              # add/update
heroku config:unset KEY                  # remove
```

### One-off commands

```bash
heroku run python manage.py shell        # Django shell
heroku run python manage.py migrate      # manual migration
heroku run python manage.py createsuperuser
heroku run bash                          # full shell on a one-off dyno
```

### Database

```bash
heroku pg:info                           # connection info
heroku pg:psql                           # interactive psql
heroku pg:backups:capture                # take a backup
heroku pg:backups:download               # download latest backup
```

### Redis

```bash
heroku redis:info                        # connection info
heroku redis:cli                         # interactive redis-cli
```

### Releases / rollbacks

```bash
heroku releases                          # release history
heroku releases:info v42                 # details about a release
heroku rollback v41                      # roll back to v41
```

---

## 5. Required environment variables

The app reads these on boot. Anything not set will use the defaults shown.

| Variable | Required? | Default | Notes |
|---|---|---|---|
| `DJANGO_SETTINGS_MODULE`     | ✅ | — | Must be `config.settings.production` |
| `DJANGO_SECRET_KEY`          | ✅ | — | Generate with `secrets.token_urlsafe(64)` |
| `DJANGO_ALLOWED_HOSTS`       | ✅ | `.herokuapp.com` | Comma-separated; include any custom domain |
| `DATABASE_URL`               | auto | — | Set by Heroku Postgres addon |
| `REDIS_URL`                  | auto | — | Set by Heroku Redis addon (will be `rediss://`) |
| `DJANGO_SECURE_SSL_REDIRECT` | rec. | `True` | Force HTTPS |
| `WEB_CONCURRENCY`            | rec. | `2` | gunicorn workers per web dyno |
| `DJANGO_CSRF_TRUSTED_ORIGINS`| opt. | `https://*.herokuapp.com` | Add custom domain here when you have one |
| `DJANGO_AWS_STORAGE_BUCKET_NAME` | opt. | unset | Triggers S3 mode (see below) |
| `DJANGO_AWS_ACCESS_KEY_ID`       | opt. | unset | Required if using S3 |
| `DJANGO_AWS_SECRET_ACCESS_KEY`   | opt. | unset | Required if using S3 |
| `DJANGO_AWS_S3_ENDPOINT_URL`     | opt. | unset | For Cloudflare R2 / Tigris / etc. |
| `DJANGO_AWS_S3_REGION_NAME`      | opt. | `None` | `auto` for R2 |

API keys for Gemini and OpenAI are **not** environment variables — they live in the Django `APISettings` model and are configured via `/admin/`.

---

## 6. Object storage (⚠ important — read before sharing the app)

By default the app uses **WhiteNoise** for static files and writes generated images to the **dyno's local filesystem**. The dyno filesystem is **ephemeral** — it is wiped on every:

- deploy
- `heroku restart`
- daily dyno cycle (~24h)
- crash + auto-restart

**Result:** generated ad images will disappear unpredictably and the database will reference dead files. This is fine to smoke-test the pipeline; it is **not** acceptable for real use.

### Switching to persistent storage (Cloudflare R2 example)

1. Create an R2 bucket and an API token with read/write permissions
2. Set the env vars:

```bash
heroku config:set \
  DJANGO_AWS_ACCESS_KEY_ID=<r2-access-key> \
  DJANGO_AWS_SECRET_ACCESS_KEY=<r2-secret> \
  DJANGO_AWS_STORAGE_BUCKET_NAME=<bucket-name> \
  DJANGO_AWS_S3_REGION_NAME=auto \
  DJANGO_AWS_S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
```

3. The app auto-detects `DJANGO_AWS_STORAGE_BUCKET_NAME` and switches both static and media files to S3 mode. No code changes, no redeploy needed beyond the dyno restart triggered by `config:set`.

AWS S3 works the same way — just omit `DJANGO_AWS_S3_ENDPOINT_URL` and set `DJANGO_AWS_S3_REGION_NAME` to your bucket's region.

---

## 7. How Celery runs on Heroku

- The `worker` dyno runs `celery -A config worker -l info --concurrency=2` (see `Procfile`)
- Broker + result backend are both Heroku Redis (`rediss://`)
- Heroku Redis uses self-signed certs, so the app sets `ssl_cert_reqs=CERT_NONE` for both Celery and django-redis cache (`config/settings/base.py` and `production.py`). TLS is still on; only certificate verification is skipped.
- Generation tasks bridge sync→async with `asyncio.run()` inside `GenerationService.run()`
- ⚠ Stick to the default `prefork` pool. `gevent`/`eventlet` will break the asyncio bridge.

To stop the worker (e.g. to save money while you're not testing):

```bash
heroku ps:scale worker=0
```

To start it back up:

```bash
heroku ps:scale worker=1
```

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `H10 App crashed` on first request | Missing `DJANGO_SECRET_KEY` or `DATABASE_URL` | `heroku config` to verify; `heroku config:set` what's missing |
| `DisallowedHost` 400 error | `DJANGO_ALLOWED_HOSTS` doesn't include your domain | Add it: `heroku config:set DJANGO_ALLOWED_HOSTS=".herokuapp.com,yourdomain.com"` |
| CSRF failures on POST from custom domain | Domain not in `CSRF_TRUSTED_ORIGINS` | `heroku config:set DJANGO_CSRF_TRUSTED_ORIGINS="https://*.herokuapp.com,https://yourdomain.com"` |
| Worker crashes with `rediss:// requires ssl_cert_reqs` | Old code without the SSL fix | Pull latest `main` — `base.py` now sets `CELERY_BROKER_USE_SSL` when `REDIS_URL` starts with `rediss://` |
| Worker not picking up tasks | Worker dyno not scaled | `heroku ps:scale worker=1` |
| `Generator` stuck in `PROCESSING` | Task crashed after dispatch | Check `heroku logs --tail --dyno worker` around the dispatch timestamp |
| Generated image 404 after deploy | Ephemeral filesystem wiped | Wire up object storage (section 6) |
| Build fails: "runtime.txt isn't supported when using uv" | `runtime.txt` exists alongside `uv.lock` | Delete `runtime.txt`; `.python-version` drives the Python version under uv |
| Build picks wrong Python | `.python-version` mismatch with `pyproject.toml` `requires-python` | Both should agree (currently `3.12`) |

---

## 9. Cost (as of writing)

| Resource | Plan | Monthly |
|---|---|---|
| Web dyno | Basic | ~$7 |
| Worker dyno | Basic | ~$7 |
| Postgres | Essential-0 | ~$5 |
| Redis | Mini | ~$3 |
| **Total (no object storage)** | | **~$22** |

Cloudflare R2 storage adds ~$0.015/GB-month with no egress fees. AWS S3 is similar on storage but charges egress.

To pause spend without deleting the app:

```bash
heroku ps:scale web=0 worker=0
```

You'll still pay for addons (Postgres + Redis) until you remove them with `heroku addons:destroy`.

---

## 10. Useful links

- App URL: set after `heroku create` — find it with `heroku info`
- Heroku Dashboard: https://dashboard.heroku.com/apps
- Heroku Python buildpack docs: https://devcenter.heroku.com/articles/python-support
- Heroku Redis docs: https://devcenter.heroku.com/articles/heroku-redis
- Heroku Postgres docs: https://devcenter.heroku.com/articles/heroku-postgresql
