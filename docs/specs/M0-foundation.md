# Spec: M0 — Foundation

| | |
|---|---|
| **Milestone** | M0 Foundation (Phase 1, week 3) |
| **Status** | ✅ Implemented & verified (2026-10-12) |
| **Owners** | Dev A (infra), Dev C (models/migrations) |
| **Related** | [Product spec](../releaseradar_product_specification.md) §5–§6 · [Roadmap](../releaseradar_phased_roadmap.md) Phase 1 |

---

## 1. Purpose & Scope

M0 provides the runnable skeleton every later milestone builds on. It contains **no business logic** (no diff fetching, no LLM calls) — only the platform those features plug into.

**In scope**

| ID | Feature |
|---|---|
| F1 | Monorepo layout, uv-managed backend, Vite/React frontend |
| F2 | Typed configuration (`RR_*` env vars) |
| F3 | Complete database schema (11 tables) as Alembic migration `0001` + SQLAlchemy models |
| F4 | Shared domain enums (single source of truth for DB `CHECK`s and LLM contract) |
| F5 | LLM structured-output contract (PRD §6) as Pydantic models |
| F6 | Secret handling: provider-token encryption + ReleaseRadar access-token hashing |
| F7 | HTTP API skeleton with liveness/readiness endpoints |
| F8 | Celery worker skeleton |
| F9 | `rr` operator CLI (`seed`, `openapi`) |
| F10 | Frontend shell with typed API client and live API status |
| F11 | Local stack (Docker Compose), backend Docker image, CI pipeline |
| F12 | Automated test suites (DB constraints, unit, frontend) |

**Out of scope** (later milestones): Git provider clients (M1), analysis engine & LLM calls (M2), review/auth endpoints (M3), comparator UI (M4).

---

## 2. Architecture

```mermaid
flowchart LR
    subgraph Browser
        UI["React SPA :5173"]
    end
    subgraph Backend
        API["FastAPI :8000"]
        W["Celery worker"]
        CLI["rr CLI"]
    end
    DB[("PostgreSQL 16")]
    R[("Redis 7")]

    UI -- "/api proxy (same origin)" --> API
    API --> DB
    W --> DB
    API -- "enqueue (M3)" --> R
    R --> W
    CLI --> DB
```

| Path | Responsibility |
|---|---|
| `backend/app/core/` | Settings, DB engine/session, crypto |
| `backend/app/domain/` | Framework-free domain types (enums, mappings) |
| `backend/app/models/` | SQLAlchemy ORM models |
| `backend/app/schemas/` | Pydantic schemas (LLM contract; API DTOs from M3) |
| `backend/app/api/v1/` | HTTP routers |
| `backend/app/workers/` | Celery app + tasks |
| `backend/app/cli.py` | `rr` operator CLI |
| `backend/alembic/` | Migrations |
| `frontend/src/api/` | Generated OpenAPI types, client, query hooks |
| `frontend/src/features/` | Feature modules (comparator, hazards, checklist) |

---

## 3. Feature Specifications

### F2 — Configuration

Source: [`app/core/config.py`](../../backend/app/core/config.py). Read via `pydantic-settings`, prefix `RR_`, optional `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `RR_ENVIRONMENT` | `development` | `development` \| `test` \| `production` |
| `RR_DATABASE_URL` | `postgresql+asyncpg://releaseradar:releaseradar@localhost:5432/releaseradar` | Async SQLAlchemy URL |
| `RR_REDIS_URL` | `redis://localhost:6379/0` | Celery broker + result backend |
| `RR_ENCRYPTION_KEY` | *(empty)* | Fernet key for provider secrets. **Required** before storing any token |
| `RR_CORS_ORIGINS` | `["http://localhost:5173"]` | Allowed browser origins |
| `RR_LLM_PRIMARY_MODEL` | `gemini/gemini-2.5-pro` | LiteLLM model id (used from M2) |
| `RR_LLM_FALLBACK_MODEL` | `groq/llama-3.3-70b-versatile` | LiteLLM fallback (used from M2) |
| `RR_TEST_DATABASE_URL` | *(unset)* | Tests only. DB tests are **skipped** when unset |

`get_settings()` is cached; DB engine and session factory are created lazily on first use.

### F3 — Database Schema

Source: [`app/models/`](../../backend/app/models/), migration [`0001_initial_schema.py`](../../backend/alembic/versions/0001_initial_schema.py). Implements PRD §5 exactly.

