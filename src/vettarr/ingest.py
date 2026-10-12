"""How a post becomes an item, whichever way it arrives.

The userbot (Telethon) and the bot (python-telegram-bot) each turn their own
message object into an ``Incoming`` and hand it to ``ingest(source, message)``;
everything after that is shared. What a message can be:

- a post with text, or a caption: stored as written, the parser reads it.
- a forwarded channel post: its ``origin_key`` is the original's chat and
  message id, so a post already in the inbox is not added a second time.
- a ``.torrent`` attachment or a ``magnet:`` link: a direct add. The title
  comes from the torrent's name, and once approved the torrent itself goes to
  the download client with no search (``add_direct``). Only from a sender the
  source's ``direct_adds`` lets do that; from anyone else the message is an
  item by that name and is searched for like any post.
- an IMDb, TMDB or TVDB link: the id is read from the URL and TMDB names the
  title (``/find`` for IMDb and TVDB ids). Needs the tmdb integration.
- a tracker details link: handed to the linked-torrent path, which fetches
  the exact torrent; its name is the title.
- a plain title line ("Some Film 2024"): the title and year are split off and a
  TMDB search names the type.

A Seerr request is not a message. ``vettarr.seerr_source`` names it from
its TMDB id and hands it to ``add``.

``ingest`` asks the database whether the message is already an item before it
downloads or looks up anything, so reading a history again costs one query a
message.

Also here, because they act on a post by its source: ``PostActions`` (what
``on_download`` and ``on_reject`` do to the post, on the item's own chat),
``held_for_review`` (``notify_on_review``) and ``bot_added`` (the bot was
added to a chat).
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vettarr import dryrun, events
from vettarr.config import settings
from vettarr.database import async_session
from vettarr.models import OP_APPROVAL, Event, ItemStatus, MediaItem, MediaType
from vettarr.parser.regex_parser import has_release_structure, should_skip_message

if TYPE_CHECKING:
    from vettarr.config.schema import Config, Source
    from vettarr.searcher.search_service import ItemRef

log = logging.getLogger(__name__)

#: Where a ``.torrent`` attachment waits until its item is approved, as
#: ``<item id>.torrent``. When it is gone the item is searched for instead.
TORRENTS_DIR = Path("/data/torrents")
#: Where a post's photo is kept, as ``<chat id>_<message id>.jpg``.
PHOTOS_DIR = Path("/data/photos")
#: The largest ``.torrent`` attachment that is downloaded: what the Bot API
#: lets a bot fetch. A real one is far smaller; a film renamed ``x.torrent``
#: is not read into memory.
MAX_TORRENT_BYTES = 20 * 1024 * 1024

_TMDB_BASE = "https://api.themoviedb.org/3"

# Reactions Telegram accepts on any chat with default reactions.
REACTIONS = {"download": "\U0001f44d", "reject": "\U0001f44e"}

_MAGNET = re.compile(r"magnet:\?[^\s<>\"']+", re.IGNORECASE)
_URL = re.compile(r"https?://\S+")
_IMDB = re.compile(r"imdb\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?title/(tt\d{5,})", re.IGNORECASE)
_TMDB = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.IGNORECASE)
_TVDB_ID = re.compile(
    r"thetvdb\.com/(?:\?\S*?\bid=(\d+)|(?:dereferrer/)?series/(\d+)(?:[/?#\s]|$))", re.IGNORECASE
)
_TVDB_SLUG = re.compile(r"thetvdb\.com/series/([a-z0-9][a-z0-9-]*)", re.IGNORECASE)

_YEAR = re.compile(r"\(?((?:19|20)\d{2})\)?")
_SEASON = re.compile(r"S(\d{1,2})(?:E\d{1,3})*", re.IGNORECASE)
# Tokens that end the title part of a release name.
_RELEASE_MARKER = re.compile(
    r"\d{3,4}[pi]|4K|UHD|WEB-?DL|WEB-?Rip|WEB|BluRay|BDRip|BRRip|HDTV|DVDRip|HDRip|REMUX"
    r"|AMZN|NF|DSNP|HMAX|ATVP|[xh]\.?26[45]|HEVC|AVC|AV1|HDR\d*|DV|MULTI|DUAL|PROPER|REPACK"
    r"|COMPLETE|PACK|EPUB|PDF|MOBI|AZW3|CBZ|CBR|M4B|MP3|FLAC",
    re.IGNORECASE,
)
_VIDEO_MARKER = re.compile(
    r"\d{3,4}[pi]|4K|UHD|WEB-?DL|WEB-?Rip|BluRay|BDRip|BRRip|HDTV|DVDRip|HDRip|REMUX|[xh]\.?26[45]|HEVC",
    re.IGNORECASE,
)
_FILE_EXT = re.compile(r"\.(torrent|mkv|mp4|avi|epub|pdf|mobi|azw3|cbz|cbr|m4b|mp3|flac)$", re.IGNORECASE)
_EXT_TYPES = {
    ".epub": MediaType.EBOOK, ".mobi": MediaType.EBOOK, ".azw3": MediaType.EBOOK,
    ".pdf": MediaType.EBOOK, ".m4b": MediaType.AUDIOBOOK,
    ".cbz": MediaType.COMIC, ".cbr": MediaType.COMIC,
    ".mkv": MediaType.MOVIE, ".mp4": MediaType.MOVIE, ".avi": MediaType.MOVIE,
}

# A plain title is one short line.
_TITLE_LINE_MAX_WORDS = 8
_TITLE_LINE_MAX_CHARS = 120

Kind = Literal["post", "torrent", "magnet", "id_link", "tracker_link", "title"]


# ---------------------------------------------------------------------------
# What arrives
# ---------------------------------------------------------------------------


@dataclass
class Incoming:
    """One message, as either Telegram library delivered it."""

    chat_id: int
    message_id: int
    #: Text or caption, with the URLs of text links appended.
    text: str = ""
    #: The sender's user id; None when no person sent it (a channel post, an
    #: anonymous admin). For a forward this is who forwarded it.
    sender_id: int | None = None
    #: (chat id, message id) of the channel post this message forwards.
    forward_from: tuple[int, int] | None = None
    document_name: str | None = None
    #: Downloads the attached document; only called for a ``.torrent``.
    fetch_document: Callable[[], Awaitable[bytes]] | None = None
    #: The attached document's size in bytes, as Telegram states it.
    document_size: int | None = None
    #: When the message was sent, as Telegram dates it.
    posted_at: datetime | None = None

    @property
    def origin_key(self) -> str:
        chat_id, message_id = self.forward_from or (self.chat_id, self.message_id)
        return f"{chat_id}:{message_id}"


@dataclass
class Resolved:
    """What intake already knows about the item; the parser fills the rest."""

    title: str | None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    season: str | None = None


@dataclass
class IngestResult:
    outcome: Literal["added", "duplicate", "skipped", "denied"]
    kind: str = "post"
    item_id: int | None = None
    title: str | None = None

    def reply(self) -> str | None:
        """What the bot answers in a private chat; None says nothing."""
        if self.outcome == "added":
            return f"Added: {self.title}" if self.title else "Added; reading it now."
        if self.outcome == "duplicate":
            return "Already in your inbox."
        if self.outcome == "skipped":
            return "Nothing to add in that message."
        return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _config(config: Config | None) -> Config:
    return config if config is not None else settings.config


def reviewers(config: Config | None = None) -> list[int]:
    tg = _config(config).telegram
    return list(tg.reviewers) if tg else []


def sender_allowed(source: Source, sender_id: int | None, config: Config | None = None) -> bool:
    """Whether ``allowed_senders`` lets this sender in.

    No sender (a channel post, an anonymous admin) gets in only where anyone may.
    """
    allowed = source.allowed_senders
    if allowed == "anyone":
        return True
    if sender_id is None:
        return False
    if allowed == "reviewers":
        return sender_id in reviewers(config)
    return sender_id in (allowed or [])


def bot_source_for(
    chat_id: int, chat_type: str, username: str | None, config: Config | None = None,
) -> Source | None:
    """The bot source a chat belongs to: its id, its @username, or ``private`` for DMs."""
    for source in _config(config).sources:
        if source.kind != "bot":
            continue
        chat = source.chat
        if chat == "private":
            if chat_type == "private":
                return source
        elif isinstance(chat, int):
            if chat == chat_id:
                return source
        elif isinstance(chat, str) and username and chat.lstrip("@").lower() == username.lower():
            return source
    return None


def direct_adds_allowed(source: Source, sender_id: int | None, config: Config | None = None) -> bool:
    """Whether this sender's own torrent (a ``.torrent``, a magnet) may be added as it is.

    The policy judges a direct add by the name its sender gave it, and what
    downloads is whatever that torrent holds, so by default only a reviewer's
    is taken. An item with no recorded sender passes only where anyone may.
    """
    allowed = source.direct_adds
    if allowed == "anyone":
        return True
    if allowed == "reviewers":
        return sender_id is not None and sender_id in reviewers(config)
    return False


#: Chat id -> source key, for sources whose ``chat`` is an @username: the
#: userbot resolves those at start and tells us (``set_resolved_chats``).
_resolved_chats: dict[int, str] = {}


def set_resolved_chats(chats: dict[int, str] | None) -> None:
    """The chat ids the userbot resolved its sources' chats to."""
    global _resolved_chats
    _resolved_chats = dict(chats or {})


