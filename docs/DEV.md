# Working on the web UI

How to run the web UI with fake data, run the browser checks and take the screenshots a pull request carries. It also lists what every page gets from the layout, so a page task starts from the same pieces.

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), a local Postgres (the commands below run it in Docker), and Node for Playwright and axe. The paths below use `SCRATCH=$HOME/scratch/vettarr`; any directory on a disk with room works.

## The local setup

- **One Postgres container, `vettarr-dev-pg`**, on `127.0.0.1:5439`, kept between sessions.
- **One database per branch**, named after the branch with `/` and `-` as `_` (`feat/ui-inbox` is `feat_ui_inbox`), so two branches never share data.
- **One app process per branch**, on a free port (the examples use 4860 to 4879). Stop it when you are done.
- **One headless Chromium at a time.** [`scripts/shots.js`](../scripts/shots.js) already works that way.

## 1. Postgres

```sh
docker ps -a --format '{{.Names}}' | grep -qx vettarr-dev-pg || docker run -d \
  --name vettarr-dev-pg --restart unless-stopped --memory 512m \
  -p 127.0.0.1:5439:5432 \
  -e POSTGRES_USER=vettarr -e POSTGRES_PASSWORD=vettarr -e POSTGRES_DB=vettarr \
  postgres:17-alpine

DB=feat_ui_inbox        # your branch
docker exec vettarr-dev-pg createdb -U vettarr "$DB"
```

The user and password are both `vettarr`. The port listens on the loopback address only, and the container holds fake data and nothing else.

## 2. The checkout

```sh
cd vettarr                             # your clone, on your branch
uv venv --python 3.12 && uv pip install -e .
scripts/build-css.sh                   # once; add --watch while you edit templates
```

[`scripts/build-css.sh`](../scripts/build-css.sh) downloads the Tailwind standalone CLI, one binary that the script pins, checks by sha256 and keeps in `.cache/tailwind`. It compiles [`static/src/app.css`](../src/vettarr/web/static/src/app.css) into `static/app.css`. No Node is involved. `static/app.css` is not in git. A page with no styles means this step was skipped, and the app says so in its log at start. Tailwind only emits the utility classes it finds in `web/templates`, `web/routes` and `static/app.js`, so a class you just used appears after the next build.

## 3. Fake data and its config

```sh
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5439 \
       POSTGRES_USER=vettarr POSTGRES_PASSWORD=vettarr POSTGRES_DB="$DB"

.venv/bin/vettarr demo-seed                      # 60 items, their releases, downloads and events
.venv/bin/vettarr demo-seed --config > config.yaml

openssl rand -hex 12 > .dev.password && chmod 600 .dev.password
export WEB_PORT=4861                                  # a free one, 4860 to 4879
export WEB_PASSWORD_HASH="$(.venv/bin/vettarr web hash-password < .dev.password)"
export CONFIG_PATH="$PWD/config.yaml"
```

`demo-seed` creates the schema on a new database and writes 60 invented items across every status and media type, from two sources (`demo-group`, `demo-bot`) and two providers (`northwind`, `harbor`). Each decision is the real policy engine's, run on a made-up TMDB record and parents guide, so reasons, signals and the policy version are what a real instance stores. One provider is left in an open cooldown, so the banner shows. Running it again replaces the demo rows. On a database that holds anything else it writes nothing and exits 1; `--force` adds the demo rows beside the real ones. Use `--force` only on a database you can throw away: the full app takes the approved and searching demo items for real ones, and searches and downloads for them.

`demo-seed --config` prints the config those rows belong to: the same sources, providers and policy, the web UI on `127.0.0.1:${WEB_PORT}` behind `${WEB_PASSWORD_HASH}`. Its Telegram values are placeholders, so it is a config for the next step only.

## 4. Run it

```sh
.venv/bin/vettarr web serve
```

`web serve` runs the web UI alone. Nothing reads a source, searches or downloads, so the seeded rows stay as they are. The full app would pick the approved items up and move them. An action you take in the UI still writes to the database, and seeding again puts the rows back.