**Conventions**
- Primary keys: `UUID DEFAULT gen_random_uuid()`.
- All timestamps `TIMESTAMPTZ`; `created_at` defaults to `now()`.
- Enumerated columns are `VARCHAR` + `CHECK (col IN (...))` generated from domain enums (no native PG enums → cheaper migrations).
- Deterministic constraint names:

| Kind | Pattern | Example |
|---|---|---|
| Primary key | `pk_<table>` | `pk_projects` |
| Unique | `uq_<table>_<cols>` | `uq_projects_git_connection_id_repo_identifier` |
| Check | `ck_<table>_<name>` | `ck_review_jobs_completed_requires_result` |
| Foreign key | `fk_<table>_<cols>_<ref_table>` | `fk_projects_git_connection_id_org_id_git_connections` |
| Index | `ix_<table>_<cols>` | `ix_review_jobs_project_id_created_at` |

**Tables**

| Table | Purpose |
|---|---|
| `organizations` | Tenant; `timezone` + `digest_local_time` drive the Phase 2 digest |
| `users` | Org members; `email` globally unique (MVP: one org per user) |
| `api_tokens` | ReleaseRadar access tokens (hash only) |
| `git_connections` | Provider credentials (GitHub/GitLab/Bitbucket, cloud or self-hosted) |
| `projects` | Monitored repositories |
| `daily_digests` | Org-wide end-of-day rollup (Phase 2) |
| `review_jobs` | One analysis run; async lifecycle |
| `review_hazards` | Flagged hazards; breaking-change fields |
| `review_prerequisites` | Deployment checklist items |
| `review_audit_events` | Append-only sign-off trail |

**Business rules enforced by PostgreSQL**

| Rule | Constraint |
|---|---|
| Completed job must carry verdict, risk score, summary, rollback flag, `completed_at` | `ck_review_jobs_completed_requires_result` |
| Failed job must carry `error_message` | `ck_review_jobs_failed_requires_error` |
| `DAILY_DIGEST` job must link a digest | `ck_review_jobs_digest_type_requires_digest` |
| `PULL_REQUEST` job must have `pr_number` | `ck_review_jobs_pr_type_requires_pr_number` |
| Risk score 0–100 | `ck_review_jobs_risk_score_range` |
| Breaking hazard needs `change_type` + `affected_symbol` | `ck_review_hazards_breaking_requires_symbol` |
| `ACCESS_TOKEN` connection needs a token; `APP_INSTALLATION` needs installation id | `ck_git_connections_credentials_match_auth_type` |
| Project's connection belongs to the **same org** | composite FK `(git_connection_id, org_id) → git_connections(id, org_id)` |
| Repo unique **per connection** (same repo allowed across orgs) | `uq_projects_git_connection_id_repo_identifier` |
| One digest per org per day; window end > start | `uq_daily_digests_org_id_digest_date`, `ck_daily_digests_window_order` |
| Token hash unique; email unique | `uq_api_tokens_token_hash`, `uq_users_email` |

**Delete behaviour**: deleting an org cascades to everything it owns; deleting a git connection that still has projects is **blocked** (`RESTRICT`); deleting a user sets `triggered_by` / `acknowledged_by` / `completed_by` / `actor_id` to `NULL` (audit rows keep `actor_display`).

**Indexes**: org/user lookups; `review_jobs(project_id, created_at DESC)` for history; partial `review_jobs(status) WHERE status IN ('QUEUED','RUNNING')` for the worker; partial `review_jobs(digest_id) WHERE digest_id IS NOT NULL`; per-job lookups on hazards, prerequisites, audit events.

**Migration workflow**
```bash
# after changing a model
uv run alembic revision --autogenerate -m "describe change"
# review the generated file, then
uv run alembic upgrade head
```
A test (`test_migrations_match_models`) fails CI if models and migrations diverge.

### F4 — Domain Enums

Source: [`app/domain/enums.py`](../../backend/app/domain/enums.py).

