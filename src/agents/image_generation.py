"""
ImageGenerationAgent — AI image generation via Replicate (Flux Schnell)

Two generation modes:

  BRAND BATCH (mode="brand") — runs after mood board approval
    Generates ~15 images as a reusable creative library for the whole site.
    6 categories: hero_lifestyle, detail_closeup, texture_background,
    environment, product_flatlay, brand_abstract.
    Driven by: approved mood board + brand guidelines + Image Direction notes.

  PAGE BATCH (mode="pages") — runs after content approval
    Generates 3 images per sitemap page, contextually relevant to each
    page's content, H1, and purpose.
    Driven by: sitemap + content DB + brand context.

Image Direction notes live in Notion Brand Guidelines ("Image Direction" field).
Write per-category style guidance there — e.g.:
  "Detail Close-Up: dewy skin texture, editorial beauty, Vogue-adjacent, not clinical"
  "Hero Lifestyle: professional woman 25-55, natural light, teal/ivory tones"

Input kwargs (brand batch):
  - client_info_db_id, brand_guidelines_db_id, mood_board_db_id, images_db_id

Input kwargs (page batch):
  - client_info_db_id, brand_guidelines_db_id, mood_board_db_id,
    sitemap_db_id, content_db_id, images_db_id

Optional kwargs (both modes):
  - revision_notes (str): feedback to steer regeneration
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from ..config import settings
from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent

logger = logging.getLogger(__name__)

# ── Brand batch category definitions ──────────────────────────────────────────

BRAND_CATEGORIES = [
    {"key": "hero_lifestyle",     "label": "Hero Lifestyle",     "count": 3, "aspect_ratio": "16:9",
     "description": "Editorial lifestyle shots — the hero images for homepage and key landing sections"},
    {"key": "detail_closeup",     "label": "Detail Close-Up",    "count": 3, "aspect_ratio": "4:3",
     "description": "Intimate close-up details — skin texture, hands, product details, beauty details"},
    {"key": "texture_background", "label": "Texture Background", "count": 3, "aspect_ratio": "16:9",
     "description": "Full-page background textures and abstract images — subtle, behind-content usage"},
    {"key": "environment",        "label": "Environment",        "count": 2, "aspect_ratio": "3:2",
     "description": "Setting shots — boutique clinic interiors, clean workspaces, aspirational spaces"},
    {"key": "product_flatlay",    "label": "Product Flat Lay",   "count": 2, "aspect_ratio": "3:2",
     "description": "Overhead flat lay of skincare products, tools, and brand objects"},
    {"key": "brand_abstract",     "label": "Brand Abstract",     "count": 2, "aspect_ratio": "1:1",
     "description": "Color-palette-aligned abstract compositions — geometric, textural, minimal"},
]

BRAND_TOTAL = sum(c["count"] for c in BRAND_CATEGORIES)  # 15

# ── Claude system prompts ──────────────────────────────────────────────────────

BRAND_SYSTEM_PROMPT = """\
You are a creative director for RxMedia, a digital marketing agency specializing in
telehealth and wellness brands. Generate brand creative images for a client website.

These {total} images form the visual library that will be reused across the entire site —
every category serves a distinct design role.

Flux Schnell prompt rules:
- Lead with the main subject and scene
- Specify lighting: soft window light, diffused studio, golden hour, etc.
- Include color grading that aligns with the brand palette
- Specify photography style: editorial, lifestyle, commercial healthcare, etc.
- Add technical specs at the end: camera, aperture, resolution
- Avoid: text, logos, UI elements, watermarks
- 80–130 words per prompt — specific beats vague

IMPORTANT: Read the "Image Direction Notes" in the brand context carefully.
These are client-specific instructions that OVERRIDE general creative guidance.

Return ONLY this JSON (no markdown, no commentary):
{
  "images": [
    {
      "category": "hero_lifestyle",
      "index": 1,
      "label": "Hero Lifestyle 1",
      "prompt": "...",
      "aspect_ratio": "16:9",
      "rationale": "One sentence on how this serves the brand"
    }
  ],
  "direction_notes": "2-3 sentences on the overall visual direction for this batch"
}

Generate ALL {total} images across ALL categories.
"""

PAGE_SYSTEM_PROMPT = """\
You are a creative director for RxMedia, a digital marketing agency specializing in
telehealth and wellness brands. Generate page-specific images for a client website.

