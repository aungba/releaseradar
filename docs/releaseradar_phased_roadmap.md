# ReleaseRadar — Phased Build & Go-to-Market Plan

**Purpose of this document:** The original PRD specs the full product. This document re-sequences that same product into phases sized for your actual starting conditions — a bootstrapped, self-funded founder, 4 part-time devs, some existing validation, and no dedicated GTM yet, competing in a market where CodeRabbit and Qodo already ship PR-level "blast radius" detection.

The guiding rule across every phase: **don't build further than your evidence supports.** Each phase ends with a Go/No-Go checkpoint — if it's not met, stop and fix the gap before spending more time or money, rather than continuing on momentum.

---

## Phase 0 — Validate & Commit
**Timeline:** Weeks 0–2 · **Team:** Current 4 part-time devs (no new hires) · **Spend:** $0 beyond existing commitments

**Goal:** Convert your existing informal validation into committed pilot usage — this is the single highest-leverage thing to do before writing more code.

**Actions:**
- Go back to the teams who gave you validation. Ask them directly: *"Would you actively use a rough version of this for 4 weeks and give real feedback? Would you pay something, even a small pilot fee, for it?"*
- Get to **2–3 committed pilot teams** — not polite interest, actual commitment to use it.
- Ask those pilots which surface they'd reach for first: the **on-demand Pre-Deploy Comparator** (run before a release) or the **Daily EOD Digest** (passive, manager-facing). Their answer picks your Phase 1 wedge — don't guess this internally.

**Go/No-Go to enter Phase 1:** At least 2 committed pilot teams. If you can't get 2 teams to commit even informally, that's real signal to pause before building further — fix this before scope, not after.

---

## Phase 1 — Core Engine + Single Wedge (Pre-Deploy Comparator)
**Timeline:** Weeks 3–10 (~8 weeks) · **Team:** Current 4 part-time devs · **Spend:** LLM API costs only

**Goal:** Ship the smallest version of the product that delivers the core validated value: *"30-second automated risk report instead of 2+ hours of manual commit review."*

**In scope (deliberately narrow):**
- Ingestion pipeline: **GitHub + GitLab** (cloud and self-hosted) behind a provider-agnostic `GitProvider` interface, authenticated with provider access tokens. Bitbucket is additive later, not a rewrite
- API diff retrieval + deterministic pre-filtering (lockfiles, build artifacts, media) — from PRD §4.1
- File priority tiering (Tier 1/2/3) — from PRD §4.1
- LLM structured output engine (PRD §6 Pydantic contract) — this is your core IP, build it well
- **Arbitrary Commit-Range Comparator only** (PRD §3.2) — verdict, risk score, breaking changes matrix, rollback safety score
- Minimal schema: `organizations`, `projects`, `review_jobs`, `review_hazards` (PRD §5) — skip multi-tenant polish beyond what pilots need
- Bare UI: input toolbar + executive banner + hazard matrix (PRD §7.2) — functional, not polished

**Explicitly out of scope for Phase 1:**
- Daily EOD Digest (Phase 2)
- PR webhook auto-check (Phase 2+)
- Bitbucket support (Phase 4, or earlier on pilot demand)
- Any pricing tier, billing, or self-serve signup (pilots are manually onboarded by you)
- Enterprise anything (SSO, self-hosted, BYOK)

**Go/No-Go to enter Phase 2:** Pilot teams are actually running the Comparator against real releases, unprompted, more than once. If they only use it when you remind them, the value isn't landing yet — iterate here before adding the next surface.

---

## Phase 2 — Differentiation Layer: Daily EOD Digest
**Timeline:** Weeks 11–16 (~6 weeks) · **Team:** Current 4 part-time devs · **Spend:** LLM API costs, still no hires

**Goal:** Add the feature that differentiates you from CodeRabbit/Qodo — they're PR-comment bots read by individual engineers; this is a manager-facing rollup nobody else leads with.

