# RxMedia Agency Pipeline — shortcuts
# Usage: make run
#        make content
#        make sitemap
#
# For revisions: make content NOTES="homepage feels too formal, loosen the tone"

PYTHON := .venv/bin/python3
CLIENT ?=
NOTES  ?=

# ── Interactive menu (recommended) ───────────────────────────────────────────

run:
	@$(PYTHON) scripts/pipeline/run.py

# ── Direct stage shortcuts ────────────────────────────────────────────────────

sitemap:
	@$(PYTHON) scripts/pipeline/run_pipeline_stage.py --stage sitemap --client $(CLIENT) \
	  $(if $(NOTES),--revision "$(NOTES)",)
	@$(PYTHON) scripts/visual/generate_sitemap_visual.py --client $(CLIENT) --notion
	@$(PYTHON) scripts/pipeline/advance_pipeline.py --client $(CLIENT) --mark-pending

keyword-research:
	@$(PYTHON) scripts/seo/keyword_research.py --client $(CLIENT) \
	  $(if $(EXPORT),--export,) \
	  $(if $(FORCE),--force,) \
	  $(if $(YES),--yes,) \
	  $(if $(OPEN),--open,)

competitor-research:
	@$(PYTHON) scripts/seo/competitor_research.py --client $(CLIENT) \
	  $(if $(LIMIT),--limit $(LIMIT),) \
	  $(if $(FORCE),--force,) \
	  $(if $(YES),--yes,) \
	  $(if $(ENRICH),--enrich-only,)

suggest-keywords:
	@$(PYTHON) scripts/seo/suggest_keywords.py --client $(CLIENT) \
	  $(if $(FORCE),--force,)

approve-sitemap:
	@$(PYTHON) -c "\
import asyncio, sys; sys.path.insert(0, '.'); \
from config.clients import CLIENTS; from src.config import settings; from src.integrations.notion import NotionClient; \
async def run(): \
    notion = NotionClient(settings.notion_api_key); \
    entries = await notion.query_database(CLIENTS['$(CLIENT)']['sitemap_db_id']); \
    [await notion._client.request(path=f'pages/{e[\"id\"]}', method='PATCH', body={'properties': {'Status': {'select': {'name': 'Approved'}}}}) for e in entries]; \
    print(f'Done — {len(entries)} sitemap pages set to Approved'); \
asyncio.run(run())"

content:
	@$(PYTHON) scripts/pipeline/run_pipeline_stage.py --stage content --client $(CLIENT) \
	  $(if $(NOTES),--revision "$(NOTES)",)
	@$(PYTHON) scripts/pipeline/advance_pipeline.py --client $(CLIENT) --mark-pending

# ── Visual generators ─────────────────────────────────────────────────────────

stock-images:
	@$(PYTHON) scripts/visual/fetch_stock_images.py --client $(CLIENT) \
	  $(if $(NOTES),--notes "$(NOTES)",) \
	  $(if $(PHOTOGRAPHER),--photographer "$(PHOTOGRAPHER)",) \
	  $(if $(FILL),--fill,) \
	  $(if $(COMMIT),--commit,) \
	  $(if $(OPEN),--open,)

images-brand:
	@$(PYTHON) scripts/visual/generate_images.py --client $(CLIENT) --mode brand \
	  $(if $(NOTES),--revision "$(NOTES)",) \
	  $(if $(OPEN),--open,)

images-pages:
	@$(PYTHON) scripts/visual/generate_images.py --client $(CLIENT) --mode pages \
	  $(if $(NOTES),--revision "$(NOTES)",) \
	  $(if $(OPEN),--open,)

mood-board-visuals:
	@$(PYTHON) scripts/visual/generate_mood_board_visuals.py --client $(CLIENT) --open

sitemap-visuals:
	@$(PYTHON) scripts/visual/generate_sitemap_visual.py --client $(CLIENT) --open --notion

brand-export:
	@$(PYTHON) scripts/visual/export_brand_guidelines.py --client $(CLIENT) --open