For EACH page listed, generate exactly 3 image prompts. Each set should:
- Feel contextually relevant to that specific page's content and purpose
- Stay visually consistent with the brand (colors, mood, photography style)
- Serve different layout roles: wide hero (16:9), medium feature (4:3), accent (3:2)

Flux Schnell prompt rules:
- 80–130 words per prompt — specific and descriptive
- Lead with subject, include lighting, color grading, photography style, camera specs
- Avoid: text, logos, UI elements, watermarks

IMPORTANT: Read the Image Direction Notes — these are client-specific instructions that
OVERRIDE general creative guidance.

Return ONLY this JSON:
{
  "pages": [
    {
      "page_title": "...",
      "images": [
        {"label": "...", "prompt": "...", "aspect_ratio": "16:9"},
        {"label": "...", "prompt": "...", "aspect_ratio": "4:3"},
        {"label": "...", "prompt": "...", "aspect_ratio": "3:2"}
      ]
    }
  ],
  "direction_notes": "2-3 sentences on visual consistency across page images"
}
"""

# ── Notion helpers ─────────────────────────────────────────────────────────────

def _get_rich_text(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("rich_text", [])
    )


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _blocks_to_text(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})
        rich_text = content.get("rich_text", [])
        text = "".join(seg.get("text", {}).get("content", "") for seg in rich_text)
        if text:
            lines.append(text)
    return "\n".join(lines)


# ── ImageGenerationAgent ──────────────────────────────────────────────────────

class ImageGenerationAgent(BaseAgent):
    """
    Generates brand creative images (brand batch) and page-specific images
    (page batch) via Claude prompt generation + Replicate Flux Schnell.
    """

    name = "image_generation"
    tools: list[dict] = []

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Dispatch to brand batch or page batch based on mode kwarg.

        mode="brand"  → _run_brand_batch()   (~15 images)
        mode="pages"  → _run_page_batch()    (~3 per sitemap page)
        """
        if not settings.replicate_api_key:
            raise AgentError(
                "REPLICATE_API_KEY is not set. Add it to your .env file.\n"
                "Get a token at https://replicate.com/account/api-tokens"
            )

        mode = kwargs.get("mode", "brand")
        if mode == "brand":
            return await self._run_brand_batch(client_id, **kwargs)
        elif mode == "pages":
            return await self._run_page_batch(client_id, **kwargs)
        else:
            raise AgentError(f"Unknown image generation mode: {mode!r}. Use 'brand' or 'pages'.")

    # ── Brand Batch ────────────────────────────────────────────────────────────

    async def _run_brand_batch(self, client_id: str, **kwargs: Any) -> dict:
        client_info_db_id      = kwargs["client_info_db_id"]
        brand_guidelines_db_id = kwargs["brand_guidelines_db_id"]
        mood_board_db_id       = kwargs["mood_board_db_id"]
        images_db_id           = kwargs["images_db_id"]
        revision_notes         = kwargs.get("revision_notes", "")

        self.log.info(f"Brand batch starting | client={client_id} | target={BRAND_TOTAL} images")

        brand_context, image_direction = await self._load_brand_context(brand_guidelines_db_id)
        mood_context, mood_option = await self._load_mood_context(mood_board_db_id)
        client_name, business_type = await self._load_client_info(client_info_db_id, client_id)

        # Build category list for prompt
        categories_desc = "\n".join(
            f"  {c['key']} × {c['count']} ({c['aspect_ratio']}): {c['description']}"
            for c in BRAND_CATEGORIES
        )

        user_message = f"""CLIENT: {client_name}
BUSINESS TYPE: {business_type}

BRAND GUIDELINES:
{brand_context[:3000]}

{mood_context[:2000]}

IMAGE DIRECTION NOTES — follow these precisely, they override general guidance:
{image_direction if image_direction else "(none set — derive visual direction entirely from brand guidelines and mood board above)"}

CATEGORIES TO GENERATE ({BRAND_TOTAL} total):
{categories_desc}

Generate all {BRAND_TOTAL} images. Use the brand guidelines, mood board, and
Image Direction notes above — not generic defaults. The images should feel like
a cohesive editorial shoot: different subjects and crops, one unified language.
{f"REVISION REQUEST: {revision_notes}" if revision_notes else ""}"""

        system = BRAND_SYSTEM_PROMPT.format(total=BRAND_TOTAL)
        images_data = await self._call_claude_for_prompts(system, user_message, mode="brand")

        results = await self._generate_and_save_batch(
            images_data=images_data,
            images_db_id=images_db_id,
            batch="Brand Creative",
            mood_option=mood_option,
        )

        successes = [r for r in results if r["status"] == "success"]
        self.log.info(f"Brand batch complete: {len(successes)}/{len(results)} succeeded")

        return {
            "status": "success" if successes else "error",
            "mode": "brand",
            "stage": PipelineStage.MOOD_BOARD_APPROVED.value,
            "images_generated": len(successes),
            "images_failed": len(results) - len(successes),
            "direction_notes": images_data.get("direction_notes", ""),
            "results": results,
        }

    # ── Page Batch ─────────────────────────────────────────────────────────────

    async def _run_page_batch(self, client_id: str, **kwargs: Any) -> dict:
        client_info_db_id      = kwargs["client_info_db_id"]
        brand_guidelines_db_id = kwargs["brand_guidelines_db_id"]
        mood_board_db_id       = kwargs["mood_board_db_id"]
        sitemap_db_id          = kwargs["sitemap_db_id"]
        content_db_id          = kwargs["content_db_id"]
        images_db_id           = kwargs["images_db_id"]
        revision_notes         = kwargs.get("revision_notes", "")

        brand_context, image_direction = await self._load_brand_context(brand_guidelines_db_id)
        mood_context, mood_option = await self._load_mood_context(mood_board_db_id)
        client_name, business_type = await self._load_client_info(client_info_db_id, client_id)

        # Load pages from sitemap + content
        pages = await self._load_pages(sitemap_db_id, content_db_id)
        if not pages:
            raise AgentError("No pages found in sitemap/content DB. Run sitemap + content stages first.")

        self.log.info(f"Page batch starting | {len(pages)} pages × 3 = {len(pages) * 3} images")

        pages_desc = "\n".join(
            f"  Page: {p['title']}\n  H1: {p['h1']}\n  Purpose: {p['purpose']}\n"
            f"  Key Sections: {p['key_sections']}\n"
            for p in pages
        )

        user_message = f"""CLIENT: {client_name}
BUSINESS TYPE: {business_type}

BRAND GUIDELINES:
{brand_context[:2500]}

{mood_context[:1500]}

IMAGE DIRECTION NOTES (follow carefully):
{image_direction if image_direction else "(none set — use brand context to guide style)"}

PAGES TO GENERATE IMAGES FOR:
{pages_desc[:4000]}

Generate 3 images per page. Each image should serve a distinct layout role on that
specific page: one wide hero (16:9), one medium feature (4:3), one accent (3:2).
{f"REVISION REQUEST: {revision_notes}" if revision_notes else ""}"""

        pages_data = await self._call_claude_for_prompts(PAGE_SYSTEM_PROMPT, user_message, mode="pages")

        # Flatten page → images list
        all_images: list[dict] = []
        for page_entry in pages_data.get("pages", []):
            page_title = page_entry.get("page_title", "")
            for img in page_entry.get("images", []):
                img["page"] = page_title
                all_images.append(img)

        results = await self._generate_and_save_batch(
            images_data={"images": all_images, "direction_notes": pages_data.get("direction_notes", "")},
            images_db_id=images_db_id,
            batch="Page Content",
            mood_option=mood_option,
        )

        successes = [r for r in results if r["status"] == "success"]
        self.log.info(f"Page batch complete: {len(successes)}/{len(results)} succeeded")

        return {
            "status": "success" if successes else "error",
            "mode": "pages",
            "stage": PipelineStage.CONTENT_APPROVED.value,
            "images_generated": len(successes),
            "images_failed": len(results) - len(successes),
            "pages_covered": len(pages),
            "direction_notes": pages_data.get("direction_notes", ""),
            "results": results,
        }

    # ── Shared helpers ─────────────────────────────────────────────────────────

    async def _load_client_info(self, db_id: str, fallback: str) -> tuple[str, str]:
        entries = await self.notion.query_database(db_id)
        if not entries:
            return fallback, ""
        props = entries[0]["properties"]
        name = _get_rich_text(props.get("Company", {})) or fallback
        btype = _get_select(props.get("Business Type", {}))
        return name, btype

    async def _load_brand_context(self, db_id: str) -> tuple[str, str]:
        """Returns (brand_context_text, image_direction_notes)."""
        entries = await self.notion.query_database(db_id)
        if not entries:
            return "", ""
        bp = entries[0]["properties"]
        page_id = entries[0]["id"]

        parts = []
        for field in ["Primary Color", "Secondary Color", "Accent Color",
                      "Primary Font", "Secondary Font", "Tone Descriptors",
                      "Inspiration URLs"]:
            val = _get_rich_text(bp.get(field, {}))
            if val:
                parts.append(f"{field}: {val}")

        image_direction = _get_rich_text(bp.get("Image Direction", {}))

        brand_context = "\n".join(parts)
        blocks = await self.notion.get_block_children(page_id)
        body = _blocks_to_text(blocks)
        if body:
            brand_context += f"\n\nFULL BRAND DOCUMENT:\n{body[:3000]}"

        self.log.info("Brand guidelines loaded")
        return brand_context, image_direction

    async def _load_mood_context(self, db_id: str) -> tuple[str, str]:
        """Returns (mood_context_text, approved_option_name)."""
        entries = await self.notion.query_database(db_id)
        if not entries:
            return "", ""

        approved = [e for e in entries if _get_select(e["properties"].get("Status", {})) == "Approved"]
        pending  = [e for e in entries if _get_select(e["properties"].get("Status", {})) == "Pending Review"]
        target   = (approved or pending or entries)[0]

        mp = target["properties"]
        option = _get_select(mp.get("Variation", {}))
        style_kw = _get_rich_text(mp.get("Style Keywords", {}))
        palette_desc = _get_rich_text(mp.get("Color Palette Description", {}))

        context = f"APPROVED MOOD BOARD ({option}):\n"
        context += f"Style Keywords: {style_kw}\n"
        context += f"Color Palette: {palette_desc}\n"

        blocks = await self.notion.get_block_children(target["id"])
        body = _blocks_to_text(blocks)
        if body:
            imagery_start = body.find("Imagery & Layout")
            if imagery_start != -1:
                context += f"\n{body[imagery_start:imagery_start + 1200]}"
            else:
                context += f"\n{body[:1200]}"

        self.log.info("Mood board loaded")
        return context, option

    async def _load_pages(self, sitemap_db_id: str, content_db_id: str) -> list[dict]:
        """Load page list from sitemap + content DB, merged."""
        content_map: dict[str, dict] = {}
        content_entries = await self.notion.query_database(content_db_id)
        for entry in content_entries:
            ep = entry["properties"]
            title = "".join(
                t.get("text", {}).get("content", "")
                for t in ep.get("Page Title", {}).get("title", [])
            )
            if title:
                content_map[title] = {
                    "h1": _get_rich_text(ep.get("H1", {})),
                }

        pages: list[dict] = []
        sitemap_entries = await self.notion.query_database(sitemap_db_id)
        for entry in sitemap_entries:
            ep = entry["properties"]
            title = "".join(
                t.get("text", {}).get("content", "")
                for t in ep.get("Page Title", {}).get("title", [])
            )
            if not title:
                continue
            purpose = _get_rich_text(ep.get("Purpose", {}))
            key_sections = _get_rich_text(ep.get("Key Sections", {}))
            h1 = content_map.get(title, {}).get("h1", "")

            pages.append({
                "title": title,
                "purpose": purpose[:200],
                "key_sections": key_sections[:200],
                "h1": h1,
            })

        self.log.info(f"Loaded {len(pages)} pages")
        return pages

    async def _call_claude_for_prompts(
        self, system: str, user_message: str, mode: str
    ) -> dict:
        """Call Claude and parse JSON response."""
        self.log.info("Sending context to Claude for prompt generation...")
        response = await self.anthropic.messages.create(
            model=self.model,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text if response.content else ""
        self.log.info(f"Claude response: {response.usage.output_tokens} tokens")

        try:
            clean = re.sub(r"```(?:json)?\n?", "", raw).strip()
            data: dict = json.loads(clean)
        except json.JSONDecodeError as e:
            self.log.error(f"JSON parse failed: {e}\nRaw (first 500): {raw[:500]}")
            raise AgentError(f"ImageGenerationAgent: JSON parse failed — {e}") from e

        count = len(data.get("images", data.get("pages", [])))
        self.log.info(f"Got prompts for {count} {'images' if mode == 'brand' else 'pages'} from Claude")
        return data

    async def _generate_and_save_batch(
        self,
        images_data: dict,
        images_db_id: str,
        batch: str,
        mood_option: str,
    ) -> list[dict]:
        """Generate each image via Replicate and save to Notion. Returns results list."""
        raw_images: list[dict] = images_data.get("images", [])
        results: list[dict] = []

        for img in raw_images:
            label = img.get("label", "Image")
            prompt = img.get("prompt", "")
            aspect_ratio = img.get("aspect_ratio", "16:9")
            category = img.get("category", img.get("key", ""))
            page = img.get("page", "")

            self.log.info(f"Generating: {label} ({aspect_ratio})...")

            try:
                pred_id, image_url = await self._generate_image(prompt, aspect_ratio)
                self.log.info(f"  ✓ {label}")
            except Exception as e:
                self.log.error(f"  ✗ {label}: {e}")
                results.append({"label": label, "status": "error", "error": str(e), "prompt": prompt})
                continue

            # Save to Notion
            properties: dict = {
                "Image Name": self.notion.title_property(label),
                "Batch": self.notion.select_property(batch),
                "Status": self.notion.select_property("Generated"),
                "Image URL": {"url": image_url},
                "Prompt Used": self.notion.text_property(prompt[:2000]),
                "Replicate Job ID": self.notion.text_property(pred_id),
                "Mood Board Option": self.notion.text_property(mood_option),
            }
            if category:
                properties["Category"] = self.notion.select_property(
                    self._category_to_label(category)
                )
            if page:
                properties["Page"] = self.notion.text_property(page)

            entry_id = await self.notion.create_database_entry(images_db_id, properties)

            await self.notion.append_blocks(entry_id, [
                {"object": "block", "type": "image",
                 "image": {"type": "external", "external": {"url": image_url}}},
                {"object": "block", "type": "callout", "callout": {
                    "rich_text": [{"type": "text", "text": {"content": f"Prompt: {prompt[:1800]}"}}],
                    "icon": {"emoji": "🎨"}, "color": "gray_background",
                }},
            ])

            results.append({
                "label": label,
                "status": "success",
                "image_url": image_url,
                "replicate_job_id": pred_id,
                "notion_entry_id": entry_id,
                "category": category,
                "page": page,
                "prompt": prompt,
            })

        return results

    @staticmethod
    def _category_to_label(key: str) -> str:
        mapping = {
            "hero_lifestyle":     "Hero Lifestyle",
            "detail_closeup":     "Detail Close-Up",
            "texture_background": "Texture Background",
            "environment":        "Environment",
            "product_flatlay":    "Product Flat Lay",
            "brand_abstract":     "Brand Abstract",
        }
        return mapping.get(key, key.replace("_", " ").title())

    async def _generate_image(self, prompt: str, aspect_ratio: str = "16:9") -> tuple[str, str]:
        """Submit to Replicate Flux Schnell. Returns (prediction_id, image_url)."""
        headers = {
            "Authorization": f"Token {settings.replicate_api_key}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        }
        payload = {
            "input": {
                "prompt": prompt,
                "num_outputs": 1,
                "aspect_ratio": aspect_ratio,
                "output_format": "webp",
                "output_quality": 90,
                "go_fast": True,
            }
        }

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            pred_id = data["id"]
            status = data.get("status", "")

            if status == "succeeded" and data.get("output"):
                return pred_id, data["output"][0]

            if status in ("failed", "canceled"):
                raise AgentError(
                    f"Replicate prediction {pred_id} {status}: {data.get('error', 'unknown')}"
                )

            # Poll fallback
            for _ in range(45):
                await asyncio.sleep(2)
                poll = await client.get(
                    f"https://api.replicate.com/v1/predictions/{pred_id}",
                    headers={"Authorization": f"Token {settings.replicate_api_key}"},
                )
                poll.raise_for_status()
                pd = poll.json()
                if pd.get("status") == "succeeded" and pd.get("output"):
                    return pred_id, pd["output"][0]
                if pd.get("status") in ("failed", "canceled"):
                    raise AgentError(
                        f"Replicate prediction {pred_id} failed: {pd.get('error', 'unknown')}"
                    )

            raise AgentError(f"Replicate prediction {pred_id} timed out")

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        raise NotImplementedError(
            f"ImageGenerationAgent uses direct API calls. Tool {tool_name} not dispatched."
        )
