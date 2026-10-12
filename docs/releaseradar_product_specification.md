# Product Specification Document (PRD)

**Product Name:** ReleaseRadar (Working Title)  
**Tagline:** Pre-Production Blast Radius & Release Intelligence for Engineering Leads  
**Version:** 1.0.0 (MVP)  
**Date:** October 2026  
**Status:** Approved for Implementation  

---

## 1. Executive Summary & Value Proposition

### 1.1 The Problem
Modern engineering teams review pull requests in small, isolated increments. However, production downtime is rarely caused by a single syntax or logic bug inside an isolated PR. Major regressions are caused by **systemic, cross-service side effects**:
* Backward-incompatible database migrations (table locks, dropped columns, unhandled nulls).
* Undocumented API contract changes breaking frontend web apps, mobile clients, or microservices.
* Missing or misconfigured environment variables causing boot loops in containerized services.
* Engineering managers and release leads lacking visibility into what 20+ merged pull requests actually do when bundled together for a production release.

### 1.2 The Solution
**ReleaseRadar** is an independent, developer-facing platform that connects directly to Git providers (GitHub, GitLab, Bitbucket). It analyzes code changes, predicts blast radius, identifies breaking contracts, and provides daily engineering digests.

Unlike CI/CD runners (e.g., Jenkins, GitHub Actions), ReleaseRadar is:
* **Read-only and decoupled:** Does not touch deployment credentials, SSH keys, or cloud infrastructure.
* **Multi-project native:** Serves as a single pane of glass across all repositories in an organization.
* **Manager & lead focused:** Summarizes systemic risk rather than spamming inline stylistic PR comments.

---

## 2. Personas & Target Audience

| Persona | Role | Core Pain Point | Value Delivered |
|---|---|---|---|
| **Tech Lead / Release Engineer** | Prepares releases, coordinates hotfixes | Spends 2+ hours manually reviewing commit lists before production deployments. | 30-second automated blast radius report, breaking changes matrix, and rollback safety check. |
| **Engineering Manager / VP** | Oversees 15–100 engineers across multiple services | Zero cross-project visibility into daily code churn and technical debt. | Automated end-of-day digest showing all merged code, high-churn areas, and hazard trends. |
| **Backend / Full-Stack Engineer** | Merges feature branches to staging/main | Unintentionally breaks downstream consumers due to undocumented API/DB changes. | Instant feedback on branch compatibility before merging. |

---

## 3. Core Functional Requirements

### 3.1 Feature: Daily EOD Intelligence Feed ("Today in Code")
* **Trigger:** Automated cron job running daily at 6:00 PM local time.
* **Scope:** Analyzes all commits merged into default branches (`main`, `develop`) over the preceding 24 hours across all registered repositories.
* **Output:**
  * Executive digest: *"14 commits merged across 4 repos. 1 critical DB index change, 2 new required env vars."*
  * Identification of touched modules, architectural refactors, and high-churn files.
  * Risk rating per project: `GREEN` (Safe), `AMBER` (Caution), `RED` (Hazardous).

### 3.2 Feature: Arbitrary Commit-Range Comparator (Pre-Deploy Inspector)
* **Trigger:** On-demand execution from the UI or an external webhook.
* **Inputs:** `Project ID`, `Base Ref` (e.g., current production tag `v2.4.0`), `Target Ref` (e.g., release candidate `rc-v2.5.0`).
* **Output:**
  * **Overall Verdict:** `APPROVED`, `APPROVED_WITH_CAUTION`, or `BLOCKED`.
  * **Risk Score:** Integer from 0 (harmless) to 100 (critical outage risk).
  * **Breaking Changes Matrix:** Structured list of altered routes, renamed fields, and DB table modifications (hazards flagged `is_breaking`, with `change_type` and `affected_symbol`).
  * **Rollback Safety Check:** Boolean verdict (`rollback_safe`) plus explanation of whether reverting `Target Ref` to `Base Ref` will result in state corruption or data loss.
  * **Deployment Checklist:** Required environment variables and pre-deploy actions, each checkable by the release lead.
  * **Interactive Sign-Off:** Allows release leads to review hazards, check off items, and log an audit trail entry.

### 3.3 Feature: Branch & Pull Request Safety Check
* **Trigger:** Git webhook on PR open or update (`pull_request.synchronize`).
* **Scope:** Analyzes the target branch against the incoming feature branch.
* **Output:**
  * Generates an entry in the ReleaseRadar dashboard.
  * (Optional) Injects a concise summary card into the PR comments with a link to the full blast-radius inspector.

