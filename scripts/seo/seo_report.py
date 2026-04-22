#!/usr/bin/env python3
"""
seo_report.py — SEO baseline + monthly performance report

Pulls data from:
  - Google Search Console (impressions, clicks, CTR, position, branded vs non-branded)
  - Google Analytics 4 (sessions, organic sessions, users, engagement rate)
  - Google Business Profile (impressions, calls, directions, website clicks)
  - DataForSEO Backlinks (domain authority, referring domains)
  - Search Atlas Rank Tracker (keyword rankings + traffic estimate, if project configured)

Modes:
  --baseline  Last 90 days averaged — run once when client onboards to SEO
  --monthly   Previous calendar month vs baseline — run every month

Outputs:
  1. Notion SEO Metrics DB entry
  2. HTML report saved to output/{client}/seo_report_{month}.html

Usage:
    make seo-baseline CLIENT=summit_therapy
    make seo-report   CLIENT=summit_therapy
    make seo-report   CLIENT=summit_therapy MONTH="March 2026"

Requirements per client in clients.json:
    gsc_site_url        — exact URL registered in Search Console
    ga4_property_id     — numeric GA4 property ID
    gbp_location_id     — GBP location resource name (e.g. "locations/1234567890")
    search_atlas_project_id — optional; skipped if not set
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import anthropic
import httpx

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"
CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DATAFORSEO_BASE  = "https://api.dataforseo.com/v3"
SEARCH_ATLAS_BASE = "https://keyword.searchatlas.com/api"


# ── Notion field helpers ───────────────────────────────────────────────────────

def _get_rt(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _get_title(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _get_num(prop: dict) -> float | None:
    if not prop:
        return None
    return prop.get("number")


# ── Google OAuth token refresh ─────────────────────────────────────────────────

def _get_access_token() -> str:
    """Exchange refresh token for a fresh access token."""
    refresh_token = settings.google_refresh_token
    client_id     = settings.google_client_id
    client_secret = settings.google_client_secret

    if not all([refresh_token, client_id, client_secret]):
        raise RuntimeError(
            "Missing Google OAuth credentials. Run: python3 scripts/google_auth.py"
        )

    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} — {resp.text}")

    return resp.json()["access_token"]


# ── Date range helpers ─────────────────────────────────────────────────────────

def _baseline_range() -> tuple[date, date]:
    """Last 90 days."""
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=89)
    return start, end

def _monthly_range(month_label: str | None = None) -> tuple[date, date]:
    """Previous calendar month, or the month specified by label (e.g. 'March 2026')."""
    if month_label:
        dt = datetime.strptime(month_label, "%B %Y")
        y, m = dt.year, dt.month
    else:
        today = date.today()
        m = today.month - 1 or 12
        y = today.year if today.month > 1 else today.year - 1
    _, last_day = monthrange(y, m)
    return date(y, m, 1), date(y, m, last_day)

def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


# ── Google Search Console ──────────────────────────────────────────────────────

def _fetch_gsc(
    access_token: str,
    site_url: str,
    start: date,
    end: date,
    business_name: str,
) -> dict:
    """
    Pull GSC search analytics: impressions, clicks, CTR, position.
    Also computes branded vs non-branded split.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    base    = f"https://www.googleapis.com/webmasters/v3/sites/{site_url}/searchAnalytics/query"

    # Overall metrics
    body_overall = {
        "startDate":  _fmt_date(start),
        "endDate":    _fmt_date(end),
        "dimensions": [],
        "rowLimit":   1,
    }
    r = httpx.post(base, headers=headers, json=body_overall, timeout=30)
    if r.status_code != 200:
        print(f"  ⚠ GSC error {r.status_code}: {r.text[:200]}")
        return {}

    rows = r.json().get("rows", [{}])
    overall = rows[0] if rows else {}

    impressions  = int(overall.get("impressions", 0))
    clicks       = int(overall.get("clicks", 0))
    ctr          = round(overall.get("ctr", 0) * 100, 2)
    avg_position = round(overall.get("position", 0), 1)

    # Top queries (for branded split)
    body_queries = {
        "startDate":  _fmt_date(start),
        "endDate":    _fmt_date(end),
        "dimensions": ["query"],
        "rowLimit":   100,
    }
    r2 = httpx.post(base, headers=headers, json=body_queries, timeout=30)
    query_rows = r2.json().get("rows", []) if r2.status_code == 200 else []

    # Branded detection: query contains any word from business name
    brand_tokens = set(business_name.lower().split())
    brand_tokens.discard("the")
    brand_tokens.discard("and")
    brand_tokens.discard("of")

    branded_impressions   = 0
    unbranded_impressions = 0
    branded_clicks        = 0
    unbranded_clicks      = 0
    top_queries           = []

    for qrow in query_rows:
        q  = qrow.get("keys", [""])[0]
        qi = int(qrow.get("impressions", 0))
        qc = int(qrow.get("clicks", 0))
        is_branded = any(tok in q.lower() for tok in brand_tokens)
        if is_branded:
            branded_impressions += qi
            branded_clicks      += qc
        else:
            unbranded_impressions += qi
            unbranded_clicks      += qc
        top_queries.append({
            "query":       q,
            "impressions": qi,
            "clicks":      qc,
            "ctr":         round(qrow.get("ctr", 0) * 100, 2),
            "position":    round(qrow.get("position", 0), 1),
            "branded":     is_branded,
        })

    top_queries.sort(key=lambda x: x["impressions"], reverse=True)

    branded_pct   = round(branded_impressions / impressions * 100, 1) if impressions else 0
    unbranded_pct = round(100 - branded_pct, 1)

    # Top pages
    body_pages = {
        "startDate":  _fmt_date(start),
        "endDate":    _fmt_date(end),
        "dimensions": ["page"],
        "rowLimit":   10,
    }
    r3 = httpx.post(base, headers=headers, json=body_pages, timeout=30)
    page_rows = r3.json().get("rows", []) if r3.status_code == 200 else []
    top_pages = [
        {
            "page":        pr.get("keys", [""])[0],
            "impressions": int(pr.get("impressions", 0)),
            "clicks":      int(pr.get("clicks", 0)),
            "position":    round(pr.get("position", 0), 1),
        }
        for pr in page_rows
    ]

    return {
        "impressions":          impressions,
        "clicks":               clicks,
        "ctr":                  ctr,
        "avg_position":         avg_position,
        "branded_impressions":  branded_impressions,
        "unbranded_impressions":unbranded_impressions,
        "branded_clicks":       branded_clicks,
        "unbranded_clicks":     unbranded_clicks,
        "branded_pct":          branded_pct,
        "unbranded_pct":        unbranded_pct,
        "top_queries":          top_queries[:10],
        "top_pages":            top_pages,
    }