def source_for(source_key: str | None, chat_id: int, config: Config | None = None) -> Source | None:
    """The source an item came from.

    By its ``source_key``; a row from before sources existed has none, and
    belongs to the source configured on its chat (by id, or by the id its
    @username resolved to), or else to the first userbot source: the one chat
    the app used to read.
    """
    cfg = _config(config)
    if source_key:
        return next((s for s in cfg.sources if s.key == source_key), None)
    on_chat = next(
        (s for s in cfg.sources if s.kind in ("userbot", "bot") and s.chat == chat_id), None
    )
    if on_chat is None and chat_id in _resolved_chats:
        on_chat = next((s for s in cfg.sources if s.key == _resolved_chats[chat_id]), None)
    return on_chat or next((s for s in cfg.sources if s.kind == "userbot"), None)


# ---------------------------------------------------------------------------
# Reading names, torrents and links
# ---------------------------------------------------------------------------


def _bdecode(data: bytes, pos: int = 0) -> tuple[Any, int]:
    token = data[pos:pos + 1]
    if token == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if token == b"l":
        pos += 1
        out: list = []
        while data[pos:pos + 1] != b"e":
            if pos >= len(data):
                raise ValueError("unterminated list")
            value, pos = _bdecode(data, pos)
            out.append(value)
        return out, pos + 1
    if token == b"d":
        pos += 1
        mapping: dict = {}
        while data[pos:pos + 1] != b"e":
            if pos >= len(data):
                raise ValueError("unterminated dict")
            key, pos = _bdecode(data, pos)
            if not isinstance(key, bytes):
                raise ValueError("a dictionary key that is not a string")
            mapping[key], pos = _bdecode(data, pos)
        return mapping, pos + 1
    if token.isdigit():
        colon = data.index(b":", pos)
        start = colon + 1
        end = start + int(data[pos:colon])
        if end > len(data):
            raise ValueError("truncated string")
        return data[start:end], end
    raise ValueError("not bencoded")


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def torrent_name(data: bytes) -> tuple[str | None, list[str]]:
    """The torrent's name and its file names; (None, []) when ``data`` is not a torrent.

    The bytes come from whoever posted them, so every shape is checked: a
    file that is bencode but not a torrent reads as (None, []), never raises.
    """
    try:
        meta, _ = _bdecode(data)
    except (ValueError, IndexError, RecursionError):
        return None, []
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict) or not isinstance(info.get(b"name"), bytes) or not info[b"name"]:
        return None, []
    name = _text(info[b"name"])
    listed = info.get(b"files")
    if listed is None:
        return name, [name]
    if not isinstance(listed, list):
        return None, []
    files = []
    for entry in listed:
        path = entry.get(b"path") if isinstance(entry, dict) else None
        if not (isinstance(path, list) and path and isinstance(path[-1], bytes)):
            return None, []
        files.append(_text(path[-1]))
    return name, files or [name]


