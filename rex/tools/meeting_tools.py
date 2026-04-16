"""
Meeting processing tools for Rex.

Handles:
  - Detecting and reading Notion AI meeting transcripts
  - Parsing transcripts into the 12-section Client Log entry
  - Creating ClickUp tasks from action items
  - Drafting follow-up emails for Slack approval

This is the core of Rex-as-operations-director.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime

import anthropic
import httpx


MEETING_TOOL_NAMES = {
    "process_meeting",
    "list_unprocessed_meetings",
}

# Keegan's ClickUp user ID — default assignee for unclear tasks
KEEGAN_CLICKUP_ID = 3852174
# Henna's ClickUp user ID — default assignee for action items
HENNA_CLICKUP_ID = 5847731

# ── Claude prompt for meeting parsing ─────────────────────────────────────────

MEETING_PARSE_SYSTEM = """\
You are Rex, the operations director AI for RxMedia, a digital marketing agency
specializing in healthcare websites. You parse meeting transcripts into structured
internal meeting notes.

You are thorough, specific, and candid. You capture what was actually said, not
a sanitized version. You flag risks honestly. You identify upsell opportunities
when the client mentions needs they aren't currently paying for.

Return ONLY valid JSON — no markdown, no preamble, no explanation.
"""

MEETING_PARSE_PROMPT = """\
Parse this meeting transcript into structured meeting notes.

Client: {client_name}
Meeting date: {meeting_date}
Active services: {active_services}

TRANSCRIPT:
{transcript}

Return this exact JSON structure:
{{
  "meeting_type": "Kickoff" | "Pipeline Review" | "Content Review" | "Design Review" | "Check-in" | "Ad Hoc",
  "duration_minutes": <estimated from transcript length>,
  "attendees": ["Name 1", "Name 2"],
  "summary": "3-5 sentences summarizing what this meeting was about",
  "key_decisions": [
    {{"decision": "what was decided", "reasoning": "why this direction was chosen"}}
  ],
  "approvals_given": [
    {{"what": "what was approved (e.g. sitemap, content, design)", "by_whom": "who gave the approval"}}
  ],
  "action_items": [
    {{
      "task": "specific task description",
      "owner": "person's name",
      "due_date": "YYYY-MM-DD or 'unspecified'",
      "priority": "high" | "medium" | "low",
      "pipeline_stage": "related stage or 'general'"
    }}
  ],
  "revision_feedback": [
    {{"page_or_area": "what it applies to", "feedback": "exact feedback given"}}
  ],
  "client_requests": [
    {{"request": "what they asked for", "in_scope": true | false}}
  ],
  "brand_updates": [
    {{"field": "what should be updated", "value": "new preference or direction"}}
  ],
  "client_quotes": [
    {{"quote": "verbatim or near-verbatim quote", "context": "what they were talking about"}}
  ],
  "value_add_opportunities": [
    {{"opportunity": "what Rex noticed", "current_service": "what they're paying for (or nothing)", "potential_service": "what they might need"}}
  ],
  "risk_flags": [
    {{"flag": "what Rex noticed", "severity": "low" | "medium" | "high"}}
  ],
  "client_sentiment": "one sentence honest read on how the client feels",
  "next_steps": "what happens next, including next meeting if scheduled"
}}

Be specific — use names, dates, and details from the transcript. Don't invent
information that isn't in the transcript. If a section has nothing, use an empty array [].
"""

# ── Follow-up email prompt ────────────────────────────────────────────────────

EMAIL_SYSTEM = """\
You write follow-up emails for Keegan Warrington, owner of RxMedia (digital marketing agency).
Match his voice exactly: professional but casual, excited, happy. He genuinely likes his clients.

FORMATTING RULES (non-negotiable):
- Opening: 1-2 casual, warm sentences. Can include a compliment or personal touch.
- Transition: "Here is a recap of our next steps:" (one line, then straight to items)
- Section headers: **Bold** (e.g., **RxMedia Action Items:**)
- Each item: **Bold Label:** followed by description. Bullet points.
- Three sections in order: RxMedia Action Items, Action Items for You, Future Roadmap (if applicable)
- Close: "Please let me know if you have any questions."
- Sign-off: "Best regards,\\n\\nKeegan\\nRxMedia"

