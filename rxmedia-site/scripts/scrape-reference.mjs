#!/usr/bin/env node
// One-shot scrape of https://rxmedia.io → ./reference/
// Discovers same-origin pages from the homepage, saves HTML + screenshots
// at 3 viewports, downloads image/font assets, extracts design tokens.
// Re-run is idempotent (overwrites).

import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { basename, join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = 'https://rxmedia.io';
const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(__dirname, '..', 'reference');
const VIEWPORTS = [
  { name: 'mobile', width: 375, height: 812 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'desktop', width: 1440, height: 900 },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function slugFromUrl(url) {
  const u = new URL(url);
  const path = u.pathname.replace(/\/+$/, '');
  if (!path || path === '') return 'index';
  return path.replace(/^\//, '').replace(/\//g, '_');
}

async function ensureDir(p) {
  await mkdir(p, { recursive: true });
}

async function downloadAsset(page, url, destDir) {
  try {
    const filename = basename(new URL(url).pathname) || `asset-${Date.now()}`;
    const dest = join(destDir, filename);
    if (existsSync(dest)) return dest;
    const resp = await page.request.get(url);
    if (!resp.ok()) return null;
    const buf = await resp.body();
    await writeFile(dest, buf);
    return dest;
  } catch {
    return null;
  }
}

async function discoverPages(page) {
  await page.goto(ROOT, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(4000);
  const hrefs = await page.$$eval('a[href]', (as) =>
    Array.from(new Set(as.map((a) => a.href)))
  );
  const seen = new Set([ROOT + '/', ROOT]);
  const pages = [ROOT];
  for (const href of hrefs) {
    try {
      const u = new URL(href);
      if (u.origin !== new URL(ROOT).origin) continue;
      if (u.hash) u.hash = '';
      const clean = u.origin + u.pathname.replace(/\/+$/, '');
      if (seen.has(clean) || seen.has(clean + '/')) continue;
      // skip file downloads
      if (/\.(pdf|zip|jpg|jpeg|png|gif|webp|svg|mp4|mov)$/i.test(u.pathname)) continue;
      seen.add(clean);
      pages.push(clean);
    } catch {
      /* ignore bad urls */
    }
  }
  return pages;
}

async function extractTokens(page) {
  return await page.evaluate(() => {
    const getComputed = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const cs = getComputedStyle(el);
      return {
        fontFamily: cs.fontFamily,
        fontSize: cs.fontSize,
        fontWeight: cs.fontWeight,
        lineHeight: cs.lineHeight,
        color: cs.color,
        backgroundColor: cs.backgroundColor,
      };
    };
    const rgb = (c) => c;
    const colors = new Set();
    const fontFamilies = new Set();
    document.querySelectorAll('*').forEach((el) => {
      const cs = getComputedStyle(el);
      if (cs.color && cs.color !== 'rgba(0, 0, 0, 0)') colors.add(rgb(cs.color));
      if (cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)')
        colors.add(rgb(cs.backgroundColor));
      if (cs.fontFamily) fontFamilies.add(cs.fontFamily);
    });
    return {
      body: getComputed('body'),
      h1: getComputed('h1'),
      h2: getComputed('h2'),
      h3: getComputed('h3'),
      p: getComputed('p'),
      a: getComputed('a'),
      button: getComputed('button, .button, [role="button"]'),
      colors: Array.from(colors),
      fontFamilies: Array.from(fontFamilies),
    };
  });
}

async function extractImageAndFontUrls(page) {
  return await page.evaluate(() => {
    const imgs = Array.from(document.querySelectorAll('img'))
      .map((i) => i.currentSrc || i.src)
      .filter(Boolean);
    const bgImgs = [];
    document.querySelectorAll('*').forEach((el) => {
      const bg = getComputedStyle(el).backgroundImage;
      if (bg && bg.startsWith('url(')) {
        const m = bg.match(/url\(["']?(.*?)["']?\)/);
        if (m) bgImgs.push(m[1]);
      }
    });
    const fonts = [];
    document.querySelectorAll('link[rel="stylesheet"],link[rel="preload"][as="font"]').forEach((l) => {
      if (l.href) fonts.push(l.href);
    });
    return { imgs: Array.from(new Set(imgs)), bgImgs: Array.from(new Set(bgImgs)), fonts };
  });
}

async function scrapePage(browser, url) {
  const slug = slugFromUrl(url);
  console.log(`\n→ ${url}  (slug: ${slug})`);
  const htmlDir = join(OUT, 'html');
  const shotDir = join(OUT, 'screenshots');
  const imgDir = join(OUT, 'assets', 'images');
  const fontDir = join(OUT, 'assets', 'fonts');
  await ensureDir(htmlDir);
  await ensureDir(shotDir);
  await ensureDir(imgDir);
  await ensureDir(fontDir);

  const result = { url, slug, screenshots: {}, tokens: null, assets: [] };

  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 1,
      userAgent:
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36',
    });
    const page = await context.newPage();
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      try {
        await page.waitForLoadState('networkidle', { timeout: 10000 });
      } catch {
        /* Webflow sites rarely reach idle — proceed after DOM is ready */
      }
      await sleep(2500); // let animations settle
      // scroll to bottom + back to top to trigger lazy-loaded content
      await page.evaluate(async () => {
        await new Promise((resolve) => {
          let y = 0;
          const id = setInterval(() => {
            window.scrollTo(0, y);
            y += 600;
            if (y > document.body.scrollHeight) {
              clearInterval(id);
              window.scrollTo(0, 0);
              resolve();
            }
          }, 120);
        });
      });
      await sleep(1200);

      const shotPath = join(shotDir, `${slug}-${vp.name}.png`);
      await page.screenshot({ path: shotPath, fullPage: true });
      result.screenshots[vp.name] = shotPath;
      console.log(`  ✓ ${vp.name}  ${shotPath}`);

      // Only dump HTML + tokens + assets on the desktop pass
      if (vp.name === 'desktop') {
        const html = await page.content();
        const htmlPath = join(htmlDir, `${slug}.html`);
        await writeFile(htmlPath, html);
        result.html = htmlPath;

        result.tokens = await extractTokens(page);

        const { imgs, bgImgs, fonts } = await extractImageAndFontUrls(page);
        const all = [...imgs, ...bgImgs].filter((u) => /^https?:/.test(u));
        for (const asset of all) {
          const p = await downloadAsset(page, asset, imgDir);
          if (p) result.assets.push({ src: asset, local: p });
        }
        for (const f of fonts.filter((u) => /\.(woff2?|ttf|otf)/i.test(u) || /fonts\.googleapis/i.test(u))) {
          await downloadAsset(page, f, fontDir);
        }
      }
    } catch (e) {
      console.log(`  ✗ ${vp.name} failed: ${e.message}`);
    } finally {
      await context.close();
    }
  }
  return result;
}

(async () => {
  await ensureDir(OUT);
  const browser = await chromium.launch({ headless: true });

  const discoverCtx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const discoverPage = await discoverCtx.newPage();
  console.log('Discovering pages from', ROOT);
  const pages = await discoverPages(discoverPage);
  await discoverCtx.close();
  console.log(`Found ${pages.length} pages:`);
  pages.forEach((p) => console.log('  -', p));

  const manifest = { root: ROOT, scrapedAt: new Date().toISOString(), pages: [] };
  for (const url of pages) {
    try {
      const r = await scrapePage(browser, url);
      manifest.pages.push(r);
    } catch (e) {
      console.log(`  ✗ fatal on ${url}: ${e.message}`);
    }
  }
  await browser.close();

  await writeFile(join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 2));

  // Tokens summary — aggregate across pages, homepage wins ties
  const home = manifest.pages.find((p) => p.slug === 'index') || manifest.pages[0];
  const tokens = {
    source: ROOT,
    scrapedAt: manifest.scrapedAt,
    typography: home?.tokens || null,
    colors: home?.tokens?.colors || [],
    fontFamilies: home?.tokens?.fontFamilies || [],
  };
  await writeFile(join(OUT, 'tokens.json'), JSON.stringify(tokens, null, 2));

  console.log('\n✅ Scrape complete');
  console.log('   Manifest:', join(OUT, 'manifest.json'));
  console.log('   Tokens:  ', join(OUT, 'tokens.json'));
})();
