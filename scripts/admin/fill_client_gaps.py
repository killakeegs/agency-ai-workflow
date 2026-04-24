#!/usr/bin/env python3
"""
fill_client_gaps.py — Interactive Q&A that walks through every open
readiness gap and writes your answers directly to Notion.

Usage:
    make fill-client-gaps CLIENT=lotus_recovery

At each prompt:
    Type an answer and hit Enter.
    Type `s` (or just Enter with no text) to skip this one for later.
    Type `q` to quit (progress so far is saved).

After you finish (or quit), the script re-runs the readiness check so
you can see the delta. Skipped gaps stay as gaps for the next pass.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from src.config import settings
from src.integrations.notion import NotionClient
from src.services.client_readiness import run_readiness_check
from src.services.gap_filler import load_gaps_and_questions, write_answer


def _prompt_user(question: str, hint: str, answer_format: str,
                 current_idx: int, total: int) -> str:
    """Show the question, collect a single-line answer. Returns '' for skip,
    '__QUIT__' for quit, else the answer text."""
    print()
    print("━" * 78)
    print(f"[{current_idx + 1}/{total}]  {question}")
    if hint:
        print(f"  💡 {hint}")
    if answer_format == "list":
        print("  (comma-separated list)")
    elif answer_format == "paragraph":
        print("  (sentence or short paragraph)")
    elif answer_format == "short":
        print("  (a few words)")
    print()
    try:
        raw = input("  Your answer (or s=skip, q=quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  (interrupted — progress saved)")
        return "__QUIT__"
    if not raw or raw.lower() in ("s", "skip"):
        return ""
    if raw.lower() in ("q", "quit", "exit"):
        return "__QUIT__"
    return raw


def _summarize_gaps(gaps: list[dict]) -> None:
    blocked = [g for g in gaps if g["severity"] == "blocked"]
    partial = [g for g in gaps if g["severity"] == "partial"]
    print(f"  🚨 {len(blocked)} blocked, ⚠  {len(partial)} partial "
          f"→ {len(gaps)} total to walk through")


async def main(client_key: str) -> int:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found in registry")
        return 1

    client_name = cfg.get("name", client_key)
    notion = NotionClient(settings.notion_api_key)

    print(f"\n── Fill Client Gaps: {client_name} ──")
    print("  Loading current readiness report + generating questions...\n")

    gaps, questions = await load_gaps_and_questions(notion, cfg, client_key)

    if not gaps:
        print("  ✓ No actionable gaps — this client is already in good shape.")
        print("    (If you think something's still missing, run")
        print("     `make check-client-readiness CLIENT=" + client_key + "` to see details.)\n")
        return 0

    _summarize_gaps(gaps)

    print("\n  You'll be asked one question at a time.")
    print("    • Type an answer and hit Enter to save it.")
    print("    • Type `s` or just Enter to skip (leave the gap for later).")
    print("    • Type `q` to quit — progress so far is saved.\n")
    try:
        go = input("  Ready? (Enter to start, q to quit) ").strip()
    except (EOFError, KeyboardInterrupt):
        return 0
    if go.lower() == "q":
        return 0

    answered = 0
    skipped  = 0
    errored  = 0

    for i, gap in enumerate(gaps):
        q = questions.get(i, {})
        question = q.get("question") or (
            f"What goes in {gap['source']} → {gap['field']}? ({gap['description']})"
        )
        hint = q.get("hint", "")
        afmt = q.get("answer_format", "sentence")

        answer = _prompt_user(question, hint, afmt, i, len(gaps))
        if answer == "__QUIT__":
            print("\n  Stopping. Progress saved. Re-run anytime to continue.")
            break
        if not answer:
            skipped += 1
            continue

        print(f"  → saving to {gap['source']} → {gap['field']}...")
        try:
            result = await write_answer(notion, cfg, gap, answer)
            if result.startswith("error") or "no " in result[:4]:
                print(f"    ⚠ {result}")
                errored += 1
            else:
                print(f"    ✓ {result}")
                answered += 1
        except Exception as e:
            print(f"    ⚠ error: {e}")
            errored += 1

    print("\n" + "━" * 78)
    print(f"\n  Session done — {answered} answered, {skipped} skipped, {errored} errors.\n")

    if answered > 0:
        print("  Re-checking readiness...\n")
        report = await run_readiness_check(notion, cfg, client_key)
        status = report["status"].upper()
        icon = {"READY": "✓", "PARTIAL": "⚠️", "BLOCKED": "🚨"}.get(status, "?")
        print(f"  New status: {icon}  {status}  "
              f"({len(report['blocked'])} blocked, {len(report['partial'])} partial)")

        if report["status"] == "blocked":
            print("\n  Still blocked — run `make fill-client-gaps CLIENT="
                  f"{client_key}` again to continue.")
        elif report["status"] == "partial":
            print("\n  Now partial — downstream agents will run with a warning.")
        else:
            print("\n  ✓ Fully ready! Downstream agents can run cleanly.")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Interactive gap-fill for a client's readiness data",
    )
    parser.add_argument("--client", required=True,
                        help="client_key from config/clients.py")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(client_key=args.client)))