relume-sitemap:
	@$(PYTHON) scripts/visual/export_relume_sitemap.py --client $(CLIENT) \
	  $(if $(OPEN),--open,)

relume-export:
	@$(PYTHON) scripts/visual/export_relume_prompt.py --client $(CLIENT) --open

# ── Blog pipeline ─────────────────────────────────────────────────────────────

blog-setup:
	@$(PYTHON) scripts/blog/blog_setup.py --client $(CLIENT)

blog-ideas:
	@$(PYTHON) scripts/blog/blog_ideas.py --client $(CLIENT) \
	  $(if $(FORCE),--force,)

blog-write:
	@$(PYTHON) scripts/blog/blog_write.py --client $(CLIENT) \
	  $(if $(NOTES),--notes "$(NOTES)",)

blog-publish:
	@$(PYTHON) scripts/blog/blog_publish.py --client $(CLIENT) \
	  $(if $(COMMIT),--commit,) \
	  $(if $(ALL),--all,)

# ── Care plan ─────────────────────────────────────────────────────────────────

care-plan:
	@$(PYTHON) scripts/care/care_plan_report.py \
	  $(if $(CLIENT),--client $(CLIENT),)

care-plan-init:
	@$(PYTHON) scripts/care/care_plan_report.py --init --client $(CLIENT)

# ── SEO ───────────────────────────────────────────────────────────────────────

# One-time setup for existing clients (new clients get these DBs via make onboard)
seo-init:
	@$(PYTHON) scripts/seo/seo_init.py --client $(CLIENT)

# Step 1 (one-time): create Battle Plan Input page in Notion for team to fill in
battle-plan-init:
	@$(PYTHON) scripts/seo/battle_plan.py --client $(CLIENT) --init

# Step 2: generate full battle plan from Notion data + competitor/keyword rows
battle-plan:
	@$(PYTHON) scripts/seo/battle_plan.py --client $(CLIENT) \
	  $(if $(NOTES),--notes "$(NOTES)",)

gbp-posts:
	@$(PYTHON) scripts/seo/gbp_posts.py --client $(CLIENT) \
	  $(if $(MONTH),--month "$(MONTH)",) \
	  $(if $(NOTES),--notes "$(NOTES)",)

gbp-reviews:
	@$(PYTHON) scripts/seo/gbp_reviews.py --client $(CLIENT) \
	  $(if $(DRY_RUN),--dry-run,)

# ── Social media pipeline ──────────────────────────────────────────────────────

social-posts:
	@$(PYTHON) scripts/social/social_posts.py --client $(CLIENT) \
	  $(if $(MONTH),--month "$(MONTH)",) \
	  $(if $(NOTES),--notes "$(NOTES)",)

linkedin-posts:
	@$(PYTHON) scripts/social/linkedin_posts.py --client $(CLIENT) \
	  $(if $(MONTH),--month "$(MONTH)",) \
	  $(if $(NOTES),--notes "$(NOTES)",)

seo-baseline:
	@$(PYTHON) scripts/seo/seo_report.py --client $(CLIENT) --baseline \
	  $(if $(OPEN),--open,)

seo-report:
	@$(PYTHON) scripts/seo/seo_report.py --client $(CLIENT) --monthly \
	  $(if $(MONTH),--month "$(MONTH)",) \
	  $(if $(OPEN),--open,)

# Activate full SEO retainer for a client (creates SEO Metrics DB, sets gbp_location_id)
seo-activate:
	@$(PYTHON) scripts/seo/seo_activate.py --client $(CLIENT) \
	  $(if $(GBP_ID),--gbp-location-id "$(GBP_ID)",) \
	  $(if $(GSC_URL),--gsc-site-url "$(GSC_URL)",) \
	  $(if $(GA4_ID),--ga4-property-id "$(GA4_ID)",) \
	  $(if $(SA_PROJECT),--search-atlas-project-id "$(SA_PROJECT)",)

