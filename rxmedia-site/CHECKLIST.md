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

- 🟡 **Nav** — placeholder logo + "(nav placeholder)" label. NEEDS: real menu items (Home, About, Industries dropdown, Services dropdown, Blog, FAQ), Get in touch CTA, mobile hamburger
- 🟡 **Footer** — placeholder copyright only. NEEDS: logo, nav columns, social icons (fb, linkedin)

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
