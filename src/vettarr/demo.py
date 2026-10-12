"""``vettarr demo-seed``: fake items for screenshots and UI work.

Sixty invented items across every status and media type, with their content
checks, stored releases, downloads and events, from two sources and two
providers. Every decision is the real policy engine's: each item gets the
lookups a real one would (a TMDB record, a parents guide), the engine decides
it under ``POLICY``, and the row holds what the engine wrote (reasons,
signals, policy version). Only what happens after a decision is staged by
hand (searching, downloading, done, rejected by a person). The oldest third
is decided as ``auto_approve: shadow`` would, so there are ``would_have``
events for what a person then approved.

``demo-seed --config`` prints the config.yaml the data belongs to: the same
sources, providers and policy. Run the web UI on it with
``vettarr web serve``, which reads no source and downloads nothing.

Demo rows are told apart by their chat ids (``CHATS``) and, for events, by
``demo: true`` in the payload. Seeding again replaces them. A database that
holds anything else is refused without ``--force``, and ``--force`` adds the
demo rows beside the real ones without touching those.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from vettarr import events as event_kinds
from vettarr.checker import gather
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.policy import APPROVE, REJECT, REVIEW, WAIT, act, evaluate
from vettarr.checker.tmdb_client import MATCH_WITH_YEAR, TMDBResult
from vettarr.config.schema import Config
from vettarr.config.signals import MEDIA_TYPES
from vettarr.models import (
    ContentCheck,
    Download,
    DownloadStatus,
    Event,
    ItemStatus,
    MediaItem,
    MediaType,
    SeverityRating,
    TorrentSearch,
)

# source key -> the chat its demo posts come from. No real chat has these ids.
CHATS = {"demo-group": -1009990000001, "demo-bot": 999000002}
PROVIDERS = ("northwind", "harbor")
# A group the demo bot was added to; no source reads it.
BOT_CHAT_ID = -1009990000003
_SEED = 20261012
# Stands where the config wants a credential. `web serve` never uses one.
_PLACEHOLDER = "placeholder"

POLICY: dict[str, Any] = {
    "auto_approve": "on",
    "types": {
        "movie": {"mode": "rules", "when_data_missing": "wait", "max_wait_hours": 24},
        "series": {"mode": "rules", "when_data_missing": "wait", "max_wait_hours": 24},
        "magazine": {"mode": "always"},
        "ebook": {"mode": "never"},
        "audiobook": {"mode": "never"},
        "comic": {"mode": "never"},
        "music": {"mode": "never"},
    },
    "lists": {"unwanted": ["spiders"]},
    "rules": [
        {"id": "unwanted-keywords", "name": "A keyword we never want", "check": "keyword",
         "matches_any": "unwanted", "on_hit": "reject"},
        {"id": "violence", "name": "Violence is severe", "check": "imdb.violence",
         "at_least": "severe", "split_vote": 0.40, "on_hit": "review"},
        {"id": "nudity", "name": "Nudity is moderate or more", "check": "imdb.nudity",
         "at_least": "moderate", "split_vote": 0.40, "on_hit": "review"},
        {"id": "frightening", "name": "Frightening is severe", "check": "imdb.frightening",
         "at_least": "severe", "split_vote": 0.40, "on_hit": "review"},
        {"id": "adult-certification", "name": "Rated for adults only", "check": "certification",
         "any_of": ["US:NC-17", "US:X", "GB:R18", "GB:18"], "on_hit": "review"},
    ],
}


def document(*, web: bool = True) -> dict[str, Any]:
    """The config the demo data belongs to. ``web`` adds the block ``web serve`` needs."""
    doc: dict[str, Any] = {
        "telegram": {"api_id": 1, "api_hash": _PLACEHOLDER, "bot_token": _PLACEHOLDER, "reviewers": [CHATS["demo-bot"]]},
        "sources": [
            {"key": "demo-group", "kind": "userbot", "chat": CHATS["demo-group"], "on_reject": "delete"},
            {"key": "demo-bot", "kind": "bot", "chat": "private"},
        ],
        # harbor searches by itself, so Search is offered: with no Prowlarr and no
        # provider search, Search is not, and Setup says there is nowhere to search.
        "providers": [
            {"key": key, "base_url": f"https://{key}.example",
             "auth": {"from": "cookie_file", "cookie_file": f"/data/cookies/{key}.txt"},
             **({"search": "unit3d"} if key == "harbor" else {})}
            for key in PROVIDERS
        ],
        "integrations": {
            "tmdb": {"api_key": _PLACEHOLDER},
            "qbittorrent": {"url": "http://qbittorrent.example"},
        },
        # The card the demo's posts are written as: a header line naming the
        # type, then labelled lines. Without it Setup's "Try a post" read a
        # demo post's header line as its title.
        "formats": [{
            "key": "demo-card",
            "kind": "card",
            "type_headers": {media_type.value: [header] for media_type, header in _EMOJI.items()},
            "fields": {"title": ["Title"]},
        }],
        # The demo's downloads are the download client's, so every type goes there.
        "deliver": {media_type: "qbittorrent" for media_type in MEDIA_TYPES},
        "policy": POLICY,
    }
    if web:
        doc["web"] = {
            "bind": "127.0.0.1",
            "port": "${WEB_PORT}",
            "auth": {"password_hash": "${WEB_PASSWORD_HASH}"},
        }
    return doc


def config_text() -> str:
    from vettarr.config.loader import dump

    return (
        "# Written by `vettarr demo-seed --config`. For `vettarr web serve` only:\n"
        "# the Telegram values are placeholders, so the full app cannot start on it.\n"
        "# WEB_PORT and WEB_PASSWORD_HASH come from the environment.\n"
        + dump(document())
    )


# --- what the items are -----------------------------------------------------

_M, _S = MediaType.MOVIE, MediaType.SERIES
_ST = ItemStatus
# status -> the media types of its items. 60 in all, every MediaType at least once.
_PLAN: tuple[tuple[ItemStatus, tuple[MediaType, ...]], ...] = (
    (_ST.PENDING_REVIEW, (_M,) * 6 + (_S,) * 4 + (MediaType.COMIC,) * 2 + (MediaType.EBOOK, MediaType.AUDIOBOOK)),
    (_ST.AWAITING_DATA, (_M,) * 4 + (_S,) * 2),
    (_ST.NO_MATCH, (_M, _S, MediaType.EBOOK, MediaType.MAGAZINE)),
    (_ST.PARSE_FAILED, (MediaType.UNKNOWN,) * 2),
    (_ST.ERROR, (_M, MediaType.MUSIC)),
    (_ST.APPROVED, (_M, _M, _S, MediaType.EBOOK)),
    (_ST.SEARCHING, (_M, MediaType.AUDIOBOOK)),
    (_ST.DOWNLOADING, (_M, _M, _S, _S, MediaType.EBOOK)),
    (_ST.COMPLETED, (_M,) * 4 + (_S, _S, MediaType.EBOOK, MediaType.EBOOK, MediaType.AUDIOBOOK, MediaType.MAGAZINE)),
    (_ST.REJECTED, (_M,) * 4 + (_S, _S, MediaType.COMIC, MediaType.EBOOK)),
    (_ST.IGNORED, (MediaType.UNKNOWN,) * 2),
    (_ST.CHECKING, (_M,)),
)

_TITLES: dict[MediaType, tuple[str, ...]] = {
    _M: (
        "The Lantern Keeper", "Salt and Iron", "A Quiet Harvest", "Glass Harbor", "The Ninth Winter",
        "Paper Kites", "The Cartographer's Daughter", "Ember Road", "Still Water Station",
        "Halfway to Morning", "The Orchard Above the Sea", "Signal Fire at Dusk", "The Long Field",
        "Copper Sky", "The Weight of Snow", "Harbor Lights Motel", "Night Ferry to Anywhere",
        "The Beekeeper's Year", "Dust Line", "Open Water Club", "Last Light at Cove Point",
        "The Glassblower's Apprentice", "Thirteen Postcards", "A Map of Small Rooms",
        "The Tidewatcher", "Seven Miles of Fence",
    ),
    _S: (
        "Tidewater Lane", "The Signalmen", "Cold Harbour Road", "Field Notes", "Paper Crown",
        "The Long Shift", "Lowlands Division", "Meridian Street", "The Allotment", "Night Desk",
        "Saltmarsh", "Quarter Mile", "The Understudy Club", "Greywater",
    ),
    MediaType.EBOOK: (
        "A Short History of Lighthouses", "The Gardener's Almanac", "Rivers of the North",
        "Bread, Salt and Time", "The Clockmaker's Notes", "Walking the Coast Path",
        "An Atlas of Small Islands",
    ),
    MediaType.AUDIOBOOK: ("The Winter Garden", "Letters from the Harbour", "A Field Guide to Getting Lost"),
    MediaType.MAGAZINE: ("Workshop Monthly", "Trail and Tide"),
    MediaType.COMIC: ("Lanternjaw", "The Paper Fleet", "Moth and Fox"),
    MediaType.MUSIC: ("Low Tide Sessions",),
}
_NOT_MEDIA = (
    "does anyone know when the next batch lands?",
    "thanks, that one worked",
    "mirror is back up, same place as before",
    "[photo]",
)
_GENRES = (
    ((18, "Drama"), (9648, "Mystery")), ((35, "Comedy"), (16, "Animation")),
    ((53, "Thriller"), (80, "Crime")), ((12, "Adventure"), (10751, "Family")),
    ((878, "Science Fiction"), (18, "Drama")), ((99, "Documentary"),),
)
_PLOTS = (
    "A lighthouse keeper's last season on the rock, and the stranger who rows out to take it from her.",
    "Two brothers inherit a failing ferry line and one summer to make it pay.",
    "A night-shift dispatcher starts answering calls that have not happened yet.",
    "After the harvest fails, a village votes on whether to follow the river south.",
    "A retired cartographer is asked to draw a town that keeps moving.",
    "Three strangers share a taxi out of a snowed-in city and one of them is lying about why.",
)
_GROUPS = ("KITE", "ROOK", "TARN", "VALE", "FERN")
_EMOJI = {
    _M: "\U0001f3ac Film", _S: "\U0001f4fa Series", MediaType.EBOOK: "\U0001f4d6 Book",
    MediaType.AUDIOBOOK: "\U0001f3a7 Audiobook", MediaType.MAGAZINE: "\U0001f4f0 Magazine",
    MediaType.COMIC: "\U0001f4ac Comic", MediaType.MUSIC: "\U0001f3b5 Music",
}
_BOOK_FORMAT = {
    MediaType.EBOOK: ("EPUB", "MB", 2, 18), MediaType.AUDIOBOOK: ("M4B", "MB", 180, 620),
    MediaType.MAGAZINE: ("PDF", "MB", 30, 90), MediaType.COMIC: ("CBZ", "MB", 60, 240),
    MediaType.MUSIC: ("FLAC", "MB", 280, 640),
}
# Why a film or a series is held for a person, in turn.
_HOLDS = ("violence", "nudity", "certification", "frightening", "identity", "duplicate")
_CLEAN = {"NUDITY": "none", "VIOLENCE": "mild", "PROFANITY": "mild", "FRIGHTENING": "none", "ALCOHOL": "mild"}
_SEVERITIES = ("none", "mild", "moderate", "severe")
_RATING_FIELD = {
    "NUDITY": "sex_rating", "VIOLENCE": "violence_rating", "PROFANITY": "profanity_rating",
    "FRIGHTENING": "frightening_rating", "ALCOHOL": "alcohol_rating",
}


@dataclass
class Seed:
    """What a seeding writes. An event's first field is the index of its item, or None."""

    items: list[MediaItem] = field(default_factory=list)
    events: list[tuple[int | None, Event]] = field(default_factory=list)


