"""
website_scraper.py — Lightweight site scraper for Business Profile population.

Given a client's public website URL, discovers high-signal pages (about /
services / team / insurance / contact / FAQ), fetches each, strips boilerplate,
and returns {url: clean_text}. Output is passed verbatim to Claude for
fact extraction — we don't try to structure it here, just clean it up
enough to be useful context.

Scope: public HTML pages only. No JS rendering (Playwright isn't needed
for 95% of healthcare sites — they're WordPress/Webflow with server-rendered
content). If a future client has a JS-heavy site and scraping misses content,
we'll add a Playwright fallback at that point.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


# Pages most likely to carry the facts a Business Profile needs. Matched as
# case-insensitive substrings against <a href> values on the homepage.
PRIORITY_PATH_HINTS: list[str] = [
    "about",
    "services", "programs", "treatment", "care", "what-we-treat",
    "levels-of-care", "detox", "residential", "php", "iop", "outpatient",
    "team", "staff", "who-we-are", "leadership",
    "insurance", "admissions", "intake", "payment",
    "facility", "facilities", "tour", "amenities",
    "populations", "who-we-serve", "women", "men", "veterans",
    "approach", "philosophy", "methodology",
    "faq", "faqs",
    "contact", "location", "locations",
]

# Paths that reliably aren't useful — skip even if linked from homepage.
SKIP_PATH_HINTS: list[str] = [
    "blog", "news", "press", "events", "resources",
    "privacy", "terms", "accessibility", "sitemap", "legal",
    "careers", "jobs", "employment",
    ".pdf", ".jpg", ".png", ".webp", ".mp4", "#", "mailto:", "tel:",
]

MAX_PAGES_DEFAULT = 12           # homepage + up to ~11 internal pages
MAX_PAGE_CHARS_DEFAULT = 25_000  # trim each page — full Claude context is already large
REQUEST_TIMEOUT_S = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RxMediaAgencyBot/1.0; +https://rxmedia.io/bot) "
        "BusinessProfilePopulator"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def _same_host(a: str, b: str) -> bool:
    try:
        return urlparse(a).netloc.lower().lstrip("www.") == urlparse(b).netloc.lower().lstrip("www.")
    except Exception:
        return False


def _clean_html_to_text(html: str) -> str:
    """Strip scripts/styles/nav/footer, collapse whitespace, return readable text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    # Nav and footer are usually duplicated on every page — keep only the first.
    # Strip all for simplicity; the main content usually carries the facts.
    for tag in soup(["nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse 3+ newlines and tabs
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _pick_priority_links(html: str, base_url: str, max_links: int) -> list[str]:
    """From the homepage, return internal links most likely to carry BP-worthy
    content. Ranked by PRIORITY_PATH_HINTS order."""
    soup = BeautifulSoup(html, "html.parser")
    all_links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        absolute = urljoin(base_url, href)
        if not _same_host(absolute, base_url):
            continue
        # Strip query strings and trailing slashes for dedup
        normalized = absolute.split("#")[0].split("?")[0].rstrip("/")
        if normalized in seen or normalized == base_url.rstrip("/"):
            continue
        seen.add(normalized)
        all_links.append(normalized)

    def _skip(url: str) -> bool:
        u = url.lower()
        return any(s in u for s in SKIP_PATH_HINTS)

    def _priority(url: str) -> int:
        u = url.lower()
        for i, hint in enumerate(PRIORITY_PATH_HINTS):
            if hint in u:
                return i
        return 999

    ranked = sorted([u for u in all_links if not _skip(u)], key=_priority)
    return ranked[:max_links]


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Fetch one URL; returns (url, text). Empty text on failure."""
    try:
        r = await client.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
            return url, ""
        return url, _clean_html_to_text(r.text)
    except Exception:
        return url, ""


async def scrape_site(
    root_url: str,
    max_pages: int = MAX_PAGES_DEFAULT,
    max_page_chars: int = MAX_PAGE_CHARS_DEFAULT,
) -> dict[str, str]:
    """
    Returns {url: clean_text} for the homepage + up to `max_pages - 1` high-signal
    internal pages. Failures are logged but don't block — partial results are fine.

    Raises only for the homepage failing entirely (can't derive anything without
    at least that).
    """
    async with httpx.AsyncClient() as client:
        # 1. Fetch homepage
        r = await client.get(root_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
        r.raise_for_status()
        home_html = r.text
        out: dict[str, str] = {root_url: _clean_html_to_text(home_html)[:max_page_chars]}

        # 2. Discover internal links worth fetching
        internal_urls = _pick_priority_links(home_html, str(r.url), max_links=max_pages - 1)
        if not internal_urls:
            return out

        # 3. Fetch in parallel (bounded concurrency)
        results = await asyncio.gather(*[_fetch(client, u) for u in internal_urls])
        for url, text in results:
            if text:
                out[url] = text[:max_page_chars]
        return out


def summarize_scrape(pages: dict[str, str]) -> str:
    """Render the scrape output as a single text blob suitable for Claude
    prompt injection. Each page is delimited with a header so Claude can
    cite which URL a fact came from."""
    parts: list[str] = []
    for url, text in pages.items():
        if not text.strip():
            continue
        parts.append(f"=== URL: {url} ===\n{text.strip()}")
    return "\n\n".join(parts)