To sign in by hand, the password is in `.dev.password`. The app keeps its session secret in `/data/sessions/web-session.key`. A local checkout has no `/data`, so the start logs one warning and a restart signs you out.

## 5. Screenshots

Once, a pinned Playwright and its Chromium, shared by every branch:

```sh
TOOLS="$SCRATCH/tools"
export PLAYWRIGHT_BROWSERS_PATH="$TOOLS/browsers" NODE_PATH="$TOOLS/node_modules"
[ -d "$TOOLS/node_modules/playwright" ] || npm install --prefix "$TOOLS" --save-exact playwright@1.64.0
"$TOOLS/node_modules/.bin/playwright" install chromium
```

Then, with the app running:

```sh
VETTARR_PASSWORD="$(cat .dev.password)" node scripts/shots.js \
  --base "http://127.0.0.1:$WEB_PORT" --out "$SCRATCH/shots/feat-ui-inbox" \
  /login / /stats
```

The script takes each page at 1280 and 390 wide, in light and in dark, as `<page>-<width>-<scheme>.png`, where `/` is `inbox`, `/item/7` is `item-7` and `/?tab=done` is `inbox-tab-done`. The password comes from the environment so it stays out of the process list. Without it the script can only take `/login`. A page that answers an error or lands on the login page fails the run. `--allow 429` lets a page with that status through, which is how the locked login page is taken. The script only opens pages, so a state behind a click, such as the open confirm dialog or an item in the Inbox's slide-over, needs a few lines of Playwright of your own.

Two things to know when you read a shot. The shots are full pages, so on a 390 shot taller than one screen the bottom bar is drawn once, where the first screen ends. And five wrong passwords lock the address for 15 minutes; restarting the app clears the lock.

Copy the PNGs to where you can look at them, look at every one, and attach them to the pull request.

## 6. Browser checks

The unit tests cannot see what a browser does with the layout's script and stylesheet. This does, with the app running on the demo data:

```sh
VETTARR_PASSWORD="$(cat .dev.password)" node scripts/checks.js --base "http://127.0.0.1:$WEB_PORT"
```

[`scripts/checks.js`](../scripts/checks.js) prints one line per check and exits 1 when one fails:

- Reject opens the confirm dialog with focus on Cancel and sends nothing. Escape and Cancel send nothing. Confirm, by keyboard, sends the request once.
- An error answer shows as a toast with its text.
- Tab reaches the skip link first, and what has focus shows an outline.
- A bulk action that is refused says so in a toast. With no session it goes to the login page and back.
- Back after signing out shows the login page, not the item.
- In the Inbox a filter applies on change and the tabs get the filtered counts. No copy of the list is left in sessionStorage, and Back and Forward show the selects as the list is. On a phone the open tab is in sight on every one of the five tabs, and a title is a target 44 px tall. Ticking a row shows the sticky action bar, and the bulk reject dialog says how many items and what happens to their posts. A 409 from a row's button is a toast and the row stays. The title opens the item in the slide-over, a toast shows over it, and Escape closes and empties it.
- The download block asks every 5 seconds while it says "downloading", and stops once an answer says the download is over. The panel around it then reads the item again.
- In Review each key clicks its button, never under an open dialog and never with Ctrl or Cmd. A held key acts once: A, S, and Enter on a focused button each send one request however long they are held. Grab is offered only on an item with a stored release. The reject dialog says what happens to the post. `?` folds the legend, J and K move. On a phone the actions are a fixed bar above the nav, and a swipe left or right moves to the next or the previous item.
- Policy and Setup do not scroll sideways on a phone. Enter in a rule's field saves nothing. Tab reaches the severity chips, the chosen one shows an outline and an arrow key moves it. A rule moved by keyboard keeps the focus on its own button, and a new rule gets it on its name. Removing a source asks first. A second save in the same Setup section loads the page again.

The Policy and Setup checks need a config file the page may write: the demo one, with `CONFIG_READONLY` unset.