---

## 4. Technical Architecture

```text
[ Git Providers (GitHub / GitLab / Bitbucket) ]
                     │
                     │  Webhooks & REST APIs (Read-Only)
                     ▼
┌──────────────────────────────────────────────────────────────┐
│                    ReleaseRadar Backend                      │
│                                                              │
│  ┌───────────────────────────┐   ┌────────────────────────┐  │
│  │ FastAPI Ingestion Gateway │   │ PostgreSQL 16          │  │
│  │  - Webhook verification   │   │  - Multi-tenant data   │  │
│  │  - Git token management   │   │  - JSONB Hazard Models │  │
│  └─────────────┬─────────────┘   └───────────┬────────────┘  │
│                │ Enqueue Job                 │               │
│                ▼                             │ Query / Store │
│  ┌───────────────────────────┐               │               │
│  │ Worker Pool (Celery/Redis)│◄──────────────┘               │
│  │  - Smart Diff Filter      │                               │
│  │  - Token Window Optimizer │                               │
│  │  - LLM Structured Engine  │                               │
│  └───────────────────────────┘                               │
└──────────────────────────────┬───────────────────────────────┘
                               │ REST / WebSocket
                               ▼
            [ Web Dashboard (React / Vite / Tailwind) ]
```

### 4.1 Ingestion & Token Optimization Engine
To handle enterprise-scale repositories without cloning whole codebases:
1. **API Diff Retrieval:** Fetches unified diffs using Git provider comparison endpoints:
   * GitHub: `GET /repos/{owner}/{repo}/compare/{base}...{head}`
   * GitLab: `GET /projects/{id}/repository/compare?from={base}&to={head}`
2. **Deterministic Pre-Filtering:** Removes non-logic noise before passing code to the LLM:
   * Drops lockfiles: `package-lock.json`, `yarn.lock`, `poetry.lock`, `uv.lock`, `pnpm-lock.yaml`.
   * Drops build artifacts: `dist/*`, `build/*`, `target/*`, `*.min.js`.
   * Drops static visual media: `*.svg`, `*.png`, `*.jpg`, `*.woff2`.
3. **File Priority Tiering:**
   * **Tier 1 (High Criticality):** Migrations, DB schemas, API router definitions, configuration files (`.env.example`, settings).
   * **Tier 2 (Core Logic):** Domain services, controllers, utility libraries.
   * **Tier 3 (Low Priority):** Unit tests, styling (`*.css`, `*.scss`), documentation (`*.md`).
   * When diffs exceed LLM context window limits, Tier 1 and Tier 2 files are processed first.

---

## 5. Data Model & Database Schema

The database relies on PostgreSQL 16 with JSONB support for dynamic hazard attributes. Enumerated columns use `CHECK` constraints that mirror the Pydantic enums in §6 exactly.