# Create the Style Reference DB — agent feedback loop (approvals/rejections/edits)
style-reference-init:
	@$(PYTHON) scripts/seo/style_reference_init.py --client $(CLIENT)

# Generate the Local SEO Setup Checklist page for a client (GBP + citations +
# vertical-specific directories + NAP canonicals + foundation). Gate for agent
# activation: rank monitor only produces meaningful data once Tier 1 is done.
local-setup-init:
	@$(PYTHON) scripts/seo/local_setup_init.py --client $(CLIENT) \
	  $(if $(DRY),--dry-run,)

# Rank monitor — Target/Ranking/Won lifecycle for approved keywords.
# Polls top-100 SERP per keyword, auto-transitions Status, logs rank history,
# posts win/anomaly/first-appearance flags to the client's Slack channel.
# Local clients: weekly cadence (Mon 6am UTC via Railway cron) is the right default.
#   make rank-monitor CLIENT=x         # one client
#   make rank-monitor ALL=1             # all SEO-active clients (cron mode)
#   make rank-monitor ALL=1 MODE=local  # filter by SEO Mode
#   make rank-monitor CLIENT=x DRY=1    # preview, no Notion / Slack writes
rank-monitor:
	@$(PYTHON) scripts/seo/rank_monitor.py \
	  $(if $(ALL),--all-clients,--client $(CLIENT)) \
	  $(if $(MODE),--seo-mode $(MODE),) \
	  $(if $(LOC),--location-code $(LOC),) \
	  $(if $(DRY),--dry-run,)

# Sweep Content DB + Blog Posts DB → Style Reference (agent feedback loop)
# Default: sweep all eligible clients, both DBs.
#   CLIENT=x            — scope to one client
#   TARGET=content|blog — scope to one DB only (default: both)
#   DRY=1               — preview without writing
style-sweep:
	@$(PYTHON) scripts/seo/style_reference_sweep.py \
	  $(if $(CLIENT),--client $(CLIENT),) \
	  $(if $(TARGET),--target $(TARGET),) \
	  $(if $(DRY),--dry-run,)

# ── Onboarding ────────────────────────────────────────────────────────────────

check-env:
	@$(PYTHON) scripts/setup/check_env.py

check-env-seo:
	@$(PYTHON) scripts/setup/check_env.py --service seo

onboarding-form:
	@$(PYTHON) scripts/onboarding/setup_onboarding_form.py

onboard:
	@$(PYTHON) scripts/onboarding/onboard_client.py

onboard-list:
	@$(PYTHON) scripts/onboarding/onboard_client.py --list

migrate-client:
	@$(PYTHON) scripts/onboarding/migrate_client.py \
		--name "$(NAME)" \
		--services $(SERVICES) \
		--verticals $(VERTICALS) \
		--drive-folder "$(DRIVE)" \
		$(if $(EMAIL),--contact-email "$(EMAIL)",) \
		$(if $(FROM_JSON),--from-json "$(FROM_JSON)",) \
		$(if $(DRY),--dry-run,)

# ── Gmail enrichment ──────────────────────────────────────────────────────────
# Pulls last N days of email threads for a client, synthesizes with Claude,
# writes Client Log entries + Business Profile enrichments + flags.
#   make enrich-emails CLIENT=wellness_works_management_partners
#   make enrich-emails CLIENT=the_manor DAYS=90
#   make enrich-emails CLIENT=skycloud_health DRY=1

enrich-emails:
	@$(PYTHON) scripts/enrichment/enrich_from_emails.py \
		--client $(CLIENT) \
		$(if $(DAYS),--days $(DAYS),) \
		$(if $(MAX),--max-threads $(MAX),) \
		$(if $(DRY),--dry-run,)

# Meeting processor — processes Notion AI transcripts into Client Log + ClickUp + email
#   make meeting-processor                    # Process all unprocessed transcripts
#   make meeting-processor CLIENT=pdx_plumber # Process only for one client

meeting-processor:
	@$(PYTHON) scripts/enrichment/meeting_processor.py \
		$(if $(CLIENT),--client $(CLIENT),)

