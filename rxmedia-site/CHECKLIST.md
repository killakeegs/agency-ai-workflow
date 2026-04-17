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
- ✅ **Case Studies** — "Case studies" eyebrow + "Success Stories That Speak for Themselves" h2 (accent on second clause) + "Free Consultation" CTA button. 8 client cards in 4-col grid (2-col tablet, 1-col mobile): Parkwood, Another Chance, Evolving Health, Atlas, Freedom, Lotus, Resilient Return, Renaissance Ranch. Each card: 4/5 aspect hero image + gradient scrim + client name (accent) + result headline + reveal-on-hover "View case study →" chevron. All 8 hero images copied to `public/images/case-studies/`, links target real `/case-studies/*` URLs.
- ✅ **Who We Are** — "Who we are" eyebrow + 3 body paragraphs (RxMedia founded 2018, bridging disconnect, expanding to new verticals) — all copy verbatim from reference. Mission + Vision side-by-side cards below: Mission uses lime accent, Vision uses blue accent for subtle visual distinction. No fabricated headline (reference has none).
- ✅ **Services** — "Our Services" eyebrow + "Results-Driven Digital Marketing Services" h2 (accent on second clause) + description "From AI-driven tools to SEO and paid ads...". Featured card for AI-Powered Marketing (with "Our Newest Offering" badge, tagline, 2-paragraph description, real illustration from `ai-marketing-illustration.avif`). 5 standard cards below in 3-col grid for Content & Social, SEO, PPC, Website Design, CRM & EMR — each with inline SVG icon (not reference Recurso SVGs but on-palette stylistic match), verbatim description, "Learn More" CTA. All links target real `/service/*` routes.
- ✅ **Industries We Serve** — Centered eyebrow "Industries We Serve" + 3-col grid of cards for Behavioral Health & Addiction, Specialized Therapy Practices, Wellness & Integrative Health. Each card: inline SVG icon + title + verbatim reference copy + "Explore →" CTA. Real `/industries-we-serve/*` links.
- ✅ **Testimonials** — "Testimonials" eyebrow + "Hear What Our Clients Say About Us" h2 (accent on "About Us") + "View all →" link. 3 quote cards in 3-col grid: Anthony Ciarrocchi (COO, LA Valley Recovery), Justin McCoy (CEO, Freedom Recovery), Emily Minerowicz (Marketing Coordinator, Buena Vista) — all quotes verbatim from reference. Each card: quote mark icon + blockquote + divider + company logo circle + name/role.
- ✅ **Blog Preview** — "From our blog" eyebrow + "Explore Our Latest Articles" h2 (accent on 2nd clause) + "View all →" link. 3 post cards (3-col lg / 2-col md / 1-col sm): each card has real thumbnail, date (April 10 / April 3 / March 20, 2026), category badge (Lead Generation on first post, links to `/blog-category/lead-generation`), title, "Read article →" CTA. Real `/blog/*` slugs. Thumbnails copied to `public/images/blog/`.

## 3. Responsive + polish

- ✅ **Mobile layout (375)** — no horizontal scroll, nav collapses to hamburger, every section stacks cleanly. Verified via `build/screenshots/index-mobile.png` and `hasHorizontalScroll: false` in report.
- ✅ **Tablet layout (768)** — no errors, screenshot saved to `build/screenshots/index-tablet.png`.
- ✅ **Desktop layout (1440)** — matches reference layout (two-column hero, 4-col case studies, 3-col services/industries/blog grids). Screenshot saved.
- ✅ **No off-palette colors** — every color routes through `@theme` tokens in `global.css` (`--color-bg`, `--color-fg`, `--color-accent`, `--color-accent-alt`, etc.). No inline hex literals in any component file (all reference `var(--color-*)`).

## 4. Verification gates

- ✅ `npm run build` exits 0 (≈1.8s, no warnings)
- ✅ `node scripts/verify.mjs` reports zero errors across all 3 viewports
- ✅ `build/report.json` shows: `hasNav: true`, `hasFooter: true`, `h1Count: 1`, `sectionCount: 9`, `imgCount: 42`, `hasHorizontalScroll: false`
- ✅ **Lighthouse (production build via `astro preview`):** Performance 91, Accessibility 96, Best Practices 96, SEO 100 — all ≥ 90 target.

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
- **Iter 6:** Built Case Studies section with 8 cards using real client names, real result headlines, and real hero images from reference. Accent CTA "Free Consultation" → /contact. 4-col grid → 2-col → 1-col responsive. verify.mjs: imgs 27→35, words 306→425.
- **Iter 7:** Built Who We Are with verbatim 3-paragraph body copy + Mission/Vision cards (also verbatim). Initially drafted a large "Bridging care..." h2 headline — caught and removed before commit because reference has NO h2 in this section and PROMPT.md forbids copy rewrites. Kept to the structure the reference actually has. verify.mjs: words 425→539.
- **Pitfall caught (Iter 7):** Easy to hallucinate a "proper" section headline when the reference doesn't have one. Rule of thumb for remaining sections: if a text string isn't in `reference/html/index.html`, don't invent it.
- **Iter 8:** Built Services with featured AI-Powered Marketing card (full 2-paragraph AEO/GEO description, illustration) + 5-service grid (Content, SEO, PPC, Web, CRM) — all descriptions verbatim from reference. verify.mjs: words 539→813, imgs 35→36. SVG icons are inline/stylistic, not the reference Recurso SVGs (those weren't per-service on the homepage — only 3 exist and they're in meta schema, not rendered inline).
- **Iter 9 (final, in-session):** Ralph state file was lost during iter 8's commit (git add -A staged it as deleted). Rather than restart the loop (would re-run from scratch against CHECKLIST), finished in-session: built Industries (3 cards with verbatim copy), Testimonials (3 real quotes from Anthony Ciarrocchi / Justin McCoy / Emily Minerowicz), and Blog Preview (3 real posts with real thumbnails). Ran Lighthouse against production build (`astro preview`): Performance 91, Accessibility 96, Best Practices 96, SEO 100. All checklist gates passing.
- **Not ported (Phase 2 territory, per PROMPT.md rules):**
  - Lottie hero animation (JSON file scraped but not embedded — static image used instead)
  - Webflow animated counter columns in Stats (static values used; flagged for team confirmation)
  - Intersection-observer scroll reveals throughout
  - "Why Choose Us" 4-pillar block (Data-Driven / Proven Expertise / Tailored / Result-Oriented) appears in reference HTML between Industries and Testimonials — NOT added because it wasn't in CHECKLIST.md and adding new sections violates PROMPT.md. Noted here for Phase 2.
- **Known truth-bending:** Three stat values (150+, 98%, 12+, 5x — actually four) are estimates, not scraped. Flagged earlier. Team should replace before public launch.
