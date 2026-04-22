"""
gemini_meeting.py — parse Google Meet "Notes by Gemini" Drive docs.

Gemini (Google Meet's first-party note-taker) writes a Drive doc per meeting
with a predictable filename pattern and body structure. This module extracts
the fields we need (title, meeting datetime, attendee emails, readiness) so
the meeting processor can ingest Gemini docs the way it used to ingest
Notion AI transcripts.

Filename pattern:
    <Meeting Title> - YYYY/MM/DD HH:MM TZ - Notes by Gemini

Body markers used:
    "### Transcription ended after ..." — doc is complete
    "Invited <email> [Name](mailto:email) ..."   — attendee line near the top
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_FILENAME_RE = re.compile(
    r"^(?P<title>.+?)\s*-\s*"
    r"(?P<ymd>\d{4}/\d{2}/\d{2})\s+"
    r"(?P<hm>\d{1,2}:\d{2})\s+"
    r"(?P<tz>\w+)"
    r"\s*-\s*Notes by Gemini\s*$"
)

# Map Gemini's abbreviated tz names to IANA zones (expand as needed).
_TZ_MAP = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "UTC": "UTC",
    "GMT": "UTC",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Markdown (`### Transcription ended after ...`) OR plain-text export
# (just "Transcription ended after ..." — Drive's text/plain export strips markdown).
_TRANSCRIPTION_END_RE = re.compile(
    r"(?:^|\n)\s*(?:###\s*)?Transcription ended after",
    re.IGNORECASE,
)
_INVITED_LINE_RE = re.compile(r"Invited\s+(.+)", re.IGNORECASE)


@dataclass
class GeminiMeeting:
    doc_id: str
    title: str
    meeting_date: str          # YYYY-MM-DD (in the meeting's local tz)
    meeting_start_utc: datetime
    tz_name: str
    attendee_emails: list[str]
    body: str
    has_end_marker: bool       # True if "Transcription ended after" appears

    @property
    def is_complete(self) -> bool:
        """Kept for backward-compat; see is_ready_to_process for the richer signal."""
        return self.has_end_marker

    def is_ready_to_process(self, modified_time: datetime, stability_minutes: int = 15) -> bool:
        """A Gemini doc is ready when either:
          (a) it has the "Transcription ended after" end marker, OR
          (b) the doc hasn't been modified for >stability_minutes and has substantial content.

        (b) handles meetings where Gemini didn't write an end marker — short calls,
        abrupt disconnects, or docs where Gemini only produced summary/decisions
        without a transcript body. Without this fallback, those docs stay in
        "waiting" forever.
        """
        if self.has_end_marker:
            return True
        if len(self.body.strip()) < 1500:
            return False
        age_min = (datetime.now(timezone.utc) - modified_time).total_seconds() / 60
        return age_min >= stability_minutes


def parse_filename(name: str) -> tuple[str, datetime, str] | None:
    """Return (title, meeting_start_utc, tz_name) from a Gemini doc filename.

    Returns None if the filename doesn't match the Gemini pattern.
    """
    m = _FILENAME_RE.match(name.strip())
    if not m:
        return None

    title = m.group("title").strip()
    ymd = m.group("ymd")
    hm = m.group("hm")
    tz_name = m.group("tz").upper()
    tz_iana = _TZ_MAP.get(tz_name)
    if not tz_iana:
        return None

    try:
        y, mo, d = (int(x) for x in ymd.split("/"))
        h, mi = (int(x) for x in hm.split(":"))
        local = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz_iana))
        return title, local.astimezone(timezone.utc), tz_name
    except (ValueError, KeyError):
        return None


def extract_attendee_emails(body: str) -> list[str]:
    """Pull every email address from the 'Invited ...' attendee line.

    Gemini renders attendees in two forms — markdown-linked names
    (`[Name](mailto:x@y.com)`) and bare angled addresses (`<x@y.com>`).
    Both contain the email in plain text; a simple regex catches them all.
    """
    emails: list[str] = []
    for line in body.splitlines()[:40]:  # attendees live near the top
        m = _INVITED_LINE_RE.search(line)
        if not m:
            continue
        found = _EMAIL_RE.findall(m.group(1))
        for e in found:
            lo = e.lower()
            if lo not in emails:
                emails.append(lo)
        break  # first match only — avoid bleed into later sections
    return emails


def is_transcription_complete(body: str) -> bool:
    """Gemini appends '### Transcription ended after HH:MM:SS' when done."""
    return bool(_TRANSCRIPTION_END_RE.search(body))


def build_meeting(
    doc_id: str,
    file_name: str,
    body: str,
) -> GeminiMeeting | None:
    """Return a GeminiMeeting from a Drive file name + exported plain-text body."""
    parsed = parse_filename(file_name)
    if not parsed:
        return None
    title, start_utc, tz_name = parsed

    local = start_utc.astimezone(ZoneInfo(_TZ_MAP[tz_name]))
    meeting_date = local.date().isoformat()

    return GeminiMeeting(
        doc_id=doc_id,
        title=title,
        meeting_date=meeting_date,
        meeting_start_utc=start_utc,
        tz_name=tz_name,
        attendee_emails=extract_attendee_emails(body),
        body=body,
        has_end_marker=is_transcription_complete(body),
    )