```sql
-- Organizations / Accounts
CREATE TABLE organizations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              VARCHAR(100) NOT NULL,
    timezone          VARCHAR(64)  NOT NULL DEFAULT 'UTC',   -- IANA tz; drives "6 PM local" digest
    digest_local_time TIME         NOT NULL DEFAULT '18:00',
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- People who trigger reviews, acknowledge hazards, and sign off releases
CREATE TABLE users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email        VARCHAR(255) NOT NULL UNIQUE,               -- MVP: one org per user
    display_name VARCHAR(100) NOT NULL,
    role         VARCHAR(20)  NOT NULL DEFAULT 'MEMBER' CHECK (role IN ('ADMIN', 'MEMBER')),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ReleaseRadar-issued access tokens (UI login via session cookie + Bearer auth for CI/scripts)
CREATE TABLE api_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         VARCHAR(100) NOT NULL,                       -- 'laptop', 'release-ci'
    token_prefix VARCHAR(16)  NOT NULL,                       -- displayed to identify the token
    token_hash   CHAR(64)     NOT NULL UNIQUE,                -- SHA-256 hex; plaintext never stored
    expires_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Git provider credentials. Primary model: provider access tokens (works identically for
-- GitHub, GitLab, Bitbucket, incl. self-hosted). APP_INSTALLATION reserved for an optional GitHub App later.
CREATE TABLE git_connections (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider                 VARCHAR(20)  NOT NULL CHECK (provider IN ('github', 'gitlab', 'bitbucket')),
    base_url                 VARCHAR(255) NOT NULL,           -- 'https://api.github.com', 'https://gitlab.acme.internal/api/v4'
    auth_type                VARCHAR(20)  NOT NULL DEFAULT 'ACCESS_TOKEN'
                             CHECK (auth_type IN ('ACCESS_TOKEN', 'APP_INSTALLATION')),
    external_installation_id VARCHAR(100),
    account_login            VARCHAR(255) NOT NULL,           -- owner / group / workspace: 'acme'
    auth_username            VARCHAR(255),                    -- only for Basic-auth tokens (Bitbucket API token + email)
    access_token_encrypted   TEXT,
    token_expires_at         TIMESTAMPTZ,                     -- drives "token expiring soon" warnings
    last_verified_at         TIMESTAMPTZ,                     -- last successful credential check
    webhook_secret_encrypted TEXT,                            -- shared secret for repo webhooks (Phase 4)
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (id, org_id),                                      -- target for composite FK below
    UNIQUE (provider, external_installation_id),
    CHECK (
        (auth_type = 'ACCESS_TOKEN'     AND access_token_encrypted   IS NOT NULL) OR
        (auth_type = 'APP_INSTALLATION' AND external_installation_id IS NOT NULL)
    )
);

-- Monitored Projects / Repositories
CREATE TABLE projects (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    git_connection_id UUID NOT NULL,
    name              VARCHAR(100) NOT NULL,
    repo_identifier   VARCHAR(255) NOT NULL,                  -- 'acme/payment-service', 'group/sub/project', 'workspace/repo-slug'
    external_repo_id  VARCHAR(100),                           -- stable across repo renames
    default_branch    VARCHAR(100) NOT NULL DEFAULT 'main',
    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- connection must belong to the same org as the project
    FOREIGN KEY (git_connection_id, org_id) REFERENCES git_connections(id, org_id) ON DELETE RESTRICT,
    UNIQUE (git_connection_id, repo_identifier)               -- unique per connection, not globally
);

-- Org-wide end-of-day rollup ("Today in Code"); per-project results live in review_jobs
CREATE TABLE daily_digests (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    digest_date       DATE        NOT NULL,                   -- in the org's timezone
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL,
    status            VARCHAR(20) NOT NULL DEFAULT 'QUEUED'
                      CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')),
    executive_summary TEXT,
    org_health_score  INT CHECK (org_health_score BETWEEN 0 AND 100),
    totals            JSONB       NOT NULL DEFAULT '{}'::jsonb, -- { commits: 14, repos: 4, critical: 1 }
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    UNIQUE (org_id, digest_date),
    CHECK (window_end > window_start)
);

-- Audit / Review Executions (asynchronous: results are NULL until COMPLETED)
CREATE TABLE review_jobs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    digest_id         UUID REFERENCES daily_digests(id) ON DELETE CASCADE,
    triggered_by      UUID REFERENCES users(id) ON DELETE SET NULL, -- NULL = system / webhook
    review_type       VARCHAR(30) NOT NULL
                      CHECK (review_type IN ('DAILY_DIGEST', 'PULL_REQUEST', 'ON_DEMAND_COMPARE')),
    status            VARCHAR(20) NOT NULL DEFAULT 'QUEUED'
                      CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    base_ref          VARCHAR(255) NOT NULL,
    target_ref        VARCHAR(255) NOT NULL,
    base_sha          VARCHAR(64),                            -- resolved at run start; refs move
    target_sha        VARCHAR(64),
    pr_number         INT,
    verdict           VARCHAR(30) CHECK (verdict IN ('APPROVED', 'APPROVED_WITH_CAUTION', 'BLOCKED')),
    risk_score        INT CHECK (risk_score BETWEEN 0 AND 100),
    executive_summary TEXT,
    rollback_safe     BOOLEAN,
    rollback_notes    TEXT,
    raw_metrics       JSONB NOT NULL DEFAULT '{}'::jsonb,     -- { additions, deletions, files_count, skipped_files, tokens, cost_usd }
    llm_model         VARCHAR(100),
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    CHECK (status <> 'COMPLETED' OR (
        verdict IS NOT NULL AND risk_score IS NOT NULL AND executive_summary IS NOT NULL
        AND rollback_safe IS NOT NULL AND completed_at IS NOT NULL)),
    CHECK (status <> 'FAILED' OR error_message IS NOT NULL),
    CHECK (review_type <> 'DAILY_DIGEST' OR digest_id IS NOT NULL),
    CHECK (review_type <> 'PULL_REQUEST' OR pr_number IS NOT NULL)
);

-- Granular Hazards Flagged by the Engine (Breaking Changes Matrix = rows WHERE is_breaking)
CREATE TABLE review_hazards (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_job_id   UUID NOT NULL REFERENCES review_jobs(id) ON DELETE CASCADE,
    category        VARCHAR(30) NOT NULL
                    CHECK (category IN ('API_CONTRACT', 'DATABASE', 'RUNTIME_ENV', 'SECURITY', 'SHARED_STATE')),
    severity        VARCHAR(20) NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    file_path       TEXT         NOT NULL,
    hazard_title    VARCHAR(255) NOT NULL,
    explanation     TEXT         NOT NULL,
    remediation     TEXT         NOT NULL,
    is_breaking     BOOLEAN      NOT NULL DEFAULT FALSE,
    change_type     VARCHAR(20)  CHECK (change_type IN ('ADDED', 'MODIFIED', 'REMOVED', 'RENAMED')),
    affected_symbol VARCHAR(255),                             -- 'POST /v1/payments', 'users.email', 'STRIPE_KEY'
    acknowledged_by UUID REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at TIMESTAMPTZ,                              -- acknowledged = acknowledged_at IS NOT NULL
    CHECK (NOT is_breaking OR (change_type IS NOT NULL AND affected_symbol IS NOT NULL))
);

-- Deployment Checklist: required env vars and pre-deploy actions from the LLM report
CREATE TABLE review_prerequisites (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_job_id UUID NOT NULL REFERENCES review_jobs(id) ON DELETE CASCADE,
    kind          VARCHAR(30) NOT NULL CHECK (kind IN ('REQUIRED_ENV_VAR', 'PRE_DEPLOY_ACTION')),
    description   TEXT        NOT NULL,                       -- 'STRIPE_WEBHOOK_SECRET' / 'Run migration 0042 first'
    sort_order    INT         NOT NULL DEFAULT 0,
    completed_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    completed_at  TIMESTAMPTZ
);

-- Append-only sign-off audit trail
CREATE TABLE review_audit_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_job_id   UUID NOT NULL REFERENCES review_jobs(id) ON DELETE CASCADE,
    actor_id        UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_display   VARCHAR(255) NOT NULL,                    -- snapshot; survives user deletion
    event_type      VARCHAR(40)  NOT NULL CHECK (event_type IN (
                        'HAZARD_ACKNOWLEDGED', 'HAZARD_UNACKNOWLEDGED',
                        'PREREQUISITE_COMPLETED', 'PREREQUISITE_REOPENED',
                        'RELEASE_SIGNED_OFF', 'SIGNOFF_REVOKED')),
    hazard_id       UUID REFERENCES review_hazards(id) ON DELETE CASCADE,
    prerequisite_id UUID REFERENCES review_prerequisites(id) ON DELETE CASCADE,
    note            TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_org               ON users(org_id);
CREATE INDEX idx_api_tokens_user         ON api_tokens(user_id);
CREATE INDEX idx_projects_org            ON projects(org_id);
CREATE INDEX idx_review_jobs_lookup      ON review_jobs(project_id, created_at DESC);
CREATE INDEX idx_review_jobs_digest      ON review_jobs(digest_id) WHERE digest_id IS NOT NULL;
CREATE INDEX idx_review_jobs_active      ON review_jobs(status) WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX idx_review_hazards_lookup   ON review_hazards(review_job_id, severity);
CREATE INDEX idx_review_prereqs_lookup   ON review_prerequisites(review_job_id, sort_order);
CREATE INDEX idx_review_audit_lookup     ON review_audit_events(review_job_id, created_at);
```

