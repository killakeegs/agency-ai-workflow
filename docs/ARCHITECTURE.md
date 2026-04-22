# System Architecture

**Last updated: 2026-04-22**

This document describes the agency workflow system as a layered architecture. Read this when you need to:
- Understand where a new piece of functionality should live
- Decide whether something is an agent, service, or integration
- Trace why something broke and what else might be affected
- Onboard a new person (or a new Claude Code session) to the full picture

Companion: `CLAUDE.md` is the working-reference (operational details per feature, commands, pipeline flow). **ARCHITECTURE.md is the conceptual map** (how the pieces relate).

---

## The 6-Layer Hierarchy

```
┌──────────────────────────────────────────────────────┐
│ LAYER 6 — USER INTERFACES                            │
│  • Rex (Slack)                                       │
│  • Make commands (`make onboard`, `make sitemap`…)   │
│  • Interactive menu (`make run`)                     │
│  • Direct script invocation (dev only)               │
└──────────────────────────────────────────────────────┘
              ↑ triggers ↑
┌──────────────────────────────────────────────────────┐
│ LAYER 5 — ORCHESTRATORS                              │
│  • Railway crons: email_monitor, meeting_processor,  │
│    morning_briefing, meeting_prep, care_plan_report  │
│  • Make scripts (scripts/pipeline/)                  │
│  • Rex tool dispatchers (rex/tools/)                 │
└──────────────────────────────────────────────────────┘
              ↑ invokes ↑
┌──────────────────────────────────────────────────────┐
│ LAYER 4 — AGENTS (LLM-driven, one primary DB each)   │
│  BUILT: Onboarding, Sitemap, Content, ImageGen       │
│  PLANNED: 7 SEO agents, Blog, Social, PaidAds        │
└──────────────────────────────────────────────────────┘
              ↑ consumes ↑
┌──────────────────────────────────────────────────────┐
│ LAYER 3 — SERVICES (shared business logic)           │
│  • email_enrichment                                  │
│  • style_reference                                   │
│  • business_profile populator                        │
└──────────────────────────────────────────────────────┘
              ↑ calls ↑
┌──────────────────────────────────────────────────────┐
│ LAYER 2 — INTEGRATIONS (pure API wrappers)           │
│  • notion   • clickup   • gmail                      │
│  • google_calendar                                   │
└──────────────────────────────────────────────────────┘
              ↑ speaks to ↑
┌──────────────────────────────────────────────────────┐
│ LAYER 1 — DATA SOURCES                               │
│  • Notion workspace (source of truth)                │
│  • External APIs: Gmail, Calendar, ClickUp, GSC,     │
│    GA4, GBP, DataForSEO, Search Atlas, Replicate,    │
│    Pexels, Webflow, Slack                            │
└──────────────────────────────────────────────────────┘
```

### The Dependency Rule

**Dependencies flow up only.** A higher layer may import from any layer below it. A lower layer must never import from a higher layer.

Examples:
- ✅ An orchestrator (5) calling an agent (4) → OK
- ✅ An agent (4) calling a service (3) → OK
- ✅ A service (3) calling an integration (2) → OK
- ❌ A service (3) importing from an agent (4) → **breaks the rule**
- ❌ An integration (2) calling a service (3) → **breaks the rule**

**Why this matters:** when dependencies only flow up, a break at one layer only affects layers above it. If you change `src/integrations/notion.py`, you only need to verify layers 3–6. If you change `src/services/email_enrichment.py`, only layers 4–6 need verification. This is how a 3-person team scales to a system with dozens of agents without the whole thing becoming unshippable.

---

## Layer 1 — Data

**Notion is the single source of truth.** All structured client data lives in Notion databases. Everything else is a data source we READ from (and occasionally write to).

### Notion structure per client
- **Client Info DB** — pipeline stage, contacts, services, vertical, template
- **Client Log DB** — chronological timeline of every interaction (meetings, emails, calls)
- **Brand Guidelines DB** — voice, colors, fonts, photography style, reviewer info
- **Business Profile page** — deep client knowledge, 12+ sections
- **Care Plan DB** — monthly PageSpeed + ADA status (if care plan active)