# Morning briefing — DMs overdue tasks + flags to each team member
#   make morning-briefing          # Live run (posts to Slack)
#   make morning-briefing DRY=1    # Preview only

morning-briefing:
	@$(PYTHON) scripts/enrichment/morning_briefing.py \
		$(if $(DRY),--dry,)

# Meeting Prep DBs — one per client, holds upcoming/recent prep docs
#   make meeting-prep-setup        # Provision Meeting Prep DB for every client (idempotent)
#   make meeting-prep-setup DRY=1  # Preview only
#   make meeting-prep-setup CLIENT=summit_therapy  # Only one client
#   make meeting-prep-archive      # Archive entries older than 90 days (daily cron)
#   make meeting-prep-archive DRY=1 DAYS=60  # Preview with custom cutoff

meeting-prep-setup:
	@$(PYTHON) scripts/setup/add_meeting_prep_dbs.py \
		$(if $(CLIENT),--client $(CLIENT),) \
		$(if $(DRY),--dry,)

meeting-prep-archive:
	@$(PYTHON) scripts/enrichment/archive_meeting_prep.py \
		$(if $(CLIENT),--client $(CLIENT),) \
		$(if $(DAYS),--days $(DAYS),) \
		$(if $(DRY),--dry,)

# Real-time email monitor — one tick across all clients
#   make email-monitor                  # Run one tick (checks since last run)
#   make email-monitor LOOKBACK=120     # First run: check last 2 hours
#   make email-monitor-setup            # Create state DB only

email-monitor:
	@$(PYTHON) scripts/enrichment/email_monitor.py \
		$(if $(LOOKBACK),--lookback $(LOOKBACK),)

email-monitor-setup:
	@$(PYTHON) scripts/enrichment/email_monitor.py --setup

# ── Approval flow ─────────────────────────────────────────────────────────────

advance:
	@$(PYTHON) scripts/pipeline/advance_pipeline.py --client $(CLIENT)

mark-pending:
	@$(PYTHON) scripts/pipeline/advance_pipeline.py --client $(CLIENT) --mark-pending

