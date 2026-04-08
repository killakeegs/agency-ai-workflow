# RxMedia Agency Pipeline — shortcuts
# Usage: make run
#        make content
#        make sitemap
#        make mood-board
#
# For revisions: make content NOTES="homepage feels too formal, loosen the tone"

PYTHON := .venv/bin/python3
CLIENT := wellwell
NOTES  ?=

# ── Interactive menu (recommended) ───────────────────────────────────────────

run:
	@$(PYTHON) scripts/run.py

# ── Direct stage shortcuts ────────────────────────────────────────────────────

transcript:
	@$(PYTHON) scripts/run_pipeline_stage.py --stage transcript_parser --client $(CLIENT)

mood-board:
	@$(PYTHON) scripts/run_pipeline_stage.py --stage mood_board --client $(CLIENT) \
	  $(if $(NOTES),--revision "$(NOTES)",)
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT) --mark-pending

sitemap:
	@$(PYTHON) scripts/run_pipeline_stage.py --stage sitemap --client $(CLIENT) \
	  $(if $(NOTES),--revision "$(NOTES)",)
	@$(PYTHON) scripts/generate_sitemap_visual.py --client $(CLIENT) --notion
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT) --mark-pending

suggest-keywords:
	@$(PYTHON) scripts/suggest_keywords.py --client $(CLIENT) \
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
	@$(PYTHON) scripts/run_pipeline_stage.py --stage content --client $(CLIENT) \
	  $(if $(NOTES),--revision "$(NOTES)",)
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT) --mark-pending

wireframe:
	@$(PYTHON) scripts/run_pipeline_stage.py --stage wireframe --client $(CLIENT) \
	  $(if $(NOTES),--revision "$(NOTES)",)
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT) --mark-pending

# ── Visual generators (HTML + JSON for Figma plugin) ─────────────────────────

stock-images:
	@$(PYTHON) scripts/fetch_stock_images.py --client $(CLIENT) \
	  $(if $(NOTES),--notes "$(NOTES)",) \
	  $(if $(PHOTOGRAPHER),--photographer "$(PHOTOGRAPHER)",) \
	  $(if $(COMMIT),--commit,) \
	  $(if $(OPEN),--open,)

images-brand:
	@$(PYTHON) scripts/generate_images.py --client $(CLIENT) --mode brand \
	  $(if $(NOTES),--revision "$(NOTES)",) \
	  $(if $(OPEN),--open,)

images-pages:
	@$(PYTHON) scripts/generate_images.py --client $(CLIENT) --mode pages \
	  $(if $(NOTES),--revision "$(NOTES)",) \
	  $(if $(OPEN),--open,)

mood-board-visuals:
	@$(PYTHON) scripts/generate_mood_board_visuals.py --client $(CLIENT) --open

sitemap-visuals:
	@$(PYTHON) scripts/generate_sitemap_visual.py --client $(CLIENT) --open --notion

brand-export:
	@$(PYTHON) scripts/export_brand_guidelines.py --client $(CLIENT) --open

relume-sitemap:
	@$(PYTHON) scripts/export_relume_sitemap.py --client $(CLIENT) \
	  $(if $(OPEN),--open,)

relume-export:
	@$(PYTHON) scripts/export_relume_prompt.py --client $(CLIENT) --open

onboarding-form:
	@$(PYTHON) scripts/setup_onboarding_form.py

onboard:
	@$(PYTHON) scripts/onboard_client.py

onboard-list:
	@$(PYTHON) scripts/onboard_client.py --list

# ── Approval flow ─────────────────────────────────────────────────────────────

advance:
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT)

mark-pending:
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT) --mark-pending

pipeline-setup:
	@$(PYTHON) scripts/advance_pipeline.py --client $(CLIENT) --setup

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  RxMedia Agency Pipeline"
	@echo "  ───────────────────────────────────────────────"
	@echo "  make run              Interactive menu (easiest)"
	@echo ""
	@echo "  PIPELINE STAGES"
	@echo "  make transcript       Parse meeting transcript → Notion"
	@echo "  make mood-board       Generate mood board variations → Notion"
	@echo "  make sitemap          Generate sitemap → Notion"
	@echo "  make suggest-keywords Suggest target keywords for all sitemap pages → Notion"
	@echo "  make content          Generate page copy → Notion"
	@echo "  make wireframe        Generate Relume component maps → Notion"
	@echo ""
	@echo "  REVISIONS (re-run with feedback)"
	@echo "  make mood-board NOTES=\"Option A is too clinical\""
	@echo "  make sitemap    NOTES=\"Add a FAQs page\""
	@echo "  make content    NOTES=\"Homepage tone needs to be warmer\""
	@echo "  make wireframe  NOTES=\"Use more split-layout components\""
	@echo ""
	@echo "  STOCK PHOTOGRAPHY (Pexels)"
	@echo "  make stock-images                    Discovery pass → HTML report"
	@echo "  make stock-images OPEN=1             Discovery pass + open report"
	@echo "  make stock-images NOTES=\"warmer tones\"  Re-run with style notes"
	@echo "  make stock-images PHOTOGRAPHER=\"Name\"   Lean into one photographer"
	@echo "  make stock-images COMMIT=1           Download images + save to Notion"
	@echo ""
	@echo "  AI IMAGE GENERATION (Replicate + Flux Schnell)"
	@echo "  make images-brand         Brand creative library (~15 images) → Notion"
	@echo "  make images-pages         Page-specific images (~3 per page)  → Notion"
	@echo "  make images-brand NOTES=\"Make textures softer\"  Regenerate with feedback"
	@echo ""
	@echo "  VISUAL GENERATORS (for Figma plugin)"
	@echo "  make mood-board-visuals   Generate mood board HTML + JSON"
	@echo "  make sitemap-visuals      Generate sitemap HTML + JSON"
	@echo "  make brand-export         Export brand guidelines JSON"
	@echo "  make relume-export        Export Relume AI prompt (paste into relume.io)"
	@echo "  make onboarding-form      Create the Client Onboarding Submissions database in Notion"
	@echo "  make onboard              Process new form submissions → provision client"
	@echo "  make onboard-list         List pending submissions without processing"
	@echo "  make pipeline-setup       Add approval fields to an existing client's Notion DB"
	@echo "  make mark-pending         After a manual stage run — sets Pending Review + creates ClickUp task"
	@echo "  make advance              Check Notion for approval and run the next pipeline stage"
	@echo ""

.PHONY: run transcript mood-board sitemap content wireframe \
        stock-images images-brand images-pages \
        mood-board-visuals sitemap-visuals brand-export relume-export \
        onboarding-form onboard onboard-list advance mark-pending pipeline-setup help
