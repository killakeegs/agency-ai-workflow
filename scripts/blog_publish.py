#!/usr/bin/env python3
"""
blog_publish.py — Publish scheduled blog posts to Webflow CMS

Checks Blog Posts DB for entries with Status = "Scheduled" and a
Suggested Publish Date <= today. For each matched post, pushes a new
CMS item to the client's Webflow Blog collection and updates Notion
Status → "Published" with the live URL.

By default runs in dry-run mode — prints what would be published.
Use --commit to actually push.

Prerequisites:
  1. Webflow master template must be deployed for this client
  2. Blog CMS collection must exist in Webflow (created by developer)
  3. webflow_blog_collection_id must be set in clients.json
  4. WEBFLOW_API_TOKEN must be set in .env (site-level token for this client)

Usage:
    make blog-publish CLIENT=summit_therapy             # dry run
    make blog-publish CLIENT=summit_therapy COMMIT=1    # actually publish
    make blog-publish CLIENT=summit_therapy ALL=1       # publish all Scheduled regardless of date
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

# ── Notion helpers ─────────────────────────────────────────────────────────────

def _rt(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _title_text(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _select(prop: dict) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""

def _date_val(prop: dict) -> str:
    if not prop:
        return ""
    d = prop.get("date")
    return d.get("start", "") if d else ""

def _blocks_to_html(blocks: list[dict]) -> str:
    """Convert Notion blocks to simple HTML for Webflow rich text field."""
    html_parts = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("text", {}).get("content", "") for r in rich)

        if not text.strip():
            continue

        if btype == "heading_2":
            html_parts.append(f"<h2>{text}</h2>")
        elif btype == "heading_3":
            html_parts.append(f"<h3>{text}</h3>")
        elif btype == "bulleted_list_item":
            html_parts.append(f"<li>{text}</li>")
        elif btype == "paragraph":
            html_parts.append(f"<p>{text}</p>")
        elif btype == "divider":
            html_parts.append("<hr>")

    # Wrap consecutive <li> in <ul>
    result = "\n".join(html_parts)
    result = re.sub(r'(<li>.*?</li>\n?)+', lambda m: f"<ul>\n{m.group(0)}</ul>\n", result, flags=re.DOTALL)
    return result


# ── Webflow API ────────────────────────────────────────────────────────────────

class WebflowClient:
    BASE_URL = "https://api.webflow.com/v2"

    def __init__(self, api_token: str) -> None:
        if not httpx:
            raise ImportError("httpx is required: pip install httpx")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def get_collections(self, site_id: str) -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.BASE_URL}/sites/{site_id}/collections",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("collections", [])

    async def find_blog_collection(self, site_id: str) -> str | None:
        """Auto-detect the Blog CMS collection by name."""
        collections = await self.get_collections(site_id)
        for col in collections:
            name = col.get("displayName", col.get("slug", "")).lower()
            if "blog" in name or "post" in name:
                return col["id"]
        return None

    async def create_item(self, collection_id: str, fields: dict, live: bool = False) -> dict:
        """Create a CMS item. If live=True, publish immediately."""
        endpoint = "items/live" if live else "items"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.BASE_URL}/collections/{collection_id}/{endpoint}",
                headers=self.headers,
                json={"fieldData": fields},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()

    async def publish_item(self, collection_id: str, item_id: str) -> None:
        """Publish a staged CMS item."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.BASE_URL}/collections/{collection_id}/items/publish",
                headers=self.headers,
                json={"itemIds": [item_id]},
                timeout=30,
            )
            resp.raise_for_status()


# ── Data loaders ───────────────────────────────────────────────────────────────

