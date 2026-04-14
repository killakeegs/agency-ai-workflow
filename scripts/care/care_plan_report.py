#!/usr/bin/env python3
"""
care_plan_report.py — Monthly website care plan report

For each care plan client:
  1. Runs Google PageSpeed Insights (mobile + desktop)
  2. Extracts Core Web Vitals + top insights
  3. Asks Claude to generate actionable recommendations
  4. Writes a new monthly entry to the client's Care Plan Notion DB
  5. Posts a Slack summary

Usage:
    make care-plan                          # run report for all care plan clients
    make care-plan CLIENT=summit_therapy    # run for a specific client
    python scripts/care_plan_report.py --init --client summit_therapy
                                            # create Care Plan DB for existing client

Runs automatically on the 1st of each month via Railway cron.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
import httpx

from config.clients import CLIENTS
from src.config import settings  # loads .env via pydantic-settings
from src.integrations.notion import NotionClient

logger = logging.getLogger(__name__)

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CLIENTS_JSON_PATH = Path(__file__).parent.parent / "config" / "clients.json"


# ── Rating helpers ────────────────────────────────────────────────────────────

def _score_rating(score: int) -> str:
    if score >= 90:
        return "Good (90–100)"
    if score >= 50:
        return "Needs Improvement (50–89)"
    return "Poor (0–49)"


def _score_emoji(score: int) -> str:
    return "🟢" if score >= 90 else ("🟡" if score >= 50 else "🔴")


def _metric_emoji(metric_score: float) -> str:
    """Lighthouse metric scores are 0.0–1.0."""
    if metric_score >= 0.9:
        return "🟢"
    if metric_score >= 0.5:
        return "🟡"
    return "🔴"


# ── PageSpeed API ─────────────────────────────────────────────────────────────

async def _run_pagespeed(url: str, strategy: str) -> dict:
    """
    Run PageSpeed Insights for a URL and strategy ('mobile' or 'desktop').

    Returns dict with:
      score       — overall performance score (0–100)
      metrics     — dict of Core Web Vitals (fcp, lcp, tbt, si, cls)
      insights    — list of top opportunities sorted by impact
    """
    params: dict = {"url": url, "strategy": strategy}
    google_api_key = settings.google_api_key or os.environ.get("GOOGLE_API_KEY", "")
    if google_api_key:
        params["key"] = google_api_key.strip()

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                r = await http.get(PAGESPEED_URL, params=params)
            break
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            if attempt < 2:
                wait = 15 * (attempt + 1)
                logger.warning(f"Timeout on {strategy} attempt {attempt+1} — retrying in {wait}s")
                await asyncio.sleep(wait)
            else:
                logger.warning(f"PageSpeed timed out after 3 attempts for {url} ({strategy})")
                return {"score": 0, "metrics": {}, "insights": []}

    if r.status_code != 200:
        logger.warning(f"PageSpeed API error {r.status_code} for {url} ({strategy})")
        return {"score": 0, "metrics": {}, "insights": []}

    data = r.json()
    lighthouse = data.get("lighthouseResult", {})
    audits = lighthouse.get("audits", {})

    # Overall performance score
    perf = lighthouse.get("categories", {}).get("performance", {})
    score = round((perf.get("score") or 0) * 100)

    # Core Web Vitals
    def _metric(audit_id: str) -> dict:
        audit = audits.get(audit_id, {})
        return {
            "display": audit.get("displayValue", "N/A"),
            "value":   audit.get("numericValue", 0),
            "score":   audit.get("score") or 0,  # 0.0–1.0
        }

    metrics = {
        "fcp": _metric("first-contentful-paint"),
        "lcp": _metric("largest-contentful-paint"),
        "tbt": _metric("total-blocking-time"),
        "si":  _metric("speed-index"),
        "cls": _metric("cumulative-layout-shift"),
    }

    # Top insights — opportunities + diagnostics with impact
    insights = []
    for audit_id, audit in audits.items():
        details = audit.get("details", {})
        d_type = details.get("type", "")
        title = audit.get("title", "")
        audit_score = audit.get("score")

        if d_type == "opportunity":
            savings_ms = audit.get("numericValue", 0)
            savings_disp = audit.get("displayValue", "")
            if title and savings_ms > 0:
                insights.append({
                    "type": "opportunity",
                    "title": title,
                    "savings": savings_disp,
                    "savings_ms": savings_ms,
                    "score": audit_score if audit_score is not None else 0,
                })
        elif d_type == "table" and audit_score is not None and audit_score < 0.9:
            # Diagnostics that failed
            if title:
                insights.append({
                    "type": "diagnostic",
                    "title": title,
                    "savings": "",
                    "savings_ms": 0,
                    "score": audit_score,
                })

    # Sort: failed opportunities first (by savings), then diagnostics
    insights.sort(key=lambda x: (-x["savings_ms"], x["score"]))

    return {
        "score": score,
        "metrics": metrics,
        "insights": insights[:8],  # top 8
    }


def _format_metrics(metrics: dict, strategy: str) -> str:
    """Format Core Web Vitals as a readable string for Notion."""
    if not metrics:
        return "No data"
    lines = [f"{strategy.upper()} Core Web Vitals:"]
    labels = {
        "fcp": "First Contentful Paint (FCP)",
        "lcp": "Largest Contentful Paint (LCP)",
        "tbt": "Total Blocking Time (TBT)",
        "si":  "Speed Index (SI)",
        "cls": "Cumulative Layout Shift (CLS)",
    }
    for key, label in labels.items():
        m = metrics.get(key, {})
        emoji = _metric_emoji(m.get("score", 0))
        display = m.get("display", "N/A")
        lines.append(f"  {emoji} {label}: {display}")
    return "\n".join(lines)


def _format_insights(insights: list[dict]) -> str:
    """Format insights list as a readable string for Notion."""
    if not insights:
        return "No major issues found."
    lines = ["Top Issues & Opportunities:"]
    for item in insights:
        emoji = _score_emoji(round(item["score"] * 100)) if item["score"] else "🔴"
        savings = f" — Est. savings: {item['savings']}" if item["savings"] else ""
        lines.append(f"  {emoji} {item['title']}{savings}")
    return "\n".join(lines)


# ── Claude recommendations ────────────────────────────────────────────────────

async def _generate_recommendations(
    client_name: str,
    site_url: str,
    mobile: dict,
    desktop: dict,
) -> str:
    """Ask Claude to generate specific, actionable recommendations based on the data."""
    claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    mobile_metrics = _format_metrics(mobile.get("metrics", {}), "Mobile")
    desktop_metrics = _format_metrics(desktop.get("metrics", {}), "Desktop")
    insights_text = _format_insights(mobile.get("insights", []))

    prompt = f"""You are a web performance consultant reviewing a monthly PageSpeed report for {client_name} ({site_url}).