**Verdict → risk rating mapping** (used by the Daily Radar and banners): `APPROVED → GREEN`, `APPROVED_WITH_CAUTION → AMBER`, `BLOCKED → RED`.

---

## 6. Structured Output Contract (LLM Schema)

All LLM calls enforce strict JSON generation using the following Pydantic contract:

```python
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class SeverityEnum(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class CategoryEnum(str, Enum):
    API_CONTRACT = "API_CONTRACT"
    DATABASE = "DATABASE"
    RUNTIME_ENV = "RUNTIME_ENV"
    SECURITY = "SECURITY"
    SHARED_STATE = "SHARED_STATE"

class VerdictEnum(str, Enum):
    APPROVED = "APPROVED"
    APPROVED_WITH_CAUTION = "APPROVED_WITH_CAUTION"
    BLOCKED = "BLOCKED"

class ChangeTypeEnum(str, Enum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"
    RENAMED = "RENAMED"

class HazardItem(BaseModel):
    category: CategoryEnum
    severity: SeverityEnum
    file_path: str = Field(description="Relative path of the modified file")
    hazard_title: str = Field(description="Concise description of the hazard")
    explanation: str = Field(description="Why this change is dangerous in production")
    remediation: str = Field(description="Step required to safely deploy or resolve")
    is_breaking: bool = Field(default=False, description="True if this breaks an existing contract (API, schema, env)")
    change_type: Optional[ChangeTypeEnum] = Field(default=None, description="Required when is_breaking is true")
    affected_symbol: Optional[str] = Field(default=None, description="e.g. 'POST /v1/payments', 'users.email', 'STRIPE_KEY'; required when is_breaking")

class DeploymentPrerequisites(BaseModel):
    required_env_vars: List[str] = Field(default_factory=list)
    pre_deploy_actions: List[str] = Field(default_factory=list)
    zero_downtime_rollback_safe: bool = Field(description="True if safe to immediately revert target commit")
    rollback_notes: str = Field(description="Why rollback is or is not safe (e.g. irreversible column drop)")

class ReleaseReviewReport(BaseModel):
    verdict: VerdictEnum
    risk_score: int = Field(ge=0, le=100, description="Risk index between 0 (safe) and 100 (critical hazard)")
    executive_summary: str = Field(description="1-2 sentences summarizing changes and points of failure")
    hazards: List[HazardItem]
    deployment_prerequisites: DeploymentPrerequisites
```

