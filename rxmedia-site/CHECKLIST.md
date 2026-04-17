# Clone Progress — rxmedia.io

Update this file every iteration. Use `✅` for done, `🟡` for partial, `❌` for not started.

Work **top-to-bottom** — foundation first, then each section in order.

---

## 0. Foundation

- ✅ **Shared Layout** — `src/layouts/Layout.astro` with `<html>/<head>/<body>`, Mona Sans Google Fonts, `global.css` import, favicon, meta tags matching `<title>Best Marketing Remedy for Healthcare | RxMedia</title>`
- ✅ **Homepage shell** — `src/pages/index.astro` imports Layout, composes sections in order (Nav → Hero → TrustedBy → Stats → CaseStudies → WhoWeAre → Services → Industries → Testimonials → BlogPreview → Footer)
- 🟡 **Public assets copied** — rxmedia-logo.avif copied to `public/images/`. Remaining: client logos, service icons, hero imagery (pending per-section iterations)
- ✅ **Clean build passes** — `npm run build` exits 0

## 1. Global components

- ✅ **Nav** — Logo + Home, About, Industries (dropdown with 3 items), Services (dropdown with 6 items), Blog, FAQ, "Get in touch" CTA. Desktop hover dropdowns + mobile hamburger + click-to-open panel. All links use real /service/, /industries-we-serve/, etc paths.
- ✅ **Footer** — Logo, newsletter signup ("Stay Tuned for Our Latest Services and Offerings!" + email input), 4 columns (Navigation, Services, Industries We Serve, Contact with phone/email/address), Facebook + LinkedIn icons, bottom row with copyright + Terms/Privacy.

## 2. Sections (in reference order — top to bottom)

- ✅ **Hero** — Two-column layout on lg+: (1) eyebrow pill "Absolutely Free" + h1 "More Admissions. Less Marketing Stress." (with lime accent on second line) + description paragraph + Webflow Expert + Google Partner trust badges; (2) form card with "Absolutely Free!" eyebrow + "Get Your Personalized Digital Marketing Plan Today!" heading + Name/Email/Website fields + "Claim My Free Plan" submit + success message state. Form submission is client-side stub. Background uses radial gradient glow (lime + blue).
- ✅ **Trusted-By Ribbon** — Eyebrow h2 "Trusted by leading mental health and addiction treatment providers" (matches reference text-transform: Trusted with accent on first word). Horizontal infinite marquee of 10 client logos (Freedom, Another Chance, Cielo, Roots, Parkwood, Team Recovery, Resilient Return, Restore, Buena Vista, Lotus) duplicated for seamless loop. Edge fade gradients, hover-to-pause, respects `prefers-reduced-motion`. Logos copied to `public/images/ribbon/`.
- ✅ **Stats Overview** — 4-up row (2-col on mobile) with large numeric value + label. Labels verbatim from reference. ⚠ VALUES ARE PLACEHOLDERS (150+ / 98% / 12+ / 5x) — live site uses Webflow animated counters and final landed values couldn't be reliably scraped. Flag for team to confirm actual numbers before Phase 2 handoff.
- 🟡 **Case Studies** — eyebrow + heading. NEEDS: client cards with logo + name + one-line result + "free Consultation" CTA
- 🟡 **Who We Are** — eyebrow + Mission/Vision headings. NEEDS: body copy pulled from reference HTML
- 🟡 **Services** — eyebrow + heading + 6 cards with titles + "Our Newest Offering" badge on AI-Powered. NEEDS: service icons from reference (Recurso SVG files), descriptions, hover states
- 🟡 **Industries We Serve** — 3 named cards. NEEDS: imagery + one-line descriptions
- 🟡 **Testimonials** — eyebrow + heading + VIEW ALL link. NEEDS: quote cards (extract quotes from reference)
- 🟡 **Blog Preview** — 3 cards with real post titles. NEEDS: thumbnails, excerpts, real href URLs

## 3. Responsive + polish

