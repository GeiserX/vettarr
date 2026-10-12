# AGENTS.md

vettarr takes a link, a post, a magnet, a `.torrent` or a Seerr request, looks the title up, checks it against the user's rules and then downloads it, holds it for review or refuses it. Python 3.12, FastAPI with htmx templates, SQLAlchemy on Postgres, Telethon and python-telegram-bot.

## Commands

- Install: `uv venv --python 3.12 && uv pip install -e .`
- Tests: `.venv/bin/python -m unittest discover -s tests` (no database or browser needed)
- Config check: `vettarr config check config.example.yaml`
- Web UI with demo data, browser checks and screenshots: [docs/DEV.md](docs/DEV.md)

## Rules

- Read [CONTRIBUTING.md](CONTRIBUTING.md) first. No tracker, group or site is named anywhere in the tree; examples use `tracker-a.example`.
- `config.yaml` is a contract ([docs/CONFIG.md](docs/CONFIG.md)): add optional fields, never rename or remove one.
- Migrations only add tables and nullable columns.
- No secret value in a log line, a test fixture that looks real, or a screenshot. Screenshots come from `vettarr demo-seed`.
- `static/app.js` stays under 150 lines; behaviour is htmx attributes first.
- Conventional commits that say why.