### Notion structure when service-specific stages start
- **Sitemap DB** — page hierarchy, slugs, types, sections, status
- **Page Content DB** — copy per page, SEO fields, body blocks
- **Images DB** — stock + AI images
- **Keywords DB, Competitors DB, SEO Metrics DB** — SEO clients
- **Blog Posts DB, Social Posts DB, GBP Posts DB** — content retainer clients

### Workspace-level DBs (shared across all clients)
- **Clients DB** — master registry
- **Meeting Transcripts DB** — Notion AI dumps here after every call
- **Flags DB** — blockers, open actions, risks, wins across all clients
- **Email Monitor State DB** — monitor cursor + alerted thread dedup cache

### External data (read-only or write-only)
| Source | Access | Purpose |
|---|---|---|
| Gmail | OAuth refresh token | Email monitoring, sending, drafts |
| Google Calendar | OAuth refresh token | Meeting prep, follow-up recipient lookup |
| Google Search Console | OAuth refresh token | SEO reporting (planned agent consumption) |
| Google Analytics 4 | OAuth refresh token | SEO reporting |
| Google Business Profile | OAuth refresh token | GBP posts, reviews, upcoming metrics |
| ClickUp | API token | Task creation, workspace browsing |
| DataForSEO | Basic auth | Keyword volumes, SERP, backlinks |
| Search Atlas | API key | Keyword tracker (grid rank planned) |
| Replicate | API key | Image generation (Flux Schnell) |
| Pexels | API key | Stock photography |
| Slack | Bot token | Rex, alerts, briefings |
| Webflow | API token | Blog publishing, title/meta push (CMS) |

---

## Layer 2 — Integrations (`src/integrations/`)

**Rule: pure API wrappers. No business logic.**

| File | Wraps | Provides |
|---|---|---|
| `notion.py` | Notion API | Raw HTTP client (bypasses broken v3 SDK methods) |
| `clickup.py` | ClickUp REST | Task create, workspace browse |
| `gmail.py` | Gmail API | OAuth, search, fetch, thread summarize, noise filter |
| `google_calendar.py` | Calendar API | Event lookup by time, attendee extraction |
| `business_profile.py` | Notion (page-block specific) | Loads + updates Business Profile page content |

### When to add a new integration
- You're talking to a new external API for the first time
- The module has zero agency logic (no Claude calls, no "if client has X, do Y")
- It's a thin wrapper that any agent/service could call the same way

