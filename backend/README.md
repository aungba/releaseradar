# ReleaseRadar Backend

FastAPI + SQLAlchemy (async) + Alembic + Celery, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # install deps into .venv (from uv.lock)
uv run alembic upgrade head               # apply migrations
uv run fastapi dev app/main.py            # API on :8000 with reload
uv run celery -A app.workers.celery_app worker -l info
uv run rr seed                            # demo org, user, connection, project + prints an access token
uv run pytest                             # tests (DB tests need RR_TEST_DATABASE_URL)
uv run ruff check . && uv run mypy app
```
