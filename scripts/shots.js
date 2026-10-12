#!/usr/bin/env node
// Screenshots of the running web UI, for a pull request.
//
//   node scripts/shots.js --base http://127.0.0.1:4861 --out shots/ /login / /stats
//
// Every page is taken four times: 1280 and 390 wide, light and dark, as
// <out>/<page>-<width>-<scheme>.png ("/" is "inbox", "/item/7" is "item-7",
// "/?status=completed" is "inbox-status-completed"). One headless Chromium,
// one page at a time.
//
// A page that answers 400 or more fails the run, unless --allow names the
// status: `--allow 429 /login` takes the locked login page. The script only
// opens pages; a state that needs a click (an open dialog) is not its job.
//
// Pages behind the login need VETTARR_PASSWORD in the environment (never an
// argument, so it stays out of the process list); the script signs in once
// and reuses the session. /login itself is always taken signed out.
//
// Needs the `playwright` package and its Chromium; docs/DEV.md says where.
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const VIEWPORTS = [
  { width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
  { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
];
const SCHEMES = ['light', 'dark'];

function parse(argv) {
  const opts = { base: 'http://127.0.0.1:4850', out: 'shots', pages: [], allow: [] };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--base') opts.base = argv[(i += 1)];
    else if (argv[i] === '--out') opts.out = argv[(i += 1)];
    else if (argv[i] === '--allow') opts.allow = argv[(i += 1)].split(',').map(Number);
    else if (argv[i].startsWith('/')) opts.pages.push(argv[i]);
    else throw new Error(`shots: what is ${argv[i]}? Pages start with a slash.`);
  }
  if (!opts.pages.length) opts.pages.push('/login');
  opts.base = opts.base.replace(/\/+$/, '');
  return opts;
}

function slug(page) {
  const [path, query] = page.split('?');
  const name = path.replace(/^\/+|\/+$/g, '').replace(/[^a-zA-Z0-9]+/g, '-') || 'inbox';
  const rest = (query || '').replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return rest ? `${name}-${rest}` : name;
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

(async () => {
  const opts = parse(process.argv.slice(2));
  fs.mkdirSync(opts.out, { recursive: true });
  const browser = await chromium.launch();
  let failed = 0;
  try {
    const password = process.env.VETTARR_PASSWORD;
    const session = password ? await signIn(browser, opts.base, password) : undefined;
    for (const target of opts.pages) {
      for (const viewport of VIEWPORTS) {
        for (const colorScheme of SCHEMES) {
          const context = await browser.newContext({
            viewport: { width: viewport.width, height: viewport.height },
            deviceScaleFactor: viewport.deviceScaleFactor,
            isMobile: viewport.isMobile,
            hasTouch: viewport.hasTouch,
            colorScheme,
            reducedMotion: 'reduce',
            storageState: target.startsWith('/login') ? undefined : session,
          });
          const page = await context.newPage();
          const file = path.join(opts.out, `${slug(target)}-${viewport.width}-${colorScheme}.png`);
          const response = await page.goto(opts.base + target, { waitUntil: 'networkidle' });
          const landed = new URL(page.url()).pathname;
          if (!response || (response.status() >= 400 && !opts.allow.includes(response.status()))) {
            console.error(`shots: ${target} answered ${response ? response.status() : 'nothing'}`);
            failed += 1;
          } else if (landed.startsWith('/login') && !target.startsWith('/login')) {
            console.error(`shots: ${target} needs a session; set VETTARR_PASSWORD`);
            failed += 1;
          } else {
            await page.screenshot({ path: file, fullPage: true });
            console.log(file);
          }
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