def magnet_name(magnet: str) -> str | None:
    """The display name a magnet carries (``dn``), if any."""
    names = parse_qs(urlparse(magnet).query).get("dn")
    return (names[0].strip() or None) if names else None


def split_release_name(name: str) -> tuple[str | None, int | None, str | None]:
    """(title, year, season) of a release name or a typed title.

    ``Some.Film.2024.1080p.WEB-DL-GRP`` and ``Some Film 2024`` both read as
    ("Some Film", 2024, None); ``Show.S02E01.720p`` as ("Show", None, "S02").
    """
    text = _FILE_EXT.sub("", name.strip())
    text = re.sub(r"\[[^\]]*\]", " ", text)
    tokens = re.sub(r"[._]+", " ", text).split()
    year = season = None
    cut = len(tokens)
    for i, token in enumerate(tokens):
        if i == 0:
            continue
        if (m := _YEAR.fullmatch(token)) is not None:
            year, cut = int(m.group(1)), i
            break
        if (m := _SEASON.fullmatch(token)) is not None:
            season, cut = f"S{int(m.group(1)):02d}", i
            break
        if _RELEASE_MARKER.fullmatch(token.split("-")[0]):
            cut = i
            break
    if season is None:
        for token in tokens[cut:]:
            if (m := _SEASON.fullmatch(token)) is not None:
                season = f"S{int(m.group(1)):02d}"
                break
    title = " ".join(tokens[:cut]).strip(" -–") or None
    return title, year, season


def _type_from_name(name: str, season: str | None, files: list[str]) -> MediaType:
    if season:
        return MediaType.SERIES
    if _VIDEO_MARKER.search(name):
        return MediaType.MOVIE
    for file in files:
        suffix = Path(file).suffix.lower()
        if suffix in _EXT_TYPES:
            return _EXT_TYPES[suffix]
    return MediaType.UNKNOWN


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------


async def _tmdb_get(path: str, **params: Any) -> dict | None:
    """One TMDB call, or None when the tmdb integration is off or the call fails."""
    api_key = settings.tmdb_api_key
    if not api_key:
        return None
    tmdb = settings.config.integrations.tmdb
    params = {"api_key": api_key, "language": tmdb.language if tmdb else "en-US", **params}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_TMDB_BASE}{path}", params=params)
    except httpx.HTTPError as exc:
        log.warning("TMDB %s failed: %s", path, type(exc).__name__)
        return None
    if resp.status_code != 200:
        log.warning("TMDB %s answered http %d", path, resp.status_code)
        return None
    return resp.json()


def _year_of(date_text: Any) -> int | None:
    m = re.match(r"(\d{4})", str(date_text or ""))
    return int(m.group(1)) if m else None


def _from_tmdb(result: dict, kind: str) -> Resolved:
    if kind == "tv":
        return Resolved(
            title=result.get("name") or result.get("original_name"),
            year=_year_of(result.get("first_air_date")),
            media_type=MediaType.SERIES,
        )
    return Resolved(
        title=result.get("title") or result.get("original_title"),
        year=_year_of(result.get("release_date")),
        media_type=MediaType.MOVIE,
    )


