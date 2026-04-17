# Ralph Loop — Clone rxmedia.io (Phase 1)

You are rebuilding [rxmedia.io](https://rxmedia.io) as a static Astro + Tailwind site. This is a greenfield clone: structural + typographic match at ~90% fidelity. NOT pixel-perfect.

## How this loop works

You will be re-prompted with this same file until you emit the completion tag. Your previous work persists in the filesystem — you are not starting fresh each iteration. Treat `CHECKLIST.md` as your working memory.

## Hard rules — violating any of these is failure

1. **Never emit `<promise>RXMEDIA CLONE COMPLETE</promise>` without first running `node scripts/verify.mjs` and confirming the report is clean.**
2. **Never add motion, animation, new pages, new sections, or design "improvements."** Phase 1 is clone-only. If the reference doesn't have it, you don't build it. (Phase 2 handles elevation — don't anticipate it.)
3. **Never rewrite the site's copy.** Pull text verbatim from `reference/html/index.html`.
4. **Never use colors outside `src/styles/global.css` `@theme`.** If you need a new color, add it to `@theme` with a name — don't inline hex values in components.
5. **Never scrape rxmedia.io again.** Reference material is already in `reference/`. Hitting the live site wastes time and risks drift.
6. **Every iteration must end with a `git commit`** so progress is rollback-able.

## Reference material (read-only)

| Path | What's in it |
|---|---|
| `reference/html/index.html` | Full HTML dump of rxmedia.io homepage (source of truth for copy, structure, section order) |
| `reference/screenshots/index-{mobile,tablet,desktop}.png` | Visual reference at 375 / 768 / 1440 viewports |
| `reference/tokens.json` | Extracted design tokens (colors, fonts, typography scale) — already applied to `src/styles/global.css` |
| `reference/assets/images/` | Downloaded imagery — use these directly, copy into `public/images/` as needed |
| `reference/manifest.json` | List of pages that were scraped (currently just `index` — rxmedia.io is a one-pager) |

## Your job this iteration

### Step 1 — Read `CHECKLIST.md`
Find the highest-priority ❌ item. Work on it (and only it) this iteration. If the checklist has no ❌ items, jump to Step 4.

### Step 2 — Do the work
Use `reference/html/index.html` as the structural source of truth and `reference/screenshots/index-desktop.png` as the visual target. Common patterns:

- **New section:** create a component in `src/components/sections/`, import it into `src/pages/index.astro`
- **Shared layout:** put the `<html>/<head>/<body>` shell in `src/layouts/Layout.astro` with Mona Sans font link + `global.css` import
- **Images:** copy the relevant asset from `reference/assets/images/` to `public/images/` and reference it as `/images/<filename>`
- **Nav + Footer:** extract into shared components

### Step 3 — Update the checklist
Flip the item to ✅ (or 🟡 for partial). Add a one-line note: "e.g. Hero section built — headline + CTA + bg image match desktop."

### Step 4 — Verify before promising
Before emitting the completion promise, run:

```bash
node scripts/verify.mjs
```

This boots `astro dev`, takes Playwright screenshots at 3 viewports, checks for page errors / horizontal scroll / broken HTTP, writes `build/report.json`. Only if:
- All `pages[].errors` arrays are empty
- `pages[].stats.hasNav === true` and `hasFooter === true`
- `sectionCount` is within 1 of the reference (currently ~10 sections on the homepage — adjust CHECKLIST.md if your section count differs but is structurally complete)
- Every CHECKLIST.md item is ✅

…then you may emit `<promise>RXMEDIA CLONE COMPLETE</promise>`.

Otherwise, do NOT emit the promise. Commit your work and exit; the loop will re-prompt you.

### Step 5 — Commit
```bash
git add -A
git commit -m "ralph(<slug>): <one-line description>"
```

## Conventions

- Astro files only (`.astro`). No React/Svelte unless strictly needed.
- Tailwind utility classes. No inline `<style>` blocks in components.
- Font: Mona Sans via Google Fonts — preload in `Layout.astro` head.
- Image optimization: use Astro's `<Image>` component where possible.
- Build cleanliness: `npm run build` must exit 0 before you promise completion.

## Scope reminder

rxmedia.io is a **single-page marketing site** — one route (`/`), many sections. Don't invent additional routes. If `reference/manifest.json` lists more pages later, `CHECKLIST.md` will reflect that.

---

**Begin iteration.** Read `CHECKLIST.md` first.
