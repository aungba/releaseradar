# Spec: M3 — Review API, Authentication & Interactive Sign-Off

| | |
|---|---|
| **Milestone** | M3 Review API, Auth & Interactive Sign-Off (Phase 1) |
| **Status** | ✅ Implemented & verified (2026-10-14) |
| **Owners** | Dev C (auth, endpoints, sign-off, audit trail), Dev A (provider endpoints, CLI) |
| **Related** | [M0 spec](M0-foundation.md) · [M1 spec](M1-git-integration.md) · [M2 spec](M2-analysis-engine.md) · [Product spec](../releaseradar_product_specification.md) §3–7 |

---

## 1. Purpose & Scope

M3 exposes the M1 Git integration and M2 analysis engine over an authenticated, multi-tenant HTTP API (`/api/v1`) and adds interactive hazard acknowledgement, deployment prerequisite tracking, and release sign-off with an append-only audit trail.

**In scope**

| ID | Feature | Module |
|---|---|---|
| A1 | Dual authentication (`Authorization: Bearer rr_...` + `HttpOnly` session cookie) & role guards | `app/core/auth.py` |
| A2 | Session login/logout, `/me`, and self-service access token lifecycle (`/me/tokens`) | `app/api/v1/auth.py` |
| A3 | Git connection onboarding, live credential verification, token health & rotation | `app/api/v1/git_connections.py` |
| A4 | Project registration (auto-filling `default_branch` & `external_repo_id`) & ref autocomplete | `app/api/v1/projects.py` |
| A5 | Review job creation (`202 Accepted` + Celery enqueue), history listing, and full report detail | `app/api/v1/reviews.py` |
| A6 | Interactive hazard ack/un-ack, prerequisite completion/reopen, release sign-off/revoke & audit trail | `app/services/signoff.py`, `app/api/v1/signoff.py` |
| A7 | Operator CLI onboarding commands (`rr create-user`, `rr create-token`) & OpenAPI TypeScript schema | `app/cli.py`, `frontend/src/api/schema.d.ts` |

**Out of scope:** React Comparator UI (M4), webhooks / PR comments / daily digest (Phase 2), GitHub OAuth (Phase 3).

---

## 2. Authentication & Tenant Isolation

- **Single credential primitive:** Both browser sessions and scripts/CI pipelines authenticate against `api_tokens` rows (256-bit random tokens prefixed with `rr_`, stored as SHA-256 hex hashes).
- **Session cookie exchange:** `POST /api/v1/auth/session` validates a token and sets an `HttpOnly; SameSite=Lax` cookie (`rr_session`). Because the cookie holds the token itself rather than an independent JWT, revoking a token via `DELETE /api/v1/me/tokens/{id}` immediately terminates both API access and any active browser session.
- **Throttled `last_used_at`:** `authenticate_token()` updates `api_tokens.last_used_at` only when it is `NULL` or older than 5 minutes, avoiding write amplification during 2-second UI polling.
- **Role-based access (`UserRole`):**
  - `ADMIN`: required to create or update `git_connections` and `projects`.
  - `MEMBER` (and `ADMIN`): can list connections/projects, query refs, trigger reviews, acknowledge hazards, complete prerequisites, and sign off releases.
- **Strict organization isolation:** Every resource query filters by `org_id == current_user.org_id`. Accessing a non-existent or cross-org UUID returns `404 Not Found`.

---

## 3. Endpoint Summary (`/api/v1`)

