# SEO Railway Crons — setup reference

Deployment guide for the two SEO-pipeline cron services that close
Release 1's "system is active, not passive" loop. Add these as separate
Railway cron services alongside the existing ones (`email_monitor`,
`meeting_processor`, `care_plan_report`).

Both scripts are **idempotent** — safe to run multiple times per tick;
same-day data is overwritten, not duplicated.

## 1. Rank monitor — weekly for local clients

**What it does per tick:** for every SEO-active client (filtered by SEO
Mode when configured), pulls top-100 SERP for every keyword at
`Status ∈ {Target, Ranking, Won}`, writes `Current Rank` + `Last Checked`
+ `Rank History` to the client's Keywords DB, auto-transitions Status
(rank ≤ 3 → Won, 4-100 → Ranking), and posts win/anomaly/first-appearance
flags to the client's `slack_channel`.

### Recommended schedule — by SEO Mode

Local healthcare SEO moves on a timescale of weeks. Daily rank checks on
local clients produce more noise than signal (SERPs bounce ±5-10
positions day-over-day from personalization / freshness / time-of-day
effects alone). Per-mode defaults:

| SEO Mode  | Cadence        | Cron expression       | Why |
|-----------|----------------|-----------------------|-----|
| Local     | Weekly, Monday | `0 13 * * 1` (6am PT) | Real signal moves weekly; matches content + review cadence |
| Hybrid    | Twice weekly   | `0 13 * * 1,4`        | Mix of local + national dynamics |
| National  | Daily          | `0 13 * * *`          | Faster competitive + algorithm cycles |

One cron service per cadence. Each one calls `--all-clients` with the
appropriate `--seo-mode` filter so the script only hits clients due
for that cadence.

### Railway setup

Three cron services (or fewer if you don't yet have national/hybrid
clients worth their own service):

**Service: `rank-monitor-local`**
- Schedule: `0 13 * * 1`   (Mondays 6am PT / 1pm UTC)
- Start command:
  ```
  python3 scripts/seo/rank_monitor.py --all-clients --seo-mode local
  ```

**Service: `rank-monitor-hybrid`**
- Schedule: `0 13 * * 1,4`  (Monday + Thursday 6am PT)
- Start command:
  ```
  python3 scripts/seo/rank_monitor.py --all-clients --seo-mode hybrid
  ```

**Service: `rank-monitor-national`** (add when first national client gets SEO)
- Schedule: `0 13 * * *`   (Daily 6am PT)
- Start command:
  ```
  python3 scripts/seo/rank_monitor.py --all-clients --seo-mode national
  ```

### Environment variables required

Same set the existing crons use (Railway shared variables fine):

- `NOTION_API_KEY`
- `ANTHROPIC_API_KEY` (not strictly needed for rank_monitor but loaded by `src.config`)
- `DATAFORSEO_LOGIN` + `DATAFORSEO_PASSWORD`
- `SLACK_BOT_TOKEN` (for win/anomaly Slack posts)

### First run

Kick off once manually via Railway "Run" button to seed rank history.
From then on, the cron runs on schedule. Slack will stay silent on runs
where nothing noteworthy happened — by design.

---

## 2. Style Reference sweep — daily

**What it does per tick:** reads every eligible client's Content DB
(website copy) and Blog Posts DB for entries finalized by the team
(`Status ∈ {Approved, Revision Requested, Published, Scheduled}` with
a non-empty `Feedback` field and `Style Logged` unchecked), writes each
as a Style Reference row (with `log_feedback`), and marks the source
`Style Logged = True`.

Zero team behavior change — the team already approves content in Notion
with feedback. The sweep just turns those approvals into the priming
corpus that future agent runs read from.

### Railway setup

**Service: `style-reference-sweep`**
- Schedule: `0 13 * * *`    (Daily 6am PT / 1pm UTC — co-runs with morning agent work)
- Start command:
  ```
  python3 scripts/seo/style_reference_sweep.py
  ```

### Environment variables required

- `NOTION_API_KEY`
- `ANTHROPIC_API_KEY` (loaded at import time)

---

## Verification after deployment

First Monday after the rank-monitor-local cron is live:
1. Check `#cielo-recovery` Slack channel for the Monday morning rank-monitor Slack post (win/anomaly summary if any, silence if no changes)
2. Open Cielo's Keywords DB → filter `Status = Ranking` → verify `Current Rank` + `Last Checked` have fresh values
3. Check `Rank History` on any one row — should show two dated entries (baseline + this week's)

If the Slack post doesn't appear and you expected one:
- Verify `SLACK_BOT_TOKEN` is set in Railway env
- Verify the client's `slack_channel` field is populated in `config/clients.json`
- Check Railway logs for the cron service — errors bubble up to stdout

## Adding a new client

When a new client gets SEO activated:
1. Run `make seo-activate CLIENT=new_client` — provisions SEO Metrics DB, confirms Google access
2. Ensure the client has `seo_mode` set in the Clients DB (Local/National/Hybrid)
3. Run `make install_seo_mode` — re-syncs `clients.json` with Notion
4. Next rank-monitor tick for their mode picks them up automatically