WRITING RULES:
- No em dashes
- No AI filler: "It was great to discuss," "I wanted to follow up on," "Looking forward to"
- Use "We will" not "We'll" in the body (slightly more formal)
- Use "Please" for client requests (polite but direct)
- No filler paragraphs between sections
- No horizontal rules or dashes as dividers
- If the meeting was today, say "today" or "earlier today" — never "yesterday"
- If nothing is needed from the client, skip that section entirely
- Keep it tight — one sentence per item, no padding
"""

EMAIL_PROMPT = """\
Write a follow-up email for this meeting.

Client: {client_name}
Meeting date: {meeting_date}
Summary: {summary}
Key decisions: {decisions}
Action items (agency): {agency_items}
Action items (client): {client_items}
Next steps: {next_steps}

Return ONLY this JSON:
{{
  "subject": "short, clear subject line — e.g. 'PDX Plumber - Q2 Recap & Next Steps'",
  "body": "full email body (plain text with **bold** markers for headers and labels)"
}}
"""


# ── Notion helpers ────────────────────────────────────────────────────────────

def _rt(text: str) -> dict:
    """Build a rich_text property value."""
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": text[:200]}}]}


def _blocks_to_text(blocks: list[dict]) -> str:
    """Extract plain text from Notion block children."""
    parts = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("text", {}).get("content", "") for r in rich)
        if text:
            parts.append(text)
    return "\n".join(parts)


# ── Core meeting processing ──────────────────────────────────────────────────

async def _find_transcript_page(notion_client, client_key: str, cfg: dict, meeting_ref: str) -> dict | None:
    """
    Find a Notion AI transcript page. Searches the client's Meeting Notes DB
    (legacy) or recent pages under the client root.

    meeting_ref can be:
      - "today" or "this morning" → find most recent transcript from today
      - "yesterday" → find most recent from yesterday
      - A Notion page ID → load directly
    """
    # If it looks like a Notion page ID, load directly
    if len(meeting_ref) > 30 and "-" in meeting_ref:
        try:
            page = await notion_client.request(path=f"pages/{meeting_ref}", method="GET")
            return page
        except Exception:
            return None

    # Search the meeting notes DB for recent entries
    db_id = cfg.get("meeting_notes_db_id") or cfg.get("client_log_db_id", "")
    if not db_id:
        return None

    rows = await notion_client.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body={
            "page_size": 5,
            "sorts": [{"property": "Date" if cfg.get("client_log_db_id") else "Meeting Date",
                       "direction": "descending"}],
        },
    )

    results = rows.get("results", [])
    if not results:
        return None

    # For "today"/"yesterday", filter by date
    target_date = None
    ref_lower = meeting_ref.lower().strip()
    if ref_lower in ("today", "this morning", "this afternoon"):
        target_date = date.today().isoformat()
    elif ref_lower == "yesterday":
        from datetime import timedelta
        target_date = (date.today() - timedelta(days=1)).isoformat()

    if target_date:
        for row in results:
            props = row.get("properties", {})
            date_prop = props.get("Date", props.get("Meeting Date", {}))
            d = date_prop.get("date", {})
            if d and d.get("start", "").startswith(target_date):
                return row
        # Fall back to most recent
        return results[0] if results else None

    # Default: return most recent
    return results[0] if results else None


async def _get_transcript_text(notion_client, page_id: str) -> str:
    """Read the full text content of a Notion page (transcript)."""
    all_text = []
    has_more = True
    start_cursor = None

    while has_more:
        params = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        resp = await notion_client.request(
            path=f"blocks/{page_id}/children",
            method="GET",
            **{k: v for k, v in [("query", params)] if False},  # notion client doesn't support query params easily
        )
        # Use direct request with query string
        url_params = f"?page_size=100"
        if start_cursor:
            url_params += f"&start_cursor={start_cursor}"
        resp = await notion_client.request(
            path=f"blocks/{page_id}/children{url_params}",
            method="GET",
        )

        blocks = resp.get("results", [])
        all_text.append(_blocks_to_text(blocks))

        has_more = resp.get("has_more", False)
        start_cursor = resp.get("next_cursor")

    return "\n".join(all_text)


async def _parse_transcript(
    transcript: str,
    client_name: str,
    active_services: list[str],
    meeting_date: str,
) -> dict:
    """Use Claude to parse a transcript into structured meeting notes."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()

    client = anthropic.Anthropic(api_key=api_key)

    prompt = MEETING_PARSE_PROMPT.format(
        client_name=client_name,
        meeting_date=meeting_date,
        active_services=", ".join(active_services) or "unknown",
        transcript=transcript[:15000],  # cap at ~15k chars
    )

    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=MEETING_PARSE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Extract JSON
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        raise ValueError(f"Could not parse meeting notes JSON from Claude response")

    return json.loads(match.group(0))


