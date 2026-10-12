#!/usr/bin/env node
// What only a browser can check, for every pull request that touches a
// template, static/app.js or the stylesheet.
//
//   VETTARR_PASSWORD=... node scripts/checks.js --base http://127.0.0.1:4861
//
// It runs against the demo data (docs/DEV.md) and changes none of it: every
// request that would is answered by the script, not the app. One headless
// Chromium, one page at a time. Exit 0 when every check passes, 1 otherwise.
//
// The layout: a destructive button asks first (the dialog opens with focus on
// Cancel and nothing is sent; Escape and Cancel send nothing; Confirm sends
// one request), an error answer is a toast, Tab shows an outline, and Back
// after signing out shows no page.
// The Inbox: a filter applies on change and leaves no copy of the list in
// sessionStorage, Back and Forward show the selects as the list is, on a
// phone the open tab is in sight on every tab and a title is a 44 px target,
// ticked rows show the action bar and the reject dialog counts them, a
// refused bulk action says so and one with no session goes to the login page,
// a 409 from a row's button is a toast, the title opens the item in the
// slide-over, and the download block stops asking once an answer says the
// download is over and the panel around it reads the item again.
// Review: each key clicks its button (never under a dialog, never with Ctrl),
// a held key acts once (A, S, and Enter on a focused button), Grab is offered
// only with a release to take, ? folds the legend, J and K move, and on a
// phone the actions are a fixed bar above the nav and a swipe moves to the
// next and the previous item.
// Policy and Setup: neither scrolls sideways on a phone, Enter in a rule
// field saves nothing, the severity chips work from the keyboard, a moved
// rule keeps the focus and a new one gets it, removing a source asks first,
// and a second save in a Setup section loads the page again.
'use strict';
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const at = args.indexOf('--base');
const BASE = (at >= 0 ? args[at + 1] : 'http://127.0.0.1:4850').replace(/\/+$/, '');
const PASSWORD = process.env.VETTARR_PASSWORD;
const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true };
const DESK = { viewport: { width: 1280, height: 800 } };
const BUSY = 'This item is busy. Try again in a moment.';