- ❌ **Mobile layout (375)** — no horizontal scroll, nav collapses to hamburger, sections stack cleanly. Compare to `reference/screenshots/index-mobile.png`.
- ❌ **Tablet layout (768)** — compare to `reference/screenshots/index-tablet.png`.
- ❌ **Desktop layout (1440)** — compare to `reference/screenshots/index-desktop.png`.
- ❌ **No off-palette colors** — grep components for hex literals; every color must route through `@theme` in `global.css`.

## 4. Verification gates

- ❌ `npm run build` exits 0
- ❌ `node scripts/verify.mjs` reports zero errors across all 3 viewports
- ❌ `build/report.json` shows: `hasNav: true`, `hasFooter: true`, `h1Count >= 1`, `sectionCount >= 9`, `imgCount >= 10`, `hasHorizontalScroll: false`

---

## Notes / known issues

- **Iter 1:** Scaffolded Layout.astro, index.astro composition, and placeholder section components. All 9 sections render (verify.mjs reports 9 sections, h1=1, h2=4). Now iterating per-section with real content.
- **Observation:** rxmedia.io h1 is "More Admissions. Less Marketing Stress." (line-break between sentences). Confirmed in reference HTML.
- **Observation:** Stat values weren't scraped as individual data — need to re-examine reference HTML around `overview-section-2` class for actual numeric values (currently stubbed as 150+ / 98% / 12+ / 4.2x).
- **Iter 2:** Built real Nav (with Industries + Services dropdowns) and real Footer (newsletter, 4 columns, social icons, contact). verify.mjs: imgs 0→4, words 191→270, still 9 sections / 1 h1.
- **Observation:** rxmedia.io is actually multi-page (/about, /blog, /contact, /service/*, /industries-we-serve/*). Phase 1 clones only the homepage — internal nav links use real paths but will 404 locally. Phase 2 / future iterations can add those routes if needed.
- **Observation:** Hero CTA on live site uses blue accent (`--color-accent-alt` #2fa9e0) not lime green. "Get in touch" nav button + Subscribe footer button use lime. Worth checking hero button color when that section is built out.
- **Iter 3:** Built Hero with real copy, form (Name/Email/Website → "Claim My Free Plan" submit), trust badges, stub client-side form submit. Lottie animation in reference is NOT ported in Phase 1 — static image stand-in. verify.mjs: h2 5→6, imgs 4→7, words 270→306.
- **Observation:** The description paragraph under H1 isn't visible in the scraped reference screenshot on mobile (might be visually hidden or rendered as marquee), but text is present in HTML. Leaving it visible — matches HTML structure, may need responsive tweak later.
- **Iter 4:** Built Trusted-By ribbon with 10 client logos + CSS marquee animation. Animation matches reference (Webflow transform animation on `.logo-grid`) — this is clone-faithful, not a new motion addition. verify.mjs: imgs 7→27 (10 logos × 2 for seamless loop + existing), h2 6→7, words unchanged.
- **Observation:** Reference ribbon uses text-transform quirk where "tRUSTED" has lowercase 't' — implemented as a plain `<span>` with accent color on "Trusted" word. Not reproducing the lowercase quirk since it's likely a Webflow CSS transform artifact.
- **Iter 5:** Built Stats section with 4 cards (Successful Campaigns / Client Satisfaction / Years / Avg ROI). Numeric values are best-estimate placeholders — live site uses Webflow `.overview-counter-title` animated columns whose final position couldn't be reliably extracted from the static HTML or Playwright (intersection observer trigger + many animated digits spread across multiple h4 elements). Flagged in CHECKLIST for team confirmation. No animated counter added in Phase 1 (would be Phase 2 motion); values are static.
- **Scope check:** rxmedia.io's `overview-section-2` actually contains BOTH the trusted-by ribbon AND the stats. The clone splits them into two distinct Astro components (TrustedBy + Stats) for cleaner structure. Visually still rendered as one continuous dark band.