async def resolve_id(source: str, value: str) -> Resolved | None:
    """The title an IMDb, TMDB or TVDB id names, through TMDB."""
    if source in ("movie", "tv"):
        data = await _tmdb_get(f"/{source}/{value}")
        return _from_tmdb(data, source) if data else None
    data = await _tmdb_get(f"/find/{value}", external_source=f"{source}_id")
    if not data:
        return None
    for key, kind in (("movie_results", "movie"), ("tv_results", "tv")):
        if data.get(key):
            return _from_tmdb(data[key][0], kind)
    return None


async def resolve_title(text: str) -> Resolved | None:
    """A typed title ("Some Film 2024") TMDB knows by that exact name.

    Exact on the title, and within a year of the one typed, so a stray word
    in a chat is not taken for a film that happens to share it.
    """
    title, year, _season = split_release_name(text)
    if not title:
        return None
    data = await _tmdb_get("/search/multi", query=title)
    wanted = _fold(title)
    for result in (data or {}).get("results", []):
        kind = result.get("media_type")
        if kind not in ("movie", "tv"):
            continue
        names = (
            (result.get("name"), result.get("original_name")) if kind == "tv"
            else (result.get("title"), result.get("original_title"))
        )
        if wanted not in {_fold(n) for n in names if n}:
            continue
        resolved = _from_tmdb(result, kind)
        if year is not None and (resolved.year is None or abs(resolved.year - year) > 1):
            continue
        return resolved
    return None


async def resolve_name(name: str, files: list[str] | None = None) -> Resolved:
    """Title, year and type from a torrent's name; TMDB names the type when the name does not."""
    title, year, season = split_release_name(name)
    media_type = _type_from_name(name, season, files or [])
    if media_type == MediaType.UNKNOWN and title:
        found = await resolve_title(f"{title} {year}" if year else title)
        if found is not None:
            media_type = found.media_type
    return Resolved(title=title, year=year, media_type=media_type, season=season)


def _id_link(text: str) -> tuple[str, str] | None:
    """("imdb" | "movie" | "tv" | "tvdb" | "tvdb_slug", value) of the first id link."""
    if (m := _IMDB.search(text)) is not None:
        return "imdb", m.group(1)
    if (m := _TMDB.search(text)) is not None:
        return m.group(1).lower(), m.group(2)
    if (m := _TVDB_ID.search(text)) is not None:
        return "tvdb", m.group(1) or m.group(2)
    if (m := _TVDB_SLUG.search(text)) is not None:
        return "tvdb_slug", m.group(1)
    return None


def tmdb_link(text: str | None) -> tuple[str, int] | None:
    """("movie" | "tv", TMDB id) of the first TMDB link in a text, or None."""
    m = _TMDB.search(text or "")
    return (m.group(1).lower(), int(m.group(2))) if m else None


