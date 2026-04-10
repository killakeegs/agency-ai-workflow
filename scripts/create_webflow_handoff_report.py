"""
Create a Webflow Handoff Status report in Notion.
Shows what SEO content was pushed to Webflow and what needs manual attention.
"""
import os, sys, json, httpx, asyncio
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

NOTION_TOKEN = os.environ["NOTION_API_KEY"]
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CONTENT_DB_ID = "33bf7f45333e81cdab1cf6aa9d13bbb0"
PARENT_PAGE_ID = "33bf7f45333e811999a5c5bc00c3a3b4"  # Summit Therapy main page
WEBFLOW_SITE_URL = "https://webflow.com/design/summit-therapy-09a846"

def get_text(prop):
    items = prop.get("rich_text", []) or prop.get("title", [])
    return "".join(t.get("plain_text", "") for t in items).strip()

def notion_text(content):
    return [{"type": "text", "text": {"content": content}}]

def heading_block(level, text, color=None):
    key = f"heading_{level}"
    block = {
        "type": key,
        key: {"rich_text": notion_text(text)},
    }
    if color:
        block[key]["color"] = color
    return block

def para_block(text, color=None):
    b = {"type": "paragraph", "paragraph": {"rich_text": notion_text(text)}}
    if color:
        b["paragraph"]["color"] = color
    return b

def bullet_block(text, color=None):
    b = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": notion_text(text)}}
    if color:
        b["bulleted_list_item"]["color"] = color
    return b

def divider():
    return {"type": "divider", "divider": {}}

def callout_block(text, icon="ℹ️", color="blue_background"):
    return {
        "type": "callout",
        "callout": {
            "rich_text": notion_text(text),
            "icon": {"type": "emoji", "emoji": icon},
            "color": color,
        }
    }

