#!/usr/bin/env python3
"""
rank_monitor.py — daily rank tracker for Keywords DB Target/Ranking/Won entries.

Makes the Target → Ranking → Won lifecycle automatic. For every keyword
the team has approved (Status ∈ {Target, Ranking, Won}), pulls the top-100
organic SERP via DataForSEO, finds the client's domain, updates the row's
Current Rank + Last Checked + Rank History fields, and transitions Status
based on the new position:

  rank ≤ 3           → Won       (top-3, defend mode)
  rank 4-100         → Ranking   (on the board)
  rank > 100 / none  → stays at last-known status (don't demote silently;
                                  team decides what to do with "dropped")

Flags:
  - Rank drop > 5 positions from previous day → ANOMALY (console + optional Slack)
  - New break into top 3                      → WIN     (console + optional Slack)
  - First time appearing in top 100           → FIRST APPEARANCE

Self-heals the Keywords DB with Current Rank / Last Checked / Rank History
fields on first run. Idempotent — running multiple times per day updates
the latest snapshot without duplicating history entries for the same day.

Usage:
    make rank-monitor CLIENT=cielo_treatment_center
    make rank-monitor CLIENT=cielo_treatment_center DRY=1
    python3 scripts/seo/rank_monitor.py --client cielo_treatment_center --location "Portland,Oregon,United States"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


DATAFORSEO_BASE = "https://api.dataforseo.com/v3"
LANGUAGE_CODE   = "en"
DEFAULT_DEPTH   = 100        # top-100 SERP per keyword
MAX_HISTORY     = 30         # keep last N snapshots in Rank History field

# Status transitions based on latest rank.
def rank_to_status(rank: int | None) -> str | None:
    """Return the Status we should move to for a given rank. None = don't change."""
    if rank is None or rank > 100 or rank <= 0:
        return None  # not in top 100 — don't demote silently; team decides
    if rank <= 3:
        return "Won"
    return "Ranking"


# ── DataForSEO ────────────────────────────────────────────────────────────────

def _dfs_headers() -> dict:
    if not settings.dataforseo_login or not settings.dataforseo_password:
        raise RuntimeError("DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD required in .env")
    tok = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


async def fetch_top_100(keyword: str, location_code: int) -> list[dict]:
    """Top-100 organic results for the keyword at the target location."""
    headers = _dfs_headers()
    payload = [{
        "keyword":       keyword,
        "location_code": location_code,
        "language_code": LANGUAGE_CODE,
        "device":        "desktop",
        "depth":         DEFAULT_DEPTH,
    }]
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/serp/google/organic/live/regular",
            headers=headers, json=payload,
        )
    if resp.status_code != 200:
        print(f"  ⚠ SERP {resp.status_code} for '{keyword}': {resp.text[:200]}")
        return []
    out: list[dict] = []
    for task in resp.json().get("tasks", []) or []:
        if task.get("status_code") != 20000:
            continue
        for result in (task.get("result") or []):
            for item in (result.get("items") or []):
                if item.get("type") != "organic":
                    continue
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                domain = urlparse(url).netloc.lower().replace("www.", "")
                out.append({
                    "rank":   item.get("rank_group") or item.get("rank_absolute") or 0,
                    "domain": domain,
                    "url":    url,
                    "title":  item.get("title") or "",
                })
    return out


def find_client_rank(client_domain: str, serp_results: list[dict]) -> tuple[int | None, str | None]:
    """Find the best (lowest) rank of the client's domain in the SERP. Returns (rank, url)."""
    if not client_domain:
        return (None, None)
    cd = client_domain.lower().replace("www.", "")
    best_rank = None
    best_url = None
    for r in serp_results:
        d = r["domain"]
        is_match = d == cd or d.endswith("." + cd) or cd.endswith("." + d)
        if is_match and (best_rank is None or r["rank"] < best_rank):
            best_rank = r["rank"]
            best_url = r["url"]
    return (best_rank, best_url)


# ── Notion helpers ────────────────────────────────────────────────────────────

def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:limit]}}]}


def _number(v) -> dict:
    if v is None:
        return {"number": None}
    try:
        return {"number": float(v)}
    except (ValueError, TypeError):
        return {"number": None}


def _select(name: str) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def _date(d: date) -> dict:
    return {"date": {"start": d.isoformat()}}