async def _write_client_log(
    notion_client,
    client_log_db_id: str,
    client_name: str,
    parsed: dict,
    meeting_date: str,
    source_page_id: str,
) -> str:
    """Write parsed meeting notes to the Client Log DB. Returns the new entry ID."""

    def _list_to_text(items: list, key: str = "") -> str:
        if not items:
            return ""
        lines = []
        for item in items:
            if isinstance(item, dict):
                if key:
                    lines.append(str(item.get(key, str(item))))
                else:
                    lines.append(json.dumps(item, ensure_ascii=False))
            else:
                lines.append(str(item))
        return "\n".join(lines)[:2000]

    def _action_items_text(items: list) -> str:
        if not items:
            return ""
        lines = []
        for ai in items:
            owner = ai.get("owner", "unassigned")
            task  = ai.get("task", "")
            due   = ai.get("due_date", "unspecified")
            prio  = ai.get("priority", "medium")
            lines.append(f"[{prio.upper()}] {owner} | {task} | Due: {due}")
        return "\n".join(lines)[:2000]

    def _decisions_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(
            f"- {d.get('decision', '')} (Why: {d.get('reasoning', '')})"
            for d in items
        )[:2000]

    def _approvals_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(f"- {a.get('what', '')} (by {a.get('by_whom', '')})" for a in items)[:2000]

    def _feedback_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(f"- {f.get('page_or_area', '')}: {f.get('feedback', '')}" for f in items)[:2000]

    def _requests_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(
            f"- {'[IN SCOPE]' if r.get('in_scope') else '[OUT OF SCOPE]'} {r.get('request', '')}"
            for r in items
        )[:2000]

    def _brand_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(f"- {b.get('field', '')}: {b.get('value', '')}" for b in items)[:2000]

    def _quotes_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(f'"{q.get("quote", "")}" — re: {q.get("context", "")}' for q in items)[:2000]

    def _value_add_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(
            f"- {v.get('opportunity', '')} (current: {v.get('current_service', 'none')}, potential: {v.get('potential_service', '')})"
            for v in items
        )[:2000]

    def _risk_text(items: list) -> str:
        if not items:
            return ""
        return "\n".join(f"- [{r.get('severity', 'low').upper()}] {r.get('flag', '')}" for r in items)[:2000]

    properties = {
        "Title":                  _title(f"{client_name} — {parsed.get('meeting_type', 'Meeting')} — {meeting_date}"),
        "Date":                   {"date": {"start": meeting_date}},
        "Type":                   {"select": {"name": "Meeting"}},
        "Meeting Type":           {"select": {"name": parsed.get("meeting_type", "Check-in")}},
        "Attendees":              _rt(", ".join(parsed.get("attendees", []))),
        "Duration (min)":         {"number": parsed.get("duration_minutes", 0)},
        "Summary":                _rt(parsed.get("summary", "")),
        "Key Decisions":          _rt(_decisions_text(parsed.get("key_decisions", []))),
        "Approvals Given":        _rt(_approvals_text(parsed.get("approvals_given", []))),
        "Action Items":           _rt(_action_items_text(parsed.get("action_items", []))),
        "Revision Feedback":      _rt(_feedback_text(parsed.get("revision_feedback", []))),
        "Client Requests":        _rt(_requests_text(parsed.get("client_requests", []))),
        "Brand Updates":          _rt(_brand_text(parsed.get("brand_updates", []))),
        "Client Quotes":          _rt(_quotes_text(parsed.get("client_quotes", []))),
        "Value Add Opportunities": _rt(_value_add_text(parsed.get("value_add_opportunities", []))),
        "Risk Flags":             _rt(_risk_text(parsed.get("risk_flags", []))),
        "Client Sentiment":       _rt(parsed.get("client_sentiment", "")),
        "Next Steps":             _rt(parsed.get("next_steps", "")),
        "Processed":              {"checkbox": True},
        "Tasks Created":          {"number": len(parsed.get("action_items", []))},
        "Source":                 _rt(f"notion://page/{source_page_id}" if source_page_id else ""),
    }

    result = await notion_client.request(
        path="pages",
        method="POST",
        body={
            "parent": {"database_id": client_log_db_id},
            "properties": properties,
        },
    )
    return result["id"]