---

## 7. User Interface Specifications

The frontend is a dedicated Single Page Application (SPA) designed with a clean, high-density developer tool layout.

### 7.1 View 1: Daily Radar (EOD Feed)
* **Header Bar:** Project dropdown filter, date picker, and organization health score badge.
* **Timeline Feed:** Chronological list of daily commit rollups. Each card displays:
  * Repository name, branch, and author count.
  * Color-coded risk status badge.
  * Bulleted summary of notable changes and flagged items.

### 7.2 View 2: Release Comparator
* **Input Toolbar:** Repository selector, `Base Ref` input, `Target Ref` input, and a primary `Run Inspection` button.
* **Executive Banner:** Large verdict indicator (`APPROVED` in green, `APPROVED_WITH_CAUTION` in amber — displayed as "Caution", `BLOCKED` in red) with risk score gauge and rollback-safety badge.
* **Breaking Changes Matrix:** Table of hazards where `is_breaking = true`, columns: `change_type`, `affected_symbol`, category, file path.
* **Hazard Inspection Matrix:** Collapsible cards for each hazard showing:
  * Severity badge and affected category (API, DB, Config).
  * Impacted file path.
  * Failure mode description and concrete remediation step.
  * Checkbox for tech leads to mark the item as acknowledged.
* **Deployment Checklist Panel:** Displays required environment variables and pre-deployment steps (e.g., database migration order).

---

## 8. Commercialization & Pricing Model

ReleaseRadar is positioned as an **Engineering Reliability & Governance Platform** rather than a general PR code-style bot.

| Tier | Target Customer | Pricing | Feature Inclusions |
|---|---|---|---|
| **Free / Starter** | Small projects & startups (< 10 devs) | $0 / month | 3 repositories, 50 on-demand reviews/month, Cloud hosted. |
| **Team** | Growth engineering teams (10–50 devs) | $19 / active developer / month | Unlimited repositories, daily EOD digests, Slack/Teams alerts, 90-day audit log retention. |
| **Enterprise** | Scale-ups & Enterprises (50+ devs) | Custom ($15k–$40k / year) | **Self-hosted / On-prem Docker deployment**, Bring Your Own API Key (BYOK), Single Sign-On (SAML/Okta), custom rule definitions. |

---

## 9. Implementation Roadmap

### Phase 1: Ingestion & Core LLM Pipeline (Weeks 1–2)
* Setup FastAPI project structure and PostgreSQL database.
* Implement Git comparison client (fetching diffs via GitHub/GitLab REST APIs).
* Implement pre-filtering heuristics (dropping lockfiles, assets, and build artifacts).
* Connect LiteLLM with Pydantic structured output validation.

### Phase 2: User Interface & Review Comparator (Weeks 3–4)
* Build frontend SPA layout (Comparator screen and Hazard matrix components).
* Implement on-demand review execution API.
* Implement hazard acknowledgment and sign-off functionality.

### Phase 3: Automation & Daily Digests (Weeks 5–6)
* Implement Celery task scheduler for nightly cron jobs across registered repositories.
* Build Daily EOD Radar UI feed.
* Add Slack / Teams outgoing webhook notifications for daily reports.