async def _load_scheduled_posts(
    notion: NotionClient,
    blog_posts_db_id: str,
    publish_all: bool = False,
) -> list[dict]:
    """
    Return posts with Status = Scheduled.
    If publish_all=False, only return posts whose Suggested Publish Date <= today.
    """
    rows = await notion._client.request(
        path=f"databases/{blog_posts_db_id}/query",
        method="POST",
        body={
            "filter": {"property": "Status", "select": {"equals": "Scheduled"}},
            "page_size": 50,
        },
    )

    today_str = date.today().isoformat()
    posts = []
    for row in rows.get("results", []):
        props = row.get("properties", {})
        publish_date = _date_val(props.get("Suggested Publish Date", {}))

        if not publish_all and publish_date and publish_date > today_str:
            continue  # Not yet

        posts.append({
            "page_id":         row["id"],
            "title":           _title_text(props.get("Title", {})),
            "h1":              _rt(props.get("H1", {})),
            "title_tag":       _rt(props.get("Title Tag", {})),
            "meta_description":_rt(props.get("Meta Description", {})),
            "primary_keyword": _rt(props.get("Primary Keyword", {})),
            "reviewer_name":   _rt(props.get("Reviewer Name", {})),
            "reviewer_creds":  _rt(props.get("Reviewer Credentials", {})),
            "author_name":     _rt(props.get("Author Name", {})),
            "publish_date":    publish_date,
            "word_count":      props.get("Word Count", {}).get("number") or 0,
        })

    return posts


async def _load_post_blocks(notion: NotionClient, page_id: str) -> list[dict]:
    """Load all Notion page blocks for a blog post."""
    blocks = []
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = await notion._client.request(
            path=f"blocks/{page_id}/children",
            method="GET",
        )
        results = resp.get("results", [])
        blocks.extend(results)
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


async def _mark_published(notion: NotionClient, page_id: str, live_url: str) -> None:
    """Update Notion entry: Status → Published, save live URL."""
    await notion._client.request(
        path=f"pages/{page_id}",
        method="PATCH",
        body={
            "properties": {
                "Status":       {"select": {"name": "Published"}},
                "Published URL": {"url": live_url},
            }
        },
    )


# ── Slug generation ────────────────────────────────────────────────────────────