# ── Google Analytics 4 ─────────────────────────────────────────────────────────

def _fetch_ga4(
    access_token: str,
    property_id: str,
    start: date,
    end: date,
) -> dict:
    """Pull GA4: total sessions, organic sessions, users, engagement rate."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url     = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

    body = {
        "dateRanges": [{"startDate": _fmt_date(start), "endDate": _fmt_date(end)}],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalUsers"},
            {"name": "engagementRate"},
        ],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "limit": 20,
    }

    r = httpx.post(url, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        print(f"  ⚠ GA4 error {r.status_code}: {r.text[:200]}")
        return {}

    data = r.json()
    rows = data.get("rows", [])

    total_sessions    = 0
    organic_sessions  = 0
    total_users       = 0
    engagement_sum    = 0.0
    engagement_count  = 0

    for row in rows:
        dims    = row.get("dimensionValues", [{}])
        channel = dims[0].get("value", "") if dims else ""
        vals    = row.get("metricValues", [{}, {}, {}])
        sess    = int(vals[0].get("value", 0)) if len(vals) > 0 else 0
        users   = int(vals[1].get("value", 0)) if len(vals) > 1 else 0
        eng     = float(vals[2].get("value", 0)) if len(vals) > 2 else 0.0

        total_sessions   += sess
        total_users      += users
        engagement_sum   += eng * sess
        engagement_count += sess

        if "organic" in channel.lower():
            organic_sessions += sess

    avg_engagement = round(engagement_sum / engagement_count * 100, 1) if engagement_count else 0
    organic_pct    = round(organic_sessions / total_sessions * 100, 1) if total_sessions else 0

    return {
        "total_sessions":   total_sessions,
        "organic_sessions": organic_sessions,
        "total_users":      total_users,
        "engagement_rate":  avg_engagement,
        "organic_pct":      organic_pct,
    }


# ── Google Business Profile Performance ───────────────────────────────────────

def _fetch_gbp(
    access_token: str,
    location_id: str,
    start: date,
    end: date,
) -> dict:
    """Pull GBP performance: impressions, calls, directions, website clicks."""
    headers = {"Authorization": f"Bearer {access_token}"}

    # Ensure location_id is in "locations/XXXXX" format
    if not location_id.startswith("locations/"):
        location_id = f"locations/{location_id}"

    url = (
        f"https://businessprofileperformance.googleapis.com/v1/"
        f"{location_id}:fetchMultiDailyMetrics"
    )

    params = {
        "dailyMetrics": [
            "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
            "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
            "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
            "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
            "CALL_CLICKS",
            "BUSINESS_DIRECTION_REQUESTS",
            "WEBSITE_CLICKS",
        ],
        "dailyRange.startDate.year":  start.year,
        "dailyRange.startDate.month": start.month,
        "dailyRange.startDate.day":   start.day,
        "dailyRange.endDate.year":    end.year,
        "dailyRange.endDate.month":   end.month,
        "dailyRange.endDate.day":     end.day,
    }

    r = httpx.get(url, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        print(f"  ⚠ GBP error {r.status_code}: {r.text[:200]}")
        return {}

    data       = r.json()
    metric_map = {
        m.get("dailyMetric", ""): sum(
            int(dv.get("value", 0))
            for dv in m.get("timeSeries", {}).get("datedValues", [])
        )
        for m in data.get("multiDailyMetricTimeSeries", [])
    }

    impressions = (
        metric_map.get("BUSINESS_IMPRESSIONS_DESKTOP_MAPS", 0) +
        metric_map.get("BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", 0) +
        metric_map.get("BUSINESS_IMPRESSIONS_MOBILE_MAPS", 0) +
        metric_map.get("BUSINESS_IMPRESSIONS_MOBILE_SEARCH", 0)
    )

    return {
        "impressions":  impressions,
        "calls":        metric_map.get("CALL_CLICKS", 0),
        "directions":   metric_map.get("BUSINESS_DIRECTION_REQUESTS", 0),
        "website_clicks": metric_map.get("WEBSITE_CLICKS", 0),
    }


# ── DataForSEO Backlinks (Domain Authority) ───────────────────────────────────

def _fetch_authority(domain: str) -> dict:
    """Pull domain authority metrics from DataForSEO backlinks summary."""
    login    = settings.dataforseo_login
    password = settings.dataforseo_password
    if not login or not password:
        return {}

    auth = base64.b64encode(f"{login}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}

    # Strip protocol/path — just the domain
    domain = re.sub(r"https?://", "", domain).rstrip("/").split("/")[0]

    body = [{"target": domain, "limit": 1}]
    r = httpx.post(
        f"{DATAFORSEO_BASE}/backlinks/summary/live",
        headers=headers,
        json=body,
        timeout=30,
    )
    if r.status_code != 200:
        return {}

    tasks = r.json().get("tasks", [{}])
    result = (tasks[0].get("result") or [{}])[0] if tasks else {}

    return {
        "authority_score":   result.get("rank"),
        "referring_domains": result.get("referring_domains"),
        "backlinks":         result.get("backlinks"),
    }


# ── Search Atlas Rank Tracker ─────────────────────────────────────────────────

def _fetch_search_atlas(project_id: str) -> dict:
    """Pull rank tracker summary from Search Atlas if project is configured."""
    api_key = settings.search_atlas_api_key
    if not api_key or not project_id:
        return {}

    try:
        r = httpx.get(
            f"{SEARCH_ATLAS_BASE}/v1/rank-tracker/{project_id}/keywords-details/",
            params={"searchatlas_api_key": api_key},
            timeout=30,
        )
        if r.status_code != 200:
            return {}

        rows = r.json() if isinstance(r.json(), list) else r.json().get("results", [])
        total_keywords = len(rows)
        top_3  = sum(1 for kw in rows if isinstance(kw.get("position"), (int, float)) and kw["position"] <= 3)
        top_10 = sum(1 for kw in rows if isinstance(kw.get("position"), (int, float)) and kw["position"] <= 10)
        est_traffic = sum(kw.get("estimated_traffic", 0) or 0 for kw in rows)

        return {
            "tracked_keywords": total_keywords,
            "top_3_rankings":   top_3,
            "top_10_rankings":  top_10,
            "estimated_traffic": int(est_traffic),
        }
    except Exception:
        return {}


# ── Claude narrative summary ───────────────────────────────────────────────────

def _generate_summary(
    anthropic_client: anthropic.Anthropic,
    business_name: str,
    report_type: str,
    date_range: str,
    gsc: dict,
    ga4: dict,
    gbp: dict,
    authority: dict,
    search_atlas: dict,
) -> str:
    """Ask Claude to write a 2–3 paragraph plain-English summary of the report data."""

    def _n(v):
        """Comma-format a number. Returns 'N/A' for missing / non-numeric values
        so data source gaps (e.g. GBP pre-API-approval) don't crash the formatter."""
        if v is None:
            return "N/A"
        try:
            return f"{int(v):,}"
        except (ValueError, TypeError):
            return str(v)

    def _pct(v):
        return "N/A" if v is None else f"{v}"

    data_block = f"""
Business: {business_name}
Report type: {report_type}
Date range: {date_range}

SEARCH CONSOLE:
- Impressions: {_n(gsc.get('impressions'))}
- Clicks: {_n(gsc.get('clicks'))}
- CTR: {_pct(gsc.get('ctr'))}%
- Avg Position: {_pct(gsc.get('avg_position'))}
- Branded impressions: {_pct(gsc.get('branded_pct'))}% | Non-branded: {_pct(gsc.get('unbranded_pct'))}%

ANALYTICS (GA4):
- Total sessions: {_n(ga4.get('total_sessions'))}
- Organic sessions: {_n(ga4.get('organic_sessions'))} ({_pct(ga4.get('organic_pct'))}% of total)
- Users: {_n(ga4.get('total_users'))}
- Engagement rate: {_pct(ga4.get('engagement_rate'))}%

GOOGLE BUSINESS PROFILE:
- Impressions: {_n(gbp.get('impressions'))}
- Calls: {_n(gbp.get('calls'))}
- Direction requests: {_n(gbp.get('directions'))}
- Website clicks: {_n(gbp.get('website_clicks'))}

DOMAIN AUTHORITY (DataForSEO, 0–1000 scale):
- Authority score: {_pct(authority.get('authority_score'))}
- Referring domains: {_n(authority.get('referring_domains'))}
- Total backlinks: {_n(authority.get('backlinks'))}
"""

    if search_atlas:
        data_block += f"""
SEARCH ATLAS RANK TRACKER:
- Tracked keywords: {_pct(search_atlas.get('tracked_keywords'))}
- Top 3 rankings: {_pct(search_atlas.get('top_3_rankings'))}
- Top 10 rankings: {_pct(search_atlas.get('top_10_rankings'))}
- Estimated monthly traffic: {_n(search_atlas.get('estimated_traffic'))}
"""

    prompt = f"""You are an SEO analyst writing a plain-English summary for a digital marketing agency's internal report.

Write 2–3 short paragraphs summarizing the SEO performance data below. Focus on:
1. Overall visibility and traffic health
2. The branded vs. non-branded split — what it means for this business
3. The biggest opportunity or concern visible in this data

Be direct and specific. Use actual numbers. No filler phrases like "it's important to note."
Do not suggest tools (Moz, Ahrefs, Semrush) — we use DataForSEO and Search Atlas.
Authority score is on a 0–1000 logarithmic scale (not 0–100).

{data_block}"""

    response = anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ── Notion SEO Metrics DB ─────────────────────────────────────────────────────