### When to NOT add a new integration
- You need Claude to decide something (that's an agent)
- The logic is "given client data, do X" (that's a service)

---

## Layer 3 — Services (`src/services/`)

**Rule: shared business logic, no LLM calls from here directly (unless encapsulated behind a single utility), and no orchestration.**

| File | Purpose | Consumed by |
|---|---|---|
| `email_enrichment.py` | Thread synthesis, dedup, Client Log writing, profile enrichment, flag writing | `email_monitor.py`, `enrich_from_emails.py` |
| `style_reference.py` | Feedback-loop service. Agents log approved/rejected outputs; future runs pull recent examples for per-client voice continuity | *Not yet wired into agents* (planned) |

**Also service-like (ambient across modules):**
- `src/integrations/business_profile.py` has a `populate_from_meeting()` function that's really a service (has agency logic, calls Claude) — should eventually move to `src/services/business_profile.py`

### When to add a new service
- Same logic is called by two or more scripts/agents/orchestrators
- The logic is stateful or involves Notion read+write coordination
- You find yourself copy-pasting helpers between scripts

---

## Layer 4 — Agents (`src/agents/`)

**Rule: one `run()` method per agent. One primary Notion DB per agent.** Agents inherit from `BaseAgent` which provides the shared Notion/Anthropic clients, retry logic, and logging.

### Built (4 agents)

| Agent | Primary DB | Stage | Trigger |
|---|---|---|---|
| **OnboardingAgent** | Client Info DB (+ sets up 4 base DBs total) | ONBOARDING | `make onboard` |
| **SitemapAgent** | Sitemap DB | SITEMAP_DRAFT | `make sitemap` |
| **ContentAgent** | Page Content DB | CONTENT_DRAFT | `make content` |
| **ImageGenerationAgent** | Images DB | IMAGES | `make images-brand`, `make images-pages` |

### Planned (8 agents)

See SEO strategic plan in Notion (`notion.so/349f7f45333e816fa756dacd373f21f2`) for the full 7-agent SEO architecture. Plus:

- **BlogAgent** — would own Blog Posts DB (currently 4 scripts instead)
- **SocialAgent** — would own Social Posts DB (currently 2 scripts instead)
- **PaidAdsAgent** — greenfield, zero code yet

### Agent design principles (non-negotiable)

These come from `CLAUDE.md`:

1. **One `run()` method per agent, returns a typed dict.** No creeping scope.
2. **One primary Notion DB per agent's output.** Keep the blast radius narrow.
3. **Inherit `BaseAgent`.** Shared Notion/Anthropic clients, logging, retries, rate limiting.
4. **Agents don't start themselves.** Triggered only by: make command, Railway cron, Rex, or webhook.
5. **Templates and prompts live in `config/`, not agent code.** Per-vertical customization via config only.
6. **Strategy vs execution separation.** A strategy agent proposes → human approves → execution agent acts.
7. **Dry-run mode mandatory.** Every agent must support a "show me what you'd do without writing" mode.
8. **Write to Notion, not to the file system.** File output is only for visual review artifacts.
9. **Agents don't truly delete.** Append, mark stale, or use `in_trash: True`. Always reversible.
10. **Rex tool per agent.** Every agent gets a Rex-callable tool so the team can invoke via Slack.

### When to add a new agent vs. extend an existing one

**Add a new agent when:**
- The output lives in a new primary DB that no existing agent owns
- The trigger is distinct (new pipeline stage, new cron, new Rex command)
- The prompt/strategy is fundamentally different from what existing agents do

**Extend an existing agent when:**
- The new capability writes to an existing agent's DB
- It's a natural mode/flag on the existing agent's `run()` method
- Example: ImageGenerationAgent has `mode="brand"` vs `mode="page"` — same agent, different output types

---

## Layer 5 — Orchestrators

**Rule: orchestrators invoke agents and services. They never implement agency logic themselves.**

### Railway crons (always running)

| Cron | Schedule | Script | What it does |
|---|---|---|---|
| Email Monitor | `*/15 * * * *` | `scripts/enrichment/email_monitor.py` | Routes new emails to clients, writes Client Log, raises flags |
| Meeting Processor | `*/5 * * * *` | `scripts/enrichment/meeting_processor.py` | Processes Notion AI transcripts into notes + Gmail draft + ClickUp tasks |
| Morning Briefing | `0 15 * * *` (7am PST) | `scripts/enrichment/morning_briefing.py` | Agency pulse + per-team-member overdue task DMs |
| Meeting Prep | (per calendar lookup) | `scripts/enrichment/meeting_prep.py` | Generates prep docs for today's meetings |
| Care Plan | `0 9 1 * *` (1st @ 4am CT) | `scripts/care/care_plan_report.py` | Monthly PageSpeed + care plan report per client |

### Make-command orchestrators (on demand)
- `scripts/pipeline/run.py` — interactive menu
- `scripts/pipeline/run_pipeline_stage.py` — direct stage invocation
- `scripts/pipeline/advance_pipeline.py` — checks approval + runs next stage

### Rex tool dispatchers (`rex/tools/`)
Each module is its own dispatcher; Rex's `app.py` routes Slack messages to them. See Layer 6.

### When to add a new orchestrator
- New event source (new cron schedule, new webhook, new queue)
- New aggregation of existing agents/services (a "super-flow" that runs multiple agents in sequence with human gates)

### When to NOT add a new orchestrator
- You want to re-run one agent manually — that's a make target, not a new orchestrator
- You want Rex to trigger something — that's a Rex tool, not an orchestrator

---

## Layer 6 — User Interfaces

### Rex (`rex/`) — the conversational interface

| Module | Purpose |
|---|---|
| `app.py` | FastAPI + Slack-bolt entrypoint, message dispatcher, thread memory |
| `tools/notion_tools.py` | Read client DBs (pipeline, sitemap, content, keywords, competitors, GBP posts, action items, care plan) |
| `tools/clickup_tools.py` | Workspace browsing, task creation |
| `tools/pipeline_tools.py` | Trigger pipeline stages via subprocess |
| `tools/meeting_tools.py` | Parse transcripts, write Client Log, create ClickUp tasks, draft follow-up emails |
| `tools/email_tools.py` | Send Gmail on behalf of Keegan |

**Rex is NOT an agent.** It's a dispatcher that exposes a multi-tool LLM conversation layer over the existing agents + services + integrations. When an agent is built, it gets a Rex tool definition so the team can invoke it conversationally.

### Make commands
Entry points for everything runnable by hand. See `CLAUDE.md` Make Commands Reference.

### Interactive menu (`make run`)
Lightweight CLI menu. Newcomer-friendly for test runs. Same underlying scripts as Make.

### Direct script invocation
Dev only. Never the production entry point.

---

## Per-Domain Build Status

| Domain | Built | Planned | Blocker |
|---|---|---|---|
| **Website Build** | ✅ 4 agents + 20 scripts | Minor: Webflow CMS push script | Developer finishing master templates |
| **SEO** | ❌ 0 agents | 📋 7 agents per Notion plan | 8 open questions in Andrea review |
| **Blog** | ❌ 0 agents | 📋 BlogAgent | Webflow blog template + post volume defaults |
| **Social** | ❌ 0 agents | 📋 SocialAgent | Per-client voice calibration wiring |
| **Email Enrichment** | ✅ Service + Railway cron | None needed | — |
| **Meeting Ops** | ✅ Railway cron + Rex tool | None critical | — |
| **Care Plan** | ✅ Monthly report cron | Possibly: alerting on score drops | — |
| **Onboarding** | ✅ 1 agent + 4 scripts | Minor: split into provision/register/notify | None — only when re-run failures become painful |
| **Paid Ads** | ❌ Zero code | 📋 PaidAdsAgent | Scope not yet defined |

### Script-collections waiting for their agent

These domains have scripts doing the work today but no parent agent yet:

**SEO (`scripts/seo/`)** — 10 scripts:
- `keyword_research.py`, `competitor_research.py`, `battle_plan.py`, `gbp_posts.py`, `gbp_reviews.py`, `seo_report.py`, `seo_init.py` (baseline), `seo_activate.py`, `suggest_keywords.py`, `style_reference_init.py`, `style_reference_sweep.py`

**Blog (`scripts/blog/`)** — 4 scripts:
- `blog_setup.py`, `blog_ideas.py`, `blog_write.py`, `blog_publish.py`

**Social (`scripts/social/`)** — 2 scripts:
- `social_posts.py`, `linkedin_posts.py`

These scripts are the v0. Once their respective agents are built, scripts become orchestrator wrappers around the agents, and business logic migrates into the agent modules.

---

## Known Architectural Debt

Three places where current code bends the hierarchy rules. Worth fixing, but not urgent.

### 1. OnboardingAgent writes to 4+ DBs

Violates "one primary DB per agent." If one step in the 6-step onboarding sequence fails, the others are left in partial states. Fix: split into `provision_structure` / `register` / `notify`, keep `OnboardingAgent.run()` as a thin pass-through for existing callers. Defer until the next actual re-run failure.

### 2. ContentAgent does copy AND Notion block-formatting

The `_page_blocks()` function builds Notion blocks — that's Layer 2/3 work, not Layer 4. Fix: move to `src/integrations/notion_blocks.py` (or `src/services/content_rendering.py`). Low risk, good warm-up refactor, enables BlogAgent + SocialAgent to reuse the formatter without importing ContentAgent.

### 3. SEO script logic mixes layers

`battle_plan.py` has synthesis logic (service-layer work) embedded with I/O (orchestrator work). Same for `keyword_research.py`. Fix: extract logic into `src/services/seo/` when SEOAgent build kicks off. **Do NOT refactor pre-emptively** — Andrea's review may reshape the architecture, and the scripts are actively used in production.

---

## Conventions Worth Knowing

### Notion callout blocks mark team-only metadata
ContentAgent wraps SEO summaries + Internal Notes in Notion callout blocks tagged with `(team-only — not user-facing)` or `(team reference — not user-facing)`. Renderers (like the Crown Astro site) detect these markers and skip them.

### Divider headings structure content
ContentAgent writes pages with `── Hero Section ──`, `── Page Sections ──`, `── FAQs ──` H2 dividers. Renderers parse these as zone transitions. `── SEO ──`, `── Internal Notes ──`, `── Dev Notes ──` are discard markers.

### Config-driven personalization
Per-vertical customization lives in `config/` (page_sections, sitemap_templates, keyword_strategy), never in agent prompts. Prompts reference config values at runtime.

### Flags DB is the workspace-wide signal layer
Blockers, open actions, wins, scope changes — all flagged into one DB with a lifecycle (Open → Resolved). Email monitor, meeting processor, and future SEOAgent all write here. Rex reads here.

### Per-client Slack channels
Every major client has their own Slack channel (`#crown`, `#twinriverberries`, etc.). Alerts route to the client's channel, not a generic #agency-pipeline.

---

## How to Add Things Cleanly

### New integration
1. Create `src/integrations/<name>.py`
2. Pure API wrapper, no business logic
3. Environment variable for auth in `.env` + document in `CLAUDE.md`

### New service
1. Create `src/services/<name>.py`
2. Stateless functions preferred
3. Can import from integrations (Layer 2) only
4. Pre-check: is this logic used by 2+ consumers? If no, it's probably still a script.

### New agent
1. Create `src/agents/<name>.py`, inherit `BaseAgent`
2. One `run()` method, one primary DB
3. Prompts + templates → `config/`
4. Add Makefile target (`make <stage>`)
5. Add Rex tool definition in `rex/tools/` if team-callable
6. Document in `CLAUDE.md` Agent Architecture section
7. Support dry-run mode

### New orchestrator (cron)
1. Decide: Railway cron vs Make command vs Rex trigger
2. Script in `scripts/<category>/`
3. Never put business logic in the script — call services + agents
4. If Railway cron: document schedule + command in `CLAUDE.md`

### New Rex tool
1. Add handler function in `rex/tools/<category>.py`
2. Register tool definition in `rex/app.py`
3. Keep handler thin — call agents/services, don't reimplement their logic

---

## Related Plans & Memories

- **SEO strategic plan** — `notion.so/349f7f45333e816fa756dacd373f21f2`. 7-agent architecture, 3-release rollout, in review with Andrea.
- **Memory `project_ai_first_seo_plan.md`** — index + current blockers for SEO plan
- **Memory `project_subagent_roadmap.md`** — phased build plan for SEO, Blog, Social, PaidAds
- **Memory `project_rex_operations_director.md`** — Rex's expanding role
- **Memory `feedback_what_stays_human.md`** — 7 categories of work that stay human regardless of automation
- **Memory `feedback_rollout_with_adoption_gates.md`** — preferred rollout pattern for new agent systems

---

## Maintenance

**Update this doc when:**
- A new agent is built (add to Layer 4 table)
- A new integration is added (add to Layer 2 table)
- A new Railway cron is deployed (add to Layer 5 table)
- Architectural debt is paid down (remove from Known Debt section)
- The SEO plan moves from planned to built (update per-domain status)

**Do NOT update this doc for:**
- One-off script changes
- Prompt tweaks
- Schema additions to existing DBs

Those live in commit messages and per-file comments.
