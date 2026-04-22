# Client Page Restructure — Notion AI Prompt

Paste the prompt below into Notion AI on each existing client's page. Notion AI can reorder blocks on the page (which the public API can't), so this is the one-time migration for the 25 clients that existed before the section structure was baked into onboarding.

**Run this *after* `make meeting-prep-setup` has provisioned the Meeting Prep DB for every client** — otherwise the reorder will run before the Meeting Prep DB exists and you'll need a second pass.

---

```
Restructure this page as follows. Do not delete, rename, or create any
databases — only move existing items and add new blocks.

Step 1 — At the very top of the page, add this callout (use a 📘 icon):
"New to this client? Start with Business Profile (what the business is
and does) and Brand Guidelines (how we talk as them). Client Log is the
running timeline of every meeting, email, and decision."

Step 2 — Below the callout, create a Heading 2 titled "Client Information
/ Rules". Move these under it, in this exact order (skip any that don't
exist):
1. Business Profile
2. Client Info
3. Client Log
4. Meeting Prep
5. Brand Guidelines
6. Blog Voice & Author Setup
7. Style Reference
8. Client Brief

Step 3 — Below that, create a Heading 2 titled "Website". Move these
under it, in order (skip any that don't exist):
1. Sitemap
2. Sitemap Review
3. Page Content
4. Images

Step 4 — Below that, create a Heading 2 titled "SEO". Move these under
it, in order (skip any that don't exist):
1. Keywords
2. Competitors
3. SEO Metrics

Step 5 — Below that, create a Heading 2 titled "Content". Move these
under it, in order (skip any that don't exist):
1. Blog Posts
2. Social Posts
3. GBP Posts

Step 6 — Below that, create a Heading 2 titled "Care Plan". Move the
Care Plan database under it.

Leave all 5 section headings visible even if the section is empty — this
shows which services aren't active yet.

Database names are prefixed with the client's first-word name (e.g.
"PDX — Client Info"). Match by the text after " — ".
```

---

## Tracking checklist (25 clients)

- [ ] Another Chance
- [ ] ARC Network
- [ ] Atlas Addiction Treatment Center
- [ ] Bloom Recovery
- [ ] Cielo Treatment Center
- [ ] Crown Behavioral Health
- [ ] DMAB Law
- [ ] Evolving Health
- [ ] Freedom Recovery
- [ ] Lotus Recovery
- [ ] Nonno Wellness Center
- [ ] NW Recovery Homes
- [ ] Parkwood Clinic
- [ ] PDX Plumber
- [ ] Resilient Solutions
- [ ] Rose City Detox
- [ ] RxMedia *(internal — skip)*
- [ ] SkyCloud Health
- [ ] Summit Therapy
- [ ] Team Recovery
- [ ] The Manor
- [ ] Tru Living Recovery
- [ ] Twin River Berries
- [ ] Wellness Works Management Partners
- [ ] WellWell

## Troubleshooting

- **Notion AI missed a DB or put it under the wrong section.** Drag it manually — faster than re-prompting.
- **A heading is duplicated.** Delete the duplicate. Safe — headings are plain blocks.
- **The 📘 callout uses a different emoji.** Edit it in place, no other fix needed.
- **Business Profile page didn't move.** It's a sub-page, not a DB — the prompt treats it the same way, but if Notion AI leaves it behind, drag it into "Client Information / Rules" as the first item.