def main():
    client = httpx.Client(timeout=30)

    # ── 1. Fetch all content pages ──────────────────────────────────────
    print("Fetching content pages from Notion...")
    all_pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = client.post(
            f"https://api.notion.com/v1/databases/{CONTENT_DB_ID}/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        all_pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    print(f"  Found {len(all_pages)} content pages")

    # ── 2. Classify pages ─────────────────────────────────────────────────
    seo_pushed = []        # have TT + meta, content needs manual Webflow entry
    seo_missing = []       # missing TT/meta — content needs to be generated + pushed
    
    for p in sorted(all_pages, key=lambda x: get_text(x["properties"].get("Slug", {}))):
        props = p["properties"]
        name = get_text(props.get("Page Title", {}))
        slug = get_text(props.get("Slug", {})) or "/"
        title_tag = get_text(props.get("Title Tag", {}))
        meta = get_text(props.get("Meta Description", {}))
        h1 = get_text(props.get("H1", {}))
        page_id = p["id"]
        page_url = f"https://www.notion.so/{page_id.replace('-','')}"
        
        entry = {
            "name": name,
            "slug": f"/{slug}" if not slug.startswith("/") else slug,
            "title_tag": title_tag,
            "meta": meta,
            "h1": h1,
            "page_url": page_url,
        }

        has_seo = bool(title_tag and meta)
        has_content = bool(h1)
        
        if has_seo and has_content:
            seo_pushed.append(entry)
        else:
            seo_missing.append(entry)

    # ── 3. Build Notion blocks ────────────────────────────────────────────
    blocks = []

    # Header
    blocks += [
        callout_block(
            "This report tracks the automated Webflow content push for Summit Therapy. "
            "SEO metadata (title tags, meta descriptions) was pushed via the Webflow Data API. "
            "Body copy (H1s, section text, CTAs) must be entered manually in Webflow Designer — "
            "the Webflow API does not support updating primary locale content programmatically.",
            icon="📋", color="gray_background"
        ),
        divider(),
    ]

    # Summary stats
    blocks += [
        heading_block(2, "Summary"),
        bullet_block(f"✅ {len(seo_pushed)} pages — SEO metadata pushed to Webflow (title tag + meta description)"),
        bullet_block(f"✍️ {len(all_pages)} pages — Body copy needs manual entry in Webflow Designer"),
        bullet_block(f"⚠️ {len(seo_missing)} pages — Content not yet generated (no title tag, meta, or H1 in Notion)"),
        bullet_block("🗂 Webflow folder structure needs manual fix (see section below)"),
        divider(),
    ]

    # Webflow API limitation explanation
    blocks += [
        heading_block(2, "Why Body Copy Wasn't Pushed"),
        para_block(
            "The Webflow Data API v2 (POST /v2/pages/{id}/dom) only supports updating "
            "secondary locale content — it intentionally blocks primary locale writes. "
            "This is an API design decision, not a bug. "
            "The only way to update H1s, section text, and CTAs programmatically is through "
            "the Webflow Designer API (which requires an active Designer session)."
        ),
        para_block(
            "To push body copy: open Webflow Designer → each page's content can be pasted "
            "section by section from the Notion content pages linked below."
        ),
        divider(),
    ]

    # Webflow folder structure fixes
    blocks += [
        heading_block(2, "⚠️ Webflow Folder Structure — Manual Fixes Required"),
        callout_block(
            "These changes require Webflow Designer UI — they cannot be done via API.",
            icon="⚠️", color="yellow_background"
        ),
        bullet_block("Create a 'Services' folder in Webflow → drag Speech Therapy, OT, PT hub pages + all subcategory pages into it"),
        bullet_block("Create an 'About' folder → drag 'Our Team' page into it (slug: /about/team)"),
        bullet_block("Move 'Insurance & Billing' out of '/new-patient-resources/' — should be standalone at /insurance"),
        bullet_block("Verify home page slug is '/' (cannot be changed via API — Webflow locks index page slug)"),
        divider(),
    ]

    # Pages missing content
    if seo_missing:
        blocks += [
            heading_block(2, f"⚠️ Pages Missing Content ({len(seo_missing)} pages)"),
            para_block("These pages have no title tag, meta description, or H1 generated yet. Run the content agent or write copy manually."),
        ]
        for p in seo_missing:
            blocks.append(bullet_block(f"{p['name']}  ({p['slug']})  → {p['page_url']}"))
        blocks.append(divider())

    # Pages with SEO pushed — body copy still needed
    blocks += [
        heading_block(2, f"✅ SEO Pushed — Body Copy Needed ({len(seo_pushed)} pages)"),
        para_block(
            "Title tags and meta descriptions were automatically pushed to Webflow for these pages. "
            "Click each Notion link to copy the body copy into Webflow Designer."
        ),
    ]

    for p in seo_pushed:
        slug_display = p["slug"] if p["slug"] != "/" else "/ (Home)"
        blocks.append(heading_block(3, f"{p['name']}  —  {slug_display}"))
        blocks.append(bullet_block(f"Title Tag: {p['title_tag']}" if p["title_tag"] else "Title Tag: (not set)"))
        blocks.append(bullet_block(f"H1 / Hero Headline: {p['h1']}" if p["h1"] else "H1: (not set)"))
        blocks.append(bullet_block(f"Notion content page: {p['page_url']}"))

    blocks.append(divider())

    # Footer guidance
    blocks += [
        heading_block(2, "Next Steps"),
        bullet_block("1. Open Webflow Designer for Summit Therapy site"),
        bullet_block("2. Fix folder structure (Services, About/Team) per section above"),
        bullet_block("3. For each page listed above: open Notion link → copy H1 + section content → paste into Webflow"),
        bullet_block("4. Generate missing content for 4 pages (Our Team, Insurance & Billing, Privacy Policy, Terms of Service)"),
        bullet_block("5. Once all content is in Webflow, publish the site"),
    ]

    # ── 4. Create the Notion page ─────────────────────────────────────────
    print("Creating Notion report page...")
    
    # Notion API max 100 blocks per request
    page_body = {
        "parent": {"page_id": PARENT_PAGE_ID},
        "properties": {
            "title": {"title": notion_text("Webflow Handoff Status — Summit Therapy")}
        },
        "children": blocks[:100],
    }

    r = client.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json=page_body,
    )
    result = r.json()
    if result.get("object") == "error":
        print(f"ERROR creating page: {result}")
        sys.exit(1)

    page_id = result["id"]
    print(f"  Created page: https://www.notion.so/{page_id.replace('-','')}")

    # Append remaining blocks if any
    if len(blocks) > 100:
        remaining = blocks[100:]
        # Chunk into groups of 100
        for i in range(0, len(remaining), 100):
            chunk = remaining[i:i+100]
            r2 = client.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=NOTION_HEADERS,
                json={"children": chunk},
            )
            if r2.json().get("object") == "error":
                print(f"  WARNING: Error appending blocks chunk {i}: {r2.json()}")

    print(f"\n✅ Webflow Handoff Report created!")
    print(f"   https://www.notion.so/{page_id.replace('-','')}")

if __name__ == "__main__":
    main()