def _breakdown(rng: random.Random, severity: str, votes: int) -> dict[str, int]:
    """Votes per label, with ``severity`` the most voted."""
    weights = [rng.randint(2, 12) for _ in _SEVERITIES]
    weights[_SEVERITIES.index(severity)] = 60
    total = sum(weights)
    return {label: votes * w // total for label, w in zip(_SEVERITIES, weights)}


def _guide(rng: random.Random, hold: str | None) -> ParentsGuide:
    severities = dict(_CLEAN)
    if hold == "violence":
        severities["VIOLENCE"] = "severe"
    elif hold == "nudity":
        severities["NUDITY"] = "moderate"
    elif hold == "frightening":
        severities["FRIGHTENING"] = "severe"
    guide = ParentsGuide(fetched=True)
    for category, severity in severities.items():
        votes = rng.randint(60, 480)
        setattr(guide, _RATING_FIELD[category], SeverityRating(severity))
        guide.votes[category] = votes
        guide.breakdown[category] = _breakdown(rng, severity, votes)
    return guide


def _tmdb(rng: random.Random, n: int, item: MediaItem, hold: str | None, unwanted: bool) -> TMDBResult:
    genres = _GENRES[n % len(_GENRES)]
    adults = hold == "certification"
    return TMDBResult(
        tmdb_id=900000 + n,
        imdb_id=f"tt99{n:05d}",
        # A match that is not the title the post asked for is what "identity" holds.
        title="The Harbor at Night" if hold == "identity" else item.title,
        original_title=None,
        year=item.year,
        match_mode=MATCH_WITH_YEAR,
        poster_url=f"/static/demo/poster-{n % 12}.svg",
        genres=[name for _, name in genres],
        genre_ids=[gid for gid, _ in genres],
        keywords=["small town", "family", *(["spiders"] if unwanted else [])],
        plot=_PLOTS[n % len(_PLOTS)],
        certifications={"US": "NC-17", "GB": "18"} if adults else {"US": "PG-13", "GB": "12A"},
        original_language="en",
        countries=["US"],
        runtime=rng.randint(84, 142),
        vote_average=round(rng.uniform(5.8, 8.4), 1),
        vote_count=rng.randint(300, 9000),
        adult=False,
        collection=False,
        cast=["Mara Ellison", "Tomas Reyes", "Ines Okafor"],
        crew=["Dana Whitlock"],
        studios=["Low Tide Pictures"],
    )


def _post(rng: random.Random, n: int, media_type: MediaType, title: str | None, now: datetime) -> MediaItem:
    """The item as the parser leaves it: no check, no decision."""
    source = "demo-bot" if n % 4 == 3 else "demo-group"
    created = now - timedelta(hours=2 + n * 5.5, minutes=rng.randint(0, 50))
    item = MediaItem(
        telegram_message_id=5000 + n,
        telegram_chat_id=CHATS[source],
        source_key=source,
        origin_key=f"{CHATS[source]}:{5000 + n}",
        sender_id=CHATS["demo-bot"] if source == "demo-bot" else None,
        posted_at=created - timedelta(minutes=rng.randint(1, 30)),
        media_type=media_type,
        title=title,
        external_links=[],
        auto_reasons=[],
        created_at=created,
        updated_at=created,
    )
    if title is None:
        item.raw_text = _NOT_MEDIA[n % len(_NOT_MEDIA)]
        return item
    lines = [_EMOJI[media_type]]
    if media_type in (_M, _S):
        item.year = 2026 - rng.randint(0, 9)
        item.quality = rng.choice(("1080p", "1080p", "2160p", "720p"))
        item.source = rng.choice(("WEB-DL", "BluRay", "WEBRip"))
        item.release_group = rng.choice(_GROUPS)
        gb = rng.uniform(1.4, 9.8) if media_type is _M else rng.uniform(6.0, 38.0)
        item.size_text = f"{gb:.1f} GB"
        lines.append(f"Title: {title} ({item.year})")
        if media_type is _S:
            item.season = f"S{rng.randint(1, 4):02d}"
            lines.append(f"Season: {item.season} [PACK]")
        lines.append(f"Quality: {item.quality} {item.source}")
    else:
        fmt, unit, low, high = _BOOK_FORMAT[media_type]
        item.year = 2026 - rng.randint(0, 3)
        if media_type is MediaType.MAGAZINE:
            item.title = title = f"{title} {item.year}-{rng.randint(1, 9):02d}"
        elif media_type is MediaType.COMIC:
            item.title = title = f"{title} #{rng.randint(2, 24)}"
        item.size_text = f"{rng.randint(low, high)} {unit}"
        lines += [f"Title: {title}", f"Format: {fmt}"]
    lines.append(f"Size: {item.size_text}")
    item.raw_text = "\n".join(lines)
    return item


def _release_name(item: MediaItem, group: str, quality: str | None = None) -> str:
    words = "".join(c if c.isalnum() else " " for c in item.title or "").split()
    parts = [*words, *([str(item.year)] if item.media_type in (_M, _S) else [])]
    if item.season:
        parts.append(item.season)
    if item.media_type in (_M, _S):
        parts += [quality or item.quality or "1080p", item.source or "WEB-DL", "x264"]
    else:
        parts.append(_BOOK_FORMAT[item.media_type][0])
    return ".".join(parts) + f"-{group}"


def _search(rng: random.Random, n: int, item: MediaItem, at: datetime, found: bool) -> TorrentSearch:
    """A stored search. ``found=False`` is the one behind a no-match."""
    releases = []
    for k in range(rng.randint(2, 6) if found else 0):
        provider = PROVIDERS[(n + k) % len(PROVIDERS)]
        torrent_id = str(40000 + n * 10 + k)
        gb = gather.size_gb(item.size_text) or 1.0
        releases.append({
            "guid": f"https://{provider}.example/torrents/{torrent_id}",
            "title": _release_name(item, _GROUPS[(n + k) % len(_GROUPS)], ("1080p", "2160p", "720p")[k % 3]),
            "size": int(gb * rng.uniform(0.7, 1.6) * 1024**3),
            "seeders": max(1, rng.randint(40, 400) // (k + 1)),
            "indexer": provider,
            "download_url": f"https://{provider}.example/torrents/download/{torrent_id}",
            "magnet_url": None,
            "info_url": f"https://{provider}.example/torrents/{torrent_id}",
            "provider": provider,
            "provider_id": torrent_id,
        })
    search = TorrentSearch(prowlarr_results=releases, result_count=len(releases), searched_at=at)
    if releases:
        best = releases[0]
        search.best_match_guid = best["guid"]
        search.best_match_title = best["title"]
        search.best_match_size = best["size"]
        search.best_match_seeders = best["seeders"]
        search.best_match_url = best["download_url"]
    return search


def _download(rng: random.Random, n: int, item: MediaItem, search: TorrentSearch, at: datetime, done: bool) -> Download:
    return Download(
        torrent_search=search,
        qbittorrent_hash=hashlib.sha1(f"demo-{n}".encode()).hexdigest(),
        created_torrent=True,
        download_url=search.best_match_url,
        category=item.media_type.value,
        save_path=f"/downloads/{item.media_type.value}",
        progress=1.0 if done else round(rng.uniform(0.08, 0.92), 2),
        status=DownloadStatus.COMPLETED if done else DownloadStatus.DOWNLOADING,
        started_at=at,
        completed_at=at + timedelta(minutes=rng.randint(12, 180)) if done else None,
    )


_AFTER_APPROVAL = frozenset({
    _ST.APPROVED, _ST.SEARCHING, _ST.DOWNLOADING, _ST.COMPLETED, _ST.NO_MATCH, _ST.ERROR,
})
# Items are numbered newest first; from this one on they are the oldest third.
_SHADOW_FROM = 40


def _event(kind: str, at: datetime, **payload: Any) -> Event:
    return Event(at=at, kind=kind, payload={**payload, "demo": True})


def build(now: datetime | None = None) -> Seed:
    """The demo rows, not yet in any session. The same ``now`` builds the same rows."""
    from vettarr.pipeline import Verdict, decision_values, flags_of

    now = now or datetime.now(timezone.utc)
    rng = random.Random(_SEED)
    cfg = Config.model_validate(document(web=False))
    policy = cfg.policy
    plan = [(status, media_type) for status, types in _PLAN for media_type in types]
    rng.shuffle(plan)
    used = dict.fromkeys(_TITLES, 0)
    holds = rejections = 0
    seed = Seed()

    for n, (status, media_type) in enumerate(plan):
        title = None
        if media_type is not MediaType.UNKNOWN:
            title = _TITLES[media_type][used[media_type] % len(_TITLES[media_type])]
            used[media_type] += 1
        item = _post(rng, n, media_type, title, now)
        seed.items.append(item)
        if item.source_key == "demo-bot" and title:
            seed.events.append((n, _event(
                event_kinds.BOT_ADDED, item.created_at, title=item.title, source=item.source_key, read_as="card",
            )))
        if status in (_ST.PARSE_FAILED, _ST.IGNORED, _ST.CHECKING):
            item.status = status
            if status is _ST.PARSE_FAILED:
                item.error_message = "No format of this source reads the post"
            continue

        # The lookups a real item gets, then the engine's own decision.
        watched = media_type in (_M, _S)
        hold = unwanted = None
        if watched and status is _ST.PENDING_REVIEW:
            hold = _HOLDS[holds % len(_HOLDS)]
            holds += 1
        elif watched and status is _ST.REJECTED:
            # Two in three by the policy's reject rule, the rest by a person.
            unwanted = rejections % 3 != 2
            hold = None if unwanted else "violence"
            rejections += 1
        tmdb = guide = None
        if watched:
            tmdb = _tmdb(rng, n, item, hold, bool(unwanted))
            guide = ParentsGuide(error="not listed yet") if status is _ST.AWAITING_DATA else _guide(rng, hold)
        decided = item.created_at + timedelta(minutes=rng.randint(1, 9))
        signals = gather.base_signals(
            item, cfg, tmdb=tmdb, guide=guide, book=None, book_looked_up=False,
            duplicate_of="item 12" if hold == "duplicate" else None, twin_of=None, now=decided,
        )
        decision = evaluate(
            item, signals, policy, now=decided, default_min_votes=cfg.integrations.imdb_guide.min_votes,
        )
        # The oldest items date from when the instance still ran in shadow mode.
        shadow = n >= _SHADOW_FROM and status in _AFTER_APPROVAL and decision.outcome == APPROVE
        verdict = Verdict(decision, act(decision, "shadow" if shadow else policy.auto_approve), signals)
        for column, value in decision_values(verdict, decided).items():
            setattr(item, column, value)
        item.auto_recommendation, flags = flags_of(decision)
        if tmdb is not None:
            title_flags = gather.title_rule_hits(policy, media_type.value, tmdb, cfg)
            item.content_check = ContentCheck(
                tmdb_id=tmdb.tmdb_id, tmdb_title=tmdb.title, imdb_id=tmdb.imdb_id, poster_url=tmdb.poster_url,
                genres=tmdb.genres, keywords=tmdb.keywords, plot=tmdb.plot, certifications=tmdb.certifications,
                sex_rating=guide.sex_rating, violence_rating=guide.violence_rating,
                profanity_rating=guide.profanity_rating, frightening_rating=guide.frightening_rating,
                alcohol_rating=guide.alcohol_rating, guide_votes=dict(guide.votes),
                title_rule_hit=bool(title_flags), title_rule_words=title_flags, checked_at=decided,
            )
        payload = {
            "title": item.title, "media_type": media_type.value, "outcome": decision.outcome,
            "done": verdict.applied.outcome, "reasons": [r.text for r in verdict.applied.reasons],
            "policy_version": decision.policy_version,
        }
        seed.events.append((n, _event(event_kinds.DECISION, decided, **payload)))
        if verdict.applied.shadow:
            seed.events.append((n, _event(event_kinds.WOULD_HAVE, decided, **payload)))
        if verdict.applied.outcome == REVIEW:
            seed.events.append((n, _event(event_kinds.NEEDS_YOU, decided, **payload)))

        # What happened after the decision.
        expected = {_ST.PENDING_REVIEW: REVIEW, _ST.AWAITING_DATA: WAIT}.get(status)
        if unwanted:
            expected = REJECT
        elif watched and status in _AFTER_APPROVAL:
            expected = APPROVE
        if expected and decision.outcome != expected:
            raise AssertionError(f"demo item {n} ({status.value}): the policy said {decision.outcome}, not {expected}")
        later = decided + timedelta(minutes=rng.randint(20, 600))
        item.updated_at = decided
        if status is _ST.REJECTED and not unwanted:
            item.status, item.reviewed_at, item.updated_at = _ST.REJECTED, later, later
        elif status in _AFTER_APPROVAL:
            if verdict.applied.outcome != APPROVE:
                # A person approved what the policy held, or what shadow mode left to them.
                item.reviewed_at, item.error_message = later, None
            item.status, item.updated_at = status, later
            if status in (_ST.DOWNLOADING, _ST.COMPLETED, _ST.NO_MATCH):
                search = _search(rng, n, item, later, found=status is not _ST.NO_MATCH)
                item.torrent_searches.append(search)
                if status is not _ST.NO_MATCH:
                    item.download = _download(rng, n, item, search, later, done=status is _ST.COMPLETED)
                    handed = {"title": item.title, "media_type": media_type.value, "target": "qbittorrent"}
                    seed.events.append((n, _event(event_kinds.DELIVERED, later, **handed)))
                    if item.download.completed_at is not None:
                        seed.events.append((n, _event(event_kinds.COMPLETED, item.download.completed_at, **handed)))
            if status is _ST.ERROR:
                item.error_message = "The download client did not answer"
                seed.events.append((n, _event(
                    event_kinds.DELIVERY_FAILED, later,
                    title=item.title, media_type=media_type.value, target="qbittorrent",
                )))
        elif status is _ST.PENDING_REVIEW and n % 3 == 0:
            # A person looked for releases and has not decided yet.
            item.torrent_searches.append(_search(rng, n, item, later, found=True))

    # One provider in an open cooldown (the banner), one that came back.
    first, second = PROVIDERS
    seed.events += [
        (None, _event(event_kinds.GUARD, now - timedelta(minutes=12), provider=first, state="cooldown",
                      reason="the session cookie was refused", cause="session", seconds=3600)),
        (None, _event(event_kinds.COOKIE_EXPIRED, now - timedelta(minutes=12), provider=first,
                      reason="the session cookie was refused")),
        (None, _event(event_kinds.GUARD, now - timedelta(days=2, hours=3), provider=second, state="cooldown",
                      reason="HTTP 429", cause="rate_limit", seconds=900)),
        (None, _event(event_kinds.GUARD, now - timedelta(days=2, hours=2), provider=second, state="ok")),
        # A chat the bot was added to and that is no source yet: Setup offers it.
        (None, _event(event_kinds.BOT_CHAT, now - timedelta(hours=5), chat_id=BOT_CHAT_ID, title="Film club",
                      state="joined", is_admin=False, reads_all_messages=False)),
    ]
    return seed


# --- writing them -----------------------------------------------------------

REFUSED = (
    "demo-seed: this database holds {items} item(s) and {events} event(s) that are not demo data. "
    "Nothing was written. Use an empty database, or --force to add the demo rows beside them."
)


async def seed(force: bool = False, now: datetime | None = None) -> tuple[int, int]:
    """Replace the demo rows. Returns (items, events) written; raises ``RuntimeError`` when refused."""
    from sqlalchemy import delete, func, select

    from vettarr.database import async_session

    demo_item = MediaItem.telegram_chat_id.in_(list(CHATS.values()))
    demo_event = Event.payload["demo"].as_boolean().is_(True)
    built = build(now)
    async with async_session() as session:
        real_items = (await session.execute(
            select(func.count()).select_from(MediaItem).where(~demo_item)
        )).scalar() or 0
        real_events = (await session.execute(
            select(func.count()).select_from(Event).where(demo_event.isnot(True))
        )).scalar() or 0
        if (real_items or real_events) and not force:
            raise RuntimeError(REFUSED.format(items=real_items, events=real_events))
        await session.execute(delete(Event).where(demo_event))
        await session.execute(delete(MediaItem).where(demo_item))
        session.add_all(built.items)
        await session.flush()
        for index, event in built.events:
            if index is not None:
                event.item_id = built.items[index].id
        session.add_all(event for _, event in built.events)
        await session.commit()
    return len(built.items), len(built.events)


async def _run(force: bool) -> tuple[int, int]:
    from vettarr.database import engine, init_db

    try:
        await init_db()
        return await seed(force)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vettarr demo-seed",
        description="Fill the database (POSTGRES_*) with fake items for screenshots and UI work.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="seed even when the database holds real rows. Only on a database you can throw away: "
             "the full app searches and downloads for the approved demo items like for any other",
    )
    parser.add_argument("--config", action="store_true", help="print the config.yaml the demo data belongs to, and stop")
    args = parser.parse_args(argv)
    if args.config:
        sys.stdout.write(config_text())
        return 0
    try:
        items, events = asyncio.run(_run(args.force))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"demo-seed: {items} items and {events} events written")
    print(
        "demo-seed: look at them with `vettarr web serve`. The full app would take the approved "
        "and searching ones for real items, and search and download for them.",
        file=sys.stderr,
    )
    return 0