def _is_title_line(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1 or _URL.search(text):
        return False
    line = lines[0].strip()
    return len(line) <= _TITLE_LINE_MAX_CHARS and 1 <= len(line.split()) <= _TITLE_LINE_MAX_WORDS


def _not_fetched(urls: list[str]) -> Any:
    """Under ``dry_run``: the linked torrent live would fetch, and a dry run does not."""
    from vettarr.searcher.search_service import linked_not_fetched

    return linked_not_fetched(urls)


async def _linked_torrent(urls: list[str]) -> bytes | None:
    from vettarr.searcher.search_service import fetch_linked_torrent

    try:
        linked = await fetch_linked_torrent(urls)
    except Exception:
        log.exception("Fetching a linked torrent failed")
        return None
    return linked.data if linked else None


async def read(
    text: str, torrent: bytes | None, *, private: bool = False, source: str | None = None,
) -> tuple[Kind, str, Resolved | None]:
    """What the message is: (kind, the raw text to store, what intake resolved).

    ``source`` is the key of the source the message came from: a post is
    recognised by that source's formats.
    """
    if torrent is not None:
        name, files = torrent_name(torrent)
        raw = "\n".join(part for part in (name, text) if part)
        return "torrent", raw, await resolve_name(name, files) if name else None
    if (m := _MAGNET.search(text)) is not None:
        name = magnet_name(m.group(0))
        return "magnet", text, await resolve_name(name) if name else None
    if has_release_structure(text, source=source):
        return "post", text, None
    if (link := _id_link(text)) is not None:
        kind, value = link
        if kind == "tvdb_slug":
            resolved = await resolve_title(value.replace("-", " "))
        else:
            resolved = await resolve_id(kind, value)
        return ("id_link", text, resolved) if resolved else ("post", text, None)
    urls = _URL.findall(text)
    if urls:
        if _not_fetched(urls) is not None:
            # Live names the item from the linked file; a dry run does not
            # fetch it (`ingest` records that on the item).
            return "tracker_link", text, None
        data = await _linked_torrent(urls)
        name, files = torrent_name(data) if data else (None, [])
        if name:
            return "tracker_link", f"{name}\n{text}", await resolve_name(name, files)
        return "post", text, None
    # A short line in a group chat is as often a note as a title; there it
    # takes a year to be read as one. In the bot's private chat it is a request.
    if _is_title_line(text) and (private or split_release_name(text)[1] is not None):
        resolved = await resolve_title(text)
        if resolved is not None:
            return "title", text, resolved
    return "post", text, None


# ---------------------------------------------------------------------------
# ingest()
# ---------------------------------------------------------------------------

_queue: asyncio.Queue[int] | None = None
# The items `add` put on the queue in this process. The queue is in memory, so
# an item that is not here and still waits for the pipeline is in no queue.
_added: set[int] = set()


def set_queue(queue: asyncio.Queue[int] | None) -> None:
    """The pipeline queue every new item is put on."""
    global _queue
    _queue = queue
    _added.clear()


async def _existing(message: Incoming) -> int | None:
    """The item this message already is, asked before anything is downloaded or looked up.

    By the message itself (its chat and id), and by the post it stands for:
    its ``origin_key``, or the original's own row when that was written before
    origin keys existed.
    """
    origin_chat, origin_message = message.forward_from or (message.chat_id, message.message_id)
    same_post = [
        MediaItem.origin_key == message.origin_key,
        and_(
            MediaItem.telegram_chat_id == origin_chat,
            MediaItem.telegram_message_id == origin_message,
        ),
    ]
    if message.forward_from is not None:
        same_post.append(and_(
            MediaItem.telegram_chat_id == message.chat_id,
            MediaItem.telegram_message_id == message.message_id,
        ))
    async with async_session() as session:
        result = await session.execute(select(MediaItem.id).where(or_(*same_post)).limit(1))
        return result.scalar_one_or_none()


async def _insert(values: dict) -> int | None:
    """Insert the item; None when its message or its origin is already one."""
    async with async_session() as session:
        result = await session.execute(
            pg_insert(MediaItem).values(**values).on_conflict_do_nothing().returning(MediaItem.id)
        )
        await session.commit()
        return result.scalar_one_or_none()


async def add(values: dict) -> int | None:
    """Insert an item its source has already named, and queue it for the pipeline.

    For a source that is no Telegram chat (a Seerr request). None when the
    item exists already, by its message or by its ``origin_key``.
    """
    item_id = await _insert(values)
    if item_id is not None and _queue is not None:
        _added.add(item_id)
        _queue.put_nowait(item_id)
    return item_id


#: The statuses of an item the pipeline has not finished with.
UNFINISHED = (ItemStatus.PENDING_PARSE, ItemStatus.PARSED, ItemStatus.CHECKING)


async def add_again(item_id: int, status: ItemStatus) -> bool:
    """Queue again an item `add` made that the pipeline never finished. Whether it was queued.

    The queue is in memory and nothing else comes back for such a row. One
    this process did not queue was left by a process that is gone, or was
    written by one that has no pipeline (the web UI on its own). It starts
    again from the top, while it is still in ``status`` and no operation
    holds it. An item this process queued itself is the pipeline's already,
    and is left alone; so is every item where there is no queue.
    """
    if _queue is None or item_id in _added:
        return False
    async with async_session() as session:
        moved = await session.execute(
            update(MediaItem)
            .where(MediaItem.id == item_id, MediaItem.status == status, MediaItem.error_message.is_(None))
            .values(status=ItemStatus.PENDING_PARSE)
            .returning(MediaItem.id)
        )
        found = moved.scalar_one_or_none() is not None
        await session.commit()
    if found:
        _added.add(item_id)
        _queue.put_nowait(item_id)
    return found


def _torrent_path(item_id: int) -> Path:
    return TORRENTS_DIR / f"{item_id}.torrent"


def _save_torrent(item_id: int, data: bytes) -> None:
    try:
        TORRENTS_DIR.mkdir(parents=True, exist_ok=True)
        _torrent_path(item_id).write_bytes(data)
    except OSError:
        log.exception("Could not keep the .torrent of item %d; it will be searched for", item_id)


def own_torrent(item_id: int) -> bytes | None:
    """The ``.torrent`` the item was sent with, while it is kept."""
    try:
        return _torrent_path(item_id).read_bytes()
    except OSError:
        return None


def forget_torrent(item_id: int) -> None:
    """Drop the kept ``.torrent`` of an item that no longer needs it: added, or rejected."""
    try:
        _torrent_path(item_id).unlink(missing_ok=True)
    except OSError:
        log.warning("Could not remove the kept .torrent of item %d", item_id)


def _has_torrent(message: Incoming) -> bool:
    return message.fetch_document is not None and (message.document_name or "").lower().endswith(".torrent")


async def _fetch_torrent(message: Incoming) -> bytes | None:
    from vettarr.searcher.qbittorrent_client import torrent_info_hash

    if not _has_torrent(message):
        return None
    if message.document_size is not None and message.document_size > MAX_TORRENT_BYTES:
        log.warning(
            "Message %d carries a .torrent of %d bytes, over the %d this app downloads; not read",
            message.message_id, message.document_size, MAX_TORRENT_BYTES,
        )
        return None
    try:
        data = await message.fetch_document()
    except Exception:
        log.exception("Downloading the .torrent of message %d failed", message.message_id)
        return None
    # Whoever posted the file chose every byte of it: anything that is not a
    # torrent with a name, however it fails to be one, is refused here.
    try:
        is_torrent = (
            len(data) <= MAX_TORRENT_BYTES
            and torrent_info_hash(data) is not None
            and torrent_name(data)[0] is not None
        )
    except Exception:
        log.exception("Reading the .torrent of message %d failed", message.message_id)
        is_torrent = False
    if not is_torrent:
        log.warning("Message %d carries a .torrent that is not one", message.message_id)
        return None
    return data


async def ingest(source: Source, message: Incoming) -> IngestResult:
    """Turn one message from one source into an item, or say why not."""
    if not sender_allowed(source, message.sender_id):
        return IngestResult("denied")
    text = (message.text or "").strip()
    readable = bool(text) and not should_skip_message(text)
    if not readable and not _has_torrent(message):
        return IngestResult("skipped")

    # Known already? Then nothing is downloaded and nobody is asked: a history
    # read again (backfill: full, a restart) costs this one query a message.
    existing = await _existing(message)
    if existing is not None:
        return IngestResult("duplicate", "post", item_id=existing)

    torrent = await _fetch_torrent(message)
    if torrent is None and not readable:
        return IngestResult("skipped")

    kind, raw_text, resolved = await read(
        text, torrent, private=source.kind == "bot" and source.chat == "private", source=source.key,
    )
    values: dict = {
        "telegram_message_id": message.message_id,
        "telegram_chat_id": message.chat_id,
        "raw_text": raw_text,
        "status": ItemStatus.PENDING_PARSE,
        "source_key": source.key,
        "origin_key": message.origin_key,
        "sender_id": message.sender_id,
        "posted_at": message.posted_at,
    }
    if resolved is not None and resolved.title:
        values.update(title=resolved.title[:500], year=resolved.year, media_type=resolved.media_type)
        if resolved.season:
            values["season"] = resolved.season
    item_id = await _insert(values)
    if item_id is None:
        return IngestResult("duplicate", kind)
    if kind == "tracker_link" and resolved is None and (held := _not_fetched(_URL.findall(text))) is not None:
        from vettarr.searcher.search_service import not_fetched

        not_fetched(held, item_id)
    # Kept only for a sender whose own torrent may be added as it is; anyone
    # else's named the item and is not needed again.
    if torrent is not None and direct_adds_allowed(source, message.sender_id):
        _save_torrent(item_id, torrent)
    if _queue is not None:
        _queue.put_nowait(item_id)
    if source.kind == "bot":
        # What the message was read as and where from; never its text.
        events.emit(events.BOT_ADDED, item_id, title=values.get("title"), source=source.key, read_as=kind)
    log.info("Source %s: message %d ingested as item %d (%s)", source.key, message.message_id, item_id, kind)
    return IngestResult("added", kind, item_id=item_id, title=values.get("title"))


# ---------------------------------------------------------------------------
# The direct add: a torrent the post itself carried
# ---------------------------------------------------------------------------


DirectAdd = Literal["torrent file", "magnet"]


def direct_add_of(item: Any, config: Config | None = None) -> DirectAdd | None:
    """The torrent of its own this item is added with once approved, if any.

    The ``.torrent`` it was sent with, or the magnet in its post, and only
    when its source's ``direct_adds`` lets its sender do that. None means the
    item is searched for.
    """
    item_id = getattr(item, "id", None)
    has_file = item_id is not None and _torrent_path(item_id).is_file()
    if not has_file and _MAGNET.search(getattr(item, "raw_text", None) or "") is None:
        return None
    source = source_for(getattr(item, "source_key", None), getattr(item, "telegram_chat_id", 0), config)
    if source is None or not direct_adds_allowed(source, getattr(item, "sender_id", None), config):
        return None
    return "torrent file" if has_file else "magnet"


async def add_direct(ref: ItemRef) -> str | None:
    """Add the item's own torrent, skipping the search.

    The ``.torrent`` it was sent with, or the magnet in its post. Returns
    'downloading' or 'error' like the search does, and None when the item
    carries no torrent of its own that it may use (``direct_add_of``), so the
    caller searches as before.
    """
    from vettarr.searcher.search_service import qbittorrent, record_download

    async with async_session() as session:
        item = await session.get(MediaItem, ref.id)
    how = direct_add_of(item) if item is not None else None
    if how is None:
        return None
    data = own_torrent(ref.id) if how == "torrent file" else None
    magnet = None
    if data is None:
        m = _MAGNET.search(item.raw_text or "")
        if m is None:
            return None
        magnet = m.group(0)

    if data is not None:
        add_result = await qbittorrent.add_torrent_file(data, ref.media_type)
    else:
        add_result = await qbittorrent.add_torrent(
            ref.media_type, magnet_url=magnet, expected_title=ref.title,
        )
    async with async_session() as session:
        await record_download(
            session, ref.id, add_result, op=OP_APPROVAL,
            media_type=ref.media_type, download_url=None, magnet_url=magnet,
        )
    log.info(
        "Direct add (%s) for '%s' (id=%d): %s",
        "torrent file" if data is not None else "magnet", ref.title, ref.id,
        "ok" if add_result else "FAILED",
    )
    if add_result and data is not None:
        forget_torrent(ref.id)
    return "downloading" if add_result else "error"


# ---------------------------------------------------------------------------
# What happens to the post: on_download and on_reject
# ---------------------------------------------------------------------------


async def _load_post(item_id: int) -> tuple[int, int, str | None] | None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if item is None:
            return None
        return item.telegram_chat_id, item.telegram_message_id, item.source_key


async def reject_effect(item_id: int, config: Config | None = None) -> Literal["delete", "react", "none"]:
    """What rejecting the item would do to its post: its source's ``on_reject``.

    ``none`` as well for an item whose post is never touched (one the app
    could not read) and for one whose source is no longer configured.
    """
    from vettarr.models import DELETABLE_ON_REJECT

    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
    if item is None or item.status not in DELETABLE_ON_REJECT:
        return "none"
    source = source_for(item.source_key, item.telegram_chat_id, config)
    return source.on_reject if source is not None else "none"


class PostActions:
    """Carries out a source's ``on_download`` and ``on_reject`` on the item's own post.

    A userbot source acts through the Telethon client, a bot source through
    the bot. The chat is always the item's own, never a configured group.
    """

    def __init__(self, userbot=None, bot=None, config: Config | None = None) -> None:
        #: The userbot ingester (its ``client`` is used), or None.
        self.userbot = userbot
        #: The bot (``telegram.Bot``), or None; set once the bot app exists.
        self.bot = bot
        self._config = config
        self._refused: set[str] = set()

    @dryrun.about_item("item_id")
    async def after(self, event: Literal["download", "reject"], item_id: int) -> str:
        """Act on the item's post. Returns what was done:
        deleted, reacted, none, refused, unavailable or failed."""
        post = await _load_post(item_id)
        if post is None:
            return "none"
        chat_id, message_id, source_key = post
        source = source_for(source_key, chat_id, self._config)
        if source is None:
            log.info("Item %d: its source %s is no longer configured; post left alone", item_id, source_key)
            return "none"
        action = source.on_download if event == "download" else source.on_reject
        if action == "none":
            return "none"
        try:
            if source.kind == "userbot":
                return await self._as_userbot(action, chat_id, message_id, REACTIONS[event])
            if source.kind == "bot":
                return await self._as_bot(source, action, chat_id, message_id, REACTIONS[event])
        except Exception:
            log.exception("Could not %s message %d in chat %d", action, message_id, chat_id)
            return "failed"
        return "none"

    async def _as_userbot(self, action: str, chat_id: int, message_id: int, emoji: str) -> str:
        client = getattr(self.userbot, "client", None)
        if client is None:
            log.warning("No userbot running; message %d in chat %d left alone", message_id, chat_id)
            return "unavailable"
        if action == "delete":
            await client.delete_messages(chat_id, [message_id])
            log.info("Deleted message %d from chat %d", message_id, chat_id)
            return "deleted"
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        peer = await client.get_input_entity(chat_id)
        await client(SendReactionRequest(
            peer=peer, msg_id=message_id, reaction=[ReactionEmoji(emoticon=emoji)],
        ))
        return "reacted"

    async def _bot_can_delete(self, source: Source, chat_id: int) -> bool:
        # In a private chat a bot may delete what it was sent (within 48 h);
        # in a group or channel only as an admin with the delete right.
        if source.chat == "private":
            return True
        try:
            member = await self.bot.get_chat_member(chat_id, self.bot.id)
        except Exception:
            return False
        status = getattr(member, "status", None)
        return status == "creator" or (
            status == "administrator" and bool(getattr(member, "can_delete_messages", False))
        )

    async def _as_bot(self, source: Source, action: str, chat_id: int, message_id: int, emoji: str) -> str:
        if self.bot is None:
            log.warning("No bot running; message %d in chat %d left alone", message_id, chat_id)
            return "unavailable"
        if action == "delete":
            if not await self._bot_can_delete(source, chat_id):
                if source.key not in self._refused:
                    self._refused.add(source.key)
                    log.warning(
                        "Source %s: the bot is not an admin allowed to delete messages in its "
                        "chat, so posts stay; make it one, or use on_download/on_reject: react",
                        source.key,
                    )
                return "refused"
            await self.bot.delete_message(chat_id, message_id)
            log.info("Deleted message %d from chat %d", message_id, chat_id)
            return "deleted"
        await self.bot.set_message_reaction(chat_id, message_id, reaction=emoji)
        return "reacted"


# ---------------------------------------------------------------------------
# notify_on_review, and the bot being added to a chat
# ---------------------------------------------------------------------------

ReviewPusher = Callable[[int, MediaItem], Awaitable[None]]
_review_pusher: ReviewPusher | None = None


def set_review_pusher(pusher: ReviewPusher | None) -> None:
    """How a held item reaches a reviewer: the bot's review card."""
    global _review_pusher
    _review_pusher = pusher


#: The longest a push waits when Telegram asks it to slow down, in seconds.
_PUSH_MAX_WAIT = 60


def _retry_after(exc: Exception) -> float | None:
    """The seconds Telegram asked to wait (a flood limit), if that is what ``exc`` says."""
    wait = getattr(exc, "retry_after", None)
    if wait is None:
        return None
    seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
    return min(max(seconds, 0.0), _PUSH_MAX_WAIT)


async def _push(reviewer: int, item: MediaItem) -> None:
    """One reviewer's card. A flood limit is waited out once; nothing else is retried."""
    try:
        await _review_pusher(reviewer, item)
    except Exception as exc:
        wait = _retry_after(exc)
        if wait is None:
            raise
        await asyncio.sleep(wait)
        await _review_pusher(reviewer, item)


@dryrun.about_item("item_id")
async def held_for_review(item_id: int) -> None:
    """Push an item the policy held for a person to every reviewer, when its source asks.

    Never raises: a failed push must not undo the decision that was written.
    Each reviewer is tried on their own, so one who cannot be messaged (they
    never opened the bot) does not keep the card from the others.
    """
    if _review_pusher is None:
        return
    try:
        async with async_session() as session:
            item = await session.get(MediaItem, item_id)
        if item is None or item.status != ItemStatus.PENDING_REVIEW:
            return
        source = source_for(item.source_key, item.telegram_chat_id)
        if source is None or not source.notify_on_review:
            return
        everyone = reviewers()
    except Exception:
        log.exception("Could not push item %d to the reviewers", item_id)
        return
    for reviewer in everyone:
        try:
            await _push(reviewer, item)
        except Exception as exc:
            # Not the reviewer's id: a log line names no person.
            log.warning("Could not push item %d to one reviewer: %s", item_id, type(exc).__name__)


@dataclass(frozen=True)
class BotChat:
    chat_id: int
    title: str
    #: An admin can read every message and delete them.
    is_admin: bool
    #: Privacy mode off: the bot sees every group message, not only commands.
    reads_all_messages: bool
    added_at: datetime


_bot_chats: dict[int, BotChat] = {}
#: How many chats are remembered; the oldest is dropped past this.
_BOT_CHATS_MAX = 200


def bot_added(chat_id: int, title: str, *, is_admin: bool, reads_all_messages: bool) -> BotChat:
    """Record that a reviewer added the bot to a chat, for the Setup page to offer it as a source."""
    chat = BotChat(chat_id, title[:200], is_admin, reads_all_messages, datetime.now(timezone.utc))
    _bot_chats.pop(chat_id, None)
    _bot_chats[chat_id] = chat
    while len(_bot_chats) > _BOT_CHATS_MAX:
        del _bot_chats[next(iter(_bot_chats))]
    log.info(
        "Bot was added to chat %d (admin: %s, sees every message: %s)",
        chat_id, is_admin, reads_all_messages or is_admin,
    )
    # The list above is gone at the next start; the event is what the Setup page reads.
    events.emit(
        events.BOT_CHAT, chat_id=chat_id, title=chat.title, state="joined",
        is_admin=is_admin, reads_all_messages=reads_all_messages,
    )
    return chat


async def _join_stored(chat_id: int) -> bool:
    """Whether the last stored ``bot_chat`` event of this chat is a join: an earlier start saw a reviewer add the bot."""
    try:
        async with async_session() as session:
            payload = (await session.execute(
                select(Event.payload)
                .where(Event.kind == events.BOT_CHAT, Event.payload["chat_id"].astext == str(chat_id))
                .order_by(Event.at.desc(), Event.id.desc()).limit(1)
            )).scalar_one_or_none()
    except Exception as exc:
        log.debug("Could not read the bot_chat events: %s", type(exc).__name__)
        return False
    return isinstance(payload, dict) and payload.get("state") == "joined"


async def bot_removed(chat_id: int, config: Config | None = None) -> None:
    """The bot left a chat or was removed from it: it is no longer on offer.

    That is an event only for a chat the app had a reason to know: one a
    reviewer added the bot to, in this start or by the stored event of an
    earlier one, or one a bot source reads. Anyone can add the bot to a chat
    of theirs and take it out again; that writes nothing and notifies nobody.
    """
    known = _bot_chats.pop(chat_id, None) is not None
    if known:
        log.info("Bot is no longer in chat %d", chat_id)
    read = any(s.kind == "bot" and str(s.chat) == str(chat_id) for s in _config(config).sources)
    if known or read or await _join_stored(chat_id):
        events.emit(events.BOT_CHAT, chat_id=chat_id, state="left")


def bot_chats() -> list[BotChat]:
    """Chats the bot was added to since the start, newest first."""
    return sorted(_bot_chats.values(), key=lambda c: c.added_at, reverse=True)


# ---------------------------------------------------------------------------
# The photo of a post
# ---------------------------------------------------------------------------


async def keep_photo(
    item_id: int, chat_id: int, message_id: int, download: Callable[[Path], Awaitable[Any]],
) -> str | None:
    """Keep a post's photo for its review card and the web UI; returns the file's name.

    Named by chat and message, because message ids repeat from one chat to
    the next. ``download`` writes the picture to the path it is given.
    """
    filename = f"{chat_id}_{message_id}.jpg"
    dest = PHOTOS_DIR / filename
    if not dest.exists():
        try:
            PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
            await download(dest)
        except Exception:
            log.exception("Failed to download the photo of message %d", message_id)
            return None
    async with async_session() as session:
        await session.execute(
            update(MediaItem).where(MediaItem.id == item_id).values(telegram_photo_path=filename)
        )
        await session.commit()
    return filename