SEO_METRICS_SCHEMA = {
    "Month":              {"title": {}},
    "Report Type":        {"select": {"options": [
        {"name": "Baseline", "color": "blue"},
        {"name": "Monthly",  "color": "green"},
    ]}},
    "Date Range":         {"rich_text": {}},
    # GSC
    "GSC Impressions":    {"number": {}},
    "GSC Clicks":         {"number": {}},
    "GSC CTR":            {"number": {}},
    "GSC Avg Position":   {"number": {}},
    "Branded %":          {"number": {}},
    "Non-Branded %":      {"number": {}},
    # GA4
    "Total Sessions":     {"number": {}},
    "Organic Sessions":   {"number": {}},
    "Total Users":        {"number": {}},
    "Engagement Rate":    {"number": {}},
    "Organic % of Traffic": {"number": {}},
    # GBP
    "GBP Impressions":    {"number": {}},
    "GBP Calls":          {"number": {}},
    "GBP Directions":     {"number": {}},
    "GBP Website Clicks": {"number": {}},
    # Authority
    "Authority Score":    {"number": {}},
    "Referring Domains":  {"number": {}},
    "Backlinks":          {"number": {}},
    # Search Atlas
    "Tracked Keywords":   {"number": {}},
    "Top 3 Rankings":     {"number": {}},
    "Top 10 Rankings":    {"number": {}},
    "Est. Monthly Traffic": {"number": {}},
    # Summary
    "Summary":            {"rich_text": {}},
    "HTML Report URL":    {"url": {}},
}