It answers every request that would change something itself, so the demo rows stay as they are. Run it on every pull request that touches a template, `static/app.js` or `app.css`, and add a check there when your page adds a control that destroys something. It takes about a minute: the download check waits through two polls.

No workflow runs this script yet, so a green CI run says nothing about the keys, the dialogs or the phone layout. The unit tests that read `app.js` and `app.css` check their text only. To see a check fail, take `e.repeat ||` out of the key handler in `static/app.js` and run the script: "a held A approves once" then reports eight requests.

## 7. Accessibility check

axe-core on the running pages, installed once:

```sh
[ -d "$TOOLS/node_modules/axe-core" ] || npm install --prefix "$TOOLS" --save-exact axe-core@4.14.0
```

Then, with the app running and the same two exports as for the screenshots:

```sh
VETTARR_PASSWORD="$(cat .dev.password)" node scripts/axe.js \
  --base "http://127.0.0.1:$WEB_PORT" /login / /item/5 /review /policy /setup /stats /activity
```

[`scripts/axe.js`](../scripts/axe.js) checks every page at 1280 and 390 wide, in light and in dark, and once more with the confirm dialog and two toasts open. It prints one line per check, then each violation once with the elements it names, and exits 1 when there is one. The rules are WCAG 2.2 A and AA and axe's best practices. Name every page your pull request touches, with an item page for each state it draws.

axe measures what a browser can: contrast in both themes, names, roles, landmarks. It accepts a placeholder as a field's label and cannot tell whether the keyboard reaches a control, so [`tests/test_accessibility.py`](../tests/test_accessibility.py) holds every template to the rest: one `h1`, a label on every field, no focus ring switched off, no checkbox only a click reaches.

## 8. Tests

The suite runs without a database or a browser:

```sh
.venv/bin/python -m unittest discover -s tests
```

[`tests/test_web_foundation.py`](../tests/test_web_foundation.py) drives the app through Starlette's test client. Its `WebCase` builds an app on any `web` block and replaces the banner's database read and what the item pages read (`self.load_inbox`, `self.load_detail` and the rest: set `return_value` to what a page should find). Start a page's tests from it. [`tests/test_web_inbox.py`](../tests/test_web_inbox.py) builds its items from the demo seed.

Because the pages' tests replace the readers, they never run a query. [`tests/test_web_readers.py`](../tests/test_web_readers.py) does: it creates the models' tables in an in-memory SQLite database and asks `load_inbox`, `load_rows`, `load_detail`, `load_download` and `load_review` what a page asks them (paging, order, counts under the filters, which search and download belong to which item, the place in the Review queue). When you add a query to `web/cards.py`, add its test there.

## What a page gets from the layout

**Files.** A page owns one module in [`web/routes/`](../src/vettarr/web/routes/) and its templates. [`web/app.py`](../src/vettarr/web/app.py) only wires the routers. Shared code is in [`web/ui.py`](../src/vettarr/web/ui.py): `templates`, `is_htmx`, `refused` (409 and 404 answers), `toast`, and for a route addressed by an item's id, `ItemId`, `ItemRoute` and `gone`: declare the id as `item_id: ItemId` on a router made with `APIRouter(route_class=ItemRoute)`, and an address no item can have (`/item/abc`, an id past int4) answers the missing-item page or its one line for htmx, not a JSON error or a database error.

**Items.** The Inbox, Review and the item page show the same item as a row, a card and a panel. What they agree on is in [`web/cards.py`](../src/vettarr/web/cards.py): the tabs and their order, what a status and a verdict are called, which buttons an item gets, and every query (`load_inbox`, `load_rows`, `load_detail`, `load_download`, `load_review`). The pieces they draw alike are macros in [`partials/bits.html`](../src/vettarr/web/templates/partials/bits.html). An action on the item page announces `item-<id>` in its `HX-Trigger`, and a row or a card showing that item reads itself again, so a page that shows an item elsewhere listens for it the same way (`hx-trigger="item-7 from:body"`). The download poll's last answer announces `over-<id>` as well, which the panel listens for.

