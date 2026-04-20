"""
Notion tool handlers for Rex.

Each handler reads live client data from Notion and returns a formatted string
for Claude to include in its reply to Slack.
"""
from __future__ import annotations


# ── Property helpers ──────────────────────────────────────────────────────────

def _text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


NOTION_TOOL_NAMES = {
    "list_clients",
    "get_client_services",
    "get_pipeline_status",
    "get_sitemap",
    "get_page_content",
    "get_action_items",
    "get_care_plan_status",
    "get_keywords",
    "get_competitors",
    "get_gbp_posts",
    "list_flags",
    "resolve_flag",
}


import os
from datetime import date as _date

FLAGS_DB_ID = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()


async def execute_notion_tool(name: str, tool_input: dict, clients: dict, notion) -> str:
    """Dispatch a Notion tool call and return a formatted result string."""

    # Flag tools run against the workspace-level Flags DB and don't require a client_key.
    if name == "list_flags":
        if not FLAGS_DB_ID:
            return "NOTION_FLAGS_DB_ID not configured."
        client_filter = tool_input.get("client_key", "").strip()
        status_filter = tool_input.get("status", "Open").strip()
        type_filter   = tool_input.get("type", "").strip()

        filters: list[dict] = []
        if status_filter.lower() == "all":
            filters.append({"property": "Status", "select": {"does_not_equal": "Resolved"}})
            filters.append({"property": "Status", "select": {"does_not_equal": "Won't Fix"}})
        else:
            filters.append({"property": "Status", "select": {"equals": status_filter}})
        if client_filter:
            cfg = clients.get(client_filter, {})
            key_value = client_filter
            filters.append({"property": "Client Key", "rich_text": {"equals": key_value}})
        if type_filter:
            filters.append({"property": "Type", "select": {"equals": type_filter.upper()}})

        body = {
            "page_size": 50,
            "filter": {"and": filters} if len(filters) > 1 else filters[0],
            "sorts": [{"property": "Source Date", "direction": "descending"}],
        }
        try:
            r = await notion._client.request(
                path=f"databases/{FLAGS_DB_ID}/query", method="POST", body=body,
            )
        except Exception as e:
            return f"Failed to query Flags DB: {e}"

        rows = r.get("results", [])
        if not rows:
            return "No flags match those filters."

        lines = [f"Flags ({len(rows)} {status_filter}):"]
        for row in rows:
            props = row.get("properties", {})
            client_parts = props.get("Client", {}).get("rich_text", [])
            client_name = "".join(p.get("text", {}).get("content", "") for p in client_parts)
            desc_parts = props.get("Description", {}).get("rich_text", [])
            desc = "".join(p.get("text", {}).get("content", "") for p in desc_parts)
            type_sel = (props.get("Type", {}).get("select") or {}).get("name", "")
            status_sel = (props.get("Status", {}).get("select") or {}).get("name", "")
            date_obj = props.get("Source Date", {}).get("date")
            src_date = date_obj.get("start", "") if date_obj else ""
            flag_id = row.get("id", "")
            lines.append(f"• [{type_sel}] {client_name} ({src_date}) — {desc[:160]} | id: {flag_id}")
        return "\n".join(lines)

    if name == "resolve_flag":
        flag_id = tool_input.get("flag_id", "").strip()
        new_status = tool_input.get("status", "Resolved").strip()
        notes = tool_input.get("notes", "").strip()
        if not flag_id:
            return "flag_id is required. Use list_flags first to find the id."

        props: dict = {"Status": {"select": {"name": new_status}}}
        if new_status in ("Resolved", "Won't Fix"):
            props["Resolved Date"] = {"date": {"start": _date.today().isoformat()}}
        if notes:
            props["Notes"] = {"rich_text": [{"text": {"content": notes[:2000]}}]}

        try:
            await notion._client.request(
                path=f"pages/{flag_id}", method="PATCH",
                body={"properties": props},
            )
        except Exception as e:
            return f"Failed to update flag: {e}"
        return f"✓ Flag marked {new_status}" + (f" with note: {notes}" if notes else "")

    if name == "list_clients":
        lines = []
        for key, cfg in clients.items():
            services = cfg.get("services", {})
            if isinstance(services, dict):
                active = [k for k, v in services.items()
                          if v is True or (k == "blog_posts_per_month" and isinstance(v, int) and v > 0)]
                svc_str = ", ".join(active) if active else "no services configured"
            elif isinstance(services, list):
                svc_str = ", ".join(services) if services else "no services configured"
            else:
                svc_str = "no services configured"
            lines.append(f"{key} — {cfg.get('name', key)} | Services: {svc_str}")
        return "\n".join(lines)

    client_key = tool_input.get("client_key", "")
    if client_key not in clients:
        return f"Unknown client '{client_key}'. Available: {', '.join(clients)}"
    cfg = clients[client_key]

    if name == "get_client_services":
        services = cfg.get("services", {})
        name_str = cfg.get("name", client_key)
        if not services:
            return f"{name_str}: no services configured yet."
        if isinstance(services, list):
            return f"{name_str} services: {', '.join(services)}"
        lines = [f"Services — {name_str}:"]
        service_labels = {
            "website_build":        "Website Build",
            "care_plan":            "Care Plan ($199/mo — PageSpeed + ADA)",
            "seo":                  "SEO Retainer",
            "gbp_management":       "GBP Management (posts + review responses)",
            "blog":                 "Blog Pipeline",
            "blog_posts_per_month": "Blog posts/month",
            "social_media":         "Social Media (IG/FB + LinkedIn)",
            "newsletter":           "Monthly Newsletter",
            "paid_ads":             "Paid Ads Management",
        }
        for key, val in services.items():
            label = service_labels.get(key, key)
            if key == "blog_posts_per_month":
                if isinstance(val, int) and val > 0:
                    lines.append(f"  • {label}: {val}")
            elif val is True:
                lines.append(f"  • {label}: active")
            elif val is False:
                lines.append(f"  – {label}: not active")
        return "\n".join(lines)

    if name == "get_pipeline_status":
        entries = await notion.query_database(cfg["client_info_db_id"])
        if not entries:
            return "No client info found in Notion."
        pp = entries[0]["properties"]
        stage  = _select(pp.get("Pipeline Stage", {}))
        status = _select(pp.get("Stage Status", {}))
        notes  = _text(pp.get("Revision Notes", {}))
        return (
            f"Client: {cfg.get('name', client_key)}\n"
            f"Stage: {stage or 'Not set'}\n"
            f"Status: {status or 'Not set'}\n"
            f"Notes: {notes or 'None'}"
        )

    elif name == "get_sitemap":
        entries = await notion.query_database(
            cfg["sitemap_db_id"],
            sorts=[{"property": "Order", "direction": "ascending"}],
        )
        if not entries:
            return "No sitemap pages found."
        lines = [f"Sitemap: {cfg.get('name', client_key)} ({len(entries)} pages)\n"]
        for e in entries:
            pp = e["properties"]
            title = (
                _title(pp.get("Page Title", {}))
                or _title(pp.get("Name", {}))
                or "Untitled"
            )
            parent    = _text(pp.get("Parent Page", {}))
            slug      = _text(pp.get("Slug", {}))
            page_type = _select(pp.get("Page Type", {}))
            raw_sections = _text(pp.get("Key Sections", {}))
            sections = ", ".join(
                l.strip().lstrip("•–- ").strip()
                for l in raw_sections.split("\n") if l.strip()
            )
            parent_str = f" › {parent}" if parent else ""
            type_str   = f" [{page_type}]" if page_type else ""
            sec_str    = f"\n    {sections}" if sections else ""
            lines.append(f"• {title}{parent_str}{type_str} /{slug}{sec_str}")
        return "\n".join(lines)

    elif name == "get_page_content":
        page_filter = tool_input.get("page_name", "").strip().lower()
        entries = await notion.query_database(cfg["content_db_id"])
        results = []
        for e in entries:
            pp = e["properties"]
            page_name = (
                _title(pp.get("Page Name", {}))
                or _title(pp.get("Name", {}))
            )
            if page_filter and page_filter not in page_name.lower():
                continue
            title_tag = _text(pp.get("Title Tag", {}))
            meta      = _text(pp.get("Meta Description", {}))
            h1        = _text(pp.get("H1", {}))
            body      = _text(pp.get("Body Copy", {}))
            body_preview = body[:400] + ("..." if len(body) > 400 else "")
            results.append(
                f"PAGE: {page_name}\n"
                f"  Title tag: {title_tag}\n"
                f"  Meta: {meta}\n"
                f"  H1: {h1}\n"
                f"  Body: {body_preview}"
            )
        if not results:
            msg = "No content found"
            if page_filter:
                msg += f" for page matching '{page_filter}'"
            return msg + "."
        return "\n\n".join(results[:5])  # cap at 5 pages

    elif name == "get_action_items":
        assignee = tool_input.get("assignee", "").strip()
        filter_payload = None
        if assignee:
            filter_payload = {
                "property": "Assigned To",
                "select": {"equals": assignee},
            }
        entries = await notion.query_database(
            cfg["action_items_db_id"],
            filter_payload=filter_payload,
        )
        if not entries:
            return f"No action items found{' for ' + assignee if assignee else ''}."
        lines = []
        for e in entries:
            pp         = e["properties"]
            task       = _title(pp.get("Task", {})) or _title(pp.get("Name", {}))
            assigned   = _select(pp.get("Assigned To", {}))
            status_val = _select(pp.get("Status", {}))
            due_obj    = pp.get("Due Date", {}).get("date") or {}
            due        = due_obj.get("start", "no due date")
            lines.append(f"• {task} [{assigned}] — {status_val} (due: {due})")
        return "\n".join(lines)

    elif name == "get_care_plan_status":
        db_id = cfg.get("care_plan_db_id", "")
        if not db_id:
            return (
                f"No care plan configured for {client_key}. "
                f"Run: python scripts/care/care_plan_report.py --init --client {client_key}"
            )
        entries = await notion.query_database(
            db_id,
            sorts=[{"property": "Report Date", "direction": "descending"}],
        )
        if not entries:
            return f"No care plan reports found for {client_key}. Run: make care-plan CLIENT={client_key}"
        latest = entries[0]["properties"]

        def _num(p):   return p.get("number", "N/A")
        def _sel(p):   s = p.get("select"); return s.get("name", "") if s else ""
        def _date(p):  d = p.get("date"); return d.get("start", "N/A") if d else "N/A"
        def _t(p):     return "".join(x.get("text", {}).get("content", "") for x in p.get("title", []))
        def _tx(p):    return "".join(x.get("text", {}).get("content", "") for x in p.get("rich_text", []))

        name_val        = _t(latest.get("Name", {}))
        report_date     = _date(latest.get("Report Date", {}))
        mobile          = _num(latest.get("Mobile Score", {}))
        desktop         = _num(latest.get("Desktop Score", {}))
        mobile_rating   = _sel(latest.get("Mobile Rating", {}))
        desktop_rating  = _sel(latest.get("Desktop Rating", {}))
        top_opp         = _tx(latest.get("Top Opportunity", {}))
        ada             = latest.get("ADA Widget", {}).get("checkbox", None)
        privacy         = _sel(latest.get("Privacy Policy", {}))
        tos             = _sel(latest.get("Terms of Service", {}))
        hours           = _num(latest.get("Hours Used", {}))
        ada_str = "✓ Installed" if ada else ("✗ Not installed" if ada is False else "Not recorded")
        return (
            f"Care Plan: {name_val}\n"
            f"Report date: {report_date}\n"
            f"Mobile: {mobile}/100 ({mobile_rating})\n"
            f"Desktop: {desktop}/100 ({desktop_rating})\n"
            f"Top opportunity: {top_opp or 'N/A'}\n"
            f"ADA widget: {ada_str}\n"
            f"Privacy policy: {privacy or 'Not recorded'}\n"
            f"Terms of service: {tos or 'Not recorded'}\n"
            f"Hours used this month: {hours}"
        )

    elif name == "get_keywords":
        db_id = cfg.get("keywords_db_id", "")
        if not db_id:
            return f"No Keywords DB configured for {client_key}."
        priority_filter = tool_input.get("priority", "").strip()
        filter_payload  = None
        if priority_filter:
            filter_payload = {"property": "Priority", "select": {"equals": priority_filter}}
        entries = await notion.query_database(db_id, filter_payload=filter_payload)
        if not entries:
            return (
                f"No keywords found{' with priority ' + priority_filter if priority_filter else ''}. "
                f"Run: make keyword-research CLIENT={client_key}"
            )
        lines = [f"Keywords — {cfg.get('name', client_key)} ({len(entries)} results):\n"]
        for e in entries[:30]:
            pp       = e["properties"]
            kw       = _title(pp.get("Keyword", {}))
            cluster  = _text(pp.get("Cluster", {}))
            volume   = _text(pp.get("Monthly Search Volume", {}))
            priority = _select(pp.get("Priority", {}))
            intent   = _select(pp.get("Intent", {}))
            lines.append(f"• *{kw}* [{priority}] — {volume}/mo | {intent} | {cluster}")
        return "\n".join(lines)

    elif name == "get_competitors":
        db_id = cfg.get("competitors_db_id", "")
        if not db_id:
            return f"No Competitors DB configured for {client_key}."
        threat_filter  = tool_input.get("threat", "").strip()
        filter_payload = None
        if threat_filter:
            filter_payload = {"property": "Threat", "select": {"equals": threat_filter}}
        entries = await notion.query_database(db_id, filter_payload=filter_payload)
        if not entries:
            return (
                f"No competitors found{' with threat ' + threat_filter if threat_filter else ''}. "
                f"Run: make competitor-research CLIENT={client_key}"
            )
        lines = [f"Competitors — {cfg.get('name', client_key)} ({len(entries)} total):\n"]
        for e in entries:
            pp        = e["properties"]
            comp_name = _title(pp.get("Competitor Name", {}))
            threat    = _select(pp.get("Threat", {}))
            ctype     = _select(pp.get("Type", {}))
            reviews   = pp.get("Review Count", {}).get("number", "")
            rating    = pp.get("Review Rating", {}).get("number", "")
            authority = pp.get("Authority Score", {}).get("number", "")
            multi     = pp.get("Multi-Location", {}).get("checkbox", False)
            notes_val = _text(pp.get("Notes", {}))[:80]
            chain_str = " 🔗 Multi-location" if multi else ""
            lines.append(
                f"• *{comp_name}* [{threat} threat]{chain_str} — {ctype} | "
                f"⭐ {rating} ({reviews} reviews) | Auth: {authority}"
                + (f"\n  _{notes_val}_" if notes_val else "")
            )
        return "\n".join(lines)

    elif name == "get_gbp_posts":
        db_id = cfg.get("gbp_posts_db_id", "")
        if not db_id:
            return f"No GBP Posts DB configured for {client_key}. Run: make gbp-posts CLIENT={client_key}"
        status_filter  = tool_input.get("status", "").strip()
        filter_payload = None
        if status_filter:
            filter_payload = {"property": "Status", "select": {"equals": status_filter}}
        entries = await notion.query_database(db_id, filter_payload=filter_payload)
        if not entries:
            return f"No GBP posts found{' with status ' + status_filter if status_filter else ''}."
        lines = [f"GBP Posts — {cfg.get('name', client_key)} ({len(entries)} posts):\n"]
        for e in entries[:10]:
            pp          = e["properties"]
            post_title  = _title(pp.get("Post Title", {}))
            post_type   = _select(pp.get("Post Type", {}))
            status_val  = _select(pp.get("Status", {}))
            cta         = _select(pp.get("CTA Button", {}))
            month       = _text(pp.get("Month", {}))
            source_page = _text(pp.get("Source Page", {}))
            char_count  = pp.get("Char Count", {}).get("number", "")
            lines.append(
                f"• *{post_title}* [{status_val}] — {post_type} | {month} | "
                f"CTA: {cta} | {char_count} chars\n  Source: {source_page}"
            )
        return "\n".join(lines)

    return f"Unknown Notion tool: {name}"