async def _ensure_seo_metrics_db(
    notion: NotionClient,
    client_key: str,
    cfg: dict,
) -> str:
    """Create SEO Metrics DB under client's Notion page if it doesn't exist."""
    db_id = cfg.get("seo_metrics_db_id", "")
    if db_id:
        return db_id

    client_info_db = cfg.get("client_info_db_id", "")
    db_meta = await notion._client.request(
        path=f"databases/{client_info_db}",
        method="GET",
    )
    parent_page_id = db_meta.get("parent", {}).get("page_id", "")
    if not parent_page_id:
        raise ValueError("Could not determine parent page for SEO Metrics DB")

    result = await notion._client.request(
        path="databases",
        method="POST",
        body={
            "parent":     {"type": "page_id", "page_id": parent_page_id},
            "title":      [{"type": "text", "text": {"content": "SEO Metrics"}}],
            "properties": SEO_METRICS_SCHEMA,
        },
    )
    new_id = result["id"]
    print(f"  Created SEO Metrics DB: {new_id}")

    # Save to clients.json
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}
        if client_key not in data:
            data[client_key] = {}
        data[client_key]["seo_metrics_db_id"] = new_id
        CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  ⚠ Could not save seo_metrics_db_id: {e}")

    return new_id


async def _write_notion_entry(
    notion: NotionClient,
    db_id: str,
    month_label: str,
    report_type: str,
    date_range: str,
    gsc: dict,
    ga4: dict,
    gbp: dict,
    authority: dict,
    search_atlas: dict,
    summary: str,
    html_url: str,
) -> None:
    def _n(val) -> dict:
        return {"number": val} if val is not None else {"number": None}

    properties: dict = {
        "Month":        {"title": [{"text": {"content": month_label}}]},
        "Report Type":  {"select": {"name": report_type}},
        "Date Range":   {"rich_text": [{"text": {"content": date_range}}]},
        # GSC
        "GSC Impressions":  _n(gsc.get("impressions")),
        "GSC Clicks":       _n(gsc.get("clicks")),
        "GSC CTR":          _n(gsc.get("ctr")),
        "GSC Avg Position": _n(gsc.get("avg_position")),
        "Branded %":        _n(gsc.get("branded_pct")),
        "Non-Branded %":    _n(gsc.get("unbranded_pct")),
        # GA4
        "Total Sessions":       _n(ga4.get("total_sessions")),
        "Organic Sessions":     _n(ga4.get("organic_sessions")),
        "Total Users":          _n(ga4.get("total_users")),
        "Engagement Rate":      _n(ga4.get("engagement_rate")),
        "Organic % of Traffic": _n(ga4.get("organic_pct")),
        # GBP
        "GBP Impressions":    _n(gbp.get("impressions")),
        "GBP Calls":          _n(gbp.get("calls")),
        "GBP Directions":     _n(gbp.get("directions")),
        "GBP Website Clicks": _n(gbp.get("website_clicks")),
        # Authority
        "Authority Score":   _n(authority.get("authority_score")),
        "Referring Domains": _n(authority.get("referring_domains")),
        "Backlinks":         _n(authority.get("backlinks")),
        # Search Atlas
        "Tracked Keywords":     _n(search_atlas.get("tracked_keywords")),
        "Top 3 Rankings":       _n(search_atlas.get("top_3_rankings")),
        "Top 10 Rankings":      _n(search_atlas.get("top_10_rankings")),
        "Est. Monthly Traffic": _n(search_atlas.get("estimated_traffic")),
        # Summary
        "Summary": {"rich_text": [{"text": {"content": summary[:2000]}}]},
    }

    if html_url:
        properties["HTML Report URL"] = {"url": html_url}

    await notion._client.request(
        path="pages",
        method="POST",
        body={"parent": {"database_id": db_id}, "properties": properties},
    )