def _title_to_slug(title: str) -> str:
    """Convert a title to a URL-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[''\"()]", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    # Remove stop words at start
    stop_prefixes = ("a-", "an-", "the-", "how-to-", "what-is-")
    for prefix in stop_prefixes:
        if slug.startswith(prefix) and len(slug) > len(prefix) + 5:
            pass  # Keep "how-to" — it's informative in blog slugs
    return slug[:80]


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(
    client_key: str,
    commit: bool = False,
    publish_all: bool = False,
) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion      = NotionClient(api_key=settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    mode = "LIVE PUBLISH" if commit else "DRY RUN"
    print(f"\n{'='*60}")
    print(f"  Blog Publisher — {client_name} [{mode}]")
    print(f"{'='*60}\n")

    blog_posts_db_id = cfg.get("blog_posts_db_id", "")
    if not blog_posts_db_id:
        print("⚠ No blog_posts_db_id found. Run `make blog-ideas` first.")
        sys.exit(1)

    # Check Webflow prerequisites
    webflow_token = getattr(settings, "webflow_api_token", None) or ""
    webflow_site_id = cfg.get("webflow_site_id", "")
    collection_id = cfg.get("webflow_blog_collection_id", "")

    if commit and not webflow_token:
        print("⚠ WEBFLOW_API_TOKEN not set in .env. Cannot publish.")
        print("  Add the site-level Webflow token for this client.")
        sys.exit(1)

    if commit and not webflow_site_id:
        print("⚠ webflow_site_id not set in clients.json for this client.")
        print("  Add it manually or via: make seo-activate (for sites with Webflow access)")
        sys.exit(1)

    # Auto-detect blog collection if not set
    if commit and not collection_id:
        print("Auto-detecting Blog CMS collection in Webflow...")
        wf = WebflowClient(webflow_token)
        collection_id = await wf.find_blog_collection(webflow_site_id)
        if collection_id:
            print(f"  ✓ Found Blog collection: {collection_id}")
            # Save it
            try:
                data = json.loads((Path(__file__).parent.parent / "config" / "clients.json").read_text())
                if client_key not in data:
                    data[client_key] = {}
                data[client_key]["webflow_blog_collection_id"] = collection_id
                (Path(__file__).parent.parent / "config" / "clients.json").write_text(json.dumps(data, indent=2))
            except Exception:
                pass
        else:
            print("⚠ Could not find a Blog CMS collection in Webflow.")
            print("  Developer must create the Blog collection first.")
            print("  Then set webflow_blog_collection_id in clients.json.")
            sys.exit(1)

    # Load scheduled posts
    print(f"Loading posts with Status = Scheduled{' (all dates)' if publish_all else ' (due today or earlier)'}...")
    posts = await _load_scheduled_posts(notion, blog_posts_db_id, publish_all)

    if not posts:
        print("✓ No posts are scheduled for publication today.")
        if not publish_all:
            print("  Use COMMIT=1 ALL=1 to publish all Scheduled posts regardless of date.")
        return

    print(f"  {len(posts)} post(s) ready to publish\n")

    wf = WebflowClient(webflow_token) if commit else None

    for post in posts:
        title = post["title"]
        slug  = _title_to_slug(title)
        print(f"{'[DRY RUN] ' if not commit else ''}Publishing: {title}")
        print(f"  Keyword:  {post['primary_keyword']}")
        print(f"  Date:     {post['publish_date'] or 'not set'}")
        print(f"  Words:    {post['word_count']}")
        print(f"  Slug:     /blog/{slug}")

        if not commit:
            print(f"  [Would push to Webflow collection {collection_id or '(not set)'}]\n")
            continue

        # Load post body blocks
        blocks = await _load_post_blocks(notion, post["page_id"])
        body_html = _blocks_to_html(blocks)

        # Build Webflow CMS fields
        # Field names must match exactly what the developer named them in the Webflow collection
        fields: dict = {
            "name":             title,          # Required by Webflow — maps to collection title
            "slug":             f"blog/{slug}",
            "post-body":        body_html,
            "post-summary":     post["meta_description"],
            "meta-title":       post["title_tag"] or title,
            "meta-description": post["meta_description"],
            "author-name":      post["author_name"] or post["reviewer_name"],
            "reviewer-name":    post["reviewer_name"],
            "reviewer-credentials": post["reviewer_creds"],
            "published-date":   post["publish_date"] or date.today().isoformat(),
        }

        try:
            result = await wf.create_item(collection_id, fields, live=True)
            item_id = result.get("id", "")
            site_url = cfg.get("website_url", "")
            live_url = f"{site_url.rstrip('/')}/blog/{slug}" if site_url else f"/blog/{slug}"

            await _mark_published(notion, post["page_id"], live_url)
            print(f"  ✓ Published: {live_url}\n")

        except Exception as e:
            print(f"  ⚠ Failed to publish '{title}': {e}")
            print("  Check that the Webflow collection field names match what's expected.")
            print("  See blog_publish.py → fields dict for the expected names.\n")

    if commit:
        published = sum(1 for p in posts)
        print(f"✓ Done. {published} post(s) processed.")
    else:
        print(f"✓ Dry run complete. {len(posts)} post(s) would be published.")
        print(f"  Run with COMMIT=1 to publish: make blog-publish CLIENT={client_key} COMMIT=1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish scheduled blog posts to Webflow CMS")
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    parser.add_argument("--commit", action="store_true", help="Actually publish (default: dry run)")
    parser.add_argument("--all",    action="store_true", dest="publish_all",
                        help="Publish all Scheduled posts regardless of date")
    args = parser.parse_args()
    asyncio.run(run(args.client, commit=args.commit, publish_all=args.publish_all))


if __name__ == "__main__":
    main()