let failed = 0;
function check(name, ok, saw) {
  if (!ok) failed += 1;
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${ok ? '' : `: ${JSON.stringify(saw)}`}`);
}

async function signIn(context) {
  const page = await context.newPage();
  await page.goto(`${BASE}/login`);
  await page.fill('#password', PASSWORD);
  await Promise.all([page.waitForURL((url) => !url.pathname.startsWith('/login')), page.click('button.btn-primary')]);
  return page;
}

// POSTs the page sent, as paths. Each is answered here, so the app sees none.
async function hold(page, pattern, status, body) {
  const sent = [];
  await page.route(pattern, (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    sent.push(new URL(route.request().url()).pathname);
    return route.fulfill({ status, contentType: 'text/plain; charset=utf-8', body });
  });
  return sent;
}

const toasts = (page) => page.evaluate(() => [...document.querySelectorAll('.toasts .toast')].map((t) => t.innerText.trim()));
const settle = (page) => page.waitForTimeout(300);
const drawerOpen = (page) => page.evaluate(() => document.getElementById('drawer').open);
const reviewing = (page) => page.evaluate(() => {
  // hx-get, not href: the href carries ?from=review for the item page's back link.
  const link = document.querySelector('#review-card [data-key="o"]');
  return link ? link.getAttribute('hx-get') : null;
});

// A finger dragged across the review card, as the two events a browser sends.
async function swipe(page, dx) {
  await page.evaluate((delta) => {
    const card = document.querySelector('[data-swipe]');
    const touch = (x) => new Touch({ identifier: 1, target: card, clientX: x, clientY: 300 });
    const from = touch(delta < 0 ? 300 : 80);
    const to = touch((delta < 0 ? 300 : 80) + delta);
    card.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, touches: [from], changedTouches: [from] }));
    card.dispatchEvent(new TouchEvent('touchend', { bubbles: true, touches: [], changedTouches: [to] }));
  }, dx);
  await page.waitForLoadState('networkidle');
  await settle(page);
}

(async () => {
  if (!PASSWORD) throw new Error('checks: set VETTARR_PASSWORD (docs/DEV.md)');
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ ...PHONE, colorScheme: 'dark', reducedMotion: 'reduce' });
    const page = await signIn(context);

    // --- the confirm dialog, on an item that can be rejected -----------------
    await page.goto(`${BASE}/?tab=needs`, { waitUntil: 'networkidle' });
    const item = await page.getAttribute('article.row[data-id]', 'data-id');
    if (!item) throw new Error('checks: no item is waiting for review; run `vettarr demo-seed` again');
    await page.goto(`${BASE}/item/${item}`, { waitUntil: 'networkidle' });
    const sent = await hold(page, '**/item/*/reject', 409, BUSY);
    const reject = `[hx-post="/item/${item}/reject"]`;
    const open = () => page.evaluate(() => document.getElementById('confirm').open);

    await page.click(reject);
    await settle(page);
    check('Reject opens the dialog and sends nothing', (await open()) && sent.length === 0, { open: await open(), sent });
    const focused = await page.evaluate(() => document.activeElement.value);
    check('the dialog opens with focus on Cancel', focused === 'no', focused);

    await page.keyboard.press('Escape');
    await settle(page);
    check('Escape closes the dialog and sends nothing', !(await open()) && sent.length === 0, { open: await open(), sent });

    await page.click(reject);
    await page.click('#confirm [value="no"]');
    await settle(page);
    check('Cancel closes the dialog and sends nothing', !(await open()) && sent.length === 0, { open: await open(), sent });

    await page.focus(reject);
    await page.keyboard.press('Enter');
    await settle(page);
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await settle(page);
    check('by keyboard, Confirm sends the request once', sent.length === 1 && sent[0] === `/item/${item}/reject`, sent);
    check('an error answer is a toast with its text', (await toasts(page)).some((t) => t.includes(BUSY)), await toasts(page));

    // --- focus shows ---------------------------------------------------------
    await page.goto(`${BASE}/stats`, { waitUntil: 'networkidle' });
    await page.keyboard.press('Tab');
    const first = await page.evaluate(() => {
      const el = document.activeElement;
      const style = getComputedStyle(el);
      return { skip: el.classList.contains('skip-link'), style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
    });
    check('Tab reaches the skip link first', first.skip, first);
    check('what has focus shows an outline', first.style !== 'none' && first.width >= 2, first);

    // --- Policy and Setup ----------------------------------------------------
    // Needs a config file the page may write (the demo one, without CONFIG_READONLY).
    const wide = async () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    await page.goto(`${BASE}/policy`, { waitUntil: 'networkidle' });
    check('the Policy page does not scroll sideways on a phone', (await wide()) <= 0, await wide());
    // An edit swaps the whole editor; the answer says where the keyboard goes next.
    // /policy/edit writes nothing, so the app answers these two itself.
    const focus = () => page.evaluate(() => ({
      on: document.activeElement.id,
      second: (document.getElementById('rule-1-id') || {}).value,
      rules: document.querySelectorAll('[id^="rule-"][id$="-name"]').length,
    }));
    const moving = await page.$eval('#rule-0-id', (el) => el.value);
    const shape = await focus();
    await page.focus('#rule-0-down');
    await page.keyboard.press('Enter');
    await page.waitForLoadState('networkidle');
    await settle(page);
    const down = await focus();
    check('a moved rule keeps the focus on its own button', down.on === (shape.rules > 2 ? 'rule-1-down' : 'rule-1-up') && down.second === moving, { moving, shape, down });
    await page.focus('#add-rule');
    await page.keyboard.press('Enter');
    await page.waitForLoadState('networkidle');
    await settle(page);
    const added = await focus();
    check('a new rule gets the focus on its name', added.on === `rule-${shape.rules}-name` && added.rules === shape.rules + 1, { shape, added });
    await page.goto(`${BASE}/policy`, { waitUntil: 'networkidle' });
    const policy = await hold(page, '**/policy{,/*}', 409, BUSY);
    await page.focus('#rule-0-name');
    await page.keyboard.press('Enter');
    await settle(page);
    check('Enter in a rule field saves nothing', policy.length === 1 && policy[0] === '/policy/edit', policy);
    const chip = '[name="rule-1-severity"]';
    await page.focus('#rule-1-operator');
    await page.keyboard.press('Tab');
    const chosen = await page.evaluate(() => {
      const el = document.activeElement;
      const style = getComputedStyle(el.nextElementSibling);
      return { name: el.name, value: el.value, style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
    });
    check('Tab reaches the severity chips and the chosen chip shows an outline', chosen.name === 'rule-1-severity' && chosen.style !== 'none' && chosen.width >= 2, chosen);
    await page.keyboard.press('ArrowLeft');
    const moved = await page.$eval(`${chip}:checked`, (el) => el.value);
    check('an arrow key moves the severity and sends nothing', moved !== chosen.value && policy.length === 1, { moved, policy });

    await page.goto(`${BASE}/setup`, { waitUntil: 'networkidle' });
    check('the Setup page does not scroll sideways on a phone', (await wide()) <= 0, await wide());
    const removals = await hold(page, '**/setup/sources/*/delete', 409, BUSY);
    await page.click('#sources details.fold > summary');
    await page.click('form[action$="/delete"] button');
    await settle(page);
    check('Remove this source opens the dialog and sends nothing', (await open()) && removals.length === 0, { open: await open(), removals });
    await page.click('#confirm [value="no"]');
    await settle(page);
    check('Cancel keeps the source', !(await open()) && removals.length === 0, { open: await open(), removals });
    // A save answers with an address that names the file it wrote, so a save that changed the file has an
    // address of its own (the unit tests hold the app to that). The script answers for the app here: a
    // browser sent to the address it is already at would only scroll and keep showing the old page.
    let saves = 0;
    await page.route('**/setup/block/web', (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      saves += 1;
      return route.fulfill({ status: 200, headers: { 'HX-Redirect': `/setup?saved=web.${saves}#web` }, body: '' });
    });
    const saveWeb = async () => {
      await page.evaluate(() => { window.staleSetup = true; });
      await page.click('#card-web form button.btn-primary');
      await page.waitForLoadState('networkidle');
      await settle(page);
      return page.evaluate(() => ({ stale: window.staleSetup === true, at: location.search + location.hash }));
    };
    const saved = [await saveWeb(), await saveWeb()];
    check('a second save in a section loads the page again', saves === 2 && !saved[0].stale && !saved[1].stale && saved[1].at === '?saved=web.2#web', { saves, saved });

    // --- the Inbox: filters --------------------------------------------------
    await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
    const before = await page.evaluate(() => document.querySelector('.tab[aria-current] .tab-count').textContent);
    await page.focus('select[name="type"]');
    await page.selectOption('select[name="type"]', 'movie');
    await page.waitForLoadState('networkidle');
    await settle(page);
    const filtered = await page.evaluate(() => ({
      search: location.search,
      types: [...new Set([...document.querySelectorAll('article.row .row-meta .badge')].map((b) => b.textContent))],
      count: document.querySelector('.tab[aria-current] .tab-count').textContent,
      tabLink: document.querySelector('.tab:not([aria-current])').getAttribute('href'),
      focus: document.activeElement.name,
    }));
    check('a filter applies on change, without a button', filtered.search.includes('type=movie') && filtered.types.join() === 'movie', filtered);
    check('the tabs get the filtered counts and keep the filter', Number(filtered.count) < Number(before) && filtered.tabLink.includes('type=movie'), { before, ...filtered });
    check('the changed filter keeps the focus', filtered.focus === 'type', filtered.focus);
    const cached = await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith('htmx-history')));
    check('no copy of the list is kept in sessionStorage', cached.length === 0, cached);
    const shows = () => page.evaluate(() => ({
      search: location.search,
      select: document.querySelector('select[name="type"]').value,
      types: [...new Set([...document.querySelectorAll('article.row .row-meta .badge')].map((b) => b.textContent))].sort().join(),
    }));
    await page.goBack();
    await page.waitForLoadState('networkidle');
    await settle(page);
    const wentBack = await shows();
    await page.goForward();
    await page.waitForLoadState('networkidle');
    await settle(page);
    const wentForward = await shows();
    check('Back shows every type again, with the select saying so', !wentBack.search.includes('type=') && wentBack.select === '' && wentBack.types !== 'movie', wentBack);
    check('Forward shows the filtered list, with the select saying so', wentForward.search.includes('type=movie') && wentForward.select === 'movie' && wentForward.types === 'movie', wentForward);

    // --- the Inbox on a phone: tabs and tap targets ----------------------------
    for (const tab of ['needs', 'waiting', 'no-release', 'done', 'all']) {
      await page.goto(`${BASE}/?tab=${tab}`, { waitUntil: 'networkidle' });
      const strip = await page.evaluate(() => {
        const tabs = [...document.querySelectorAll('#inbox-tabs .tab')].map((t) => t.getBoundingClientRect());
        const open = document.querySelector('#inbox-tabs .tab[aria-current="page"]').getBoundingClientRect();
        return {
          open: [Math.round(open.left), Math.round(open.right)], width: window.innerWidth,
          inSight: tabs.filter((t) => t.left >= 0 && t.right <= window.innerWidth).length, tabs: tabs.length,
          short: Math.round(Math.min(...tabs.map((t) => t.height))),
        };
      });
      check(`on a phone the open tab is in sight on ${tab}, with the four others`, strip.open[0] >= 0 && strip.open[1] <= strip.width && strip.inSight === 5 && strip.tabs === 5 && strip.short >= 44, strip);
    }
    await page.goto(`${BASE}/?tab=needs&type=movie`, { waitUntil: 'networkidle' });
    const titles = await page.evaluate(() => Math.round(Math.min(...[...document.querySelectorAll('.row-title a')].map((a) => a.getBoundingClientRect().height))));
    check('on a phone a title is a target 44 px tall', titles >= 44, titles);

    // --- the Inbox: a row's own button ---------------------------------------
    const row = await page.getAttribute('article.row[data-id]', 'data-id');
    const inline = await hold(page, '**/inbox/*/approve', 409, BUSY);
    await page.click(`#row-${row} [hx-post="/inbox/${row}/approve"]`);
    await settle(page);
    check('a 409 from a row is a toast and the row stays', inline.length === 1 && (await toasts(page)).some((t) => t.includes(BUSY)) && (await page.locator(`#row-${row}`).count()) === 1, { inline, toasts: await toasts(page) });


    // --- the Inbox: several rows ---------------------------------------------
    const bar = () => page.evaluate(() => {
      const el = document.getElementById('bulk-bar');
      return { hidden: el.hidden, text: el.querySelector('[data-count]').textContent, position: getComputedStyle(el).position };
    });
    check('the action bar is hidden while nothing is ticked', (await bar()).hidden, await bar());
    await page.check(`#row-${row} input[name="ids"]`);
    const one = await bar();
    check('ticking a row shows a sticky bar that counts it', !one.hidden && one.text === '1 selected' && one.position === 'sticky', one);
    await page.check('[data-select-all]');
    const every = await page.locator('input[name="ids"]').count();
    check('Select all ticks every row', (await bar()).text === `${every} selected` && every > 1, { bar: await bar(), every });
    await page.click('#bulk-bar [hx-post="/bulk/reject"]');
    await settle(page);
    const bulkAsks = await page.evaluate(() => ({
      open: document.getElementById('confirm').open,
      title: document.querySelector('#confirm [data-confirm-title]').textContent,
      text: document.querySelector('#confirm [data-confirm-text]').textContent,
    }));
    check('the bulk reject dialog says how many and what goes', bulkAsks.open && bulkAsks.title === `Reject ${every} items?` && /post/.test(bulkAsks.text), bulkAsks);
    await page.keyboard.press('Escape');
    await settle(page);
    await page.uncheck('[data-select-all]');
    check('and unticking it hides the bar', (await bar()).hidden, await bar());

    await page.check(`#row-${row} input[name="ids"]`);
    const refused = await hold(page, '**/bulk/*', 403, 'Refused: this request came from another site.');
    await page.click('#bulk-bar [hx-post="/bulk/approve"]');
    await settle(page);
    check('a refused bulk action says so in a toast', refused.length === 1 && (await toasts(page)).some((t) => t.includes('Refused')), { refused, toasts: await toasts(page) });

    // --- the Inbox: the item in the slide-over --------------------------------
    await page.click(`#row-${row} .row-title a`);
    await page.waitForSelector('#drawer[open] #item-panel');
    const inDrawer = await page.evaluate(() => ({
      path: location.pathname,
      panel: document.querySelector('#drawer #item-panel').dataset.id,
      focusInside: document.getElementById('drawer').contains(document.activeElement),
      width: document.getElementById('drawer').getBoundingClientRect().width,
    }));
    check('the title opens the item in the slide-over, on the same page', inDrawer.path === '/' && inDrawer.panel === row && inDrawer.focusInside, inDrawer);
    check('on a phone the slide-over fills the width', inDrawer.width === 390, inDrawer.width);
    const inner = await hold(page, '**/item/*/approve', 409, BUSY);
    await page.click(`#drawer [hx-post="/item/${row}/approve"]`);
    await settle(page);
    const shown = await page.evaluate(() => [...document.querySelectorAll('#drawer .toasts .toast')].map((t) => t.innerText.trim()));
    check('a toast shows over the open slide-over', inner.length === 1 && shown.some((t) => t.includes(BUSY)), { inner, shown });
    await page.keyboard.press('Escape');
    await settle(page);
    const closed = await page.evaluate(() => ({ open: document.getElementById('drawer').open, left: document.getElementById('drawer-body').childElementCount }));
    check('Escape closes the slide-over and empties it', !closed.open && closed.left === 0, closed);

    // --- the download block stops asking --------------------------------------
    await page.goto(`${BASE}/?tab=done`, { waitUntil: 'networkidle' });
    const live = await page.evaluate(() => {
      const chip = [...document.querySelectorAll('article.row')].find((r) => /Downloading \d+%/.test(r.textContent));
      return chip ? chip.dataset.id : null;
    });
    if (!live) throw new Error('checks: no item is downloading; run `vettarr demo-seed` again');
    let polls = 0;
    await page.route('**/item/*/progress', (route) => {
      polls += 1;
      return route.fulfill({
        status: 200, contentType: 'text/html; charset=utf-8', headers: { 'HX-Trigger': JSON.stringify({ [`over-${live}`]: true }) },
        body: '<div id="download" class="download"><p>Completed</p></div>',
      });
    });
    // The panel is read once to open the slide-over, by the app. The second read is the one the
    // poll's last answer asks for (over-<id>), answered here: the demo item never stops downloading.
    let panels = 0;
    await page.route(`**/item/${live}`, (route) => {
      panels += 1;
      if (panels === 1) return route.continue();
      return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: `<article id="item-panel" class="panel" data-id="${live}"><p id="read-again">Downloaded</p></article>` });
    });
    await page.click(`#row-${live} .row-title a`);
    await page.waitForSelector('#drawer[open] #download[hx-trigger="every 5s"]');
    await page.waitForTimeout(6500);
    const asked = polls;
    await page.waitForTimeout(6500);
    check('the download block asks while it says downloading', asked === 1, asked);
    check('and stops once an answer says the download is over', polls === 1, polls);
    const readAgain = await page.locator('#drawer #item-panel #read-again').count();
    check('and the panel around it reads the item again', panels === 2 && readAgain === 1, { panels, readAgain });
    await page.unroute(`**/item/${live}`);
    await page.keyboard.press('Escape');

    // --- no session ------------------------------------------------------------
    await page.unroute('**/bulk/*');
    await page.goto(`${BASE}/?tab=needs`, { waitUntil: 'networkidle' });
    await page.check('article.row input[name="ids"]');
    const kept = await context.cookies();
    await context.clearCookies();
    await page.click('#bulk-bar [hx-post="/bulk/approve"]');      // the gate answers 401 and nothing runs
    await page.waitForURL((url) => url.pathname === '/login', { timeout: 5000 }).catch(() => {});
    const landed = new URL(page.url());
    check('a bulk action with no session goes to the login page and back', landed.pathname === '/login' && landed.searchParams.get('next') === '/?tab=needs', page.url().replace(BASE, ''));
    await context.addCookies(kept);

    // --- Review on a phone ------------------------------------------------------
    await page.goto(`${BASE}/review`, { waitUntil: 'networkidle' });
    const layout = await page.evaluate(() => {
      const actions = document.querySelector('.review-actions');
      const nav = document.querySelector('.nav-links').getBoundingClientRect();
      const box = actions.getBoundingClientRect();
      return {
        position: getComputedStyle(actions).position, bottom: Math.round(box.bottom), navTop: Math.round(nav.top),
        height: Math.round(Math.min(...[...actions.querySelectorAll('.btn')].map((b) => b.getBoundingClientRect().height))),
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      };
    });
    check('on a phone the actions are a fixed bar above the nav', layout.position === 'fixed' && layout.bottom <= layout.navTop + 1 && layout.height >= 44, layout);
    check('and the page does not scroll sideways', !layout.overflow, layout);
    const start = await reviewing(page);
    await swipe(page, -160);
    const next = await reviewing(page);
    check('a swipe left shows the next item', next && next !== start, { start, next });
    await swipe(page, 160);
    check('a swipe right shows the previous one', (await reviewing(page)) === start, { start, now: await reviewing(page) });
    await context.close();

    // --- Review with a keyboard -------------------------------------------------
    const desk = await browser.newContext({ ...DESK, colorScheme: 'light', reducedMotion: 'reduce' });
    const board = await signIn(desk);
    await board.goto(`${BASE}/review`, { waitUntil: 'networkidle' });
    const current = (await reviewing(board)).split('/').pop();
    const keyed = await hold(board, '**/review/**', 409, BUSY);
    await board.keyboard.press('a');
    await settle(board);
    check('A approves the item on the page', keyed.join() === `/review/${current}/approve`, keyed);
    check('and a 409 is a toast, with the item still there', (await toasts(board)).some((t) => t.includes(BUSY)) && (await reviewing(board)) === `/item/${current}`, await toasts(board));
    await board.keyboard.press('Control+a');
    await board.keyboard.press('Meta+a');
    await settle(board);
    check('a key with Ctrl or Cmd is the browser\'s', keyed.length === 1, keyed);
    // A key held down: one keydown, then the repeats a keyboard sends about every 80 ms.
    // Each answer leaves an enabled button on the page, so a repeat that got through would act again.
    const held = async (key) => {
      for (let n = 0; n < 8; n += 1) {
        await board.keyboard.down(key);
        await board.waitForTimeout(80);
      }
      await board.keyboard.up(key);
      await settle(board);
    };
    await held('a');
    check('a held A approves once, not once per repeat', keyed.length === 2, keyed);
    await held('s');
    check('a held S searches once', keyed.length === 3 && keyed[2] === `/review/${current}/search`, keyed);
    await board.focus(`[hx-post="/review/${current}/search"]`);
    await held('Enter');
    check('a held Enter on a focused button acts once', keyed.length === 4 && keyed[3] === `/review/${current}/search`, keyed);
    await board.evaluate(() => document.activeElement.blur());

    // Grab is there only with a release to take.
    const grabbable = () => board.evaluate(() => ({
      grab: !!document.querySelector('#review-card [data-key="g"]'),
      best: !!document.querySelector('#review-card .release-title'),
    }));
    const seen = [];
    let withRelease = null;
    for (let n = 0; n < 30 && !(withRelease && seen.some((s) => !s.best)); n += 1) {
      const now = await grabbable();
      seen.push(now);
      if (now.grab && !withRelease) withRelease = (await reviewing(board)).split('/').pop();
      await board.keyboard.press('j');
      await board.waitForLoadState('networkidle');
      await settle(board);
    }
    check('Grab is offered only on an item with a stored release', withRelease !== null && seen.some((s) => !s.best) && seen.every((s) => !s.grab || s.best), seen);
    await board.goto(`${BASE}/review?item=${withRelease}`, { waitUntil: 'networkidle' });
    const before3 = keyed.length;
    await board.keyboard.press('g');
    await settle(board);
    check('G grabs the best release', keyed.slice(before3).join() === `/review/${withRelease}/grab`, keyed.slice(before3));
    await board.goto(`${BASE}/review`, { waitUntil: 'networkidle' });
    const sentSoFar = keyed.length;
    await board.keyboard.press('r');
    await settle(board);
    const asking = await board.evaluate(() => document.getElementById('confirm').open);
    const asks = await board.evaluate(() => document.querySelector('#confirm [data-confirm-text]').textContent);
    await board.keyboard.press('a');
    await settle(board);
    check('R asks first, and no key acts under the dialog', asking && keyed.length === sentSoFar, { asking, keyed });
    check('the dialog says what happens to the post', /^Its post (will be deleted|will get a reaction|stays where it is)\./.test(asks), asks);
    await board.keyboard.press('Escape');
    const legend = () => board.evaluate(() => document.querySelector('details.keys').open);
    const shownFirst = await legend();
    await board.keyboard.press('?');
    const folded = await legend();
    await board.keyboard.press('?');
    check('the legend is shown, and ? folds and unfolds it', shownFirst && !folded && (await legend()), { shownFirst, folded });
    await board.keyboard.press('j');
    await board.waitForLoadState('networkidle');
    await settle(board);
    const after = await reviewing(board);
    await board.keyboard.press('k');
    await board.waitForLoadState('networkidle');
    await settle(board);
    check('J shows the next item and K the previous', after !== `/item/${current}` && (await reviewing(board)) === `/item/${current}`, { after, now: await reviewing(board) });
    await board.keyboard.press('o');
    await board.waitForSelector('#drawer[open] #item-panel');
    await board.keyboard.press('a');
    await settle(board);
    check('O opens the item, and no key acts under it', (await drawerOpen(board)) && keyed.length === sentSoFar, keyed);
    await desk.close();

    // --- signing out ---------------------------------------------------------
    // Its own context, with no request answered by the script: Playwright
    // turns the browser's cache off on a page that has a route, and the cache
    // is what this checks.
    const plain = await browser.newContext({ ...PHONE, colorScheme: 'light', reducedMotion: 'reduce' });
    const last = await signIn(plain);
    await last.goto(`${BASE}/item/${item}`, { waitUntil: 'networkidle' });
    await Promise.all([last.waitForURL((url) => url.pathname === '/login'), last.click('form[action="/logout"] button')]);
    await last.goBack({ waitUntil: 'networkidle' }).catch(() => {});
    const back = { path: new URL(last.url()).pathname, item: await last.evaluate(() => !!document.getElementById('item-panel')) };
    check('Back after signing out shows the login page, not the item', back.path === '/login' && !back.item, back);
    await plain.close();
  } finally {
    await browser.close();
  }
  console.log(failed ? `checks: ${failed} failed` : 'checks: all passed');
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
