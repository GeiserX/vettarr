"""Gathering the signals one decision reads, as a JSON-ready snapshot.

The pure builders turn what the pipeline already looked up (the TMDB record,
the parents guide, the book verdict, the parsed post) into signal values;
every one of them is cheap, so the snapshot always carries them and a later
preview can evaluate a new rule against it. The lookups that cost a request
(Jellyfin, qBittorrent, a search, the linked .torrent, a database count) run
only when a rule of the item's type reads their signal. Each stands alone:
one that fails leaves its own signals missing and no other's. An item
waiting for data is decided again every hour, so the answers that cost the
most are kept between decisions: a .torrent's file list for good, a search
for six hours, the download client's free space for five minutes.

The value shapes are documented in ``vettarr.checker.policy``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from vettarr import dryrun
from vettarr.checker.book_client import BookVerdict
from vettarr.checker.content_rules import identity_problem, identity_reason
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.policy import (
    META_DUPLICATE,
    META_IDENTITY,
    META_NO_TITLE,
    applicable_rules,
    check_rule,
    missing,
    needed_signals,
    unavailable,
)
from vettarr.checker.tmdb_client import TMDBResult
from vettarr.config.schema import ReleaseWords
from vettarr.config.signals import SIGNALS

log = logging.getLogger(__name__)

# The English defaults, for a caller with no config at hand.
_DEFAULT_WORDS = ReleaseWords()

TITLE_SIGNALS = tuple(name for name, s in SIGNALS.items() if s.group == "title" and name != "year")
GUIDE_SIGNALS = tuple(name for name, s in SIGNALS.items() if s.group == "parents_guide")
BOOK_CATALOGUE_SIGNALS = ("subject", "language", "publisher", "pages")

# How many subjects a catalogue record needs before its silence means
# anything. Google Books often returns one coarse category ("Fiction"), and a
# single word like that cannot rule anything out; it can still flag a book.
MIN_BOOK_SUBJECTS = 3

_GUIDE_CATEGORIES = {
    "imdb.nudity": ("NUDITY", "sex_rating"),
    "imdb.violence": ("VIOLENCE", "violence_rating"),
    "imdb.profanity": ("PROFANITY", "profanity_rating"),
    "imdb.frightening": ("FRIGHTENING", "frightening_rating"),
    "imdb.alcohol": ("ALCOHOL", "alcohol_rating"),
}


def _value(enum_or_none: Any) -> Any:
    return getattr(enum_or_none, "value", enum_or_none)


def _media_type(item: Any) -> str:
    return str(_value(getattr(item, "media_type", None)) or "unknown")


# --- title metadata (TMDB) and the parents guide ----------------------------


def title_signals(tmdb: TMDBResult | None, *, tmdb_on: bool = True) -> dict[str, Any]:
    if not tmdb_on:
        return {name: unavailable("tmdb") for name in TITLE_SIGNALS}
    if tmdb is None or not tmdb.tmdb_id:
        return {name: missing("no TMDB match") for name in TITLE_SIGNALS}
    return {
        "certification": dict(tmdb.certifications),
        "genre": [*tmdb.genre_ids, *tmdb.genres],
        # An empty keyword list is TMDB's silence, not a clean answer.
        "keyword": list(tmdb.keywords) if tmdb.keywords else missing("TMDB lists no keywords"),
        "original_language": [tmdb.original_language] if tmdb.original_language else missing("no language"),
        "country": list(tmdb.countries) if tmdb.countries else missing("no country"),
        "runtime_minutes": tmdb.runtime if tmdb.runtime else missing("no runtime"),
        "rating": tmdb.vote_average if tmdb.vote_count else missing("no votes"),
        "vote_count": tmdb.vote_count if tmdb.vote_count is not None else missing("no votes"),
        "adult": tmdb.adult if tmdb.adult is not None else missing("not stated"),
        "collection": tmdb.collection if tmdb.collection is not None else missing("not stated"),
        "cast": list(tmdb.cast),
        "crew": list(tmdb.crew),
        "studio": list(tmdb.studios),
    }


def guide_signals(
    guide: ParentsGuide | None, tmdb: TMDBResult | None, *, enabled: bool = True
) -> dict[str, Any]:
    if not enabled:
        return {name: unavailable("imdb_guide") for name in GUIDE_SIGNALS}
    if tmdb is None or not tmdb.imdb_id:
        return {name: missing("no TMDB match with an IMDb id") for name in GUIDE_SIGNALS}
    if guide is None or not guide.fetched:
        why = f"parents guide not fetched ({guide.error})" if guide and guide.error else "parents guide not fetched"
        return {name: missing(why) for name in GUIDE_SIGNALS}
    out: dict[str, Any] = {}
    for name, (category, attr) in _GUIDE_CATEGORIES.items():
        out[name] = {
            "severity": _value(getattr(guide, attr)),
            "votes": int((guide.votes or {}).get(category, 0) or 0),
            "breakdown": dict((guide.breakdown or {}).get(category) or {}),
        }
    return out


def title_rule_hits(policy: Any, media_type: str, tmdb: TMDBResult | None, cfg: Any) -> list[str]:
    """The keyword and genre rules that hit on a TMDB record, for the content check."""
    snapshot = title_signals(tmdb, tmdb_on=cfg.integrations.enabled("tmdb"))
    hits: list[str] = []
    for rule in applicable_rules(policy, media_type):
        if rule.check not in ("keyword", "genre") or rule.on_hit not in ("review", "reject"):
            continue
        hit, text = check_rule(rule, snapshot.get(rule.check), policy.lists, 0)
        if hit:
            hits.append(text)
    return hits


def identity(tmdb: TMDBResult | None, item: Any) -> dict[str, Any] | None:
    """Whether the TMDB match is the title the post asked for; None without a match."""
    if tmdb is None or not tmdb.tmdb_id:
        return None
    problem = identity_problem(tmdb, getattr(item, "title", None), getattr(item, "year", None))
    if problem:
        return {"ok": False, "text": problem}
    return {"ok": True, "text": identity_reason(getattr(item, "year", None))}


# --- the release, from the post ----------------------------------------------

_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(TiB|TB|GiB|GB|MiB|MB)\b", re.IGNORECASE)
_SIZE_FACTOR = {"tb": 1024.0, "tib": 1024.0, "gb": 1.0, "gib": 1.0, "mb": 1 / 1024, "mib": 1 / 1024}
_RESOLUTION = re.compile(r"\b(2160p|1080p|1080i|720p|576p|480p|4k|uhd)\b", re.IGNORECASE)
_RELEASE_SOURCES = (
    ("CAM", r"hd-?cam|cam(?:rip)?"), ("TS", r"hd-?ts|telesync|ts(?:rip)?"), ("TC", r"telecine|tc"),
    ("SCR", r"(?:dvd)?scr(?:eener)?"), ("WEB-DL", r"web-?dl"), ("WEBRip", r"web-?rip"),
    ("REMUX", r"(?:bd)?remux"), ("BluRay", r"blu-?ray"), ("BDRip", r"bdrip|brrip"),
    ("HDTV", r"hdtv"), ("DVDRip", r"dvdrip"), ("HDRip", r"hdrip"),
)
_CODECS = (
    ("h264", r"x\.?264|h\.?264|avc"), ("h265", r"x\.?265|h\.?265|hevc"), ("av1", r"av1"),
    ("xvid", r"xvid"), ("vp9", r"vp9"), ("mpeg2", r"mpeg-?2"),
)
_HDR = re.compile(r"\b(?:hdr10\+?|hdr|dv|dovi|dolby\s*vision|hlg)\b", re.IGNORECASE)
_RELEASE_HINT = re.compile(
    r"\b(?:2160p|1080p|720p|480p|web-?dl|web-?rip|blu-?ray|bdrip|remux|hdtv|x26[45]|h\.?26[45]|hevc)\b",
    re.IGNORECASE,
)
_FREELEECH = re.compile(r"(\d+)\s*%\s*free|\bfree-?leech\b", re.IGNORECASE)
_EPISODE = re.compile(r"\bS\d{1,2}\s*E\d{1,3}\b|\b\d{1,2}x\d{2}\b", re.IGNORECASE)
_PACK = re.compile(r"\[PACK\]", re.IGNORECASE)
_SEASON_NUMBER = re.compile(r"S(\d{1,2})", re.IGNORECASE)
_FORMATS = ("epub", "pdf", "mobi", "azw3", "azw", "cbz", "cbr", "mp3", "m4b", "m4a", "flac", "djvu", "fb2")
_FORMAT = re.compile(r"\b(" + "|".join(_FORMATS) + r")\b", re.IGNORECASE)


def size_gb(text: str | None) -> float | None:
    """A size as posts write it ("8.4 GiB", "700 MB") in GB, 1 GB = 1024 MB."""
    m = _SIZE.search(text or "")
    if not m:
        return None
    return round(float(m.group(1).replace(",", ".")) * _SIZE_FACTOR[m.group(2).lower()], 3)


def _resolution(*texts: str | None) -> str | None:
    for text in texts:
        m = _RESOLUTION.search(text or "")
        if m:
            word = m.group(1).lower()
            return {"4k": "2160p", "uhd": "2160p", "1080i": "1080p"}.get(word, word)
    return None


def _tokens(table, text: str) -> list[str]:
    return [name for name, pattern in table if re.search(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])", text, re.IGNORECASE)]


# --- release words: how a post says a language, subtitles, a whole season -----
#
# The words come from the `release_words` block of config.yaml; the schema's
# defaults are English. Nothing of any group's wording is written here.


def _word_source(word: str) -> str:
    """One word as regex source: ``/regex/`` as written, anything else literal."""
    if len(word) > 2 and word.startswith("/") and word.endswith("/"):
        return word[1:-1]
    return r"\s+".join(re.escape(part) for part in word.split())


def _any_word(words: Iterable[str]) -> str | None:
    sources = [_word_source(word) for word in words]
    return "(?:" + "|".join(sources) + ")" if sources else None


class _Words:
    """A ``release_words`` block, compiled once."""

    def __init__(self, words: ReleaseWords) -> None:
        self.languages = tuple(
            (code, source) for code, listed in words.languages.items() if (source := _any_word(listed))
        )
        subs = _any_word(words.subtitles)
        # The subtitle word, then what the line says next: its languages.
        self.subtitles = (
            re.compile(rf"(?<!\w){subs}(?!\w)[\s:.\-]*([^\n]{{0,60}})", re.IGNORECASE) if subs else None
        )
        self.subtitle_tags = tuple(
            (code, re.compile(rf"(?<!\w){source}(?!\w)", re.IGNORECASE))
            for code, listed in words.subtitle_tags.items() if (source := _any_word(listed))
        )
        season = _any_word(words.full_season)
        self.full_season = re.compile(rf"(?<!\w){season}(?!\w)", re.IGNORECASE) if season else None


# The block the words were compiled for, and its compiled form.
_words_cache: tuple[ReleaseWords, _Words] | None = None


def _compiled(words: ReleaseWords | None) -> _Words:
    global _words_cache
    if words is None:
        words = _DEFAULT_WORDS
    if _words_cache is None or _words_cache[0] is not words:
        _words_cache = (words, _Words(words))
    return _words_cache[1]


def without_title(text: str, title: str | None) -> str:
    """The post with its title taken out, so no title word is read as a release tag.

    "Cam (2018) 1080p WEB-DL" is not a cam copy and "The Italian Job" is not
    in Italian. The title goes once per line, wherever it stands as a run of
    whole words, however they are joined (spaces, dots, underscores): the
    second "CAM" of "Cam.2018.CAM.x264" is still read.
    """
    words = re.findall(r"[^\W_]+", title or "")
    if not words:
        return text
    pattern = re.compile(
        r"(?<![^\W_])" + r"[\W_]+".join(re.escape(w) for w in words) + r"(?![^\W_])", re.IGNORECASE,
    )
    return "\n".join(pattern.sub(" ", line, count=1) for line in text.splitlines())


def release_signals(item: Any, words: ReleaseWords | None = None) -> dict[str, Any]:
    """What the post itself says about the release.

    ``words`` is the config's ``release_words`` block; without it the English
    defaults read the languages and the subtitles.
    """
    known = _compiled(words)
    raw = getattr(item, "raw_text", None) or ""
    title = getattr(item, "title", None) or ""
    text = f"{title}\n{raw}"
    out: dict[str, Any] = {}

    size = size_gb(getattr(item, "size_text", None)) or size_gb(raw)
    out["size_gb"] = size if size is not None else missing("the post states no size")
    resolution = _resolution(getattr(item, "quality", None), raw)
    out["resolution"] = resolution or missing("the post states no resolution")

    # Tags are words, and so are titles: these three read the post without
    # its title, plus the source the parser read off the release name.
    tags = without_title(raw, title)
    sources = _tokens(_RELEASE_SOURCES, f"{getattr(item, 'source', None) or ''}\n{tags}")
    out["release_source"] = sources or missing("the post states no source")
    codecs = _tokens(_CODECS, tags)
    out["codec"] = codecs or missing("the post states no codec")
    if _HDR.search(text):
        out["hdr"] = True
    else:
        # A release name with no HDR tag is SDR; a post with no release name
        # at all says nothing either way.
        out["hdr"] = False if _RELEASE_HINT.search(text) else missing("the post names no release")

    # What follows a subtitle word is the subtitles' languages, not the audio's.
    spoken = known.subtitles.sub(" ", tags) if known.subtitles is not None else tags
    audio = _tokens(known.languages, spoken)
    out["audio_language"] = audio or missing("the post names no audio language")
    subs: list[str] = []
    if known.subtitles is not None:
        for m in known.subtitles.finditer(text):
            subs += [code for code in _tokens(known.languages, m.group(1)) if code not in subs]
    subs += [code for code, tag in known.subtitle_tags if code not in subs and tag.search(text)]
    out["subtitles"] = subs or missing("the post names no subtitles")

    group = getattr(item, "release_group", None)
    out["release_group"] = [group] if group else missing("the post names no group")
    m = _FREELEECH.search(text)
    if m:
        out["freeleech"] = (int(m.group(1)) >= 100) if m.group(1) else True
    else:
        out["freeleech"] = missing("the post does not say")

    formats = sorted({f.lower() for f in _FORMAT.findall(text)})
    out["format"] = formats or missing("the post names no format")
    return out


def series_signals(
    tmdb: TMDBResult | None, item: Any, *, tmdb_on: bool = True, words: ReleaseWords | None = None
) -> dict[str, Any]:
    raw = f"{getattr(item, 'title', None) or ''}\n{getattr(item, 'raw_text', None) or ''}"
    season = getattr(item, "season", None)
    full_season = _compiled(words).full_season
    out: dict[str, Any] = {}
    single = _EPISODE.search(raw)
    if single:
        out["season_complete"] = False
    elif season or _PACK.search(raw) or (full_season is not None and full_season.search(raw)):
        out["season_complete"] = True
    else:
        out["season_complete"] = missing("the post names no season")

    if not tmdb_on:
        out["status"] = unavailable("tmdb")
        out["episode_count"] = unavailable("tmdb")
        return out
    if tmdb is None or not tmdb.tmdb_id:
        out["status"] = missing("no TMDB match")
        out["episode_count"] = missing("no TMDB match")
        return out
    status = (tmdb.status or "").lower().replace(" series", "").strip()
    out["status"] = [status] if status else missing("TMDB states no status")
    if single:
        out["episode_count"] = 1
    else:
        number = _SEASON_NUMBER.search(season or "")
        count = tmdb.season_episodes.get(int(number.group(1))) if number else tmdb.number_of_episodes
        out["episode_count"] = count if count is not None else missing("TMDB states no episode count")
    return out


_LANGUAGE_CODES = {
    "spa": "es", "eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "cat": "ca", "glg": "gl", "baq": "eu", "eus": "eu",
    "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "rus": "ru", "dut": "nl", "nld": "nl",
}


def book_signals(book: BookVerdict | None, *, looked_up: bool) -> dict[str, Any]:
    if book is None:
        why = "no lookup was run" if not looked_up else "no catalogue answer"
        return {name: missing(f"no catalogue classification ({why})") for name in BOOK_CATALOGUE_SIGNALS}
    if book.error:
        why = f"lookup failed: {book.error}"
    elif not book.matched:
        why = "no confident title match"
    else:
        why = ""
    if why:
        return {name: missing(f"no catalogue classification ({why})") for name in BOOK_CATALOGUE_SIGNALS}
    subjects = list(book.subjects)
    languages: list[str] = []
    for code in book.languages:
        short = _LANGUAGE_CODES.get(code.lower(), code.lower())
        if short not in languages:
            languages.append(short)
    return {
        # Too few subjects can still flag a book, never clear one.
        "subject": subjects if len(subjects) >= MIN_BOOK_SUBJECTS else {"partial": subjects},
        "language": languages or missing("the catalogue states no language"),
        "publisher": list(book.publishers) or missing("the catalogue states no publisher"),
        "pages": book.pages if book.pages else missing("the catalogue states no page count"),
    }


def context_now(now: datetime) -> str:
    """The local clock, HH:MM, for `time_window` (TZ is the container's)."""
    return now.astimezone().strftime("%H:%M")


def source_key(item: Any, cfg: Any) -> str | None:
    key = getattr(item, "source_key", None)
    if key:
        return str(key)
    chat = getattr(item, "telegram_chat_id", None)
    for source in cfg.sources:
        if source.chat is not None and str(source.chat) == str(chat):
            return source.key
    return None


def base_signals(
    item: Any,
    cfg: Any,
    *,
    tmdb: TMDBResult | None,
    guide: ParentsGuide | None,
    book: BookVerdict | None,
    book_looked_up: bool,
    duplicate_of: str | None,
    twin_of: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Every signal that costs nothing more than what was already looked up."""
    from vettarr.checker.content_rules import CONCURRENT_DUPLICATE

    media_type = _media_type(item)
    integrations = cfg.integrations
    snapshot: dict[str, Any] = {}
    if duplicate_of:
        snapshot[META_DUPLICATE] = f"duplicate of {duplicate_of}"
    elif twin_of:
        snapshot[META_DUPLICATE] = CONCURRENT_DUPLICATE
    if not (getattr(item, "title", None) or "").strip():
        snapshot[META_NO_TITLE] = True

    if media_type in ("movie", "series"):
        snapshot.update(title_signals(tmdb, tmdb_on=integrations.enabled("tmdb")))
        snapshot.update(guide_signals(guide, tmdb, enabled=integrations.enabled("imdb_guide")))
        ident = identity(tmdb, item)
        if ident is not None:
            snapshot[META_IDENTITY] = ident
        if media_type == "series":
            snapshot.update(
                series_signals(tmdb, item, tmdb_on=integrations.enabled("tmdb"), words=cfg.release_words)
            )
    else:
        snapshot.update(book_signals(book, looked_up=book_looked_up))
    year = (tmdb.year if tmdb is not None and tmdb.year else None) or getattr(item, "year", None)
    snapshot["year"] = year if year else missing("no year")
    snapshot.update(release_signals(item, cfg.release_words))

    key = source_key(item, cfg)
    snapshot["source"] = [key] if key else missing("the item's source is not configured")
    snapshot["time_window"] = context_now(now)
    sender = getattr(item, "sender_id", None)
    snapshot["sender"] = [str(sender)] if sender else missing("the item does not record its sender")
    posted = getattr(item, "posted_at", None)
    if isinstance(posted, datetime):
        posted = posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)
        snapshot["post_age_hours"] = round((now - posted).total_seconds() / 3600, 2)
    else:
        snapshot["post_age_hours"] = missing("the item does not record when it was posted")
    return snapshot


# --- the lookups that cost a request ----------------------------------------

_SEARCH_SIGNALS = {"seeders"}
_TORRENT_SIGNALS = {"file_extensions", "file_count"}

# How long an answer is kept between decisions.
DISK_FREE_SECONDS = 300.0
SEARCH_SECONDS = 6 * 3600.0
_KEPT_MAX = 2000
# Indirection so tests can drive the clock.
_clock = time.monotonic
_kept: dict[tuple, tuple[float, Any]] = {}


def _recall(key: tuple) -> Any:
    hit = _kept.get(key)
    if hit is None:
        return None
    if hit[0] <= _clock():
        _kept.pop(key, None)
        return None
    return hit[1]


def _keep(key: tuple, value: Any, seconds: float) -> None:
    if len(_kept) >= _KEPT_MAX:
        now = _clock()
        for stale in [k for k, (until, _value) in _kept.items() if until <= now]:
            del _kept[stale]
        if len(_kept) >= _KEPT_MAX:
            _kept.clear()
    _kept[key] = (_clock() + seconds, value)


def forget_kept() -> None:
    """Drop every kept answer. For tests."""
    _kept.clear()


def _bdecode(data: bytes, pos: int = 0) -> tuple[Any, int]:
    """Decode one bencoded value. Raises ValueError on a malformed torrent."""
    lead = data[pos:pos + 1]
    if lead == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if lead == b"l":
        pos += 1
        items = []
        while data[pos:pos + 1] != b"e":
            value, pos = _bdecode(data, pos)
            items.append(value)
        return items, pos + 1
    if lead == b"d":
        pos += 1
        mapping = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            mapping[key], pos = _bdecode(data, pos)
        return mapping, pos + 1
    if lead.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        if start + length > len(data):
            raise ValueError("truncated string")
        return data[start:start + length], start + length
    raise ValueError("not bencode")


def torrent_files(data: bytes) -> list[str] | None:
    """The file names a .torrent holds; None when it cannot be read."""
    try:
        meta, _ = _bdecode(data)
        info = meta[b"info"]
    except (ValueError, KeyError, TypeError, IndexError, RecursionError):
        return None
    # The file may be one a stranger posted: every shape is checked.
    if not isinstance(info, dict):
        return None
    files = info.get(b"files")
    if isinstance(files, list):
        names = []
        for entry in files:
            path = entry.get(b"path") if isinstance(entry, dict) else None
            if isinstance(path, list) and path and isinstance(path[-1], bytes):
                names.append(path[-1].decode("utf-8", "replace"))
        return names
    name = info.get(b"name")
    return [name.decode("utf-8", "replace")] if isinstance(name, bytes) else None


def _extension(name: str) -> str | None:
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem and 1 <= len(ext) <= 5 else None


def file_signals(names: list[str]) -> dict[str, Any]:
    extensions = sorted({e for e in (_extension(n) for n in names) if e})
    return {"file_count": len(names), "file_extensions": extensions}


async def _latest_search(session, item_id: int):
    from sqlalchemy import select

    from vettarr.models import TorrentSearch

    result = await session.execute(
        select(TorrentSearch)
        .where(TorrentSearch.media_item_id == item_id)
        .order_by(TorrentSearch.searched_at.desc())
        .limit(1)
    )
    found = result.scalars().first()
    return found if isinstance(found, TorrentSearch) else None


async def _count(session, statement) -> int | None:
    value = (await session.execute(statement)).scalar_one_or_none()
    return value if isinstance(value, int) else None


async def _issue_newer_than_owned(session, item: Any) -> Any:
    """The 0.11.0 magazine comparison: is this issue newer than every owned one?"""
    from sqlalchemy import select

    from vettarr import pipeline
    from vettarr.models import MediaItem, MediaType

    own = pipeline._edition(pipeline._dup_title(item))
    if own is None:
        return missing("the title names no issue")
    base = pipeline.normalize_title(pipeline._without_edition(item.title), MediaType.MAGAZINE)
    rows = await session.execute(
        select(MediaItem.title, MediaItem.year).where(
            MediaItem.id != item.id,
            MediaItem.media_type == MediaType.MAGAZINE,
            MediaItem.status.in_(list(pipeline._SETTLED_STATUSES)),
        )
    )
    owned = []
    for title, year in rows:
        if not isinstance(title, str):
            continue
        if pipeline.normalize_title(pipeline._without_edition(title), MediaType.MAGAZINE) != base:
            continue
        edition = pipeline._edition(f"{title} {year}" if year and not re.search(r"\b(?:19|20)\d{2}\b", title) else title)
        if edition is not None:
            owned.append(edition)
    if not owned:
        return True
    def key(edition):
        if edition.month is None or edition.year is None:
            return None
        return (edition.year, edition.month)

    for other in owned:
        if own.issue is not None and other.issue is not None:
            if own.issue <= other.issue:
                return False
            continue
        a, b = key(own), key(other)
        if a is None or b is None:
            return missing("an owned issue cannot be ordered against this one")
        if a <= b:
            return False
    return True


async def _guarded(names: Iterable[str], lookup: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """One lookup. A failure costs its own signals and nothing else.

    The class of the error is logged, and a traceback only for an error that
    is not an HTTP one: an HTTP error's text can carry the URL it asked.
    """
    try:
        return await lookup()
    except Exception as exc:
        names = sorted(names)
        log.warning(
            "The lookup for %s failed (%s); it counts as missing",
            ", ".join(names), type(exc).__name__,
            exc_info=not isinstance(exc, httpx.HTTPError),
        )
        return {name: missing("the lookup failed") for name in names}


async def costly_signals(
    session, item: Any, cfg: Any, needed: set[str], *, tmdb: TMDBResult | None, now: datetime,
    size_known: bool = True,
) -> dict[str, Any]:
    """The signals a rule of this type reads that need a request to answer."""
    from sqlalchemy import func, select

    from vettarr import pipeline
    from vettarr.models import Download, DownloadStatus, ItemStatus, MediaItem

    integrations = cfg.integrations
    media_type = _media_type(item)

    async def in_library() -> dict[str, Any]:
        from vettarr import delivery
        from vettarr.checker.jellyfin_client import client_for

        # Two libraries can say it is there: Jellyfin, and the arr this type
        # is delivered to. Either saying yes is a yes.
        client = client_for(cfg)
        arr = delivery.library_of(media_type, cfg)
        if client is None and arr is None:
            return {"in_library": unavailable("jellyfin")}
        if tmdb is None or not tmdb.tmdb_id:
            return {"in_library": missing("no TMDB id to look up")}
        answers: dict[str, bool | None] = {}
        if client is not None:
            answers["Jellyfin"] = await client.has_tmdb_id(
                tmdb.tmdb_id, media_type, (tmdb.title, tmdb.original_title, getattr(item, "title", None))
            )
        if arr is not None and answers.get("Jellyfin") is not True:
            answers[delivery.LABELS[arr]] = await delivery.in_library(item, tmdb.tmdb_id, cfg)
        if True in answers.values():
            return {"in_library": True}
        silent = [name for name, answer in answers.items() if answer is None]
        if silent:
            # One library that could not be asked leaves the question open.
            return {"in_library": missing(" and ".join(silent) + " did not answer")}
        return {"in_library": False}

    async def already_requested() -> dict[str, Any]:
        # Another post of the same title somebody already asked for and that
        # is not settled yet: waiting, held for review, approved or on its way.
        statuses = (
            ItemStatus.APPROVED, ItemStatus.SEARCHING, ItemStatus.DOWNLOADING,
            ItemStatus.AWAITING_DATA, ItemStatus.PENDING_REVIEW, ItemStatus.CHECKING,
        )
        if not getattr(item, "title", None):
            return {"already_requested": missing("no title to compare")}
        return {"already_requested": bool(await pipeline._matching_title(session, item, statuses))}

    async def in_download_client() -> dict[str, Any]:
        if not integrations.enabled("qbittorrent"):
            return {"in_download_client": unavailable("qbittorrent")}
        if not getattr(item, "title", None):
            return {"in_download_client": missing("no title to compare")}
        title = pipeline._dup_title(item)
        names = await pipeline._get_qbt_names()
        return {"in_download_client": any(pipeline.titles_match(title, n, item.media_type) for n in names)}

    async def disk_free_gb() -> dict[str, Any]:
        if not integrations.enabled("qbittorrent"):
            return {"disk_free_gb": unavailable("qbittorrent")}
        # The only place qBittorrent states it is its whole sync record, every
        # torrent included, so one answer serves every decision for a while.
        free = _recall(("disk_free",))
        if free is None:
            try:
                resp = await pipeline._qbt._request("GET", "/api/v2/sync/maindata")
                free = resp.json().get("server_state", {}).get("free_space_on_disk")
            except Exception as exc:
                log.warning("qBittorrent free space could not be read: %s", type(exc).__name__)
                free = None
            if not isinstance(free, (int, float)) or isinstance(free, bool):
                return {"disk_free_gb": missing("qBittorrent did not answer")}
            _keep(("disk_free",), free, DISK_FREE_SECONDS)
        return {"disk_free_gb": round(free / 1024 ** 3, 2)}

    async def max_active_downloads() -> dict[str, Any]:
        count = await _count(
            session, select(func.count(Download.id)).where(Download.status == DownloadStatus.DOWNLOADING)
        )
        return {"max_active_downloads": count if count is not None else missing("count unavailable")}

    async def per_sender_per_week() -> dict[str, Any]:
        sender = getattr(item, "sender_id", None)
        if not sender:
            return {"per_sender_per_week": missing("the item does not record its sender")}
        count = await _count(
            session,
            select(func.count(MediaItem.id)).where(
                MediaItem.sender_id == sender, MediaItem.created_at >= now - timedelta(days=7)
            ),
        )
        return {"per_sender_per_week": count if count is not None else missing("count unavailable")}

    async def issue_newer_than_owned() -> dict[str, Any]:
        return {"issue_newer_than_owned": await _issue_newer_than_owned(session, item)}

    lookups: list[tuple[set[str], Callable[[], Awaitable[dict[str, Any]]]]] = []
    for name, lookup in (
        ("in_library", in_library), ("already_requested", already_requested),
        ("in_download_client", in_download_client), ("disk_free_gb", disk_free_gb),
        ("max_active_downloads", max_active_downloads), ("per_sender_per_week", per_sender_per_week),
    ):
        if name in needed:
            lookups.append(({name}, lookup))
    if "issue_newer_than_owned" in needed and media_type == "magazine" and getattr(item, "title", None):
        lookups.append(({"issue_newer_than_owned"}, issue_newer_than_owned))
    if needed & _SEARCH_SIGNALS or ("size_gb" in needed and not size_known):
        lookups.append((needed & _SEARCH_SIGNALS, lambda: _search_signals(session, item, tmdb, needed, cfg)))
    if needed & _TORRENT_SIGNALS:
        lookups.append((set(_TORRENT_SIGNALS), lambda: _torrent_signals(item, cfg)))

    out: dict[str, Any] = {}
    for names, lookup in lookups:
        out.update(await _guarded(names, lookup))
    return out


async def _search_signals(session, item: Any, tmdb: TMDBResult | None, needed: set[str], cfg: Any) -> dict[str, Any]:
    """Seeders (and a size the post did not state) of the release that would download.

    The item's stored search when it has one. Else the one search the worker
    runs after an approval (Prowlarr, then each provider's own search), so
    the rule reads the release that would be added. A post that carries or
    links its own torrent downloads that torrent, which no search describes:
    its seeders are missing, not another release's.
    """
    def gap(value: dict[str, str]) -> dict[str, Any]:
        return {"seeders": value} if "seeders" in needed else {}

    stored = await _latest_search(session, item.id)
    if stored is not None and stored.best_match_title:
        seeders, size = stored.best_match_seeders, stored.best_match_size
    else:
        from vettarr import ingest
        from vettarr.searcher import providers
        from vettarr.searcher.search_service import NO_SEARCH, SearchItem, search_for

        links = list(getattr(item, "external_links", None) or [])
        if ingest.direct_add_of(item, cfg) or providers.linked_torrent(cfg.providers, links) is not None:
            return gap(missing("the post's own torrent is what downloads, and no search describes it"))
        if not getattr(item, "title", None):
            return gap(missing("no title to search for"))
        found = _recall(("search", item.id))
        if found is None:
            result = await search_for(
                SearchItem(
                    title=item.title, year=item.year, season=item.season, media_type=item.media_type,
                    tmdb_title=tmdb.title if tmdb else None,
                    tmdb_id=tmdb.tmdb_id if tmdb else None, imdb_id=tmdb.imdb_id if tmdb else None,
                ),
                cfg,
            )
            if result.error == NO_SEARCH:
                return gap(unavailable("prowlarr"))
            if result.error is not None:
                return gap(missing("the search failed"))
            best = result.results[0] if result.results else None
            found = (bool(best), getattr(best, "seeders", None), getattr(best, "size", None))
            _keep(("search", item.id), found, SEARCH_SECONDS)
        if not found[0]:
            return gap(missing("the search found nothing"))
        _any, seeders, size = found
    out: dict[str, Any] = {}
    if "seeders" in needed:
        out["seeders"] = seeders if isinstance(seeders, int) else missing("no seeders stated")
    if "size_gb" in needed and isinstance(size, int) and size > 0:
        out["size_gb"] = round(size / 1024 ** 3, 3)
    return out


def _read_before(item: Any) -> dict[str, Any] | None:
    """The file list an earlier decision of this item read, if it read one."""
    previous = getattr(item, "signals", None)
    if not isinstance(previous, Mapping):
        return None
    count, extensions = previous.get("file_count"), previous.get("file_extensions")
    if isinstance(count, int) and not isinstance(count, bool) and isinstance(extensions, list):
        return {"file_count": count, "file_extensions": list(extensions)}
    return None


async def _torrent_signals(item: Any, cfg: Any) -> dict[str, Any]:
    """File count and extensions from the item's own .torrent, else the one its post links to."""
    from vettarr import ingest

    # A .torrent's file list never changes: what an earlier decision of this
    # item read stands, and the tracker is not asked for the file again.
    known = _read_before(item)
    if known is not None:
        return known
    # A direct add downloads the torrent the post carried, so that is the
    # file list a rule has to judge. A magnet names its torrent and no files.
    own = ingest.direct_add_of(item, cfg)
    if own == "torrent file":
        data = ingest.own_torrent(item.id)
        names = torrent_files(data) if data else None
        if not names:
            return {name: missing("the item's own torrent could not be read") for name in _TORRENT_SIGNALS}
        return file_signals(names)
    if own == "magnet":
        return {name: missing("a magnet carries no file list") for name in _TORRENT_SIGNALS}
    links = list(getattr(item, "external_links", None) or [])
    if not links:
        return {name: missing("the post links no torrent") for name in _TORRENT_SIGNALS}
    # Fetched with the session of the provider whose link pattern reads the
    # link, whatever that session comes from.
    from vettarr.searcher.search_service import fetch_linked_torrent, linked_not_fetched

    fetched = await fetch_linked_torrent(links)
    if fetched is None and linked_not_fetched(links) is not None:
        # The tracker counts a fetch as a download, so a dry run did not fetch it.
        return {name: unavailable("dry_run", dryrun.NOT_FETCHED) for name in _TORRENT_SIGNALS}
    names = torrent_files(fetched.data) if fetched else None
    if not names:
        return {name: missing("the linked torrent could not be read") for name in _TORRENT_SIGNALS}
    return file_signals(names)


async def gather_signals(
    session,
    item: Any,
    cfg: Any,
    *,
    tmdb: TMDBResult | None,
    guide: ParentsGuide | None,
    book: BookVerdict | None,
    book_looked_up: bool,
    duplicate_of: str | None,
    twin_of: str | None,
    now: datetime,
    lookups: bool = True,
) -> dict[str, Any]:
    """The whole snapshot for one decision. ``lookups=False`` skips every request."""
    snapshot = base_signals(
        item, cfg, tmdb=tmdb, guide=guide, book=book, book_looked_up=book_looked_up,
        duplicate_of=duplicate_of, twin_of=twin_of, now=now,
    )
    if lookups and not snapshot.get(META_DUPLICATE):
        needed = needed_signals(cfg.policy, _media_type(item))
        size_known = not isinstance(snapshot.get("size_gb"), dict)
        snapshot.update(await costly_signals(
            session, item, cfg, needed, tmdb=tmdb, now=now, size_known=size_known,
        ))
    return snapshot
