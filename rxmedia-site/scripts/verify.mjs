#!/usr/bin/env node
// Verification harness for the Ralph Loop.
// Boots `astro dev`, takes Playwright screenshots of each page at 3 viewports,
// captures page stats (headings, links, images), writes a report the loop
// can read, and exits non-zero if anything blocks launch.
//
// Usage:
//   node scripts/verify.mjs            # all pages in manifest.json
//   node scripts/verify.mjs index      # just one slug

import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');
const REF = resolve(ROOT, 'reference');
const BUILD = resolve(ROOT, 'build');
const LOCALHOST = 'http://localhost:4321';

const VIEWPORTS = [
  { name: 'mobile', width: 375, height: 812 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'desktop', width: 1440, height: 900 },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForServer(url, attempts = 60) {
  for (let i = 0; i < attempts; i++) {
    try {
      const r = await fetch(url);
      if (r.ok || r.status === 404) return true;
    } catch {
      /* not ready yet */
    }
    await sleep(1000);
  }
  return false;
}

async function loadManifest() {
  try {
    const raw = await readFile(resolve(REF, 'manifest.json'), 'utf8');
    return JSON.parse(raw);
  } catch {
    return { pages: [{ url: '/', slug: 'index' }] };
  }
}

function localUrlForSlug(slug) {
  if (slug === 'index') return `${LOCALHOST}/`;
  return `${LOCALHOST}/${slug.replace(/_/g, '/')}`;
}

async function capturePage(browser, slug) {
  const localUrl = localUrlForSlug(slug);
  const pageReport = { slug, url: localUrl, viewports: {}, stats: null, errors: [] };

  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
    });
    const page = await context.newPage();
    page.on('pageerror', (e) => pageReport.errors.push(`[${vp.name}] ${e.message}`));
    page.on('console', (msg) => {
      if (msg.type() === 'error') pageReport.errors.push(`[${vp.name}] console: ${msg.text()}`);
    });
    try {
      const resp = await page.goto(localUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
      if (!resp || !resp.ok()) {
        pageReport.errors.push(`[${vp.name}] HTTP ${resp?.status()} on ${localUrl}`);
      }
      await sleep(800);
      await mkdir(resolve(BUILD, 'screenshots'), { recursive: true });
      const shot = resolve(BUILD, 'screenshots', `${slug}-${vp.name}.png`);
      await page.screenshot({ path: shot, fullPage: true });
      pageReport.viewports[vp.name] = shot;

      if (vp.name === 'desktop') {
        pageReport.stats = await page.evaluate(() => {
          const text = document.body?.innerText || '';
          return {
            h1Count: document.querySelectorAll('h1').length,
            h2Count: document.querySelectorAll('h2').length,
            h3Count: document.querySelectorAll('h3').length,
            linkCount: document.querySelectorAll('a').length,
            imgCount: document.querySelectorAll('img').length,
            sectionCount: document.querySelectorAll('section, [data-section]').length,
            wordCount: text.trim().split(/\s+/).filter(Boolean).length,
            title: document.title,
            hasNav: !!document.querySelector('nav, header'),
            hasFooter: !!document.querySelector('footer'),
            hasHorizontalScroll: document.documentElement.scrollWidth > window.innerWidth + 1,
          };
        });
      } else {
        // mobile/tablet: just check for horizontal scroll
        const hasHScroll = await page.evaluate(
          () => document.documentElement.scrollWidth > window.innerWidth + 1
        );
        if (hasHScroll) pageReport.errors.push(`[${vp.name}] horizontal scroll detected`);
      }
    } catch (e) {
      pageReport.errors.push(`[${vp.name}] ${e.message}`);
    } finally {
      await context.close();
    }
  }
  return pageReport;
}

async function main() {
  const onlySlug = process.argv[2];
  const manifest = await loadManifest();
  const pages = manifest.pages.filter((p) => !onlySlug || p.slug === onlySlug);
  if (pages.length === 0) {
    console.error(`No matching page for slug "${onlySlug}"`);
    process.exit(1);
  }

  console.log('→ starting astro dev...');
  const dev = spawn('npm', ['run', 'dev', '--', '--port', '4321', '--host'], {
    cwd: ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: false,
  });
  let devLog = '';
  dev.stdout.on('data', (d) => {
    devLog += d.toString();
  });
  dev.stderr.on('data', (d) => {
    devLog += d.toString();
  });

  const killDev = () => {
    try {
      dev.kill('SIGTERM');
    } catch {
      /* already dead */
    }
  };
  process.on('SIGINT', () => {
    killDev();
    process.exit(130);
  });

  const ready = await waitForServer(LOCALHOST);
  if (!ready) {
    console.error('✗ astro dev did not come up within 60s. Recent log:');
    console.error(devLog.slice(-2000));
    killDev();
    process.exit(2);
  }

  const browser = await chromium.launch({ headless: true });
  const report = { at: new Date().toISOString(), pages: [] };
  let failed = false;

  for (const p of pages) {
    console.log(`→ verifying /${p.slug}`);
    const r = await capturePage(browser, p.slug);
    if (r.errors.length) {
      failed = true;
      r.errors.forEach((e) => console.log('   ✗', e));
    } else {
      console.log(`   ✓ screenshots + stats captured`);
    }
    report.pages.push(r);
  }

  await browser.close();
  killDev();

  await mkdir(BUILD, { recursive: true });
  await writeFile(resolve(BUILD, 'report.json'), JSON.stringify(report, null, 2));

  // Human-readable summary
  console.log('\n=== VERIFY SUMMARY ===');
  for (const p of report.pages) {
    const ok = p.errors.length === 0 ? '✅' : '❌';
    const s = p.stats || {};
    console.log(
      `${ok} ${p.slug}  sections=${s.sectionCount ?? '?'} h1=${s.h1Count ?? '?'} h2=${s.h2Count ?? '?'} imgs=${s.imgCount ?? '?'} words=${s.wordCount ?? '?'}`
    );
    if (p.errors.length) p.errors.forEach((e) => console.log('   -', e));
  }
  console.log(`\nReport: ${resolve(BUILD, 'report.json')}`);

  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(99);
});
