#!/usr/bin/env node
// The accessibility check: axe-core on pages of the running web UI.
//
//   VETTARR_PASSWORD=... node scripts/axe.js --base http://127.0.0.1:4861 /login / /item/7 /stats
//
// Every page is checked four times, as the screenshots are taken: 1280 and 390
// wide, light and dark, because a contrast that passes in one theme can fail
// in the other. Then once more with the confirm dialog open and a toast on
// screen, which no page shows by itself. One headless Chromium, one page at a
// time. The rules are WCAG 2.2 A and AA plus axe's best practices.
//
// It prints one line per page and variant with the number of violations, then
// each distinct violation once with the elements it names. Exit 0 when there
// is none, 1 otherwise. `--json file` also writes the full results.
//
// Needs the `playwright` and `axe-core` packages; docs/DEV.md says where.
'use strict';
const fs = require('fs');
const { chromium } = require('playwright');

const AXE = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'];
const VIEWPORTS = [
  { width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
  { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
];
const SCHEMES = ['light', 'dark'];

function parse(argv) {
  const opts = { base: 'http://127.0.0.1:4850', pages: [], json: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--base') opts.base = argv[(i += 1)];
    else if (argv[i] === '--json') opts.json = argv[(i += 1)];
    else if (argv[i].startsWith('/')) opts.pages.push(argv[i]);
    else throw new Error(`axe: what is ${argv[i]}? Pages start with a slash.`);
  }
  if (!opts.pages.length) opts.pages.push('/login');
  opts.base = opts.base.replace(/\/+$/, '');
  return opts;
}

async function signIn(browser, base, password) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(`${base}/login`);
  await page.fill('#password', password);
  await Promise.all([page.waitForURL((url) => !url.pathname.startsWith('/login')), page.click('button.btn-primary')]);
  const state = await context.storageState();
  await context.close();
  return state;
}

async function run(page) {
  await page.evaluate(AXE);
  const results = await page.evaluate((tags) => window.axe.run(document, { runOnly: { type: 'tag', values: tags } }), TAGS);
  return results.violations;
}

(async () => {
  const opts = parse(process.argv.slice(2));
  const browser = await chromium.launch();
  const found = new Map(); // rule id -> { help, impact, where: Set, nodes: Set }
  const all = [];
  let checked = 0;
  let total = 0;

  function note(where, violations) {
    checked += 1;
    total += violations.length;
    console.log(`${violations.length ? 'FAIL' : 'ok  '} ${where}: ${violations.length} violation${violations.length === 1 ? '' : 's'}`);
    all.push({ where, violations });
    for (const v of violations) {
      const entry = found.get(v.id) || { help: v.help, impact: v.impact, where: new Set(), nodes: new Set() };
      entry.where.add(where);
      for (const node of v.nodes) entry.nodes.add(`${node.target.join(' ')}  ${(node.failureSummary || '').split('\n').slice(1, 2).join(' ').trim()}`);
      found.set(v.id, entry);
    }
  }

  try {
    const password = process.env.VETTARR_PASSWORD;
    const session = password ? await signIn(browser, opts.base, password) : undefined;
    for (const target of opts.pages) {
      for (const viewport of VIEWPORTS) {
        for (const colorScheme of SCHEMES) {
          const context = await browser.newContext({
            ...viewport,
            viewport: { width: viewport.width, height: viewport.height },
            colorScheme,
            reducedMotion: 'reduce',
            storageState: target.startsWith('/login') ? undefined : session,
          });
          // The app is dark unless localStorage says light; the <head> script reads it before paint.
          await context.addInitScript((scheme) => { try { localStorage.setItem('theme', scheme); } catch (_) { /* no storage */ } }, colorScheme);
          const page = await context.newPage();
          const where = `${target} ${viewport.width} ${colorScheme}`;
          const response = await page.goto(opts.base + target, { waitUntil: 'networkidle' });
          if (!response || response.status() >= 400) {
            throw new Error(`axe: ${target} answered ${response ? response.status() : 'nothing'}`);
          }
          if (new URL(page.url()).pathname.startsWith('/login') && !target.startsWith('/login')) {
            throw new Error(`axe: ${target} needs a session; set VETTARR_PASSWORD`);
          }
          // Every <details> open, so what it holds is checked too.
          await page.evaluate(() => document.querySelectorAll('details').forEach((d) => { d.open = true; }));
          note(where, await run(page));
          if (!target.startsWith('/login') && viewport === VIEWPORTS[1]) {
            // The two things the layout shows only on demand.
            await page.evaluate(() => {
              window.showToast('Queued The Lantern Keeper.', 'ok');
              window.showToast('This item is already being processed.', 'error');
              window.confirmDialog({ title: 'Reject The Lantern Keeper?', text: 'Its post is deleted.', action: 'Reject' });
            });
            note(`${where}, dialog and toasts open`, await run(page));
          }
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }

  for (const [id, v] of found) {
    console.log(`\n${id} (${v.impact}): ${v.help}`);
    console.log(`  on: ${[...v.where].join('; ')}`);
    for (const node of [...v.nodes].slice(0, 12)) console.log(`  - ${node}`);
    if (v.nodes.size > 12) console.log(`  ... and ${v.nodes.size - 12} more`);
  }
  console.log(`\naxe: ${total} violation${total === 1 ? '' : 's'} in ${checked} checks of ${opts.pages.length} page${opts.pages.length === 1 ? '' : 's'}`);
  if (opts.json) fs.writeFileSync(opts.json, JSON.stringify(all, null, 2));
  process.exit(total ? 1 : 0);
})().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