def _plain_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("title", []))


def _plain_rt(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("rich_text", []))


def _plain_select(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _plain_number(prop: dict | None):
    if not prop:
        return None
    return prop.get("number")


# ── Schema self-heal ──────────────────────────────────────────────────────────

async def ensure_rank_fields(notion: NotionClient, keywords_db_id: str, dry_run: bool) -> None:
    db = await notion._client.request(path=f"databases/{keywords_db_id}", method="GET")
    existing = db.get("properties", {})
    to_add: dict = {}
    if "Current Rank" not in existing:
        to_add["Current Rank"] = {"number": {}}
    if "Last Checked" not in existing:
        to_add["Last Checked"] = {"date": {}}
    if "Rank History" not in existing:
        to_add["Rank History"] = {"rich_text": {}}
    if not to_add:
        return
    if dry_run:
        print(f"  [DRY] would add rank fields: {list(to_add.keys())}")
        return
    await notion._client.request(
        path=f"databases/{keywords_db_id}", method="PATCH",
        body={"properties": to_add},
    )
    print(f"  ✓ added rank fields: {list(to_add.keys())}")


# ── History management ───────────────────────────────────────────────────────

def update_rank_history(existing_history: str, today: date, new_rank: int | None) -> str:
    """Prepend today's snapshot; remove any prior entry for today; cap to MAX_HISTORY."""
    lines = [ln.strip() for ln in (existing_history or "").splitlines() if ln.strip()]
    # Strip any existing entry for today
    today_prefix = today.isoformat()
    lines = [ln for ln in lines if not ln.startswith(today_prefix)]
    # Prepend today's entry
    rank_str = f"#{new_rank}" if new_rank is not None else "not in top 100"
    lines.insert(0, f"{today_prefix}: {rank_str}")
    return "\n".join(lines[:MAX_HISTORY])


# ── Core sweep ────────────────────────────────────────────────────────────────

def resolve_client_domain(cfg: dict) -> str:
    """Derive the canonical domain we're looking for in SERPs."""
    url = cfg.get("gsc_site_url") or cfg.get("website") or ""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return urlparse(url).netloc.lower().replace("www.", "")


def resolve_location_code(cfg: dict, override: str = "") -> tuple[int, str]:
    """
    For local mode clients, we could target metro-level SERPs, but keywords
    usually carry geo modifiers already ('Portland Oregon'). Default: USA.
    Override via --location-code / --location (both forms below).
    """
    if override and override.isdigit():
        return (int(override), f"override location code {override}")
    return (2840, "USA (default; keywords carry geo modifier)")


async def run(client_key: str, dry_run: bool, location_override: str) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found")
        sys.exit(1)

    client_domain = resolve_client_domain(cfg)
    if not client_domain:
        print(f"✗ No gsc_site_url / website configured for {client_key}. Can't find our own rank.")
        sys.exit(1)

    keywords_db_id = cfg.get("keywords_db_id")
    if not keywords_db_id:
        print(f"✗ No keywords_db_id for {client_key}")
        sys.exit(1)

    location_code, location_label = resolve_location_code(cfg, location_override)

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── Rank monitor {'[DRY RUN]' if dry_run else ''} ──")
    print(f"  Client: {cfg.get('name', client_key)} ({client_domain})")
    print(f"  Location: {location_label}")

    print("\n[1/3] Self-heal Keywords DB (rank fields)...")
    await ensure_rank_fields(notion, keywords_db_id, dry_run)

    print("\n[2/3] Loading active keywords (Status ∈ {Target, Ranking, Won})...")
    entries = await notion.query_database(
        database_id=keywords_db_id,
        filter_payload={
            "or": [
                {"property": "Status", "select": {"equals": "Target"}},
                {"property": "Status", "select": {"equals": "Ranking"}},
                {"property": "Status", "select": {"equals": "Won"}},
            ]
        },
    )
    print(f"  → {len(entries)} keyword(s) to monitor")

    if not entries:
        print("No active keywords. Approve some Proposed rows first (flip Status to Target).")
        return

    print(f"\n[3/3] Polling SERPs + updating...")
    today = date.today()

    summary = {
        "won":             0,
        "ranking":         0,
        "not_ranking":     0,
        "unchanged":       0,
        "status_changes":  0,
        "win_flags":       [],
        "anomaly_flags":   [],
        "first_flags":     [],
    }

    for i, entry in enumerate(entries, 1):
        props = entry["properties"]
        kw = _plain_title(props.get("Keyword", {}))
        old_status = _plain_select(props.get("Status"))
        old_rank   = _plain_number(props.get("Current Rank"))
        old_history = _plain_rt(props.get("Rank History"))

        serp = await fetch_top_100(kw, location_code)
        new_rank, _found_url = find_client_rank(client_domain, serp)

        # Status transition (don't demote silently when missing from top 100)
        target_status = rank_to_status(new_rank)
        new_status = target_status if target_status else old_status

        # Compose updates
        updates: dict = {
            "Last Checked": _date(today),
            "Rank History": _rt(update_rank_history(old_history, today, new_rank)),
        }
        if new_rank is not None:
            updates["Current Rank"] = _number(new_rank)
        else:
            # Explicitly null out so it's visually clear we're not ranking
            updates["Current Rank"] = _number(None)

        if new_status and new_status != old_status:
            updates["Status"] = _select(new_status)
            summary["status_changes"] += 1

        # Flag detection
        change_tag = ""
        if new_rank is not None:
            if old_rank is None:
                summary["first_flags"].append((kw, new_rank))
                change_tag = f"✨ FIRST appearance at #{new_rank}"
            elif new_rank <= 3 and (old_rank > 3 or old_status != "Won"):
                summary["win_flags"].append((kw, old_rank, new_rank))
                change_tag = f"🏆 WIN (#{int(old_rank)} → #{new_rank})"
            elif new_rank - old_rank > 5:
                summary["anomaly_flags"].append((kw, old_rank, new_rank, int(new_rank - old_rank)))
                change_tag = f"⚠ DROPPED {int(new_rank - old_rank)} positions (#{int(old_rank)} → #{new_rank})"
            elif old_rank - new_rank > 5:
                change_tag = f"📈 gained {int(old_rank - new_rank)} positions (#{int(old_rank)} → #{new_rank})"
            else:
                change_tag = ""

        # Bucket for summary
        if new_rank is None:
            summary["not_ranking"] += 1
        elif new_rank <= 3:
            summary["won"] += 1
        else:
            summary["ranking"] += 1
        if new_status == old_status:
            summary["unchanged"] += 1

        rank_str = f"#{new_rank}" if new_rank is not None else "—"
        prev_str = f"#{int(old_rank)}" if old_rank else "—"
        print(f"  [{i}/{len(entries)}] {rank_str:>5} (prev {prev_str:>5}) {old_status:>8} → {new_status:<8}  {kw[:50]}  {change_tag}")

        if not dry_run:
            await notion.update_database_entry(page_id=entry["id"], properties=updates)

    # ── Summary + flags ──────────────────────────────────────────────────────
    print(f"\n── Summary ──")
    print(f"  Won (top 3):      {summary['won']}")
    print(f"  Ranking (4-100):  {summary['ranking']}")
    print(f"  Not in top 100:   {summary['not_ranking']}")
    print(f"  Status changes:   {summary['status_changes']}")

    if summary["first_flags"]:
        print(f"\n✨ First appearances ({len(summary['first_flags'])}):")
        for kw, rank in summary["first_flags"]:
            print(f"    #{rank}  '{kw}'")

    if summary["win_flags"]:
        print(f"\n🏆 WINS ({len(summary['win_flags'])}):")
        for kw, old, new in summary["win_flags"]:
            old_s = f"#{int(old)}" if old else "unranked"
            print(f"    {old_s} → #{new}  '{kw}'")

    if summary["anomaly_flags"]:
        print(f"\n⚠ ANOMALIES ({len(summary['anomaly_flags'])}) — rank dropped >5 positions:")
        for kw, old, new, delta in summary["anomaly_flags"]:
            print(f"    #{int(old)} → #{new} (dropped {delta})  '{kw}'")

    if dry_run:
        print(f"\n[DRY] No Notion writes performed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily rank monitor — Target/Ranking/Won lifecycle")
    parser.add_argument("--client", required=True, help="client_key (e.g. cielo_treatment_center)")
    parser.add_argument("--location-code", default="", help="DataForSEO location code (default 2840 USA)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(
        client_key=args.client,
        dry_run=args.dry_run,
        location_override=args.location_code,
    ))
