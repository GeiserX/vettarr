# config.yaml

**Schema frozen 2026-10-11.** From here on the schema only grows: new optional fields and new blocks are fine, renaming or removing a field or changing its meaning is not. Every later milestone builds against this file.

One file, `config.yaml`, says everything the app does: where posts come from, how they are read, which trackers and services it talks to, where approved items go, and the policy that decides. The models are in [`schema.py`](../src/vettarr/config/schema.py), the policy signals in [`signals.py`](../src/vettarr/config/signals.py), and a commented starting point in [`config.example.yaml`](../config.example.yaml).

Status: the app boots from this file. At start it loads `CONFIG_PATH` when that file exists. When it does not, it builds the same config in memory from the environment variables, through the mapping `config import-env` uses, and writes nothing. One log line says which source it used (`Config: /data/config.yaml` or `Config: environment, no config file`), never a value. Without the file, a warning before that names the path it looked for (`No config file at /data/config.yaml: booting from the environment variables`), so a boot that then stops on a missing variable still says which file it did not find.

**An environment boot is not the setup a deployment had before `config.yaml`.** What used to live in code lives only in the file now, and the environment carries none of it: the card format (labels, type headers, media words, ignored hosts), tracker link patterns, download-client categories and save paths, the [release words](#release_words) of any language but English, and the keyword, genre and subject lists. With no file the app reads every post as a scene release name. A card post lands in `parse_failed`, and no message is ignored for being a note, because no format lists any [`media_words`](#formats). Every download is added with no category and no save path, so it lands in the download client's default folder. A tracker named by `TRACKER_N_*` becomes a provider with `thank` and `keep_alive` off. The environment has no variable that turns them on, so the app thanks no uploader and keeps no session alive until a file says so. Languages, subtitles and whole seasons are read with the English release words only. A post that names them in another language's words reads as naming none, so a rule on `audio_language`, `subtitles` or `season_complete` sees missing data where the release before read a value. A tracker with only `TRACKER_N_URL` takes its session from Prowlarr, has no link pattern and no direct search, and so is never contacted. The policy is the public default, not the one the deployment decided with: the four rules of `config.example.yaml`, with the switch, the type list, the vote threshold and the wait from the environment. It has no keyword, genre or subject rule and no certification rule for the operator's own country. So a film that only one of those held for review is approved, and a book type on `AUTO_APPROVE_TYPES`, which only subject rules ever checked, has no rule left and is held for a person. TMDB is asked in `es-ES`, as before. The start logs this as a warning right after the `Config:` line. Before moving a running deployment to this version, write its `config.yaml` (`config import-env` starts one), add those blocks, and boot from the file.

- An invalid file stops the start: its anchored errors on stderr, exit code 1.
- With no file, a variable the app has always needed (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`, `TELEGRAM_GROUP_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TMDB_API_KEY`, `PROWLARR_URL`, `PROWLARR_API_KEY`, `QBITTORRENT_URL`) stops it with `Missing required environment variable: NAME`, as before.
- The userbot, the bot and the web UI start as [What starts](#what-starts) says; the scheduler always runs. Each start is logged with its reason.
- A component that fails stops the app. When the userbot, the bot, the web UI, the scheduler or the download worker raises, or ends while nobody asked for a stop, the log names it and says why, the others get 30 seconds to stop their own way, and the process exits with code 1, so a restart policy acts. A stop by signal exits 0. Give the container a restart policy and a stop timeout longer than those 30 seconds: Docker's default of 10 kills the app in the middle of its stop. The repo's `docker-compose.yml` sets `restart: unless-stopped` and `stop_grace_period: 40s`.
- An empty database gets its schema at the first start. A database an older release built is migrated; one a newer release migrated is used as it is.
- Sources are read as [`sources`](#sources) says: every `userbot` source's chat, with its `backfill`; every `bot` source's chat (`drop_pending_updates` is off while one exists, so messages sent while the app was down still arrive); `allowed_senders`; `on_download` and `on_reject` on the item's own chat; `notify_on_review`; every `seerr` source's pending requests, by its webhook and its poll ([Seerr requests](#seerr-requests)). The bot answers every id in `telegram.reviewers`, and an update with no sender (a channel post, an anonymous admin) only where `allowed_senders` is `anyone`, never for a command or a button. A bot source with `delete` where the bot is not an admin allowed to delete leaves the post and logs that once. Rows written before sources existed have no source and belong to the source on their chat, or else to the first `userbot` source.
- The app's modules read the config through `settings` in [`env.py`](../src/vettarr/config/env.py), a view over [`runtime.get_config()`](../src/vettarr/config/runtime.py), resolved on first use. Until later work reads the blocks directly, the app sees the file through the fields it had before, so a provider's `key` is the tracker's name. The `policy` block is read whole by the policy engine ([How a decision is made](#how-a-decision-is-made)).

## The rules of the file

- **A component starts only when its block exists.** No `web:` block, no web UI. No source of kind `userbot`, no Telethon session. An integration block is on unless it says `enabled: false`. An empty file starts nothing. `config check` lists what would start.
- **Unknown keys are errors.** A typo (`bnid:` for `bind:`) stops the load with the line number, instead of being ignored.
- **`${VAR}` in any string is read from the environment at load.** Use it for every secret. A variable that is not set, or is empty, is an error naming the variable and the line; the message never carries a value, and no validation message echoes a value either. `$${` writes a literal `${`. In a block with `enabled: false`, every setting that holds a `${VAR}` is dropped before validation, so a disabled integration can reference a variable that does not exist yet. `enabled` may itself be `${VAR}`; it is read first, so `enabled: ${TMDB_ON}` with `TMDB_ON=false` needs none of the block's other variables.
- **Secrets are never printed.** Secret fields are `SecretStr`: a printed or dumped config shows `**********`. In the log, every handler masks the value of each secret field as `***`, and anything shaped like a Telegram bot token whether or not it is in the file, because a library can name the token it was refused with. A secret is recognised as written, as a URL encodes it, and as JSON or a Python `repr` escapes it; in any other encoding, base64 for one, it is not. A secret shorter than four characters is not masked, and one of four or five characters is masked wherever those characters appear, inside an ordinary word or a number too.
- **Every error is anchored**: `config.yaml:12: providers[0].links[1].pattern: pattern does not compile: ...`. For a missing field the line is its parent block's.
- **YAML 1.2.** `on`, `off`, `yes` and `no` are strings, not booleans. Fields that take a boolean accept them anyway, and `auto_approve` accepts `true`/`false` as `on`/`off`.

## Where the file lives, and who may write it

| Variable | Default | Meaning |
|---|---|---|
| `CONFIG_PATH` | `config.yaml` (working directory); the image sets `/data/config.yaml` | The file the app boots from, and the one `load()` and `config check` read. |
| `CONFIG_READONLY` | unset | `1`, `true`, `yes` or `on`: every writer refuses (`ConfigReadOnly`). Use it when the file is managed in git and deployed; the UI then shows the config as managed by file. |
| `DRY_RUN` | unset | `1`, `true`, `yes` or `on`: [`dry_run`](#dry_run) is on, whatever the file says. |

The web UI's Policy and Setup pages write the file through the same writer: validated first, `config.yaml.bak` kept, comments and `${VAR}` references left as they were. The app reads the file once, at start, so a change saved there takes effect at the next restart, and the pages say so until then. Under `CONFIG_READONLY`, with no file (an environment boot), or with a file that does not load, the two pages show the config and change nothing. A secret is never shown: a page says whether one is set, and which `${VAR}` it comes from when the file names one. One thing those pages write is not the config file and works under `CONFIG_READONLY` too: a provider's cookie file, from a pasted browser export. Under [`dry_run`](#dry_run) that write is refused.

These stay environment variables and are not in the file: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `LOG_LEVEL`, `TZ`.

## Commands

```
vettarr config check [path]      # validate, list what would start, print warnings
vettarr config import-env [-o path]   # today's environment variables -> config.yaml
vettarr config example           # print config.example.yaml
```

`python -m vettarr.config ...` runs the same commands. Neither needs an environment variable for `example`, nor for `check` unless the file references one.

- `check` exits 0 when valid, 1 when not (errors on stderr, one per line), 2 on bad usage. Warnings are things that are valid but probably unintended: a rule reading TMDB data with `tmdb` off, a type in mode `rules` that no rule applies to, a rule that points at an empty list, a rule that reviews or rejects placed after an `approve` rule for the same type, a web UI with no auth, a source limited to reviewers with no reviewers, a seerr source that can take no request (`integrations.tmdb` off, or `allowed_senders: reviewers`), a `webhook_secret` with no `web` block to serve the webhook, more than one seerr source, a keep-alive whose jitter is not below its interval, an empty `deliver` while the download client, Radarr or Sonarr is on, a `deliver` that leaves media types out, a delivery to Radarr or Sonarr with no `root_folder` or `quality_profile`. The two `deliver` warnings are also logged when the app starts.
- `import-env` reads `TELEGRAM_*`, `TMDB_API_KEY`, `PROWLARR_*`, `QBITTORRENT_*`, `TRACKER_N_*`, `AUTO_APPROVE_*`, `MIN_GUIDE_VOTES`, `GOOGLE_BOOKS_API_KEY`, `WEB_PORT` and `SESSION_DIR`. Secrets become `${VAR}` references, never values; so does a URL that carries a password, a query string or a fragment, since any of those can hold a token. A tracker `username` is written as a value. With `-o` it writes through the writer (refused under `CONFIG_READONLY`, validated first, `.bak` kept); without it, it prints. What lived in code rather than in the environment is not imported, and the file says where it goes: the card format, tracker link patterns, qBittorrent categories and save paths, keyword, genre and subject lists, and the release words, written as their English defaults. Each tracker's `thank` and `keep_alive` are written with `enabled: false` and UNIT3D's path, ready to turn on.
- The policy `import-env` writes is the public default (the four rules in `config.example.yaml`), with the type modes and thresholds from the environment. Those rules read films and series only, so a book type the environment allowed is written in mode `rules` with no rule that applies to it, and is held for a person until the file gives it one. `integrations.tmdb.language` is written as `es-ES`, the language the app asked TMDB in before the setting existed.

## The Python API

```python
from vettarr.config.loader import load, check, read_document, ConfigError
from vettarr.config.writer import write_document, ConfigReadOnly
from vettarr.config.components import components

cfg = load()                         # CONFIG_PATH; raises ConfigError (.errors: list[str])
cfg, warnings = check("config.yaml")
doc = read_document("config.yaml")   # round-trip document: comments, order, ${VAR} kept
doc["web"]["port"] = 8080
write_document("config.yaml", doc)   # refuses under CONFIG_READONLY; validates; writes config.yaml.bak first
```

The writer works on the round-trip document, never on a loaded `Config`, so it can never write the value a `${VAR}` stands for. A write that would not validate is refused before anything touches disk. The new file replaces the old one atomically and keeps its permissions.

## Blocks

### `dry_run`

A top-level switch, not a block: `dry_run: true`. Default `false`.

With it on, nothing outside this database is changed, whoever asks: the policy, the worker, a person clicking Approve or Reject in the web UI or the bot. `auto_approve: shadow` stops only the policy's own actions; `dry_run` stops every write. It is meant for a preview that runs beside a live instance against the same services and a copy of its database.

Every write to the outside world is not sent, and becomes a `would_have` event whose payload carries `dry_run: true`, the target, the method, the path or action, and the item's id; never a secret, a cookie or a message's text:

- the download client: adds (magnet, `.torrent`, link), deletes, and the `.torrent` it would fetch from an indexer link;
- Radarr, Sonarr and Seerr: every POST, PUT and DELETE (adds, monitoring changes, searches, requests, approve, decline, withdraw);
- Telegram, through the userbot and the bot: sending (review cards, `notify_on_review` pushes, replies), editing, deleting, reacting, answering a button;
- a tracker: the thank, the keep-alive visit, any request with a body (a login), the cookie file the session would save, and a `.torrent` fetched from it (`download.php`, a post's linked torrent). That fetch is read-only HTTP, but a private tracker counts it as a download. The item gets the reason `dry run: torrent not fetched`, the file signals (`file_count`, `file_extensions`) that need it read as unavailable for that reason, and a release only the tracker's own search found is not added;
- the Setup page's cookie upload, which writes nothing;
- the `notify` POST.

Reads stay real: the Prowlarr search, TMDB, the IMDb guide, Google Books, Jellyfin, the GET routes of Radarr, Sonarr and Seerr, the session check and the search pages of a tracker. The config file stays writable from the UI as `CONFIG_READONLY` says: that file is the instance's own.

The item still moves as if the write had happened: approved, delivered by would-have, and completed at the next download check (a delivery a dry run made reports its files at once). Every page shows a `dry run` badge in the header, every toast carries one, approving says "Would have queued ... for download", and Activity and the item's history list each would-have as "Dry run: Radarr: POST /api/v3/movie, not sent". Shadow agreement on Stats counts the policy's would-haves only.

`DRY_RUN=1` (or `true`, `yes`, `on`) in the environment turns it on whatever the file says, an environment boot included; it cannot turn it off. `config check` lists it first among what would start, as `dry run: nothing outside this database is changed`, and the start logs it as a warning.

### `telegram`

Needed by `userbot` and `bot` sources.

| Field | Type | Default | Notes |
|---|---|---|---|
| `api_id` | int | none | From my.telegram.org. Usually `${TELEGRAM_API_ID}`. |
| `api_hash` | secret | none | |
| `phone` | secret | none | Only asked for when the userbot session has to log in again. |
| `session_path` | string | `/data/sessions/vettarr` | Telethon session file, without `.session`. |
| `bot_token` | secret | none | From @BotFather. Its presence starts the bot. |
| `reviewers` | list of int | `[]` | Telegram user ids allowed to approve, reject and use the bot. |

### `sources`

A list. Each entry is one place posts come from.

| Field | Type | Default | Notes |
|---|---|---|---|
| `key` | key | required | Lowercase letters, digits, `-`, `_`. Unique. |
| `kind` | `userbot` \| `bot` \| `seerr` | required | |
| `chat` | int \| `@username` \| `private` | required for userbot and bot | `private` (bot only) means direct messages to the bot. A quoted number is read as a number. A seerr source has no chat. |
| `backfill` | `off` \| `since_last` \| `full` | userbot `since_last`, others `off` | Only a userbot can read history. `since_last` reads what came after the last message ingested from that chat (everything, on a first run); `full` re-reads from the first message at every start, at the cost of one database query for each message that is already an item. |
| `formats` | list of format keys | `[]` = every profile, in file order | |
| `allowed_senders` | `reviewers` \| `anyone` \| list of user ids | bot `reviewers`, others `anyone` | For a seerr source the ids are Seerr user ids. |
| `direct_adds` | `reviewers` \| `anyone` \| `off` | userbot and bot `reviewers`, seerr `off` | Whose own torrent (a `.torrent` attachment, a magnet) is downloaded as it is. See [Direct adds](#direct-adds). |
| `on_download` | `delete` \| `react` \| `none` | `none` | What happens to the post once the item is downloaded: once the download client holds its torrent, or once Radarr, Sonarr or Seerr has taken the title or already has its files. A seerr source must use `none`. |
| `on_reject` | `delete` \| `react` \| `none` | `none` | Same, on reject. The web UI and the bot answer a rejection with what was done to the post. |
| `notify_on_review` | bool | `false` | Push each item held for review to the reviewers. |
| `poll_minutes` | int ≥ 1 | seerr `5` | Seerr only: how often pending requests are polled besides the webhook. |
| `webhook_secret` | secret | none | Seerr only: what Seerr's webhook sends as its `Authorization` header. At least 16 characters of plain ASCII, with no space at either end and not starting with `Bearer `; use `${VAR}`. Without it the source has no webhook route, and the poll alone reads it. See [Seerr requests](#seerr-requests). |

Cross-checks: a userbot source needs `telegram.api_id` and `telegram.api_hash`; a bot source needs `telegram.bot_token`; a seerr source needs `integrations.seerr`; every name in `formats` must exist. `poll_minutes` and `webhook_secret` on a source of another kind are errors.

#### What a source reads

Every message of a userbot or bot source that `allowed_senders` lets in is read as the first of these it fits:

| The message | What becomes of it |
|---|---|
| A `.torrent` attachment, up to 20 MB | An item named by the torrent's name. A [direct add](#direct-adds) when `direct_adds` allows its sender. A file that is not a torrent is not read. |
| A `magnet:` link anywhere in the text | An item named by the magnet's display name (`dn`). A direct add when `direct_adds` allows its sender. |
| A post one of the source's formats reads | Stored as written, and the parser reads it. |
| A link to IMDb, TMDB or TVDB | TMDB names the title and the type. This needs [`integrations.tmdb`](#integrations). Without it the parser reads the message. |
| A link a provider's [`links`](#providers) pattern reads | The app fetches the linked `.torrent` with the provider's session, and its name is the title. |
| One short line with no link, at most 8 words | Looked up on TMDB as a title. TMDB must know a film or a series by exactly that name, and within a year of the year written. In a group the line needs a year (`Some Film 2024`). In the bot's private chat every short line is looked up, so `Yes` typed there asks for a title called *Yes*. |
| Anything else | Stored as written. The parser reads it, and it lands in `parse_failed` or is ignored as a note. |

A message with no text and no `.torrent`, or one that is only an Instagram link, is not an item.

Before it downloads or looks up anything, the app asks its database whether the message is already an item. Reading a history again therefore sends nothing to TMDB or to a tracker for the messages already read.

**Forwards.** A forwarded channel post stands for the original. When the original is already an item, read by another source or forwarded before, the forward is not a second one. A forward belongs to whoever forwarded it. That is the sender `allowed_senders`, `direct_adds` and the `sender` signal see.

**New messages and history.** The userbot takes new messages from the moment the app starts. Each waits until the app has read the history of its own chat, and the app ingests a chat's messages one at a time, in order. It tries a message it cannot read three times, a minute apart, then logs it and skips it. One bad message does not stop the history read or the app.

**Photos.** The app keeps a post's photo in `/data/photos` for the review card and the web UI.

**Basic groups.** In a basic group (not a supergroup) the userbot and the bot number messages differently. Read a basic group through one source only, a userbot source or a bot source. Supergroups and channels have one numbering and can be read by both.

#### Direct adds

A `.torrent` attachment or a magnet is the one case where the download does not come from the operator's own indexers and trackers. The policy judges the name the sender gave the torrent, and the download client receives whatever that torrent holds. `direct_adds` says whose torrents the app takes that way:

- `reviewers` (the default): one sent by an id in [`telegram.reviewers`](#telegram).
- `anyone`: one sent by any sender the source lets in.
- `off`: none.

From anyone else the message is still an item, under the same name, and the app searches for it like any post. An item with no recorded sender (a channel post, an anonymous admin, a row from before the sender was stored) counts only under `anyone`.

Nothing reaches the download client before the item is approved, by the policy or by a person. The bot's review card says when approving will add the post's own torrent instead of a search result.

Only the download client takes a torrent as it is. For a type [`deliver`](#deliver) sends to Radarr, Sonarr or Seerr, the item is delivered by its title and the post's own torrent is not used: see [Direct adds and linked torrents](#direct-adds-and-linked-torrents).

A `.torrent` waits in `/data/torrents/<item id>.torrent` until the item is added or rejected, and the app then removes it. Mount `/data/torrents` as a volume. When the file is gone, the app searches for the item instead. The file rules (`file_count`, `file_extensions`) read that file. A magnet names a torrent without listing its files, so those two signals are missing for a magnet.

#### Seerr requests

Seerr has users who ask and admins who approve, and no rule that approves. A source of kind `seerr` makes the app the one who decides. Each request that waits for approval in Seerr becomes an item, the [policy](#policy) decides it like any other item, and the app carries the decision out in Seerr.

```yaml
sources:
  - key: requests
    kind: seerr
    webhook_secret: ${SEERR_WEBHOOK_SECRET}
    poll_minutes: 5
integrations:
  seerr: {url: http://seerr:5055, api_key: ${SEERR_API_KEY}}
  tmdb: {api_key: ${TMDB_API_KEY}}
```

**Which requests reach it.** Only a request Seerr creates as pending. Seerr approves a request at once when its user has an Auto-Approve permission or Manage Requests, and that request never waits for anyone. So take the Auto-Approve permissions away from the users whose requests the policy should decide. `allowed_senders` narrows it further. A list of Seerr user ids takes only those users' requests and leaves the rest pending for a person in Seerr. `reviewers` names Telegram users, so it takes nothing, and `config check` warns. A request the app made itself for [`deliver: seerr`](#deliver), which it asks as `integrations.seerr.user_id`, is not taken, because its own item already waits on it. Give `user_id` a Seerr user of its own.

**The API key.** `integrations.seerr.api_key` is Seerr's own key (Settings, General, API Key). Seerr treats a call with it as its admin, who has Manage Requests, the permission that approving and declining need. The app reads the pending list, and approves and declines, with the key alone. It still creates the requests of `deliver: seerr` as `user_id`, sent as `X-API-User`.

**What to paste in Seerr.** Settings, Notifications, Webhook:

| Seerr's field | Value |
|---|---|
| Webhook URL | `https://<the app's address>/hooks/seerr/<source key>`, so `/hooks/seerr/requests` for the source above |
| Authorization Header | The value of `webhook_secret`, as it is. `Bearer <secret>` works too. |
| JSON Payload | The template below. Seerr's default template works unchanged as well. |
| Notification Types | Request Pending Approval. Add Request Available, Request Declined and Request Processing Failed, and the item follows its request at once instead of at the next check. |

```json
{
  "notification_type": "{{notification_type}}",
  "request_id": "{{request_id}}"
}
```

The app reads two things from the body. One is the notification type. The other is the request's id, at the top as above or under `request.request_id`, where Seerr's default template puts it. Everything else it reads from Seerr's API by that id: the title's TMDB id, the seasons, whether the request is 4K, and who asked. The webhook has no variable for the requester's id, which the `sender` signals need. Reading the request back also means a call can only point at a request that really is pending in Seerr.

The route is outside the login, because Seerr has no session to show. `webhook_secret` is what lets a call in, and the app compares it in constant time against the `Authorization` header. A wrong or missing header gets 401. A source with no `webhook_secret` has no route at all and answers 404, so a hook is never open without a secret, and the poll alone reads that source. Write at least 16 random characters and keep them in an environment variable. The web UI serves the route, so it needs a [`web`](#web) block; without one `config check` warns.

| Notification | What the app does |
|---|---|
| `MEDIA_PENDING` | Makes the item of the request, unless it is one already. |
| `MEDIA_AVAILABLE`, `MEDIA_DECLINED`, `MEDIA_FAILED` | For a request the app holds, it asks Seerr about the request and updates the item now. When Seerr cannot be asked, the answer says `deferred` and the next check does it. For any other request, nothing. |
| `TEST_NOTIFICATION` (Seerr's Test button) | Answers 200 and writes one `webhook_test` event. |
| Anything else | Answers 200 and does nothing. |

**The poll.** Every `poll_minutes` the app reads `GET /api/v1/request?filter=pending`, page after page, and takes every pending request that is not an item yet. A request is one item whichever way it arrived, so the webhook and the poll never make two. The first poll runs 20 seconds after the start. The poll also hands back to the pipeline an item that was written and never decided, because the app stopped in between or because the web UI running on its own took the webhook. So a pending request is never left without a decision.

**What the item is.** The title, the year and the type come from TMDB by the request's TMDB id, so this needs [`integrations.tmdb`](#integrations). The content check reads the same record by that id and never searches by name. The requester's Seerr user id is the item's sender. That is what `sender` and `per_sender_per_week` see, so a `sender` rule that lists `7` names Seerr user 7. The source's key is what the `source` signal sees. A series request carries its seasons. The item's text names the requester by that id and never by display name. Seerr users write their own display name, and the policy reads an item's text for release signals, so a name must not be able to set one.

**What the decision does.** [`deliver`](#deliver) is not read for these items. Their delivery is Seerr itself.

| The policy says | The call to Seerr | The item |
|---|---|---|
| approve | `POST /api/v1/request/<id>/approve` | Waits for Seerr's files exactly as a delivery to Seerr does. It completes when Seerr says the title is available, or each season asked for, or the 4K copy for a 4K request. |
| reject | `POST /api/v1/request/<id>/decline` | Rejected. |
| review | None. The request stays pending. | Held in the Inbox like any other. A reviewer's Approve or Reject makes the same two calls. |
| wait | None. The request stays pending. | Waits for data, and the hourly re-check decides it again. |

Under `auto_approve: shadow` the policy decides, writes a `would_have` event and calls nothing in Seerr. The item is held for a person, whose own Approve or Reject does reach Seerr. Under `off` every request is held.

**Decided in Seerr.** When somebody approves, declines or deletes the request in Seerr, the app does not argue. The item takes that decision, with a reason that starts `decided in Seerr`. A declined or deleted request rejects the item, and for an approved one the item waits for Seerr's files. The app sends nothing to Seerr in either case. It notices at the next poll. With the Request Declined notification on it notices a decline at once. An approval made in Seerr is not acted on from the webhook, and Seerr sends no notification for a deleted request, so both wait for the poll.

The app reads a request before it declines it, and declines only a pending one. So rejecting an item whose request is already approved leaves the request as it is, and the answer says so. Seerr 3.5.0 and later refuse such a decline themselves. Older versions carry it out, which is why the app reads first; with one of those, an approval made in Seerr in the instant between the read and the decline can still be overwritten. Seerr does not approve a declined request either, so the user has to ask again. For the same reason a request the policy rejected is not run through the policy again: Rerun policy is not offered for it.

**When Seerr's ids start again.** Seerr numbers its requests from 1 again after a reinstall, after an older backup is put back, or when `integrations.seerr.url` points at another Seerr. An item is its request by the id, the title's TMDB id and the request's date together. When a pending request has the id of an item that was made from another request, the app takes it as a new item and decides it on its own. The old item approves and declines nothing in Seerr any more. If it still waited for a person, it is rejected with the reason `decided in Seerr: its request was removed there`.

**When Seerr does not answer.** An approval that only this app knows of is not one. The item stays `approved` and says that Seerr could not be told, and the app tries again after 30 seconds, then 60, doubling up to 30 minutes, for as long as it takes. The app never marks the item as on its way and never drops it. The app writes one `delivery_failed` event per outage. The next poll that reaches Seerr makes a decline that could not be sent, because it finds the request still pending beside a rejected item. A decline that keeps failing is sent again at every poll, and writes one `delivery_failed` event per problem, not one per poll. A refused API key is not an outage. That item comes back to a person with the reason, and the request stays pending.

A 404 for a request counts as removed only when Seerr itself then answers and still has no such request. A reverse proxy that answers 404 for every path while Seerr restarts is an outage like any other, and no item is rejected for it.

**Two traps.**

1. With the API key alone Seerr acts as its admin, and an admin's own request is approved at once. So the app never creates a request without `X-API-User`, and it approves and declines without it. The first would skip approval, and the second needs the admin's permission.
2. Seerr sends each webhook once, ignores the answer and never sends it again. A call that did not arrive is lost. The poll is what guarantees a request is seen, and the webhook only makes it sooner, so do not raise `poll_minutes` because the webhook is on.

### `formats`

A list of post formats, as word lists. The app compiles the words into patterns once, at load; nothing about any group's wording lives in code. Words match case-insensitively, anywhere in the post, and a space inside a word matches any run of whitespace. A word never matches inside a longer word: `Title` does not match `Subtitle`. A word written between slashes, `'/Films?\s*WEB/'`, is a regular expression, taken as written, for the shapes a list of words cannot write; one that does not compile is an error anchored to its line. So is a list whose words compile one by one but not together, which happens when two words write the same group name, and so is a group named like one of the app's own (`cat`, `rest`, `quality`, `source`, `size`, `value`). An inline flag such as `(?i)` is an error too, and never needed, because every word already matches case-insensitively. Any other group inside a word is fine and changes nothing about what is read.

Three ways a literal word fails to match:

- Glued to a letter, a digit or an underscore. A label wrapped in single underscores (`_Title:_`) is not found, so write that shape as a regular expression.
- Written with its colon. A label is `Title`, not `Title:`, because the app reads the colon itself.
- Spelled another way. A word matches its exact spelling only, so for a word with an accent, list the spelling without it as a second word when posts carry both.

| Field | Type | Default | Notes |
|---|---|---|---|
| `key` | key | required | Unique. |
| `kind` | `card` \| `scene` | `card` | `card`: labelled lines (`Title: ...`). `scene`: a release name on the first line (`Some.Film.2024.1080p.WEB-DL.x264-GROUP`), read by the built-in scene parser (quality, source, size, group, SxxEyy, year, `[PACK]`). |
| `type_headers` | media type → words | `{}` | A line holding one of these decides the media type outright. |
| `type_hints` | media type → words | `{}` | Weaker evidence: phrases that suggest a type anywhere in the post. |
| `category_words` | media type → words | `{}` | Category names as written in a header or a type line. A category that opens a line, after nothing but symbols, a header marker or a type label, and is followed by an arrow or a dash (`Movies ->`, `Type: Series -`) also hints its type. The same word inside a title (`Some Show - 1998`) hints nothing. |
| `header_markers` | words | `[]` | Markers that open a header line: `<marker> <category> -> <source>`. The category gives the type and what follows the arrow is the release source (`WEB-DL`); the title comes from the release name, which is on a line of its own. A line that opens with a marker is metadata, never the title. |
| `fields.title` / `fields.link` / `fields.year` / `fields.type` | words | `[]` | Labels of the lines a card carries. A type line reads `<category> - <quality> - <source> - <size>`, on a card with a title line as well as under a release name. |
| `ignore_line_prefixes` | words | `[]` | Lines starting with one of these are metadata, never the title. A header marker need not be repeated here. |
| `ignore_hosts` | words | `[]` | A post whose only link is to one of these hosts is not media. A message that is nothing but an Instagram link is dropped at intake by the app itself, whatever this list says. |
| `media_words` | words | `[]` | Whole words that name a kind of media (`film`, `book`). A message with no release structure that holds one is a request in plain words: it stays reviewable instead of being ignored. When no format of a source lists any, the app cannot tell a request from a note, so every message with words in it stays reviewable and only a lone link is ignored. |

`kind: scene` reads `type_headers`, `type_hints`, `ignore_line_prefixes`, `ignore_hosts` and `media_words`; the card-only settings (`fields`, `header_markers`, `category_words`) are ignored there.

How a post is read: it is tried against each format of its source in order, and the first format that reads a title and a media type wins. A source's formats are the profiles its `formats` lists, in that order; a source that lists none uses every profile in file order. When none does, the item lands in `parse_failed` with what the first format read. So does a post whose only candidate for a title is a size or a freeleech line. Within one format the type is decided in this order: a `type_headers` word (the non-video types only when the post carries no quality plus source); the category on a type line; a series signal (`SxxEyy`, `[PACK]`, a series hint); the other hints, film first; the category on a header line; a quality plus a source (a film, or a series with `Sxx`); a short `Author - Title` line (an ebook). Quality, source, size, release group, `SxxEyy`, `(YYYY)` and `[PACK]` are read by the app for every format.

A release name with no space in it, its words joined by dots (`Some.Film.2024.1080p.WEB-DL.x264-GROUP`), is read in every format when it is the whole of the post's first line: the name ends at the quality or the season (at the source when it has neither), the last year before that is the year, and what stands before the year is the title, `Some Film`. With no year after it, a year-like word of the title is taken as the year (`Blade.Runner.2049.1080p...` reads as *Blade Runner*, 2049). A dotted name on a card's title line, or followed by anything else on its line (`Some.Film.2024.1080p.WEB-DL.x264-GROUP (4.2 GB)`), is not split. A name with spaces is read as before.

Media types everywhere in this file: `movie`, `series`, `audiobook`, `ebook`, `magazine`, `comic`, `music`.

### `providers`

A list of trackers the operator has an account on. Each entry starts the provider: its session guard, and its thank and keep-alive jobs when those are on.

| Field | Type | Default | Notes |
|---|---|---|---|
| `key` | key | required | Unique. |
| `base_url` | http(s) URL | required | |
| `aliases` | list of hosts | `[]` | Other hosts of the same site, as `host` or `host:port`. Credentials and cookies are only ever sent to `base_url`'s host and these. |
| `auth.from` | `prowlarr` \| `cookie_file` \| `login` | `prowlarr` | `prowlarr`: use the cookie in the matching Prowlarr indexer (needs `integrations.prowlarr`). `cookie_file`: a Netscape cookie file. `login`: log in with username and password, keeping the session in `cookie_file` when given, and in memory until the next restart when not. One login serves every later request until the session dies. |
| `auth.cookie_file` | path | none | Required for `cookie_file`. |
| `auth.username` / `auth.password` | string / secret | none | Required for `login`. |
| `links` | list | `[]` | Turns a details link in a post into the exact torrent, with no search. |
| `links[].pattern` | regex | required | Must compile and have a named group `(?P<id>...)`. An id is letters, digits, `-` and `_`. The id goes into the download URL and the thank request as it is, so a link whose `id` reads as anything else (a slash, a dot, a `?`) is ignored. Write the group as narrowly as the tracker's ids are (`\d+`, `[0-9a-f]{40}`). |
| `links[].download_url` | template | required | Knows `{base}` and `{id}`; `{id}` is required. |
| `links[].examples` | list of `{url, id}` | at least one | Checked at load: the pattern must match each `url` and read exactly `id`, and `id` must be letters, digits, `-` and `_`. |
| `search` | `none` \| `unit3d` \| `xbtit` \| `nexusphp` | `none` | The tracker's software. Direct search on the tracker after Prowlarr finds nothing, and the login form `auth.from: login` fills in. Works with every `auth.from`; with `prowlarr` it uses the cookie Prowlarr holds. `login` with `none` is valid, and `config check` warns: there is no login form to fill in. |
| `logged_out_marker` | string | `name="password"` | Text that appears on a page only when the session is logged out. The default is the login form's password field, the same in every language. |
| `thank.enabled` | bool | `false` | Thank the uploader once a download this app added starts. |
| `thank.path` | template | none | Required when enabled; starts with `/`; `{id}` is the torrent id. |
| `thank.method` | `GET` \| `POST` \| `PUT` \| `LIVEWIRE` | `POST` | A `POST` or `PUT` carries the CSRF token of the page the session check read, as `X-CSRF-TOKEN`, when it has one. `LIVEWIRE` loads `path` and presses its Livewire `store` button: UNIT3D thanks only that way. |
| `thank.body` | string or mapping | none | Request body, sent as a form; `{id}` is substituted. |
| `keep_alive.enabled` | bool | `false` | Visit the site on an interval to keep the session alive; the first visit is a minute after start. |
| `keep_alive.every_minutes` | int ≥ 5 | `360` | |
| `keep_alive.jitter_minutes` | int ≥ 0 | `0` | Each wait is `every_minutes` plus or minus a uniform random value up to this, never below 1 minute. `0` is the fixed interval. Keep it below `every_minutes`: with a wider jitter many waits are cut to that minute and the visits come back to back, and `config check` warns. |
| `keep_alive.path` | string | `/` | Starts with `/`. |
| `guard.error_limit` | int ≥ 1 | `3` | Consecutive error responses (400 and above, not 429) that stop all contact with the tracker for a while. A failed login or a 429 stops it at once. |
| `guard.backoff_minutes` | int ≥ 1 | `15` | The first pause; each further failure doubles it. |
| `guard.max_backoff_minutes` | int ≥ 1 | `360` | The longest pause, and the cap on a `Retry-After`. |

How a provider is used:

- **Hosts.** A link is the provider's when its host is `base_url`'s or one of the `aliases`, compared exactly. The scheme is ignored and a subdomain is another host. The session (cookie file, login, or Prowlarr's cookie) travels less far. The app sends it only to those hosts, on the port `base_url` or the alias names (the scheme's own port when none is written), and over https unless `base_url` itself is `http://`. A redirect to another host, to `http://` or to another port leaves without the cookie and without the CSRF token. A request that carries a body (the login form, a thank) is not sent there at all, and the login or the thank fails instead. With `auth.from: prowlarr`, the cookie is the one of the Prowlarr indexer whose base URL is on one of those hosts, and the app sends it to all of them.
- **Links.** A post link on one of the provider's hosts that a `links` pattern reads becomes `download_url` with `{base}` and `{id}` filled in, fetched with the provider's session and added as a file. Nothing is searched then. Any other link is ignored.
- **Search.** Prowlarr is asked first when `integrations.prowlarr` is on. When it finds nothing, each provider with a `search` is asked in file order, with the item's IMDb and TMDB ids when it has them. The worker, the web UI and the bot run this one search with the same input. With no Prowlarr and no provider that searches, the item shows `No search is configured`.
- **Thank.** After a download this app added, from a post link, a direct search, or a Prowlarr release whose details link is on the provider's hosts and read by a `links` pattern.
- **Cooldown.** The app leaves a tracker whose session failed alone for `guard.backoff_minutes`, doubling on each further failure. A new session lifts that at once. That is a cookie file whose contents changed or, with `auth.from: prowlarr`, a different cookie in the Prowlarr indexer (the app re-reads the indexer list every five minutes). A pause the tracker asked for with a 429 is never shortened.
- Every request to a tracker says `User-Agent: vettarr/<version> (+https://github.com/GeiserX/vettarr)`.

`config.example.yaml` carries commented templates for xbtit, NexusPHP and UNIT3D, named by software. Some trackers forbid automation; thank and keep-alive are off unless turned on.

### `integrations`

Services the operator runs. Each block is optional; a block that exists is on unless `enabled: false`. Required fields are only required while the block is on.

| Block | Fields (default) | Required when on | Starts |
|---|---|---|---|
| `prowlarr` | `url`, `api_key`, `categories` (media type → list of Prowlarr category ids, `{}`; a type not listed sends its standard Newznab category, `[]` sends none and searches all) | `url`, `api_key` | Search through Prowlarr; cookies for `auth.from: prowlarr`. |
| `qbittorrent` | `url`, `username` (`admin`), `password`, `categories` (media type → category, `{}`), `save_paths` (media type → path, `{}`); a type not listed is added with no category or save path | `url` | Delivery to qBittorrent, download progress, duplicate check. |
| `radarr` | `url`, `api_key`, `root_folder`, `quality_profile` (name or id) | `url`, `api_key` | Delivery of movies to Radarr. Adding a movie needs `root_folder` and `quality_profile` too; `config check` warns when `deliver` uses the block without them. A profile name is looked up in Radarr without case. |
| `sonarr` | same as radarr | `url`, `api_key` | Delivery of series to Sonarr, version 4. The series is found by its TVDB id, which TMDB gives, so it needs `tmdb` on. |
| `seerr` | `url`, `api_key`, `user_id` (the Seerr user requests are made as, sent as `X-API-User`) | `url`, `api_key`; `user_id` when `deliver` uses seerr | Delivery to Seerr and [`seerr` sources](#seerr-requests). Without `user_id` a request made with the API key would be approved as the admin. A seerr source needs no `user_id`. |
| `tmdb` | `api_key`, `language` (`en-US`) | `api_key` | Title metadata: the `title` signals. TMDB answers in `language`: the title the post is compared with, and the genre names a `genre` rule can name. |
| `imdb_guide` | `min_votes` (`20`) | none | The parents guide: the `imdb.*` signals. On without a block, because it needs no key; it runs only while a rule reads an `imdb.*` signal. A severity voted by fewer than `min_votes` people counts as missing. |
| `google_books` | `api_key` | none | Book catalogue lookups. Without a key the shared anonymous quota is usually spent. |
| `jellyfin` | `url`, `api_key` | `url`, `api_key` | Library lookups (`in_library`). |

The IMDb parents guide comes from IMDb's undocumented website API. IMDb's terms forbid automated use without its consent: it is for a private, single-user instance at the operator's own risk, and it can stop working without notice. The app never proxies or redistributes it. It sends no browser User-Agent, Origin or Referer, only the client-name header the API needs, and asks at most once every two seconds.

`in_library` asks Jellyfin for items named like the title and counts one only when its TMDB provider id is the matched record's TMDB id; a name alone never counts. Only a film or a series has a TMDB id, so the signal applies to those two types. When the type is delivered to Radarr or Sonarr, that library is asked as well: Radarr for the movie with a file, Sonarr for every episode file of the seasons the post names. Either library saying yes is a yes, and Jellyfin need not be on.

The API key of Radarr, Sonarr and Seerr travels in the `X-Api-Key` header, never in an address, so it is in no URL the app logs.

### `deliver`

Where an approved item goes, per media type: `qbittorrent`, `radarr` (movie only), `sonarr` (series only), `seerr` (movie and series), or `none`. A type that is not listed is not delivered. The target integration must be on.

List every type you receive. `none` is a decision: the app records the approval and fetches nothing. A type the map leaves out is treated as a gap in the file: an approved item of that type comes back to a person in status `error` with the reason, and can be approved again once the type is listed. `config check` and the start of the app both warn about the types a map leaves out.

```yaml
deliver:
  movie: radarr
  series: sonarr
  ebook: qbittorrent
```

The app hands an item to its target once the policy or a person approves it.

| Target | What approving does | Completed when |
|---|---|---|
| `qbittorrent` | The app adds one torrent to the download client: the post's own ([direct add](#direct-adds)), else the one the post links to, else the best release of the [search](#providers). It thanks the uploader where the provider says so. | The download client reports the torrent at 100%. |
| `radarr` | The app looks the movie up by its TMDB id and adds it to `root_folder` with `quality_profile`, monitored. Radarr then runs its own search. | Radarr says the movie has a file (`hasFile`). |
| `sonarr` | The app looks the series up by its TVDB id and adds it with the seasons the post names monitored. `S02` names season 2, and so does `S02E05`; `S00` names the specials. Sonarr then searches for the missing episodes. A post that names no season monitors every season but the specials. | Every aired episode of those seasons has a file, by Sonarr's season statistics. |
| `seerr` | The app asks Seerr for the TMDB id as `integrations.seerr.user_id`, with the seasons the post names, or all of them. Seerr does the rest with its own approval and its own Radarr or Sonarr. | Seerr says the title is available (media status 5), or each season asked for is. |
| `none` | Nothing is fetched or added. The app records the decision and completes the item with the reason `not delivered`. | At once. |

- **Already there.** The app does not deliver a movie Radarr holds with a file, a series whose seasons Sonarr holds complete, or a title Seerr calls available. It completes the item with the reason `already in the library`. A movie or series that is there without its files counts as delivered. The item waits on the entry that exists, and the app turns its monitoring on and starts a search when it was off. When Seerr already has an open request (pending or approved) for the title, or for the seasons the post names, the item waits on that one. When Seerr takes no request and has no open one, the item comes back to a person: Seerr refuses a series whose seasons all have a status there already, partly available included.
- **No guessing.** The app completes a delivery to Radarr, Sonarr or Seerr only when the target says the files are there, however long that takes. It asks every five minutes, after it has read the download client's torrents. A target that does not answer, or refuses the API key, is asked again at the next check, once per check however many items wait on it. After 12 checks in a row without an answer, which is an hour, the item comes back to a person.
- **When the target says no.** The item comes back to a person in status `error` with the reason on it, and the app writes a `delivery_failed` or `declined` event and a `needs_you` event. That covers a refused API key, a timeout, a TMDB or TVDB id the target does not know, a missing `root_folder` or `quality_profile`, an entry removed or unmonitored there before its files arrived, a Seerr request that was declined, removed or failed, and a target that stopped answering for an hour. Approving the item again delivers it again.
- **The title must be known.** Radarr, Sonarr and Seerr take an id, not a name. The app does not deliver an item with no TMDB match to them. When the policy doubts the match, for example because the year differs or it is a sequel or a remake, the policy's own approval is not enough: the item comes back to a person, and the review card and the item page say that approving sends that match. A person's approval then sends it, and the item carries the reason `sent on a person's approval`.
- **A type that could not be read** is `unknown` and has no entry of its own. A person can still approve such an item. It goes to the download client when at least one type is delivered there. Otherwise it is not delivered and comes back to a person, as a type the map leaves out does.
- **Picking a release by hand**, from the release list on the item page or in the bot, adds that torrent to the download client. So it works only for a type delivered to `qbittorrent`. For any other target the app refuses and says why.

[`on_download`](#sources) acts on the post when the download client is seen downloading the torrent, and for Radarr, Sonarr and Seerr only when the target has the files. Until then the post stays, with any torrent it carries, because the target can still decline the title or never find a release. It does not act for `none`.

#### Direct adds and linked torrents

Radarr, Sonarr and Seerr take a title and find their own release. For a type delivered to one of them, a [direct add](#direct-adds) and a torrent the post links to cannot arrive as they are. The app delivers the item by its title instead, writes the reason `delivered by title` on it, and removes the kept `.torrent` once the target has the files. The review card and the item page say so before anyone approves the item. To download exactly what a post carries, deliver that type to `qbittorrent`.

#### Rejecting a delivered item

A person can reject an item until its files are there. The target then sees this:

| Target | On reject |
|---|---|
| `qbittorrent` | The app removes the torrent this item added, with its files, unless another torrent shares those files. It leaves a torrent the download client already held. |
| `radarr` | The app sets the movie to unmonitored with a `PUT` that carries `monitored: false`. It never deletes the entry or its files. |
| `sonarr` | The app sets to unmonitored the seasons this item's delivery turned on, and the series itself only when the delivery added it or turned its monitoring on. It deletes nothing. |
| `seerr` | The app deletes the request while it is still pending. An approved request stays, and the answer says so. |

The app changes Radarr, Sonarr and Seerr only where this item's own delivery made the change, which means it added the entry, turned its monitoring on, or made the request. It leaves a movie that was already monitored there, and a request someone else made. In Sonarr it goes flag by flag: a series you were monitoring yourself stays monitored, and so do the seasons that were on before the item arrived.

The answer to the rejection says what happened at the target, on the web as in the bot, and the item page keeps the sentence. When the target could not be told, it still has the title and keeps fetching it: the answer says so and the app writes a `delivery_failed` event.

Each integration has a connection test for the Setup page. Radarr and Sonarr answer `GET /api/v3/system/status` and Seerr answers `GET /api/v1/settings/about`, each only to a valid key.

### `policy`

| Field | Type | Default | Notes |
|---|---|---|---|
| `auto_approve` | `off` \| `shadow` \| `on` | `off` | `off`: every item waits for a person. `shadow`: decides and records what it would have done, acts on nothing. `on`: acts. |
| `min_votes` | int | unset | Default minimum votes for voted rules. Unset falls back to `integrations.imdb_guide.min_votes`. A rule's own `min_votes` wins over both. |
| `types` | media type → type policy | `{}` | A type not listed gets the defaults below. |
| `types.<t>.mode` | `rules` \| `always` \| `never` | `rules` | `always`: approve without rules. `never`: always held for a person. `rules` with no rule that applies to the type holds it for a person too: nothing was checked, and `config check` warns. |
| `types.<t>.when_data_missing` | `wait` \| `review` \| `approve` | `wait` | What happens while a rule's data is not in yet, unless the rule's own `when_missing` says. A wait on IMDb votes alone runs from the day after the title came out (see the decision order). |
| `types.<t>.max_wait_hours` | int | `24` | After this long waiting, the item goes to review. |
| `lists` | name → words | `{}` | Named lists for `matches_any`, `any_of` and `none_of`. A rule that points at an empty list never hits, and `config check` warns. |
| `rules` | list | `[]` | Evaluated in order. |
| `scopes` | list | `[]` | A different policy for the items from some sources, senders or hours. See [Scopes](#scopes). |

A **rule** checks one signal with one operator against one value, or combines several such checks (see [Combining conditions](#combining-conditions)). It *hits* when its condition is true, and then `on_hit` applies.

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | key | required | Unique; shown as the reason. |
| `name` | string | none | Human label. |
| `types` | media types | `[]` = every type the signal applies to | A type the signal does not apply to is an error. A combined rule applies to the types all its signals share. |
| `check` | signal name | required unless the rule is a group (`all`, `any`, `not`) | Must be in the catalogue below. An unknown name is an error that lists every known signal. A title signal may be written with a `tmdb.` prefix (`tmdb.keyword`) and a books signal with `book.` (`book.subject`). |
| one operator | see below | required | Exactly one, and one the signal takes. |
| `on_hit` | `approve` \| `review` \| `reject` \| `notify` | `review` | `reject` never deletes the post. |
| `when_missing` | `wait` \| `review` \| `approve` | the type's `when_data_missing` | What this rule's missing data does to the item: `review` holds it for a person now, `wait` waits for the data, `approve` holds nothing (the missing data is still recorded as a reason). On a combined rule it covers the whole rule. |
| `split_vote` | 0 < x ≤ 1 | none | Voted signals only: also hit when this share of the votes sits at or above the threshold, even if a milder label won. |
| `min_votes` | int | none | Voted signals only. |
| `all`, `any`, `not` | conditions | none | In place of `check` and its operator: the rule combines conditions. See below. |

Operators: `at_least`, `at_most`, `between` (`[low, high]`), `is` (true/false), `any_of`, `none_of`, `matches_any`. The last three take a list, or the name of a list under `policy.lists`. `any_of` and `none_of` compare whole values, without case or accents. `matches_any` finds a listed word or phrase inside a value as whole words, and reads punctuation as a space, so `sea monster` matches `sea-monster`.

#### Combining conditions

A rule may combine conditions instead of making one check. `all` takes a list and holds when every condition in it holds; `any` takes a list and holds when one does; `not` takes one condition and holds when it does not. A condition is a check (`check`, one operator, and `split_vote` or `min_votes` on a voted signal) or another `all`, `any` or `not`, nested as deep as needed. `id`, `name`, `types`, `on_hit` and `when_missing` stay on the rule; `split_vote` and `min_votes` go on the voted check they belong to, never on the rule itself.

```yaml
rules:
  - id: old-and-rough
    name: Pre-2000 films with rough content
    all:
      - check: year
        at_most: 1999
      - any:
          - check: imdb.violence
            at_least: moderate
            split_vote: 0.4
          - check: imdb.nudity
            at_least: moderate
    on_hit: review
  - id: not-english-and-big
    all:
      - not: {check: original_language, any_of: [en]}
      - check: size_gb
        at_least: 20
    on_hit: reject
```

A condition whose data is missing is neither true nor false. A combined condition is settled whenever its known parts settle it:

| | True | False | Missing |
|---|---|---|---|
| `all` | every condition is true | any condition is false | otherwise |
| `any` | any condition is true | every condition is false | otherwise |
| `not` | its condition is false | its condition is true | its condition is missing |

A combined rule that is missing is treated as any rule whose data is missing: its `when_missing`, else the type's `when_data_missing`, decides. Its reason names the checks that hit, joined with `;`, or the first check whose data is missing. It applies to the media types all its checks share; a rule whose checks share none is an error, and so is `types` naming a type outside them. The Policy page shows a combined rule's YAML as it loads (its comments left out), moves and removes it, and keeps it as written when it saves; its conditions are edited in the file. The content check's keyword and genre flags read single-check rules only.

#### Scopes

A scope decides some items with other settings: strict for the kids' Seerr accounts, looser for a group you trust. The first scope whose `when` is true decides the item; an item no scope takes is decided by the policy above, the base policy.

```yaml
policy:
  auto_approve: on
  types: {movie: {mode: rules, when_data_missing: wait, max_wait_hours: 24}}
  rules: [...]                          # the base policy
  scopes:
    - id: kids
      name: The kids' Seerr accounts
      when:
        all:
          - {check: source, any_of: [seerr]}
          - {check: sender, any_of: [17, 23]}
      types:
        movie: {when_data_missing: review}       # only the fields given change
      rules:                                     # checked before the base rules
        - id: kids-rating
          check: certification
          at_least: US:PG-13
          on_hit: reject
      skip: [trusted-approve]                    # base rule ids left out
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | key | required | Unique among scopes; shown as the reason. |
| `name` | string | none | Human label. |
| `when` | condition | required | One check, or `all`, `any`, `not` over checks, as in a rule. It reads only what is known before any lookup: `source`, `sender`, `time_window`, `post_age_hours`. A `when` whose data is missing (an item that does not record its sender) does not pick its scope. |
| `types` | media type → `mode`, `when_data_missing`, `max_wait_hours` | `{}` | Changes only the fields it gives; the rest are the base type's. |
| `rules` | list | `[]` | Rules checked before the base rules, with their own ids (not one a base rule has). They read `policy.lists` and carry their own `min_votes`. |
| `skip` | base rule ids | `[]` | Base rules this scope does not check. Every id must be a base rule's. |

A scope's own `approve` rule runs before the base rules, so it can end the walk before them: that is how a scope is looser. A scope's `reject` or `review` rule is collected with the base ones: that is how it is stricter. To replace the base rules entirely, `skip` all of them. `sender` is the Telegram user id of a post, or the Seerr user id of a request. A `source` value in `when`, like one in a rule, must be the key of a configured source, so a source a scope names cannot be removed from the setup page.

A worked case: Telegram posts with no data are approved, while Seerr requests still wait for it.

```yaml
scopes:
  - id: telegram
    name: Telegram posts with no data are approved
    when: {check: source, any_of: [group, inbox]}
    types:
      movie: {when_data_missing: approve}
      series: {when_data_missing: approve}
```

A concert posted to the `group` source matches on TMDB but has no IMDb parents guide votes: every `imdb.*` rule lacks its data, nothing hits, and under this scope missing data approves, so the item is approved at once with the missing data and the scope as its reasons. A hit (severe violence with enough votes, an adult certification) still reviews or rejects, and a duplicate, a post with no title or a wrong TMDB match still goes to a person: those are findings, not missing data. A Seerr request matches no scope and waits as the base policy says. To let only some rules' data be missing, give those rules `when_missing: approve` instead.

Every decision made under a scope ends with a reason of kind `scope` naming it. The Policy page shows the `scopes` block as it loads (comments left out) and keeps it as written when it saves; scopes are edited in the file. The content check's keyword and genre flags read the base rules only.

Value shapes:

- severity: `none` < `mild` < `moderate` < `severe` (case-insensitive)
- certification: `COUNTRY:rating`, e.g. `US:NC-17`, `GB:18`. `at_least` orders the ratings of one country; a title with no rating in that country is missing data, not a pass
- resolution: `480p`, `576p`, `720p`, `1080p`, `2160p`
- time: `HH:MM`; `between` may cross midnight (`['23:00', '06:00']`)
- number: int or float

**Signal catalogue.** Fixed in code; each signal is a fetch or a field the app reads. The schema accepts the whole catalogue so the file format does not change as each fetch lands.

| Group (fed by) | Signal | Kind | Operators | Types |
|---|---|---|---|---|
| title (`tmdb`) | `certification` | certification | at_least, any_of, none_of | movie, series |
| | `genre` (TMDB genre ids, or names in `integrations.tmdb.language`) | terms | any_of, none_of | movie, series |
| | `keyword`, `cast`, `crew`, `studio` | terms | any_of, none_of, matches_any | movie, series |
| | `original_language`, `country` | terms | any_of, none_of | movie, series |
| | `year` | number | at_least, at_most, between | all |
| | `runtime_minutes` | number | at_least, at_most, between | movie, series |
| | `rating`, `vote_count` | number | at_least, at_most | movie, series |
| | `adult`, `collection` | bool | is | movie, series |
| | `released` (out somewhere on or before today) | bool | is | movie, series |
| parents guide (`imdb_guide`), voted | `imdb.violence`, `imdb.nudity`, `imdb.profanity`, `imdb.frightening`, `imdb.alcohol` | severity | at_least, at_most | movie, series |
| release | `size_gb` | number | at_least, at_most, between | all |
| | `resolution` | resolution | at_least, at_most, any_of, none_of | movie, series |
| | `release_source` (CAM, TS, WEB-DL, ...), `codec`, `audio_language`, `subtitles` | terms | any_of, none_of | movie, series |
| | `hdr` | bool | is | movie, series |
| | `release_group` | terms | any_of, none_of, matches_any | all |
| | `seeders`, `file_count` | number | at_least, at_most | all |
| | `freeleech` | bool | is | all |
| | `file_extensions` | terms | any_of, none_of | all |
| library | `in_library` | bool | is | movie, series |
| | `already_requested`, `in_download_client` | bool | is | all |
| | `disk_free_gb` | number | at_least, at_most | all |
| context | `source` (source key) | terms | any_of, none_of | all |
| | `sender` | terms | any_of, none_of, matches_any | all |
| | `per_sender_per_week`, `max_active_downloads`, `post_age_hours` | number | at_least, at_most | all |
| | `time_window` | time | between | all |
| books | `subject`, `publisher` | terms | any_of, none_of, matches_any | audiobook, ebook, magazine, comic |
| | `language`, `format` (epub, pdf, cbz, ...) | terms | any_of, none_of | audiobook, ebook, magazine, comic |
| | `pages` | number | at_least, at_most, between | audiobook, ebook, magazine, comic |
| | `issue_newer_than_owned` | bool | is | magazine |
| series | `season_complete` | bool | is | series |
| | `status` (ended, returning) | terms | any_of, none_of | series |
| | `episode_count` | number | at_least, at_most | series |

`release_source` is the release's origin (CAM, WEB-DL); `source` is which configured source the post came from, so every value of a `source` rule must be the `key` of a configured source, or the load fails.

The public default policy, as shipped in `config.example.yaml`: violence at least severe, nudity at least moderate, frightening at least severe (each with `split_vote: 0.40`), and adult certifications (`US:NC-17`, `US:X`, `GB:R18`, `GB:18`), all `on_hit: review`; `auto_approve: shadow`.

#### How a decision is made

The engine is [`checker/policy.py`](../src/vettarr/checker/policy.py), a pure function of the item, its signals and this block. In order:

1. The scope: the first entry of `scopes` whose `when` is true for the item's source, sender and clock gives the type settings and rules below; otherwise the base policy does. Every reason list of a decision made under a scope ends with that scope.
2. A duplicate of something already approved, downloading or done, or a second post of a title still being worked out, goes to a person. Only the same kind counts: a series is compared with series, and in qBittorrent only with the torrents in its own `categories` entry, so the novel a series is based on is not its duplicate. A type with no `categories` entry is compared with every torrent.
3. The type's `mode`: `always` approves, `never` goes to a person. A type in mode `rules` that no rule applies to goes to a person as well.
4. An item with no title goes to a person. When a rule reads the TMDB record or IMDb data (a title signal, an `imdb.*` signal, `status`, `episode_count` or `in_library`), a TMDB match that is not the title the post asked for (another year, a sequel, a remake) goes to a person too: that data would be another title's.
5. The rules that apply to the type: the scope's own first, then the base rules it does not skip, each in file order. `reject` and `review` hits are all collected; a `notify` hit is recorded and changes nothing; the first `approve` hit ends the walk, but only when no earlier rule hit or lacked its data. A rule that combines conditions (`all`, `any`, `not`) hits when the combination is true; it lacks its data when the combination cannot be settled without a missing part.
6. Any `reject` hit rejects. Otherwise any `review` hit reviews. Otherwise an `approve` hit approves.
7. Otherwise, a rule whose data is missing, or whose integration is off (`unavailable`), holds the item as its `when_missing` says, by default the type's `when_data_missing`: any such rule saying `review` reviews; otherwise any saying `wait` waits, re-checked hourly until `max_wait_hours` have passed since the item arrived, and then reviews; a rule saying `approve` holds nothing, and its missing data is recorded as a reason. IMDb votes count only from the day after the title came out (the first theatrical, digital, physical or TV release in any country, or the first air date, from TMDB): before that day every `imdb.*` signal is missing, however many votes it has, so the item waits for its first full day out. When everything an item waits for is an `imdb.*` signal and TMDB gives that date, `max_wait_hours` runs from that day instead of from the item's arrival: a film out for years with too few votes goes to a person at once, and a new one gets its first day out plus `max_wait_hours` to gather `min_votes`.
8. Otherwise nothing holds the item: approve.

The data a type waits for is exactly what its rules read, under the scope that decides the item: the scope is settled from the item's source, sender and clock before anything is fetched. A policy with no `keyword` rule never waits for TMDB keywords, and the IMDb guide is fetched only while a rule reads an `imdb.*` signal. Lookups that cost a request (Jellyfin, qBittorrent, a search, the linked `.torrent`) run only for the signals a rule of the item's type reads, and never while `auto_approve` is `off`. A lookup that fails leaves its own signals missing and no other's. An item that waits is decided again every hour, so the costly answers are kept between decisions: a `.torrent`'s file list for good, a search for six hours, the download client's free space for five minutes.

An `approve` rule ends the walk, so a rule after it is not checked when it hits. Put the rules that review or reject first; `config check` warns when one comes after an `approve` rule for the same type.

What `auto_approve` does with the decision:

| Mode | Approve | Review | Reject | Wait |
|---|---|---|---|---|
| `on` | approved, the search starts | review queue | status `rejected`; the post is never deleted | waits for data |
| `shadow` | review queue, first reason "shadow mode: would have approved" | review queue | review queue ("would have rejected") | review queue ("would have waited") |
| `off` | review queue, first reason "auto-approval is switched off" | review queue | review queue | review queue |

Every decision made in `shadow` raises a `would_have` event, whatever it was. A wait in `shadow` is held for a person at once and is not decided again when the data arrives. Under `off` no lookup runs, so a review that rests only on data nobody fetched (`when_data_missing: review`) also leads with "auto-approval is switched off".

**A policy rejection can be overruled, a person's cannot.** An item the policy rejected never enters the review queue. It shows in the web UI's Inbox under the Decided tab (set the policy outcome filter to `reject` to see only these), and its row and its page have an *Approve anyway* button until a person decides it. Its page also has *Rerun policy*, which decides it again under the rules as they are now. With `split_vote` on a rule that rejects, a minority of the votes is enough to reject. Nobody is told about a rejection beyond its `decision` event, so a rule that should not act on a minority belongs on `on_hit: review`.

`vettarr rerun` (and the bot's `/rerun`) re-decides the reviews whose first reason is a setting (`off`, `shadow`, a type held by its mode), never one a rule flagged, and only where the setting has moved since. It does nothing while `auto_approve` is `off`. In `shadow` it leaves the shadow reviews this same policy made, and in any mode it leaves an item whose type is still held by its mode, under the base policy and under every scope. Each of those would land where it is. An item it leaves in the queue is not pushed to the reviewers again and raises no second `needs_you` event.

#### What a decision records

Each decided item keeps, beside the 0.11 columns (`auto_decision`, `auto_reasons` as plain text):

- `reasons`: a list of `{kind, rule, text}`. Kinds: `rule` (a rule hit; `rule` is its id), `missing` and `unavailable` (a rule that could not be checked), `notify`, `duplicate`, `identity`, `type`, `setting`, `shadow`, `expired` (waited `max_wait_hours`), `clear` (what an approval rested on), `scope` (the scope the decision was made under, last among the decision's reasons, with `delivery` reasons added later after it; `rule` is the scope's id), `delivery` (added after the decision: `not delivered`, `already in the library`, `delivered by title`, `sent on a person's approval`, and `rejected:` with what the rejection did at the target).
- `policy_version`: the first 12 hex characters of a SHA-256 over the `policy` block.
- `policy_outcome`: what the policy decided (`approve`, `review`, `reject`, `wait`), which under `shadow` or `off` is not what was done.
- `signals`: the snapshot the decision read, including the per-severity vote breakdown, genre ids, certifications by country and the book verdict.

Rows decided before this have none of these. Their `auto_reasons` read as reasons of kind `legacy`, which the re-run never takes. Two of them it does take, the two reviews 0.11 made for a setting alone: "auto-approval is switched off" reads as `setting`, and "<type> is not in the auto-approve list" as `type`.

#### Preview impact

`vettarr.checker.policy.preview_impact(candidate, limit=200)` re-evaluates the last 200 items from their stored `signals` under a candidate `policy` block and returns the count per outcome and every item whose outcome would change, with the new reasons. It makes no TMDB, IMDb or catalogue request. Each item is decided as of the moment it was decided, so an item that was waiting then still waits under an unchanged policy, however long ago that was. Rows without stored signals (decided before the policy engine) are counted as not comparable. Each item's scope is picked from its stored snapshot, which holds `source`, `sender`, `time_window` and `post_age_hours`; a snapshot without them matches no scope.

#### Where each signal comes from

| Signals | Read from | Missing when |
|---|---|---|
| title signals | the TMDB record (`/movie` with `release_dates`, `keywords`, `credits`; `/tv` with `content_ratings`, `keywords`, `credits`). `genre` holds the ids and the localised names; `crew` is directors, writers, producers, composer and creators; `cast` the first 20 | no TMDB match; `keyword` also when TMDB lists none, which is silence, not a clean answer |
| `released` | the TMDB record: the earliest theatrical, digital, physical or TV release in any country, against today's date (a festival premiere does not count); the first air date for a series | TMDB lists no such date |
| `year` | the TMDB record, else the post | neither has one |
| `imdb.*` | the parents guide, with the vote breakdown | no IMDb id, not fetched, fewer votes than `min_votes`, or before the title's first full day out |
| `size_gb`, `resolution`, `release_source`, `codec`, `hdr`, `audio_language`, `subtitles`, `release_group`, `freeleech` | the post and the release name in it. `size_gb` counts 1 GB as 1024 MB; `freeleech` is true for "100% Free" or "freeleech"; `hdr` is false for a release name with no HDR tag. `release_source`, `codec` and `audio_language` are read from the post without its title, so the film *Cam* is not a cam copy. `audio_language` and `subtitles` know the words of [`release_words`](#release_words) | the post does not say |
| `seeders` (and `size_gb` the post did not state) | the item's stored search, else the search an approval runs: Prowlarr, then each provider's own search. The answer is kept for six hours | the search finds nothing; the post carries or links its own torrent, which is what downloads and which no search describes; no search is configured (`unavailable`) |
| `file_count`, `file_extensions` | the file list of the item's own `.torrent` when it is a [direct add](#direct-adds), else of the `.torrent` the post links to, fetched once with its provider's session | the post links no torrent, the linked torrent could not be fetched, or the direct add is a magnet |
| `in_library` | Jellyfin, by TMDB id (films and series), and Radarr or Sonarr when the type is delivered there | no TMDB match, or a library that was asked does not answer while none says yes |
| `already_requested` | another post of the same title waiting, held for review, approved or on its way | |
| `in_download_client`, `disk_free_gb` | qBittorrent's torrent names; its `free_space_on_disk`, each read at most once in five minutes. A film, series or book already in the download client is a duplicate at step 1 and goes to a person before any rule is read, so an `in_download_client` rule decides magazines only | qBittorrent does not answer |
| `max_active_downloads` | the downloads in progress | |
| `source` | the configured source whose chat the post came from | |
| `sender`, `per_sender_per_week` | the Telegram user id of whoever sent the message; for a forward, who forwarded it; for a [Seerr request](#seerr-requests), the Seerr user id of whoever asked. `per_sender_per_week` counts that sender's items of the last seven days, this one included | nobody sent it (a channel post, an anonymous admin), or the item was read before the sender was stored |
| `post_age_hours` | the time Telegram dates the message, against the clock | the item was read before the post time was stored |
| `time_window` | the clock, in the container's time zone | |
| `subject`, `language`, `publisher`, `pages` | the catalogue record (Open Library, then Google Books). Fewer than 3 subjects can flag a book but never clear one | no confident match, or the lookup failed |
| `format` | the post, e.g. `(EPUB, PDF)` | |
| `issue_newer_than_owned` | the issue number, or month and year, against the issues of the same magazine already approved, downloading or done; true when none is owned | the title names no issue |
| `season_complete`, `status`, `episode_count` | the post (`S01E03` is one episode; `S01`, `[PACK]` or a [`release_words.full_season`](#release_words) phrase a season) and the TMDB series record | |

### `release_words`

How a post or a release name says a language, subtitles or a whole season, and which brackets in a book's title only name its file format. Three signals read it: `audio_language`, `subtitles` and `season_complete`; the book lookup reads `format_tags`. The block is optional and its defaults are English; no group's wording is in code. A list written in the file replaces its default whole, so write the English words too when posts carry both. The word rules are those of [`formats`](#formats): case-insensitive, never inside a longer word, a space matches any run of whitespace, `/.../` is a regular expression. A regular expression here may not capture: write a group as `(?:...)`, with no name, and a flag as `(?i:...)`, never `(?i)`. `config check` refuses the others, because the words of a list run joined into one pattern.

The block is outside `policy`, so changing it does not change `policy_version`: under `auto_approve: shadow`, `rerun` leaves a shadow review this policy made even when new words would read its post differently.

| Field | Type | Default | Notes |
|---|---|---|---|
| `languages` | language code → words | `es: [spanish, spa, esp]`, `en: [english, eng]`, `fr: [french, truefrench, vff, vfq]`, `de: [german, ger]`, `it: [italian, ita]`, `pt: [portuguese]`, `ja: [japanese, jap, jpn]`, `ko: [korean, kor]`, `ca: [catalan]` | The words that name each language. The code (two or three lowercase letters) is what a rule compares: `audio_language: any_of: [es]`. |
| `subtitles` | words | `[sub, subs, subtitle, subtitles]` | A word that opens a list of subtitle languages (`Subs: English, French`). The languages on the rest of that line are the `subtitles` signal and are not counted as audio. |
| `subtitle_tags` | language code → words | `{}` | A tag that alone says the release is subtitled in that language. |
| `full_season` | words | `[complete, '/\bseason\s+\d+\b/']` | A phrase that says the post is a whole season. `[PACK]` and a bare `S01` say so whatever this list holds, and `S01E03` is always one episode. |
| `format_tags` | words | `[mp3, m4a, m4b, epub, pdf, mobi, azw, azw3, cbr, cbz]` | The words a bracket of file formats is made of: `(MP3)`, `[EPUB, PDF]`, `(MP3 - Full Cast)` once `full cast` is listed. A book's title is looked up in the catalogues without such a bracket. A bracket with any other word in it is part of the title. |

### `notify`

| Field | Type | Notes |
|---|---|---|
| `url` | secret | An Apprise API `/notify` endpoint or a plain http(s) webhook. Receives every event: decisions, items that need a person, deliveries and tracker session changes. Its presence starts the notifier. |

Every event is a row in the `events` table (`id`, `at`, `kind`, `item_id`, `payload`), whether or not `notify` exists. Kinds: `decision`, `would_have` (shadow mode, or with `dry_run: true` in the payload a write [`dry_run`](#dry_run) did not send), `needs_you` (an item went to the review queue), `guard` (a tracker's session guard opened or closed its cooldown), `cookie_expired` (a tracker session could not be established; a run of 5xx answers opens the same cooldown without it), `bot_added` (an item arrived through a `bot` source), `bot_chat` (a reviewer added the bot to a chat, or the bot left a chat a reviewer added it to or a `bot` source reads; the Setup page offers a chat it joined as a source), `delivered` (an approved item was handed to its target; the payload names the target and the id there), `completed` (the target has the files, already had them, or `deliver` says `none` for the type), `delivery_failed` (the target would not take the item, lost it or stopped answering, `deliver` lists no target for its type, or a rejection could not be passed on to the target), `declined` (a Seerr request was declined or removed), `webhook_test` (Seerr's test notification reached a seerr source's webhook). A payload never carries a secret, a cookie or a message's text. Events are written every five seconds, once more when the app stops and at the end of `vettarr rerun`; a batch the database refused is kept and written with the next.

With `notify`, each row is POSTed as JSON (under `dry_run` it is a would-have instead): the row's fields plus `title`, `body` and `type`, the three fields an Apprise API server reads, so one URL works for both. One attempt per event, 5 s timeout, never retried; a failure is logged at most once an hour, without the URL. After a receiver fails to answer at all (a timeout, a refused connection) nothing is sent for five minutes, so a dead URL does not hold up the event writer; the events of those minutes are in the table and are not sent later. A URL that is not http(s) (an Apprise scheme such as `tgram://`) needs an Apprise API server in between: point `url` at its `/notify/<key>` endpoint.

### `web`

| Field | Type | Default | Notes |
|---|---|---|---|
| `bind` | string | `0.0.0.0` | |
| `port` | 1-65535 | `4850` | |
| `auth.password_hash` | secret | none | Exactly one of `password_hash` or `trusted_header`. The line `vettarr web hash-password` prints; anything else is an error at load. |
| `auth.trusted_header` | string, not empty | none | Header a trusted reverse proxy sets to the signed-in user, e.g. `Remote-User`. |
| `base_url` | http(s) URL | none | The address a reviewer's phone reaches the UI at. With it, every review card the bot sends links to the item's page (`<base_url>/item/<id>`), as a link in the text and as an "Open in vettarr" button. Telegram refuses a button to `localhost` or to a host without a dot, so for such an address the card carries the text link only. When it is `https`, the session cookie is `Secure`. |

No `auth` block is valid, and `config check` warns: anyone who reaches the port can approve and reject. The app logs the same warning at start and every page shows an `unprotected` badge. Running the UI for development is in [DEV.md](DEV.md).

**A password.** `vettarr web hash-password` asks for the password twice without echo (or reads one line from stdin) and prints one line, `scrypt:<n>:<r>:<p>:<salt>:<key>`. Put it in an environment variable and reference it: `password_hash: ${WEB_PASSWORD_HASH}`. The line has no `$` in it, so a compose `.env` file leaves it alone. The password needs at least 8 characters.

Every page, `/photos` included, then asks for a session. The login page sets a cookie that is `HttpOnly` and `SameSite=Lax`. It is Lax and not Strict so that a link the bot sends, opened from another app, arrives signed in. A session lasts 12 hours and ends with the browser, or 30 days with "Remember this device". The server stores no sessions. It signs the cookie with a key made from two things: the password hash, and a random secret the app writes once to `/data/sessions/web-session.key`, readable by its own user only. So the hash line alone cannot make a cookie, a new password signs every device out, and so does deleting that file. `/data/sessions` is a volume in the image, so the secret outlives an upgrade; when the file cannot be written the secret lives in memory, the start logs a warning, and a restart signs everyone out. Signing out clears the cookie on that device only. Pages behind the sign-in are sent with `Cache-Control: no-store`, so Back after signing out shows nothing.

Five wrong passwords from one address lock that address for 15 minutes, and the page says so; a correct password during the lock is refused too. A try counts when it arrives, before the password is checked, so of many logins sent at once five are checked and the rest are refused. The locks are in memory and a restart forgets them. Behind a reverse proxy every request comes from the proxy's address, so one person's wrong passwords lock the login for everyone, while open sessions keep working. Set the environment variable `FORWARDED_ALLOW_IPS` to the proxy's address and the app reads each client's own address from `X-Forwarded-For`.

**The webhook is outside the login.** `POST /hooks/seerr/<source key>` asks for no session and no proxy header, in every mode: Seerr calls it, not a browser. It asks for the source's `webhook_secret` instead ([Seerr requests](#seerr-requests)). A reverse proxy that signs people in has to let that path through without a login.

**A proxy that signs people in.** With `trusted_header`, a request that carries the header comes in as that user and one that does not gets a 401. There is no login page. The app believes whoever reaches the port, so `bind` it where only the proxy can reach it, and have the proxy remove the header from what clients send.

**Requests from other sites.** In every mode, with or without `auth`, the app answers 403 to a POST, PUT, PATCH or DELETE that a browser says another site sent. `Sec-Fetch-Site` must be `same-origin` or `none`. A browser sends no such header over plain http on a LAN address, and there `Origin` must name the host the request went to. A proxy that rewrites `Host` breaks that second comparison, so set `base_url` to the public address and the app accepts that origin as well. This check does not stop DNS rebinding, where a page points its own name at the app's address and so sends a `Host` and an `Origin` that agree. With a password that page has no session cookie and gets nowhere. With no `auth` block nothing stops it, which is one more reason an instance without sign-in belongs where only its operator's machines reach it.

## What starts

| Component | Starts when |
|---|---|
| dry run | `dry_run: true`, or `DRY_RUN=1`; listed first |
| userbot | at least one source of kind `userbot` |
| bot | `telegram.bot_token` is set (it serves reviewers even with no bot source) |
| seerr source | each source of kind `seerr`: its poll always, its webhook when it has a `webhook_secret` and a `web` block exists |
| web | a `web` block exists |
| prowlarr, qbittorrent, radarr, sonarr, seerr, tmdb, google_books, jellyfin | the block exists and is not `enabled: false` |
| imdb_guide | not `enabled: false`, and at least one rule reads an `imdb.*` signal |
| provider `<key>` | each provider; its thank and keep-alive only when enabled |
| delivery | `deliver` maps at least one type to something other than `none` |
| policy | `auto_approve` is `shadow` or `on` |
| notify | a `notify` block exists |

`vettarr.config.components.components(cfg)` returns this list; `config check` prints it.

## Not enforced yet

Nothing today: the code acts on every key the schema accepts. When a key is added before the milestone that implements it, it is listed here until that milestone lands.