PERFORMANCE SCORES:
Mobile: {mobile['score']}/100
Desktop: {desktop['score']}/100

{mobile_metrics}

{desktop_metrics}

{insights_text}

Write 3–5 specific, actionable recommendations to improve this site's performance. Be direct and practical — tell them exactly what to fix and why it matters. Reference the specific metrics and issues above. Keep each recommendation to 1–2 sentences. Use plain text, no markdown."""

    response = await claude.messages.create(
        model=settings.anthropic_model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip() if response.content else "No recommendations generated."


# ── Notion site URL helper ────────────────────────────────────────────────────

async def _get_site_url(notion: NotionClient, client_key: str) -> str:
    cfg = CLIENTS[client_key]
    entries = await notion.query_database(cfg["client_info_db_id"])
    if not entries:
        return ""
    prop = entries[0]["properties"].get("Website", {})
    return prop.get("url", "") or ""


# ── Auto-patch Care Plan DB with new fields ───────────────────────────────────

async def _patch_care_plan_db(notion: NotionClient, db_id: str) -> None:
    """Add new fields to existing Care Plan DBs that predate the schema update."""
    new_fields = {
        "Mobile Metrics":   {"rich_text": {}},
        "Desktop Metrics":  {"rich_text": {}},
        "Insights":         {"rich_text": {}},
        "Recommendations":  {"rich_text": {}},
    }
    try:
        await notion.update_database(db_id, new_fields)
    except Exception:
        pass  # Fields may already exist — that's fine


# ── Init: create Care Plan DB for an existing client ─────────────────────────

async def init_care_plan(client_key: str) -> None:
    if client_key not in CLIENTS:
        print(f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}")
        return

    cfg = CLIENTS[client_key]
    if cfg.get("care_plan_db_id"):
        print(f"{client_key} already has a Care Plan DB: {cfg['care_plan_db_id']}")
        return

    notion = NotionClient(settings.notion_api_key)

    info_db = cfg["client_info_db_id"]
    db_obj = await notion._client.request(path=f"databases/{info_db}", method="GET")
    parent = db_obj.get("parent", {})
    if parent.get("type") != "page_id":
        print(f"Could not determine parent page for {client_key}")
        return
    parent_page_id = parent["page_id"]

    from scripts.setup_notion import care_plan_schema
    db_id = await notion.create_database(
        parent_page_id=parent_page_id,
        title="Care Plan",
        properties_schema=care_plan_schema(),
    )
    print(f"  ✓ Care Plan DB created: {db_id}")

    existing: dict = {}
    if CLIENTS_JSON_PATH.exists():
        try:
            existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}

    if client_key not in existing:
        existing[client_key] = dict(cfg)
    existing[client_key]["care_plan_db_id"] = db_id
    CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=2))
    print(f"  ✓ clients.json updated with care_plan_db_id")
    print(f"\nNow run: make care-plan CLIENT={client_key}")


# ── Report: run PageSpeed + write to Notion ───────────────────────────────────