| Method | Path | Role | Status | Description |
|---|---|---|---|---|
| `POST` | `/auth/session` | Public | `200` | Validate `rr_...` token, set `HttpOnly` cookie, return `CurrentUserResponse` |
| `DELETE` | `/auth/session` | Any | `204` | Clear session cookie |
| `GET` | `/me` | Any | `200` | Current user, organization, and active token metadata |
| `GET` | `/me/tokens` | Any | `200` | List own access tokens (prefix + metadata; never plaintext or hash) |
| `POST` | `/me/tokens` | Any | `201` | Issue a new access token; returns `plaintext` once |
| `DELETE` | `/me/tokens/{token_id}` | Any | `204` | Revoke own access token (`revoked_at = now()`) |
| `GET` | `/git-connections` | Any | `200` | List org git connections with computed `token_health` |
| `POST` | `/git-connections` | Admin | `201` | Verify token live with GitHub/GitLab, encrypt with Fernet, persist |
| `POST` | `/git-connections/{id}/verify` | Any | `200` | Re-verify stored token with provider and refresh `last_verified_at` / `token_expires_at` |
| `PATCH` | `/git-connections/{id}` | Admin | `200` | Rotate `access_token` (re-verified live) or update `token_expires_at` |
| `GET` | `/projects` | Any | `200` | List active org projects ordered by name |
| `POST` | `/projects` | Admin | `201` | Verify repo via `provider.get_repository()`, auto-fill `default_branch` & `external_repo_id` (`409` on duplicate) |
| `GET` | `/projects/{id}` | Any | `200` | Single project details |
| `GET` | `/projects/{id}/refs` | Any | `200` | Branch/tag autocomplete (`?q=&limit=30`) via `provider.list_refs()` |
| `POST` | `/reviews` | Any | `202` | Create `QUEUED` review job and enqueue `run_review_task` (`422` if `base_ref == target_ref`) |
| `GET` | `/reviews` | Any | `200` | List org review jobs (`?project_id=&status=&limit=20&offset=0`) |
| `GET` | `/reviews/{id}` | Any | `200` | Full `ReviewDetailResponse` (job + `risk_rating` + sorted hazards + prerequisites + `signoff_status`) |
| `PUT` | `/hazards/{id}/acknowledgement` | Any | `200` | Acknowledge hazard + append `HAZARD_ACKNOWLEDGED` audit event |
| `DELETE` | `/hazards/{id}/acknowledgement` | Any | `200` | Un-acknowledge hazard + append `HAZARD_UNACKNOWLEDGED` (auto-revokes sign-off if `CRITICAL`/`HIGH`) |
| `PUT` | `/prerequisites/{id}/completion` | Any | `200` | Complete prerequisite + append `PREREQUISITE_COMPLETED` audit event |
| `DELETE` | `/prerequisites/{id}/completion` | Any | `200` | Reopen prerequisite + append `PREREQUISITE_REOPENED` (auto-revokes sign-off) |
| `POST` | `/reviews/{id}/signoff` | Any | `200` | Sign off release (`409` if gate not met) + append `RELEASE_SIGNED_OFF` |
| `DELETE` | `/reviews/{id}/signoff` | Any | `200` | Revoke release sign-off + append `SIGNOFF_REVOKED` |
| `GET` | `/reviews/{id}/audit` | Any | `200` | Chronological `ReviewAuditEvent` trail for a review job |

---

## 4. Sign-Off Rules & Audit Trail

1. **Sign-off eligibility (`can_sign_off`):**
   - `job.status == COMPLETED`
   - All `CRITICAL` and `HIGH` hazards are acknowledged (`acknowledged_at IS NOT NULL`). `MEDIUM` and `LOW` hazards are advisory.
   - All deployment prerequisites (`REQUIRED_ENV_VAR` and `PRE_DEPLOY_ACTION`) are completed (`completed_at IS NOT NULL`).
2. **Override note on `BLOCKED`:**
   - When `job.verdict == BLOCKED`, `POST /reviews/{id}/signoff` requires a non-empty `note` of at least 10 characters explaining the override (`409 Conflict` otherwise).
3. **Automatic revocation:**
   - If a release is currently signed off and a user un-acknowledges a `CRITICAL`/`HIGH` hazard or reopens a prerequisite, `app/services/signoff.py` automatically appends a `SIGNOFF_REVOKED` audit event so the audit log explicitly records why sign-off was lost.
4. **Actor snapshot:**
   - Every `ReviewAuditEvent` stores `actor_display = "{display_name} <{email}>"` so the audit trail remains intact even if a user row is later deleted (`actor_id ON DELETE SET NULL`).

---

## 5. Operator CLI

```bash
# Create an org (if missing), onboard a user, and print a one-time rr_... token:
uv run rr create-user --org "Acme Corp" --email lead@acme.io --name "Alice Lead" --role ADMIN

# Issue an additional token (e.g. for CI):
uv run rr create-token --email lead@acme.io --name release-ci --days 90
```

---

## 6. Verification

- **Backend:** 313 tests pass (`uv run pytest`), including `tests/api/test_api_v1.py` covering Bearer/cookie auth, token revocation killing active sessions, RBAC (`ADMIN` vs `MEMBER`), multi-org `404` isolation, live Git connection & project onboarding, review job queueing + worker execution, sign-off gates, automatic sign-off revocation, and CLI helpers.
- **Frontend contract:** `npm run gen:api`, `npm run lint`, `npm run build`, and `npm test` all succeed with updated `frontend/src/api/schema.d.ts`.
