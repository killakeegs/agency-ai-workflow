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

- 🟡 **Hero** — headline + subhead + eyebrow present. NEEDS: form (Name, Email, Website), hero illustration/image, button styling
- 🟡 **Trusted-By Ribbon** — eyebrow text only. NEEDS: horizontal row of client logos (AC, freedom, cielo, parkwood, evolve, lotus, resilient-return, renaissance, TR) from `reference/assets/images/`
- 🟡 **Stats Overview** — 4 labeled cards with placeholder values. NEEDS: extract actual numbers from reference HTML/screenshot
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