| Enum | Values |
|---|---|
| `UserRole` | `ADMIN`, `MEMBER` |
| `GitProviderKind` | `github`, `gitlab`, `bitbucket` |
| `GitAuthType` | `ACCESS_TOKEN`, `APP_INSTALLATION` |
| `ReviewType` | `DAILY_DIGEST`, `PULL_REQUEST`, `ON_DEMAND_COMPARE` |
| `JobStatus` | `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `DigestStatus` | `QUEUED`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED` |
| `Verdict` | `APPROVED`, `APPROVED_WITH_CAUTION`, `BLOCKED` |
| `Severity` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `HazardCategory` | `API_CONTRACT`, `DATABASE`, `RUNTIME_ENV`, `SECURITY`, `SHARED_STATE` |
| `ChangeType` | `ADDED`, `MODIFIED`, `REMOVED`, `RENAMED` |
| `PrerequisiteKind` | `REQUIRED_ENV_VAR`, `PRE_DEPLOY_ACTION` |
| `AuditEventType` | `HAZARD_ACKNOWLEDGED`, `HAZARD_UNACKNOWLEDGED`, `PREREQUISITE_COMPLETED`, `PREREQUISITE_REOPENED`, `RELEASE_SIGNED_OFF`, `SIGNOFF_REVOKED` |
| `RiskRating` | `GREEN`, `AMBER`, `RED` |

`risk_rating_for(verdict)`: `APPROVED → GREEN`, `APPROVED_WITH_CAUTION → AMBER`, `BLOCKED → RED`.

> Adding a value: edit the enum, autogenerate a migration (it updates the `CHECK`), and the LLM JSON schema updates automatically.

### F5 — LLM Contract

Source: [`app/schemas/llm_contract.py`](../../backend/app/schemas/llm_contract.py). Mirrors PRD §6 with PRD aliases (`SeverityEnum`, `CategoryEnum`, `VerdictEnum`, `ChangeTypeEnum`).

- `ReleaseReviewReport` → `verdict`, `risk_score` (0–100), `executive_summary`, `hazards[]`, `deployment_prerequisites`.
- `HazardItem` adds `is_breaking`, `change_type`, `affected_symbol`; a validator rejects breaking hazards without `change_type` + `affected_symbol` (same rule as the DB constraint).
- `DeploymentPrerequisites` adds `rollback_notes`.
- `hazard_title` / `affected_symbol` limited to 255 chars (match DB column sizes).
- `ReleaseReviewReport.model_json_schema()` is what M2 passes to the LLM as the response schema.

### F6 — Secret Handling

Source: [`app/core/crypto.py`](../../backend/app/core/crypto.py).

| Secret | Need | Mechanism |
|---|---|---|
| Provider access tokens, webhook secrets | Must be recoverable to call GitHub/GitLab | Fernet (AES-128-CBC + HMAC) with `RR_ENCRYPTION_KEY` → `encrypt_secret` / `decrypt_secret` |
| ReleaseRadar access tokens | Only verified | `generate_api_token()` → `rr_` + 43 url-safe chars (256-bit); store `token_hash` (SHA-256 hex) + `display_prefix` (first 11 chars) |

- Missing `RR_ENCRYPTION_KEY` raises `RuntimeError` (fails loudly, never stores plaintext).
- SHA-256 without salt is acceptable because tokens are high-entropy random, not user-chosen.
- Plaintext ReleaseRadar tokens are shown exactly once (CLI output) and never persisted.

### F7 — HTTP API Skeleton

Source: [`app/main.py`](../../backend/app/main.py), [`app/api/v1/health.py`](../../backend/app/api/v1/health.py). Base path `/api/v1`. OpenAPI UI at `/docs`.

| Method | Path | Response | Semantics |
|---|---|---|---|
| `GET` | `/api/v1/health` | `{"status":"ok"}` | Liveness — process is up, no dependencies touched |
| `GET` | `/api/v1/ready` | `{"status":"ok"\|"degraded","database":bool}` | Readiness — runs `SELECT 1`; returns `degraded` (HTTP 200) if the DB is unreachable |

CORS allows `RR_CORS_ORIGINS` with credentials (needed for the M3 session cookie).

### F8 — Celery Worker

Source: [`app/workers/celery_app.py`](../../backend/app/workers/celery_app.py), [`tasks.py`](../../backend/app/workers/tasks.py).

- Broker & result backend: `RR_REDIS_URL`; JSON serialization only.
- `task_acks_late=True` + `worker_prefetch_multiplier=1`: a crashed worker's review is redelivered, and long reviews aren't hoarded by one worker.
- Task `releaseradar.ping` → `"pong"` (smoke test). `run_review` arrives in M2/M3.

### F9 — `rr` Operator CLI

Source: [`app/cli.py`](../../backend/app/cli.py). Installed as a script entry point (`uv run rr ...`).