**Slide-over.** `{{ bits.drawer() }}` puts the item slide-over on a page. A link with `hx-get="/item/7" hx-target="#drawer-body" hx-swap="innerHTML"` opens the item in it; Escape, its Close button and nothing else close it, and closing empties it.

**Keys.** A control with `data-key="a"` is clicked by that key, so the key does what the button does. Keys are off in a field and under an open dialog, and a held key acts once: its repeats are dropped, and so are Enter's on a focused button or link. Say the key on the control with `<kbd class="kbd">`.

**Colours** are tokens, set once for light and once for dark in `app.css`. Use them through utilities: `bg-bg`, `bg-surface`, `bg-surface-2`, `border-line`, `text-text`, `text-muted`, `text-accent`, `text-ok`, `text-warn`, `text-danger` and the matching `bg-ok-bg`, `bg-warn-bg`, `bg-danger-bg`. The theme follows the system until the toggle in the nav stores a choice. `dark:` follows the same rule, but a page written with tokens should not need it. The pages written before the tokens use Tailwind's gray scale; those names follow the theme too until each page is rewritten, and the block that does it in `app.css` says to delete it then.

**Type and spacing** are Tailwind's scale: `text-sm` for dense rows, `text-base` for prose, `page-title` for the page's heading, a 4 px spacing unit. Titles and prose are set in the system serif (`font-serif`: Iowan Old Style, Palatino, Charter, Georgia), every control, label and figure in the system sans with tabular figures; no font is fetched. Corners are 2 px at most and nothing has a shadow: a section is told apart by a hairline rule and space.

**Components**, all in `app.css`:

| Class | Use |
|---|---|
| `btn` with `btn-primary`, `btn-danger`, `btn-danger-outline`, `btn-secondary`, `btn-quiet`; `btn-lg`, `btn-icon` | Buttons. `btn-primary` is an ink plate, `btn-lg` the 44 px touch size, solid `btn-danger` the confirm dialog's yes. Inside `panel-actions` and `review-actions` every button is set as a word in the accent. |
| `act`, `act-main` | An action set as a word in the accent, dotted from the one before: a row's Approve · Reject · Search and the bulk bar's. `act-main` is the bold one. On a desktop a row shows them under the pointer or the focus. |
| `card` | A section under a hairline, not a box. |
| `verdict` with `verdict-ok`, `verdict-warn`, `verdict-flag`, `verdict-muted` | The policy's verdict as a small caps word (`bits.verdict(card)`). The only words set in the verdict inks. |
| `label`, `input`, `check` | Form fields. Every input has a label. |
| `badge` with `badge-<media type>`, `badge-ok`, `badge-warn`, `badge-flag`, `badge-muted` | A small caps word, never a pill. The tone is a mark before it (a tick, a dot, a cross, a ring). |
| `tag` | A rule's id or another short code, in monospace. |
| `select`, `link`, `empty`, `empty-line` | A compact select for a filter row, a link inside text, the place a page shows when it has nothing, and its sentence in italic. |
| `pill` with `pill-none`, `pill-mild`, `pill-moderate`, `pill-severe`, `pill-unknown` | A rating, set as words in a dotted run. Always write the word in it (`Violence: severe`); the weight only repeats it. |
| `notice` with `notice-error`, `notice-warn` | A message inside a page. |

Policy and Setup add their own in [`static/src/forms.css`](../src/vettarr/web/static/src/forms.css), which `app.css` imports:

