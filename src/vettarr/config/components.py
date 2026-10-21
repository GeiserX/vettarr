"""Which parts of the app a config starts.

A component starts only when its block exists (and, for integrations, is not
``enabled: false``). An empty config starts nothing.
"""

from __future__ import annotations

from vettarr import dryrun
from vettarr.config.schema import Config
from vettarr.config.signals import SIGNALS


def components(cfg: Config) -> list[tuple[str, str]]:
    """``(component, why)`` pairs, in start order. ``dry run`` first, when it is on."""
    out: list[tuple[str, str]] = []
    if cfg.dry_run:
        out.append(("dry run", dryrun.SUMMARY))
    tg = cfg.telegram

    userbot = [s.key for s in cfg.sources if s.kind == "userbot"]
    if userbot:
        out.append(("userbot", "reads " + ", ".join(userbot)))
    if tg and tg.bot_token:
        bot_sources = [s.key for s in cfg.sources if s.kind == "bot"]
        why = "reviewers and sources " + ", ".join(bot_sources) if bot_sources else "reviewers only"
        if not tg.reviewers:
            why += " (telegram.reviewers is empty: it answers no one)"
        out.append(("bot", why))
    for s in cfg.sources:
        if s.kind == "seerr":
            poll = f"a poll every {s.poll_minutes} min"
            # The webhook route exists only for a source with a secret, and the web UI serves it.
            hooked = s.webhook_secret is not None and cfg.web is not None
            out.append(("seerr-source", f"{s.key}: " + (f"webhook plus {poll}" if hooked else f"{poll}, no webhook")))

    if cfg.web is not None:
        out.append(("web", f"{cfg.web.bind}:{cfg.web.port}" + ("" if cfg.web.auth else " (no auth)")))

    integ = cfg.integrations
    for name in ("prowlarr", "qbittorrent", "radarr", "sonarr", "seerr", "tmdb", "google_books", "jellyfin"):
        if integ.enabled(name):
            out.append((name, "integration"))
    imdb_rules = [
        r.id for r in cfg.policy.rules if any(SIGNALS[leaf.check].group == "parents_guide" for leaf in r.leaves())
    ]
    if integ.imdb_guide.enabled and imdb_rules:
        out.append(("imdb_guide", "used by rules " + ", ".join(imdb_rules)))

    for p in cfg.providers:
        extras = [x for x, on in (("thank", p.thank.enabled), ("keep-alive", p.keep_alive.enabled)) if on]
        why = f"auth from {p.auth.from_}" + (f", search {p.search}" if p.search != "none" else "")
        out.append((f"provider {p.key}", why + ("; " + ", ".join(extras) if extras else "")))

    delivered = {t: d for t, d in cfg.deliver.items() if d != "none"}
    if delivered:
        out.append(("delivery", ", ".join(f"{t} -> {d}" for t, d in delivered.items())))
    if cfg.policy.auto_approve != "off":
        out.append(("policy", f"auto_approve {cfg.policy.auto_approve}, {len(cfg.policy.rules)} rules"))
    if cfg.notify is not None:
        out.append(("notify", "events are posted to notify.url"))
    return out