**In scope:**
- Celery/Redis scheduler for nightly cron jobs (PRD §9 Phase 3 content, moved here)
- Daily EOD Radar feed UI (PRD §7.1)
- Slack webhook delivery (PRD §9)

**Still out of scope:** PR webhook auto-check, multi-provider, billing, Enterprise tier.

**Go/No-Go to enter Phase 3:** At least one pilot team says they'd pay for this, and ideally one actually does (even a nominal pilot fee). This is the real signal that separates "useful" from "fundable."

---

## Phase 3 — Convert to Paid, First Hiring Decision
**Timeline:** Weeks 17–20 · **Team:** 4 part-time devs + **first potential full-time hire, conditional** · **Spend:** Begins real salary risk if hiring triggers

**Goal:** Turn pilots into real paying customers and decide — with evidence, not optimism — whether to spend your savings on a full-time hire.

**In scope:**
- Launch **two** pricing tiers only: Free and Team (drop the 3-tier structure from PRD §8 for now — Enterprise stays deferred)
- Self-serve signup flow for Team tier
- **Someone explicitly owns GTM starting now** — dev.to-style write-ups, community posts, outbound to similar teams — not an afterthought, a named responsibility

**Hiring trigger (only hire if ALL of these are true):**
- You have paying customers generating real MRR, not just pilot interest
- Your personal runway can sustain the salary for at least 6 months without requiring that hire to immediately 10x output
- The hire's role is clearly defined by what's actually bottlenecking you (engineering throughput vs. GTM — don't default to "another engineer" if GTM is still the gap)

**Go/No-Go to enter Phase 4:** Sustained month-over-month growth in paying Team-tier customers, not a one-time spike.

---

## Phase 4 — Scale the Team Tier
**Timeline:** Months 6–9 · **Team:** Core team + 1 hire from Phase 3, second hire conditional

**In scope:**
- Bitbucket support (PRD §4.1 multi-provider; GitHub + GitLab already shipped in Phase 1)
- PR webhook auto-check (PRD §3.3)
- Deeper hazard categories, improved LLM accuracy/calibration (this is ongoing tuning work, treat it as such, not a one-time build)

**Second hire trigger:** Same discipline as Phase 3 — tied to sustained revenue growth, not founder optimism.

---

## Phase 5 — Enterprise Tier (Conditional, Not Pre-Committed)
**Timeline:** Month 10+, only if triggered

**Do not build this speculatively.** Enter this phase only when:
- Team-tier customers are explicitly asking for SSO/self-hosted/BYOK (inbound demand, not your assumption)
- You have revenue or funding to support an actual sales motion for $15k–40k deals — this tier doesn't sell itself the way Free/Team can

**In scope when triggered:** Self-hosted Docker deployment, BYOK, SAML/Okta SSO, custom rule definitions — all from PRD §8 Enterprise row, unchanged.

---

## Summary Table

| Phase | Weeks | New Spend | Hiring | Key Deliverable | Go/No-Go Gate |
|---|---|---|---|---|---|
| 0 | 0–2 | None | None | 2–3 committed pilots | Pilots actually committed |
| 1 | 3–10 | LLM API only | None | Core engine + Comparator (GitHub + GitLab) | Pilots use it unprompted |
| 2 | 11–16 | LLM API only | None | EOD Digest | One pilot willing to pay |
| 3 | 17–20 | Possible 1st salary | Conditional, evidence-gated | Paid Team tier + GTM owner | Sustained paying growth |
| 4 | Months 6–9 | 2nd salary possible | Conditional | Bitbucket, PR webhook | Continued MRR growth |
| 5 | Month 10+ | Sales motion | Conditional | Enterprise tier | Inbound demand + funded sales capacity |

**The core discipline this plan enforces:** every phase after Phase 0 is gated by evidence from the previous one, not by the original PRD's fixed 6-week roadmap. Given you're spending personal savings from Phase 3 onward, this sequencing is what keeps that risk bounded — you only pay for the next phase once the current one has actually proven itself.