| Class | Use |
|---|---|
| `field-inline` | An input or a select inside a sentence: as wide as what it holds. |
| `sentence` | A row of words and fields that wraps. |
| `sev-chips` | The severity scale as radio buttons. Each chip carries its word; the chosen one carries the marker. |
| `option-card` | One of a few choices, each with a line of explanation. |
| `field-error`, `hint` | Under a field: what is wrong (with `aria-invalid` and `aria-describedby` on the field), or what it is for. |
| `stack-table` | A table that becomes one block per row under 640 px. |
| `action-bar` | The bar that holds Save: it stays at the bottom of the screen, above the nav on a phone. |
| `kv`, `state` with `state-ok`, `state-off`, `state-warn`, `state-bad` | What is set, as label and value; a mark and a word for how it stands. |
| `fold` | A `<details>` that opens on its summary. |
| `setup-title`, `env-value` | A page-level section heading on Policy and Setup (the small caps `section-title` is a card's); a value the environment sets (`${VAR}`), shown as a line, not a field. |
| `notice-info`, `notice-ok` | Two more tones of `notice`. |

**The config file.** A page that shows or changes `config.yaml` goes through [`web/configfile.py`](../src/vettarr/web/configfile.py). `load()` says what the file holds and whether the page may write it; include `_config_state.html` and the page says so too ("Managed by file" under `CONFIG_READONLY`, no file, a file that does not load, a file ahead of the running app). When `state.writable` is false, disable the fieldset, leave the write buttons out and answer a POST with `state.refusal` and a 409. To save: change `state.candidate()` with `put` or `merged` (they keep comments and `${VAR}`), `validate` it, put each error beside the field its dotted path names, then `write`. The app reads its config once, so a save ends with "Restart to apply". A secret never reaches a template: `secret_state` says set or not set.

**Toasts.** The page shows an htmx answer with a 4xx or 5xx status as a toast, with the body as its text, so answer one plain line. To show one after a success, add the headers `ui.toast("Queued The Lantern Keeper.")` returns. From a script, `showToast(text, 'ok' | 'error')`. Only the newest two show, so a few quick actions do not cover a phone's screen. A success that left something undone (a post that could not be changed) is an `error` toast: the colour says what the words say.

**Confirm dialog.** Every control that destroys something carries `data-confirm="what happens"`, and optionally `data-confirm-title` and `data-confirm-action` (the button's word). It works on any element htmx sends a request from, and on a plain `<form>`. From a script, `await confirmDialog({title, text, action})` resolves `true` only on the action button.

**Banner.** Every full page shows a line for each provider in an open cooldown, read from the `guard` events. A page adds its own line with `{% block banner %}`.

**Nav.** Inbox, Review, Policy, Setup, Stats. Under 640 px it becomes a bar at the bottom of the screen, so a page's own fixed controls go above it (the toasts already do). Inbox carries the number of items waiting for a person, read on every full page by `shell.needs_you()`. Which items those are is one query, `shell.waiting_items()`, over `cards.NEEDS_YOU`: the Inbox's "Needs you" tab and Review's queue count the same statuses. Use it wherever a page says "needs you", so the numbers agree.

**Charts.** A chart is inline SVG drawn by the template from numbers the route worked out; no script and no library. The four outcomes have their colours as tokens (`chart-approve`, `chart-review`, `chart-wait`, `chart-reject` as classes), chosen to stay apart for the common colour vision deficiencies. A chart always has a legend in words and its numbers as a table. [`stats.html`](../src/vettarr/web/templates/stats.html) is the example.

**Auth.** Every route and both static mounts are behind the gate, and a page does nothing for it. `request.state.auth` says who is signed in. The gate refuses every POST, PUT, PATCH and DELETE another site sent, so a GET must never change anything. It sends every page behind the sign-in with `Cache-Control: no-store`. It closes every websocket, because no page has one: the page that adds the first adds its session and origin check to `GateMiddleware` in [`web/auth.py`](../src/vettarr/web/auth.py). Read a request's path from `request.scope["path"]`, never from `request.url`, which is built from a header the client writes. The modes and the cookie are described under [`web` in CONFIG.md](CONFIG.md#web).

**Script budget.** [`static/app.js`](../src/vettarr/web/static/app.js) stays under 150 lines, and a test holds it there. A page's behaviour is htmx attributes first. The file is at 150 now: the keys, the swipe, the slide-over and the selection took what was left, so the next behaviour has to come as attributes or replace something.

## When you are done

```sh
kill "$(lsof -ti tcp:"$WEB_PORT")"      # your app, and only yours
```

Leave `vettarr-dev-pg` running. Drop your database when the branch is merged: `docker exec vettarr-dev-pg dropdb -U vettarr "$DB"`.