async def _create_clickup_tasks(
    action_items: list[dict],
    client_name: str,
    cfg: dict,
) -> list[dict]:
    """Create ClickUp tasks from action items. Returns list of created task info."""
    clickup_key  = os.environ.get("CLICKUP_API_KEY", "").strip()
    list_id      = cfg.get("clickup_review_list_id", "")

    if not clickup_key or not list_id:
        return []

    # Map known names to ClickUp IDs
    name_to_id = {
        "keegan":  3852174,
        "justin":  54703919,
        "andrea":  78185522,
        "karla":   107627361,
        "henna":   5847731,
        "mari":    95680055,
    }

    created = []
    async with httpx.AsyncClient() as http:
        for item in action_items:
            owner_name = item.get("owner", "").lower().split()[0] if item.get("owner") else ""
            assignee_id = name_to_id.get(owner_name, HENNA_CLICKUP_ID)

            task_name = f"{client_name} — {item.get('task', 'Task')}"
            desc = (
                f"From meeting notes.\n\n"
                f"Owner: {item.get('owner', 'unassigned')}\n"
                f"Priority: {item.get('priority', 'medium')}\n"
                f"Pipeline stage: {item.get('pipeline_stage', 'general')}"
            )

            # Parse due date
            due_ts = None
            due_str = item.get("due_date", "")
            if due_str and due_str != "unspecified":
                try:
                    due_dt = datetime.strptime(due_str, "%Y-%m-%d")
                    due_ts = int(due_dt.timestamp() * 1000)
                except ValueError:
                    pass

            body: dict = {
                "name":        task_name,
                "description": desc,
                "assignees":   [assignee_id],
            }
            if due_ts:
                body["due_date"] = due_ts

            r = await http.post(
                f"https://api.clickup.com/api/v2/list/{list_id}/task",
                headers={"Authorization": clickup_key, "Content-Type": "application/json"},
                json=body,
                timeout=15,
            )

            if r.status_code in (200, 201):
                task_data = r.json()
                created.append({
                    "task": item.get("task", ""),
                    "owner": item.get("owner", ""),
                    "url": task_data.get("url", ""),
                })

    return created