| Command | Behaviour |
|---|---|
| `rr seed [--email] [--repo] [--provider github\|gitlab\|bitbucket] [--base-url]` | In one transaction creates: org "Demo Org", ADMIN user, `ACCESS_TOKEN` git connection (token from `RR_SEED_PROVIDER_TOKEN`, encrypted), project, and an API token. Prints the plaintext token once. Requires `RR_ENCRYPTION_KEY`. |
| `rr openapi` | Prints the OpenAPI JSON (no server needed). Used by the frontend type generator. |

Planned for M3: `rr create-user`, `rr issue-token`, `rr revoke-token`.

### F10 — Frontend Shell

Source: [`frontend/src/`](../../frontend/src/).

| Piece | Detail |
|---|---|
| Stack | React 19, TypeScript 5.9 (strict, `noUncheckedIndexedAccess`), Vite 8, Tailwind 4, TanStack Query 5, React Router 8 |
| API types | `npm run gen:api` → runs `rr openapi`, generates `src/api/schema.d.ts` via `openapi-typescript` |
| Client | `src/api/client.ts` — `openapi-fetch` typed client, same-origin, `credentials: 'include'` |
| Dev proxy | Vite proxies `/api` → `http://localhost:8000` so cookies work without CORS |
| Layout | Header with nav (Comparator active; Daily Radar disabled — Phase 2) and **API status badge** |
| API status | `useReadiness()` polls `/api/v1/ready` every 30 s → "API online" (green) / "API offline" (red) / "Checking API…" |
| Comparator page | Placeholder showing the disabled input toolbar planned for M4 |

### F11 — Local Stack, Image, CI

| Artifact | Detail |
|---|---|
| [`docker-compose.yml`](../../docker-compose.yml) | `postgres:16-alpine`, `redis:7-alpine` (both with health checks), `api` (runs `alembic upgrade head` then uvicorn), `worker`. `.env` optional |
| [`backend/Dockerfile`](../../backend/Dockerfile) | `python:3.12-slim` + uv 0.10.0; dependency layer cached separately; `uv sync --frozen --no-dev`; runs as non-root `app` |
| [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | **backend**: `uv sync --locked`, ruff format/lint, mypy strict, pytest against a Postgres 16 service. **frontend**: `npm ci`, eslint, `tsc -b && vite build`, vitest |

### F12 — Test Suites

| Suite | Location | Count | Needs |
|---|---|---|---|
| DB constraints + migration drift | `backend/tests/db/test_constraints.py` | 21 | `RR_TEST_DATABASE_URL` |
| Unit (contract, enums, crypto, health) | `backend/tests/unit/test_core.py` | 11 | — |
| Frontend shell | `frontend/src/App.test.tsx` | 3 | — |

DB test harness ([`tests/conftest.py`](../../backend/tests/conftest.py)): drops & recreates the `public` schema once per session, runs **real Alembic migrations** (so migrations themselves are tested), then wraps every test in a rolled-back transaction; constraint assertions run inside savepoints and match on the constraint **name**.

---

## 4. Acceptance Criteria (from implementation plan §3.4)

| Criterion | Status | Evidence |
|---|---|---|
| `docker compose up` brings up the stack | ✅ | api, worker, postgres, redis all healthy; `/ready` → `database: true` |
| `alembic upgrade head` succeeds from empty DB | ✅ | Run by test harness and by the `api` container on start |
| Constraint tests pass (e.g. COMPLETED job without verdict rejected) | ✅ | 21/21 DB tests |
| Seed script creates 1 org / 1 user / 1 project | ✅ | `rr seed` (also creates connection + token) |
| CI defined | ✅ | Workflow written; first run happens on first push |
| Lint/type clean | ✅ | ruff, mypy strict, eslint, tsc |

---

## 5. Deviations & Known Limitations

| Item | Note |
|---|---|
| React 19 / React Router 8 | Plan said React 18; latest stable installed, no impact |
| TypeScript pinned `~5.9` | TS 7 is latest, but `typescript-eslint` and `openapi-typescript` don't support it yet. Revisit when they do |
| No ORM relationships | Intentionally omitted; M3 adds them with explicit loading (async-safe) |
| `/ready` checks DB only | Redis readiness added when the API starts enqueuing (M3) |
| No authentication yet | All endpoints public until M3 (only health endpoints exist) |
| CLI `seed` is non-idempotent | Re-running fails on the unique email; use `--email` for another seed |