pipeline-setup:
	@$(PYTHON) scripts/pipeline/advance_pipeline.py --client $(CLIENT) --setup

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  RxMedia Agency Pipeline"
	@echo "  ───────────────────────────────────────────────"
	@echo "  make run              Interactive menu (easiest)"
	@echo ""
	@echo "  PIPELINE STAGES"
	@echo "  make sitemap          Generate sitemap (template-driven + Tier 3 AI suggestions) → Notion"
	@echo "  make content          Generate page copy → Notion"
	@echo ""
	@echo "  REVISIONS (re-run with feedback)"
	@echo "  make sitemap    NOTES=\"Add a FAQs page\""
	@echo "  make content    NOTES=\"Homepage tone needs to be warmer\""
	@echo ""
	@echo "  STOCK PHOTOGRAPHY (Pexels)"
	@echo "  make stock-images OPEN=1             Discovery → HTML report (click Keep/Skip)"
	@echo "  make stock-images NOTES=\"warmer tones\"  Re-run with style notes"
	@echo "  make stock-images PHOTOGRAPHER=\"Name\"   Lean into one photographer"
	@echo "  make stock-images FILL=1 OPEN=1      Keep approved, fill gaps with new images"
	@echo "  make stock-images COMMIT=1           Download approved images + save to Notion"
	@echo ""
	@echo "  AI IMAGE GENERATION (Replicate + Flux Schnell)"
	@echo "  make images-brand         Brand creative library (~15 images) → Notion"
	@echo "  make images-pages         Page-specific images (~3 per page)  → Notion"
	@echo "  make images-brand NOTES=\"Make textures softer\"  Regenerate with feedback"
	@echo ""
	@echo "  VISUAL GENERATORS"
	@echo "  make sitemap-visuals      Generate sitemap HTML + JSON"
	@echo "  make brand-export         Export brand guidelines JSON"
	@echo "  make relume-sitemap       Compact sitemap for Relume AI (text paste)"
	@echo "  make relume-export        Export Relume AI prompt"
	@echo ""
	@echo "  BLOG PIPELINE"
	@echo "  make blog-setup            Create Blog Voice & Author Setup page (one-time per client)"
	@echo "  make blog-ideas            Generate 20 ideas → Blog Posts DB"
	@echo "  make blog-ideas FORCE=1    Regenerate even if ideas exist"
	@echo "  make blog-write            Write all Approved ideas → full posts"
	@echo "  make blog-write NOTES=\"warmer tone\"  Regenerate with feedback"
	@echo "  make blog-publish          Dry run: show what would publish today"
	@echo "  make blog-publish COMMIT=1  Push Scheduled posts to Webflow CMS"
	@echo ""
	@echo "  SEO"
	@echo "  make seo-init             Create Competitors + Keywords DBs (one-time per client)"
	@echo "  make battle-plan-init     Create Battle Plan Input page in Notion"
	@echo "  make battle-plan          Generate SEO Battle Plan → Notion"
	@echo "  make seo-activate CLIENT=x GBP_ID=\"...\" GSC_URL=\"...\" GA4_ID=\"...\""
	@echo "  make keyword-research     Keyword research → Notion + HTML report"
	@echo "  make suggest-keywords     Suggest target keywords for sitemap pages"
	@echo "  make competitor-research  SERP analysis → Competitors DB"
	@echo "  make gbp-posts            3 GBP post drafts → Notion"
	@echo "  make gbp-posts MONTH=\"May 2026\""
	@echo "  make gbp-reviews              Respond to unanswered GBP reviews (auto positive, flag negative)"
	@echo "  make gbp-reviews DRY_RUN=1    Preview responses without posting"
	@echo "  make seo-baseline         90-day baseline report → Notion + HTML"
	@echo "  make seo-report           Previous month report → Notion + HTML"
	@echo ""
	@echo "  SETUP & VALIDATION"
	@echo "  make check-env            Validate all .env variables (run when onboarding a new team member)"
	@echo "  make check-env-seo        Check SEO-specific keys only"
	@echo ""
	@echo "  ONBOARDING"
	@echo "  make onboarding-form      Create Onboarding Submissions DB in Notion (one-time)"
	@echo "  make onboard              Process new form submissions → provision client"
	@echo "  make onboard-list         List pending submissions"
	@echo ""
	@echo "  SOCIAL MEDIA"
	@echo "  make social-posts              8 Instagram/Facebook drafts → Notion Social Posts DB"
	@echo "  make social-posts NOTES=\"...\"  Re-run with feedback"
	@echo "  make social-posts MONTH=\"May 2026\""
	@echo "  make linkedin-posts            2 LinkedIn thought leadership drafts → Notion"
	@echo "  make linkedin-posts NOTES=\"...\"  Re-run with feedback"
	@echo ""
	@echo "  CARE PLAN"
	@echo "  make care-plan-init       Create Care Plan DB for existing client (one-time)"
	@echo "  make care-plan            Run monthly PageSpeed report → Notion"
	@echo ""
	@echo "  PIPELINE MANAGEMENT"
	@echo "  make pipeline-setup       Add approval fields to existing client's Notion DB"
	@echo "  make mark-pending         Set Pending Review + create ClickUp task"
	@echo "  make advance              Check Notion approval → run next stage"
	@echo ""

.PHONY: run sitemap content check-env check-env-seo gbp-reviews \
        social-posts linkedin-posts \
        stock-images images-brand images-pages \
        mood-board-visuals sitemap-visuals brand-export relume-sitemap relume-export \
        onboarding-form onboard onboard-list advance mark-pending pipeline-setup approve-sitemap \
        keyword-research competitor-research suggest-keywords \
        seo-init battle-plan-init battle-plan seo-activate gbp-posts \
        seo-baseline seo-report \
        care-plan care-plan-init \
        blog-setup blog-ideas blog-write blog-publish \
        help