async def _draft_follow_up_email(
    parsed: dict,
    client_name: str,
    meeting_date: str,
) -> dict:
    """Draft a follow-up email from parsed meeting notes."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()

    # Split action items by owner
    agency_items = []
    client_items = []
    for ai in parsed.get("action_items", []):
        owner = ai.get("owner", "").lower()
        item_str = f"- {ai.get('task', '')} (due: {ai.get('due_date', 'TBD')})"
        # If owner matches a known team member, it's agency
        if any(name in owner for name in ["keegan", "justin", "andrea", "karla", "henna", "mari", "rex"]):
            agency_items.append(item_str)
        else:
            client_items.append(item_str)

    decisions = "\n".join(
        f"- {d.get('decision', '')}" for d in parsed.get("key_decisions", [])
    ) or "No major decisions made."

    prompt = EMAIL_PROMPT.format(
        client_name=client_name,
        meeting_date=meeting_date,
        summary=parsed.get("summary", ""),
        decisions=decisions,
        agency_items="\n".join(agency_items) or "None",
        client_items="\n".join(client_items) or "None needed",
        next_steps=parsed.get("next_steps", ""),
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        system=EMAIL_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return {"subject": f"{client_name} + RxMedia — Meeting Recap ({meeting_date})", "body": raw}


# ── Main tool dispatcher ──────────────────────────────────────────────────────

async def execute_meeting_tool(
    name: str,
    tool_input: dict,
    clients: dict,
    notion_client,
) -> str:
    """Dispatch a meeting tool call."""

    if name == "list_unprocessed_meetings":
        client_key = tool_input.get("client_key", "")
        cfg = clients.get(client_key)
        if not cfg:
            return f"Client '{client_key}' not found."

        db_id = cfg.get("client_log_db_id", "")
        if not db_id:
            # Fall back to legacy meeting notes DB
            db_id = cfg.get("meeting_notes_db_id", "")
        if not db_id:
            return f"No Client Log or Meeting Notes DB found for {client_key}."

        rows = await notion_client.request(
            path=f"databases/{db_id}/query",
            method="POST",
            body={
                "page_size": 10,
                "filter": {"property": "Processed", "checkbox": {"equals": False}},
                "sorts": [{"property": "Date", "direction": "descending"}],
            },
        )
        results = rows.get("results", [])
        if not results:
            return f"No unprocessed meetings found for {cfg.get('name', client_key)}."

        lines = [f"Unprocessed meetings for {cfg.get('name', client_key)}:"]
        for row in results:
            props = row.get("properties", {})
            title_prop = props.get("Title", {})
            title = "".join(t.get("text", {}).get("content", "") for t in title_prop.get("title", []))
            lines.append(f"  - {title or row['id']}")
        return "\n".join(lines)

    elif name == "process_meeting":
        client_key   = tool_input.get("client_key", "")
        meeting_ref  = tool_input.get("meeting_ref", "today")
        cfg = clients.get(client_key)
        if not cfg:
            return f"Client '{client_key}' not found."

        client_name = cfg.get("name", client_key)
        client_log_db_id = cfg.get("client_log_db_id", "")
        if not client_log_db_id:
            return f"No Client Log DB found for {client_key}. Run setup first."

        # 1. Find the transcript
        page = await _find_transcript_page(notion_client, client_key, cfg, meeting_ref)
        if not page:
            return f"Could not find a meeting transcript for '{meeting_ref}'. Try providing the Notion page ID directly."

        page_id = page["id"]

        # 2. Read the transcript text
        transcript = await _get_transcript_text(notion_client, page_id)
        if not transcript or len(transcript.strip()) < 50:
            return f"Transcript page found but has very little text ({len(transcript)} chars). Is the transcript populated?"

        # 3. Determine active services
        services = cfg.get("services", {})
        active = [k for k, v in services.items() if v is True]

        # 4. Parse with Claude
        meeting_date = date.today().isoformat()
        parsed = await _parse_transcript(transcript, client_name, active, meeting_date)

        # 5. Write to Client Log
        log_entry_id = await _write_client_log(
            notion_client, client_log_db_id, client_name, parsed, meeting_date, page_id,
        )

        # 6. Create ClickUp tasks
        action_items = parsed.get("action_items", [])
        created_tasks = await _create_clickup_tasks(action_items, client_name, cfg)

        # 7. Draft follow-up email
        email = await _draft_follow_up_email(parsed, client_name, meeting_date)

        # 8. Build summary for Slack
        summary_parts = [
            f"*{client_name} — Meeting Processed*",
            f"{parsed.get('meeting_type', 'Meeting')}, ~{parsed.get('duration_minutes', '?')} min, {len(parsed.get('attendees', []))} attendees",
            "",
        ]

        if action_items:
            summary_parts.append(f"- {len(created_tasks)}/{len(action_items)} tasks created in ClickUp")
        if parsed.get("approvals_given"):
            approvals = ", ".join(a.get("what", "") for a in parsed["approvals_given"])
            summary_parts.append(f"- Approvals: {approvals}")
        if parsed.get("brand_updates"):
            summary_parts.append(f"- {len(parsed['brand_updates'])} brand update(s) detected")
        if parsed.get("risk_flags"):
            flags = ", ".join(r.get("flag", "")[:50] for r in parsed["risk_flags"])
            summary_parts.append(f"- Risk flags: {flags}")
        if parsed.get("value_add_opportunities"):
            summary_parts.append(f"- {len(parsed['value_add_opportunities'])} value-add opportunity(ies) spotted")

        summary_parts.append("")
        summary_parts.append(f"Follow-up email ready:")
        summary_parts.append(f"Subject: {email.get('subject', '')}")
        summary_parts.append(f"```{email.get('body', '')[:500]}```")
        summary_parts.append("")
        summary_parts.append("React with :thumbsup: to send, or :pencil: to edit.")

        # Store the email draft in the result so Rex can act on approval
        result = {
            "summary": "\n".join(summary_parts),
            "email_draft": email,
            "log_entry_id": log_entry_id,
            "tasks_created": len(created_tasks),
            "parsed": parsed,
        }

        return json.dumps(result)

    return f"Unknown meeting tool: {name}"
