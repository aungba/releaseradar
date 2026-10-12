# ReleaseRadar

Pre-production blast radius & release intelligence for engineering leads.
Compares two refs (e.g. `v2.4.0` → `rc-v2.5.0`) on GitHub or GitLab and produces a verdict,
risk score, breaking-changes matrix, rollback-safety check and deployment checklist.

- Product spec: [docs/releaseradar_product_specification.md](docs/releaseradar_product_specification.md)
- Roadmap: [docs/releaseradar_phased_roadmap.md](docs/releaseradar_phased_roadmap.md)

## Stack

| Part | Tech |
|---|---|
| API | FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic — managed with **uv** |
| Jobs | Celery + Redis |
| DB | PostgreSQL 16 |
| LLM | LiteLLM (Gemini primary, Groq fallback) — from M2 |
| Frontend | React + TypeScript + Vite, Tailwind, TanStack Query, React Router |

## Quick start

```bash
cp .env.example .env              # then set RR_ENCRYPTION_KEY (command inside the file)
docker compose up -d postgres redis

# backend
cd backend
uv sync
uv run alembic upgrade head
uv run rr seed                    # demo data + prints a ReleaseRadar access token
uv run fastapi dev app/main.py    # http://localhost:8000/docs

# frontend (new terminal)
cd frontend
npm install
npm run dev                       # http://localhost:5173 (proxies /api to :8000)
```

Or run everything in containers: `docker compose up --build`.

## Tests

```bash
docker compose exec postgres createdb -U releaseradar releaseradar_test   # once
cd backend && RR_TEST_DATABASE_URL=postgresql+asyncpg://releaseradar:releaseradar@localhost:5432/releaseradar_test uv run pytest
cd frontend && npm test
```

## Layout

```text
backend/   FastAPI app, models, migrations, Celery worker, `rr` CLI
frontend/  React SPA
docs/      PRD + roadmap
```