async def run_report(client_key: str | None = None) -> None:
    notion = NotionClient(settings.notion_api_key)
    today = date.today()
    month_label = today.strftime("%B %Y")
    date_iso = today.isoformat()

    targets = {}
    for key, cfg in CLIENTS.items():
        if client_key and key != client_key:
            continue
        if cfg.get("care_plan_db_id"):
            targets[key] = cfg

    if not targets:
        if client_key:
            print(f"No care_plan_db_id found for '{client_key}'.")
            print(f"Run: python scripts/care_plan_report.py --init --client {client_key}")
        else:
            print("No clients with care plan configured. Run with --init --client <key> first.")
        return

    print(f"\nCare Plan Report — {month_label}")
    print("=" * 60)

    slack_lines = [f"*Care Plan Report — {month_label}*\n"]
    all_ok = True

    for key, cfg in targets.items():
        client_name = cfg.get("name", key)
        care_plan_db_id = cfg["care_plan_db_id"]

        print(f"\n{client_name}")
        print("-" * 40)

        # Patch DB with new fields if needed
        await _patch_care_plan_db(notion, care_plan_db_id)

        # Get site URL
        site_url = await _get_site_url(notion, key)
        if not site_url:
            print(f"  ⚠ No site URL found in Client Info DB — skipping")
            slack_lines.append(f"• *{client_name}* — ⚠ No site URL configured")
            all_ok = False
            continue

        print(f"  URL: {site_url}")

        # Run PageSpeed
        print("  Running PageSpeed (mobile)... ", end="", flush=True)
        mobile = await _run_pagespeed(site_url, "mobile")
        print(f"{mobile['score']}/100")

        await asyncio.sleep(3)

        print("  Running PageSpeed (desktop)... ", end="", flush=True)
        desktop = await _run_pagespeed(site_url, "desktop")
        print(f"{desktop['score']}/100")

        # Generate Claude recommendations
        print("  Generating recommendations... ", end="", flush=True)
        recommendations = await _generate_recommendations(client_name, site_url, mobile, desktop)
        print("done")

        # Format fields for Notion
        mobile_metrics_str  = _format_metrics(mobile.get("metrics", {}), "Mobile")
        desktop_metrics_str = _format_metrics(desktop.get("metrics", {}), "Desktop")
        insights_str        = _format_insights(mobile.get("insights", []))

        # Write to Notion
        entry_name = f"{client_name} — {month_label}"
        props = {
            "Name":             notion.title_property(entry_name),
            "Report Date":      {"date": {"start": date_iso}},
            "Site URL":         {"url": site_url},
            "Mobile Score":     {"number": mobile["score"]},
            "Desktop Score":    {"number": desktop["score"]},
            "Mobile Rating":    {"select": {"name": _score_rating(mobile["score"])}},
            "Desktop Rating":   {"select": {"name": _score_rating(desktop["score"])}},
            "Mobile Metrics":   notion.text_property(mobile_metrics_str[:2000]),
            "Desktop Metrics":  notion.text_property(desktop_metrics_str[:2000]),
            "Insights":         notion.text_property(insights_str[:2000]),
            "Recommendations":  notion.text_property(recommendations[:2000]),
        }
        entry_id = await notion.create_database_entry(care_plan_db_id, props)
        print(f"  ✓ Written to Notion: {entry_id}")

        # Build Slack message
        m = mobile.get("metrics", {})
        def _mv(key): return m.get(key, {}).get("display", "N/A")
        def _me(key): return _metric_emoji(m.get(key, {}).get("score", 0))

        top_insights = mobile.get("insights", [])[:3]
        insight_lines = "\n".join(
            f"    • {i['title']}" + (f" ({i['savings']})" if i["savings"] else "")
            for i in top_insights
        )

        slack_lines.append(
            f"• *{client_name}*\n"
            f"  Performance: Mobile {_score_emoji(mobile['score'])} {mobile['score']}  |  Desktop {_score_emoji(desktop['score'])} {desktop['score']}\n"
            f"  FCP {_mv('fcp')} {_me('fcp')}  LCP {_mv('lcp')} {_me('lcp')}  TBT {_mv('tbt')} {_me('tbt')}  SI {_mv('si')} {_me('si')}  CLS {_mv('cls')} {_me('cls')}\n"
            f"  Top issues:\n{insight_lines}"
        )

        if mobile["score"] < 50 or desktop["score"] < 50:
            all_ok = False

    # Post to Slack
    slack_token = settings.slack_bot_token or ""
    slack_channel = "#agency-pipeline"

    if slack_token:
        summary = "\n\n".join(slack_lines)
        summary += "\n\n" + ("✅ All sites looking healthy." if all_ok else "⚠ One or more sites need attention.")

        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {slack_token}"},
                json={"channel": slack_channel, "text": summary},
            )
        print(f"\n✓ Slack summary posted to {slack_channel}")
    else:
        print("\n(SLACK_BOT_TOKEN not set — skipping Slack notification)")

    print(f"\n{'='*60}")
    print(f"Done — {len(targets)} client(s) reported.")


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="Website care plan monthly report")
    parser.add_argument("--client", default="", help="Run for a specific client key only")
    parser.add_argument(
        "--init", action="store_true",
        help="Create Care Plan DB for an existing client (requires --client)",
    )
    args = parser.parse_args()

    if args.init:
        if not args.client:
            print("--init requires --client <client_key>")
            sys.exit(1)
        await init_care_plan(args.client)
    else:
        await run_report(args.client or None)


if __name__ == "__main__":
    asyncio.run(main())
