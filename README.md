<p align="center">
  <img src="https://raw.githubusercontent.com/GeiserX/vettarr/main/docs/images/banner.svg" alt="vettarr banner" width="900">
</p>

<h1 align="center">vettarr</h1>

<p align="center">
  <a href="https://github.com/GeiserX/vettarr/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue?style=flat-square" alt="License: AGPL-3.0-or-later"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-%3E%3D3.12-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
</p>

<p align="center"><strong>Send it a link, a post, a magnet, a torrent or a Seerr request. Your rules decide whether it downloads.</strong></p>

Licensed under the GNU Affero General Public License, version 3 or later ([LICENSE](LICENSE)).

You see a film somewhere: a link a friend sends, a post in a Telegram group, a magnet, a `.torrent`, a request in Seerr. Send it to vettarr, or let vettarr watch the group. It looks the title up, checks it against the rules you wrote (age rating, how violent IMDb says it is, genres and keywords you never want, size, seeders, whether you already have it), and either downloads it, asks you first, or says no and tells you why. Films, series, books and magazines. Your trackers, your rules, your box.

<table>
  <tr>
    <td><img src="https://raw.githubusercontent.com/GeiserX/vettarr/main/docs/images/ui/inbox-1280-light.png" alt="The Inbox: items grouped by what they need, with the policy's verdict on each"></td>
    <td><img src="https://raw.githubusercontent.com/GeiserX/vettarr/main/docs/images/ui/item-1280-light.png" alt="An item: the verdict, every reason tagged by the rule that gave it, and the releases found"></td>
  </tr>
  <tr>
    <td><img src="https://raw.githubusercontent.com/GeiserX/vettarr/main/docs/images/ui/policy-1280-light.png" alt="The Policy page: rules as sentences, with a preview of what a change would do"></td>
    <td><img src="https://raw.githubusercontent.com/GeiserX/vettarr/main/docs/images/ui/stats-1280-light.png" alt="Stats: decisions per day and the reasons behind them"></td>
  </tr>
</table>

The screenshots show the demo data `vettarr demo-seed` writes.

## What it does

- **Takes items from wherever you find them.** A Telegram group or channel read as your account (it can read history), a bot you message or add to a group, a magnet or a `.torrent` sent to the bot, an IMDb or TMDB link, a plain title, or Seerr's pending requests.
- **Reads posts by word lists, not code.** A group's card format is a list of words in `config.yaml`; scene release names are read out of the box.
- **Decides by rules you write.** One signal, one operator, one value per rule: certification by country, IMDb parents-guide severity, TMDB genre and keyword, size, resolution, source, seeders, release group, book subject, whether it is already in your library, and more. Each rule says what a hit does: approve, hold for review, or reject.
- **Shows its reasons.** Every decision is stored with the rules that made it. Shadow mode decides and records without acting, so you can watch it before trusting it, and the Policy page previews what a change would do to the last 200 items before you save.
- **Delivers.** An approved item goes to qBittorrent (the exact torrent a post linked, or the best search result through Prowlarr or a tracker's own search), to Radarr or Sonarr, or back to Seerr as an approval.
- **Works from a phone.** The web UI has an Inbox, a one-at-a-time Review with keys and swipes, an item page, Policy, Setup and Stats, in light and dark. The Telegram bot pushes each item that needs you, with the same actions.

## Quick start

You need Docker with Compose.

```sh
git clone https://github.com/GeiserX/vettarr.git && cd vettarr
mkdir -p data && cp config.example.yaml data/config.yaml
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 16)" > .env
docker compose up -d
```

Open http://localhost:4850. The example config starts the web UI and the policy in shadow mode, and nothing else: no source reads anything and nothing downloads until you turn it on. The database starts empty; the app creates its schema on the first start.

Then edit `data/config.yaml`:

1. Set a password: `docker compose run --rm app vettarr web hash-password`, put the line it prints in `.env` as `WEB_PASSWORD_HASH=...`, and uncomment `web.auth` in the file.
2. Add a source, the integrations you run (Prowlarr, qBittorrent, Radarr, Sonarr, Seerr, TMDB) and where each media type goes (`deliver`).
3. Check it: `docker compose run --rm app vettarr config check /data/config.yaml` validates the file and lists what would start.
4. Apply it: `docker compose up -d --force-recreate app`. Use this rather than `docker compose restart`, which keeps the container's old environment and never reads the new `.env` line.

The compose file publishes the UI on this machine only, because the example config has no password. To open it to your network, set the password first (step 1), then change the port line in `docker-compose.yml` to `"4850:4850"` and run `docker compose up -d`.

Secrets stay in the environment: any value in the file can be `${VAR}`, read at start from the container's environment (the compose file passes `.env`). `dry_run: true` turns every outside write into a recorded "would have", which is a safe way to try a new setup on real posts.

[docs/CONFIG.md](docs/CONFIG.md) is the full reference for `config.yaml`. [docs/DEV.md](docs/DEV.md) covers running the web UI with demo data, the browser checks and the screenshots.

## The IMDb parents guide

The IMDb signals (`imdb.violence`, `imdb.nudity`, `imdb.profanity`, `imdb.frightening`, `imdb.alcohol`) come from IMDb's undocumented website API. IMDb's terms forbid automated access without its consent. Use them on a private, single-user instance only, at your own risk; they can stop working without notice. vettarr never proxies or redistributes IMDb data. A severity voted by fewer than `min_votes` people counts as missing. TMDB certifications by country are the licensed alternative, and the example config ships a rule on them.

## What it does not do

- **No download client but qBittorrent.** For Transmission, Deluge or anything else, deliver through Radarr, Sonarr or Seerr and let them hand the download over.
- **No writes to a Jellyfin or Plex library.** Jellyfin is read only, to tell whether you already have a title.
- **No public tracker presets.** The example config has commented templates per tracker software (xbtit, NexusPHP, UNIT3D), never per tracker. Some trackers forbid automation, including thanking uploaders and keeping a session alive; both are off unless you turn them on. Read your tracker's rules first.
- **No Telegram account management.** A userbot source logs in as your account. Telegram may limit accounts that automate too much.

## Commands

```text
vettarr                          run everything the config switches on
vettarr config check [path]      validate a config and list what would start
vettarr config import-env [-o]   write a config from the environment variables of an older setup
vettarr config example           print config.example.yaml
vettarr web hash-password        print the line for web.auth.password_hash
vettarr web serve                run the web UI alone
vettarr rerun                    re-decide what the policy held only for a setting
vettarr demo-seed                fill an empty database with fake items
```

`media-triage`, the command's old name, still runs the same program and says it has been renamed.

## Database and upgrades

Postgres 17 (the compose file runs one). The schema is managed with Alembic and upgraded on every start; migrations only add columns and tables. `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` come from the environment and default to `localhost`, `5432` and `vettarr`.

A setup that predates `config.yaml` and ran from environment variables alone still starts, in a degraded mode that says so in its log: `vettarr config import-env -o config.yaml` writes the file to move it to.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Tests: `python -m unittest discover -s tests`.

## Licence

vettarr is free software: you can redistribute it and modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, version 3 or any later version. If you run a modified copy as a network service, its users are entitled to its source.