# ── HTML Report ───────────────────────────────────────────────────────────────

def _generate_html(
    business_name: str,
    report_type: str,
    date_range: str,
    gsc: dict,
    ga4: dict,
    gbp: dict,
    authority: dict,
    search_atlas: dict,
    summary: str,
) -> str:
    def _row(label: str, value, suffix: str = "") -> str:
        v = f"{value:,}" if isinstance(value, int) else (f"{value}" if value is not None else "—")
        return f"<tr><td>{label}</td><td><strong>{v}{suffix}</strong></td></tr>"

    top_queries_html = ""
    for q in gsc.get("top_queries", [])[:10]:
        branded_badge = '<span class="badge branded">Branded</span>' if q["branded"] else '<span class="badge">Non-branded</span>'
        top_queries_html += f"""
        <tr>
          <td>{q['query']} {branded_badge}</td>
          <td>{q['impressions']:,}</td>
          <td>{q['clicks']:,}</td>
          <td>{q['ctr']}%</td>
          <td>{q['position']}</td>
        </tr>"""

    top_pages_html = ""
    for p in gsc.get("top_pages", [])[:10]:
        short = p["page"].replace("https://", "").replace("http://", "")
        top_pages_html += f"""
        <tr>
          <td class="url">{short}</td>
          <td>{p['impressions']:,}</td>
          <td>{p['clicks']:,}</td>
          <td>{p['position']}</td>
        </tr>"""

    sa_section = ""
    if search_atlas:
        sa_section = f"""
        <div class="section">
          <h2>Search Atlas — Rank Tracker</h2>
          <table>
            {_row("Tracked Keywords", search_atlas.get("tracked_keywords"))}
            {_row("Top 3 Rankings", search_atlas.get("top_3_rankings"))}
            {_row("Top 10 Rankings", search_atlas.get("top_10_rankings"))}
            {_row("Est. Monthly Traffic", search_atlas.get("estimated_traffic"))}
          </table>
        </div>"""

    generated_date  = date.today().strftime("%B %d, %Y")
    summary_paras   = "".join(f"<p>{para}</p>" for para in summary.split("\n\n") if para.strip())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SEO Report — {business_name} — {report_type}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #1a1a1a; }}
  .header {{ background: #1a1a1a; color: white; padding: 2rem 3rem; }}
  .header h1 {{ font-size: 1.6rem; font-weight: 600; }}
  .header p {{ color: #999; margin-top: 0.3rem; font-size: 0.9rem; }}
  .container {{ max-width: 960px; margin: 2rem auto; padding: 0 1.5rem; }}
  .summary-box {{ background: white; border-radius: 8px; padding: 1.5rem 2rem; margin-bottom: 1.5rem; border-left: 4px solid #3b82f6; }}
  .summary-box h2 {{ font-size: 1rem; color: #3b82f6; margin-bottom: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  .summary-box p {{ line-height: 1.7; color: #444; font-size: 0.95rem; margin-bottom: 0.75rem; }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
  .metric-card {{ background: white; border-radius: 8px; padding: 1.25rem 1.5rem; }}
  .metric-card .label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #888; margin-bottom: 0.5rem; }}
  .metric-card .value {{ font-size: 2rem; font-weight: 700; color: #1a1a1a; }}
  .metric-card .sub {{ font-size: 0.8rem; color: #888; margin-top: 0.25rem; }}
  .section {{ background: white; border-radius: 8px; padding: 1.5rem 2rem; margin-bottom: 1.5rem; }}
  .section h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #eee; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  th {{ text-align: left; color: #888; font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; padding: 0.5rem 0.75rem; border-bottom: 2px solid #eee; }}
  td {{ padding: 0.6rem 0.75rem; border-bottom: 1px solid #f0f0f0; }}
  td.url {{ font-family: monospace; font-size: 0.8rem; color: #555; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .badge {{ font-size: 0.7rem; padding: 2px 6px; border-radius: 10px; background: #e5e7eb; color: #555; margin-left: 6px; }}
  .badge.branded {{ background: #fef3c7; color: #92400e; }}
  .branded-split {{ display: flex; gap: 1rem; margin-top: 1rem; }}
  .split-bar {{ flex: 1; background: #f0f0f0; border-radius: 4px; overflow: hidden; height: 8px; align-self: center; }}
  .split-bar-inner {{ height: 100%; background: #3b82f6; border-radius: 4px; }}
  .split-label {{ font-size: 0.85rem; color: #555; }}
  .footer {{ text-align: center; color: #aaa; font-size: 0.8rem; margin: 2rem 0; }}
</style>
</head>
<body>
<div class="header">
  <h1>SEO Performance Report — {business_name}</h1>
  <p>{report_type} &nbsp;·&nbsp; {date_range} &nbsp;·&nbsp; Generated {generated_date}</p>
</div>

<div class="container">

  <div class="summary-box">
    <h2>Summary</h2>
    {summary_paras}
  </div>

  <!-- Top metrics -->
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="label">GSC Impressions</div>
      <div class="value">{gsc.get('impressions', 0):,}</div>
      <div class="sub">Search Console</div>
    </div>
    <div class="metric-card">
      <div class="label">GSC Clicks</div>
      <div class="value">{gsc.get('clicks', 0):,}</div>
      <div class="sub">CTR: {gsc.get('ctr', 0)}%</div>
    </div>
    <div class="metric-card">
      <div class="label">Avg Position</div>
      <div class="value">{gsc.get('avg_position', '—')}</div>
      <div class="sub">Search Console</div>
    </div>
    <div class="metric-card">
      <div class="label">Organic Sessions</div>
      <div class="value">{ga4.get('organic_sessions', 0):,}</div>
      <div class="sub">{ga4.get('organic_pct', 0)}% of total traffic</div>
    </div>
    <div class="metric-card">
      <div class="label">GBP Impressions</div>
      <div class="value">{gbp.get('impressions', 0):,}</div>
      <div class="sub">Maps + Search</div>
    </div>
    <div class="metric-card">
      <div class="label">GBP Calls</div>
      <div class="value">{gbp.get('calls', 0):,}</div>
      <div class="sub">Direction requests: {gbp.get('directions', 0):,}</div>
    </div>
  </div>

  <!-- Branded split -->
  <div class="section">
    <h2>Search Console — Branded vs. Non-Branded</h2>
    <p style="color:#666; font-size:0.875rem; margin-bottom:1rem;">
      Non-branded impressions show discovery potential — people finding you without already knowing your name.
    </p>
    <div class="branded-split">
      <div class="split-label" style="min-width:140px">
        <strong>{gsc.get('branded_pct', 0)}%</strong> Branded<br>
        <span style="color:#aaa">{gsc.get('branded_impressions', 0):,} impressions</span>
      </div>
      <div class="split-bar">
        <div class="split-bar-inner" style="width:{gsc.get('branded_pct', 0)}%"></div>
      </div>
      <div class="split-label" style="min-width:140px; text-align:right">
        <strong>{gsc.get('unbranded_pct', 0)}%</strong> Non-branded<br>
        <span style="color:#aaa">{gsc.get('unbranded_impressions', 0):,} impressions</span>
      </div>
    </div>
  </div>

  <!-- Top queries -->
  <div class="section">
    <h2>Search Console — Top Queries</h2>
    <table>
      <thead><tr><th>Query</th><th>Impressions</th><th>Clicks</th><th>CTR</th><th>Avg Position</th></tr></thead>
      <tbody>{top_queries_html}</tbody>
    </table>
  </div>

  <!-- Top pages -->
  <div class="section">
    <h2>Search Console — Top Pages</h2>
    <table>
      <thead><tr><th>Page</th><th>Impressions</th><th>Clicks</th><th>Avg Position</th></tr></thead>
      <tbody>{top_pages_html}</tbody>
    </table>
  </div>

  <!-- GA4 -->
  <div class="section">
    <h2>Google Analytics 4</h2>
    <table>
      {_row("Total Sessions", ga4.get("total_sessions"))}
      {_row("Organic Sessions", ga4.get("organic_sessions"))}
      {_row("Organic % of Traffic", ga4.get("organic_pct"), "%")}
      {_row("Total Users", ga4.get("total_users"))}
      {_row("Engagement Rate", ga4.get("engagement_rate"), "%")}
    </table>
  </div>

  <!-- GBP -->
  <div class="section">
    <h2>Google Business Profile</h2>
    <table>
      {_row("Impressions (Maps + Search)", gbp.get("impressions"))}
      {_row("Calls", gbp.get("calls"))}
      {_row("Direction Requests", gbp.get("directions"))}
      {_row("Website Clicks", gbp.get("website_clicks"))}
    </table>
  </div>

  <!-- Authority -->
  <div class="section">
    <h2>Domain Authority (DataForSEO — 0–1000 scale)</h2>
    <table>
      {_row("Authority Score", authority.get("authority_score"))}
      {_row("Referring Domains", authority.get("referring_domains"))}
      {_row("Total Backlinks", authority.get("backlinks"))}
    </table>
  </div>

  {sa_section}

  <div class="footer">RxMedia Agency &nbsp;·&nbsp; {generated_date}</div>
</div>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(
    client_key: str,
    report_type: str,
    month_label: str | None,
    open_report: bool,
) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found.")
        sys.exit(1)

    client_name = cfg.get("name", client_key)
    gsc_site    = cfg.get("gsc_site_url", "")
    ga4_prop    = cfg.get("ga4_property_id", "")
    gbp_loc     = cfg.get("gbp_location_id", "")
    sa_project  = cfg.get("search_atlas_project_id", "")
    website     = gsc_site.rstrip("/") or cfg.get("website", "")

    # Determine date range
    if report_type == "Baseline":
        start, end = _baseline_range()
        label = f"Baseline (last 90 days: {_fmt_date(start)} → {_fmt_date(end)})"
    else:
        start, end = _monthly_range(month_label)
        label = f"{month_label or start.strftime('%B %Y')} ({_fmt_date(start)} → {_fmt_date(end)})"

    month_str  = month_label or (
        f"Baseline {date.today().strftime('%B %Y')}"
        if report_type == "Baseline"
        else start.strftime("%B %Y")
    )
    date_range = f"{_fmt_date(start)} → {_fmt_date(end)}"

    print(f"\n{'='*60}")
    print(f"  SEO {report_type} Report — {client_name}")
    print(f"  {date_range}")
    print(f"{'='*60}\n")

    # Get OAuth access token
    print("Getting Google access token...")
    try:
        access_token = _get_access_token()
        print("  ✓ Token obtained")
    except Exception as e:
        print(f"  ✗ {e}")
        access_token = ""

    # Pull data from each source
    gsc = {}
    if gsc_site and access_token:
        print(f"Fetching Google Search Console ({gsc_site})...")
        try:
            from urllib.parse import quote
            gsc = _fetch_gsc(access_token, quote(gsc_site, safe=""), start, end, client_name)
            print(f"  ✓ {gsc.get('impressions', 0):,} impressions, {gsc.get('clicks', 0):,} clicks")
        except Exception as e:
            print(f"  ⚠ GSC error: {e}")
    else:
        print(f"  ⚠ Skipping GSC — {'gsc_site_url not configured' if not gsc_site else 'no access token'}")

    ga4 = {}
    if ga4_prop and access_token:
        print(f"Fetching Google Analytics 4 (property {ga4_prop})...")
        try:
            ga4 = _fetch_ga4(access_token, ga4_prop, start, end)
            print(f"  ✓ {ga4.get('total_sessions', 0):,} total sessions, {ga4.get('organic_sessions', 0):,} organic")
        except Exception as e:
            print(f"  ⚠ GA4 error: {e}")
    else:
        print(f"  ⚠ Skipping GA4 — {'ga4_property_id not configured' if not ga4_prop else 'no access token'}")

    gbp = {}
    if gbp_loc and access_token:
        print(f"Fetching GBP performance ({gbp_loc})...")
        try:
            gbp = _fetch_gbp(access_token, gbp_loc, start, end)
            print(f"  ✓ {gbp.get('impressions', 0):,} impressions, {gbp.get('calls', 0):,} calls")
        except Exception as e:
            print(f"  ⚠ GBP error: {e}")
    else:
        print(f"  ⚠ Skipping GBP — gbp_location_id not configured")

    authority = {}
    if website:
        print(f"Fetching domain authority ({website})...")
        try:
            authority = _fetch_authority(website)
            print(f"  ✓ Authority: {authority.get('authority_score')}, Referring domains: {authority.get('referring_domains'):,}")
        except Exception as e:
            print(f"  ⚠ Authority error: {e}")

    search_atlas = {}
    if sa_project:
        print(f"Fetching Search Atlas rank tracker (project {sa_project})...")
        try:
            search_atlas = _fetch_search_atlas(sa_project)
            print(f"  ✓ {search_atlas.get('tracked_keywords', 0)} keywords tracked")
        except Exception as e:
            print(f"  ⚠ Search Atlas error: {e}")
    else:
        print(f"  ⚠ Skipping Search Atlas — search_atlas_project_id not configured")

    # Claude summary
    print("Generating narrative summary...")
    anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    summary = _generate_summary(
        anthropic_client, client_name, report_type, date_range,
        gsc, ga4, gbp, authority, search_atlas,
    )
    print("  ✓ Summary generated")

    # HTML report
    print("Building HTML report...")
    html = _generate_html(
        client_name, report_type, date_range,
        gsc, ga4, gbp, authority, search_atlas, summary,
    )
    out_dir = OUTPUT_DIR / client_key
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = month_str.lower().replace(" ", "_")
    html_path = out_dir / f"seo_report_{slug}.html"
    html_path.write_text(html)
    print(f"  ✓ Saved to {html_path}")

    if open_report:
        import webbrowser
        webbrowser.open(str(html_path))

    # Notion
    print("Writing to Notion SEO Metrics DB...")
    notion = NotionClient(api_key=settings.notion_api_key)
    seo_db_id = await _ensure_seo_metrics_db(notion, client_key, cfg)
    await _write_notion_entry(
        notion, seo_db_id, month_str, report_type, date_range,
        gsc, ga4, gbp, authority, search_atlas, summary, "",
    )
    print("  ✓ Entry written to Notion")

    print(f"\n✓ Done. Report: {html_path}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="SEO baseline or monthly report")
    parser.add_argument("--client",   required=True)
    parser.add_argument("--baseline", action="store_true", help="Run 90-day baseline")
    parser.add_argument("--monthly",  action="store_true", help="Run monthly report")
    parser.add_argument("--month",    default=None, help="Month label e.g. 'March 2026'")
    parser.add_argument("--open",     action="store_true", help="Open HTML report in browser")
    args = parser.parse_args()

    if not args.baseline and not args.monthly:
        parser.error("Specify --baseline or --monthly")

    report_type = "Baseline" if args.baseline else "Monthly"
    asyncio.run(run(args.client, report_type, args.month, args.open))


if __name__ == "__main__":
    main()
