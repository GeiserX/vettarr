from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from vettarr import delivery, dryrun, events, language
from vettarr.checker.book_client import BookVerdict, check_book
from vettarr.checker.gather import gather_signals, title_rule_hits
from vettarr.checker.imdb_client import ParentsGuide, fetch_parents_guide
from vettarr.checker.policy import (
    APPROVE,
    REJECT,
    REVIEW,
    RULE,
    WAIT,
    Applied,
    Decision,
    act,
    evaluate,
    needed_signals,
    worth_rerunning,
)
from vettarr.checker.tmdb_client import TMDBClient, TMDBResult
from vettarr.config import settings
from vettarr.database import async_session
from vettarr.delivery import seerr as seerr_api
from vettarr.ingest import held_for_review, source_for, tmdb_link
from vettarr.models import (
    OP_RECHECK,
    OP_REPARSE,
    AutoDecision,
    ContentCheck,
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
)
from vettarr.parser.regex_parser import classify_non_media, parse_message
from vettarr.searcher.qbittorrent_client import QBittorrentClient

log = logging.getLogger(__name__)

_VIDEO_TYPES = {MediaType.MOVIE, MediaType.SERIES}
_BOOK_TYPES = {MediaType.EBOOK, MediaType.AUDIOBOOK, MediaType.MAGAZINE, MediaType.COMIC, MediaType.MUSIC}
# Types a book catalogue can describe. A lookup runs only when a rule of the
# type reads what the catalogue answers.
_BOOK_LOOKUP_TYPES = {MediaType.EBOOK, MediaType.AUDIOBOOK, MediaType.MAGAZINE, MediaType.COMIC}
_CATALOGUE_SIGNALS = frozenset({"subject", "language", "publisher", "pages"})

# How many waiting items a single scheduled re-check may touch.
RECHECK_BATCH = 20
# How many re-check batches one backlog re-run may drive before it stops and
# leaves the rest to the hourly re-check.
RERUN_MAX_LOOPS = 50

tmdb_client = TMDBClient()
_qbt = QBittorrentClient()

_PARENS = re.compile(r"\([^)]*\)")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_QUOTES = re.compile(r'["""\'«»]')
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_QUALITY = re.compile(
    r"\b(?:1080p|720p|480p|2160p|4K|HDR10?\+?|WEB[- ]?DL|BluRay|BDRip|BDRemux|"
    r"x264|x265|H\.?26[45]|HEVC|AVC|AAC|DD\+?|DTS(?:-HD)?|FLAC|MULTi|SPANiSH|"
    r"Voz\s*Humana|PROPER|PACK|Subs|Atmos|"
    r"epub|pdf|m4a|m4b|mp3|azw3?|mobi|cbr|cbz|mkv|avi|mp4)\b",
    re.IGNORECASE,
)
_AUTHOR_SEP = re.compile(r"\s+[-–—]\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s{2,}")
_FILE_EXT = re.compile(r"\.\w{2,4}$")

_MIN_NORM_LEN = 6
_MIN_MEANINGFUL_WORDS = 3
_SUBSTR_LEN_RATIO = 0.60
_STOPWORDS = language.STOPWORDS

# What tells one issue of a periodical from the next: an issue number
# ("Nº 256", "N° 256", "Núm. 256", "#256") or a month, read on the folded,
# accent-free title. A year alone is not an edition: novels carry years too.
_ISSUE = re.compile(r"(?:\bn(?:[ºo°]|um(?:ero)?)?\.?\s*|#\s*)(\d{1,5})\b")
_MONTH = re.compile(r"\b(" + "|".join(sorted(language.MONTHS, key=len, reverse=True)) + r")\b")
# A month in a film or series title is part of its name, not an edition.
_NO_EDITIONS = {MediaType.MOVIE, MediaType.SERIES}


@dataclass(frozen=True)
class Edition:
    issue: int | None
    month: int | None  # 1 to 12, whatever language named it
    year: int | None


def _fold(title: str) -> str:
    t = unicodedata.normalize("NFD", title)
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def _edition(title: str) -> Edition | None:
    """The issue a periodical title names, or None for anything that is not one."""
    folded = _fold(_FILE_EXT.sub("", title))
    issue = _ISSUE.search(folded)
    month = _MONTH.search(folded)
    if not issue and not month:
        return None
    year = _YEAR.search(folded)
    return Edition(
        issue=int(issue.group(1)) if issue else None,
        month=language.MONTHS[month.group(1)] if month else None,
        year=int(year.group()) if year else None,
    )


def _same_edition(a: str, b: str) -> bool | None:
    """Whether two periodical titles name the same issue.

    None when neither is a periodical and the ordinary title rules decide.
    A periodical never matches a title that is not one, and two issues match
    on their number when both carry one, else on month and year.
    """
    ea, eb = _edition(a), _edition(b)
    if ea is None and eb is None:
        return None
    if ea is None or eb is None:
        return False
    if ea.issue is not None and eb.issue is not None:
        return ea.issue == eb.issue
    if ea.month and eb.month:
        return (ea.month, ea.year) == (eb.month, eb.year)
    return False


def _without_edition(title: str) -> str:
    folded = _fold(title)
    return _MONTH.sub(" ", _ISSUE.sub(" ", folded))


def normalize_title(title: str, media_type: MediaType | None = None) -> str:
    """Reduce a title to its core words for duplicate comparison."""
    t = _FILE_EXT.sub("", title)
    t = _PARENS.sub(" ", t)
    t = _BRACKETS.sub(" ", t)
    t = _QUOTES.sub("", t)
    # Only strip at author/group separator for video types;
    # for books/music "Author - Title" is the full meaningful title
    if media_type not in _BOOK_TYPES:
        parts = _AUTHOR_SEP.split(t, maxsplit=1)
        t = parts[0]
    t = _YEAR.sub("", t)
    t = _QUALITY.sub("", t)
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = _NON_ALNUM.sub(" ", t.lower())
    return _MULTI_SPACE.sub(" ", t).strip()


def titles_match(a: str, b: str, media_type: MediaType | None = None) -> bool:
    same_edition = None if media_type in _NO_EDITIONS else _same_edition(a, b)
    if same_edition is False:
        return False
    if same_edition:
        # The same issue of the same periodical, if the names agree once the
        # issue number and month are out of the way.
        a, b = _without_edition(a), _without_edition(b)
    na, nb = normalize_title(a, media_type), normalize_title(b, media_type)
    if not na or not nb or len(na) < _MIN_NORM_LEN or len(nb) < _MIN_NORM_LEN:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) / len(long) >= _SUBSTR_LEN_RATIO:
        pat = r"(?:^|\s)" + re.escape(short) + r"(?:\s|$)"
        if re.search(pat, long):
            return True
    wa = set(na.split()) - _STOPWORDS
    wb = set(nb.split()) - _STOPWORDS
    shorter = min(len(wa), len(wb))
    if shorter < _MIN_MEANINGFUL_WORDS:
        return False
    overlap = len(wa & wb) / shorter
    return overlap >= 0.85


_qbt_name_cache: list[str] = []
_qbt_cache_ts: float = 0
_qbt_cache_lock = asyncio.Lock()


async def _get_qbt_names() -> list[str]:
    """Cached list of all qBittorrent torrent names (refresh every 5 min)."""
    global _qbt_name_cache, _qbt_cache_ts
    import time
    # One refresh at a time. A batch of posts checks several items at once, and
    # without the lock each of them fetched the whole list when the cache aged.
    async with _qbt_cache_lock:
        now = time.monotonic()
        if now - _qbt_cache_ts > 300 or not _qbt_name_cache:
            try:
                _qbt_name_cache = await _qbt.list_all_names()
                _qbt_cache_ts = now
                log.info("Refreshed qBt name cache: %d torrents", len(_qbt_name_cache))
            except Exception:
                log.warning("qBittorrent name cache refresh failed", exc_info=True)
    return _qbt_name_cache


# Statuses that mean the title is already settled here: a human said yes, or
# it is on its way in, or it arrived. This is the set the review UI has always
# been told about, and it does not change.
_SETTLED_STATUSES = (
    ItemStatus.APPROVED, ItemStatus.DOWNLOADING, ItemStatus.COMPLETED,
)

# Statuses that mean another post of the same title is still being worked out.
# Nothing has been decided about it yet, so it is not a duplicate in the sense
# the UI means -- but approving a second copy of it by itself would download
# the same thing twice.
_IN_FLIGHT_STATUSES = (
    ItemStatus.CHECKING, ItemStatus.SEARCHING, ItemStatus.AWAITING_DATA,
)


def _comparable(item: MediaItem) -> bool:
    # Magazines always share the same base name, differing only by issue number
    return bool(item.title) and item.media_type != MediaType.MAGAZINE


def _dup_title(item: MediaItem) -> str:
    """The title as the duplicate check reads it.

    The group writes a magazine's month in the title and its year in the
    year field ("Workshop Monthly - Octubre", 2026), and the year is what
    tells this October's issue from the 2024 one already on disk.
    """
    if item.year and not _YEAR.search(item.title):
        return f"{item.title} {item.year}"
    return item.title


async def _matching_title(session, item: MediaItem, statuses, earlier_only=False):
    """The title of another item in one of `statuses` that is this one, or None."""
    title = _dup_title(item)
    conditions = [
        MediaItem.id != item.id,
        MediaItem.title.isnot(None),
        MediaItem.status.in_(list(statuses)),
    ]
    if earlier_only:
        # Of two posts racing each other, only the later one gives way, so
        # exactly one of them still downloads.
        conditions[0] = MediaItem.id < item.id
    existing = await session.execute(select(MediaItem.title).where(*conditions))
    for (db_title,) in existing:
        if db_title and titles_match(title, db_title, item.media_type):
            return db_title
    return None


async def _check_duplicate(session, item: MediaItem) -> str | None:
    """Return the matching title if this item is a duplicate, else None.

    Checks vettarr DB (already downloaded/approved) and qBittorrent.
    """
    if not _comparable(item):
        return None

    db_title = await _matching_title(session, item, _SETTLED_STATUSES)
    if db_title:
        return f"DB: {db_title}"

    qbt_names = await _get_qbt_names()
    title = _dup_title(item)
    for name in qbt_names:
        if titles_match(title, name, item.media_type):
            return f"qBt: {name}"

    return None


async def _check_in_flight_twin(session, item: MediaItem) -> str | None:
    """An earlier post of the same title that nobody has decided yet, or None.

    Read only by the auto-approval policy. It never becomes a "Duplicate:"
    message, so the review queue shows exactly what it showed before.
    """
    if not _comparable(item):
        return None
    return await _matching_title(
        session, item, _IN_FLIGHT_STATUSES, earlier_only=True
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Verdict:
    """A decision, what is done with it under `auto_approve`, and what it read."""

    decision: Decision
    applied: Applied
    signals: dict | None = None


# What each outcome leaves in the 0.11 column. A policy rejection has no
# label there: the column stays empty and `policy_outcome` says reject.
_AUTO_DECISION = {
    APPROVE: AutoDecision.AUTO_APPROVE,
    REVIEW: AutoDecision.NEEDS_REVIEW,
    WAIT: AutoDecision.WAIT,
    REJECT: None,
}


def decision_values(verdict: Verdict, now: datetime) -> dict:
    """The columns a decision writes. Pure: no I/O, no ORM.

    Separate from `apply_decision` so the same decision can be written either
    onto a loaded row or through a conditional UPDATE that refuses to land if
    somebody moved the row while the lookups were running.

    An approval leaves exactly what a human approval leaves, so the existing
    search worker picks the item up with no further wiring. A rejection never
    deletes the post: it only sets the status.
    """
    applied, decision = verdict.applied, verdict.decision
    values: dict = {
        "auto_decision": _AUTO_DECISION[applied.outcome],
        "auto_reasons": [r.text for r in applied.reasons],
        "auto_decided_at": now,
        "reasons": [r.to_json() for r in applied.reasons],
        "policy_version": decision.policy_version,
        "policy_outcome": decision.outcome,
        "signals": verdict.signals,
    }
    if applied.outcome == APPROVE:
        values["status"] = ItemStatus.APPROVED
        values["reviewed_at"] = now
        values["error_message"] = None
    elif applied.outcome == WAIT:
        values["status"] = ItemStatus.AWAITING_DATA
    elif applied.outcome == REJECT:
        values["status"] = ItemStatus.REJECTED
        values["error_message"] = "Rejected by policy: " + "; ".join(
            r.text for r in applied.reasons if r.kind == RULE
        )
    else:
        values["status"] = ItemStatus.PENDING_REVIEW
    return values


def apply_decision(item: MediaItem, verdict: Verdict, now: datetime) -> None:
    """Write a policy decision onto a loaded item."""
    for column, value in decision_values(verdict, now).items():
        setattr(item, column, value)


def flags_of(decision: Decision) -> tuple[Recommendation, list[str]]:
    """The advisory badge: the rule hits behind a review or a rejection."""
    if decision.outcome not in (REVIEW, REJECT):
        return Recommendation.NEUTRAL, []
    flags = [r.text for r in decision.reasons if r.kind == RULE]
    return (Recommendation.REJECT if flags else Recommendation.NEUTRAL), flags


def emit_decision(item: MediaItem, verdict: Verdict, *, newly_held: bool = True) -> None:
    """The events a written decision raises: decision, would-have, needs-you.

    ``newly_held=False`` is the backlog re-run's: an item it leaves in the
    review queue was announced when it was first held, and is not announced
    as needing a person a second time.
    """
    payload = {
        "title": item.title,
        "media_type": getattr(item.media_type, "value", item.media_type),
        "outcome": verdict.decision.outcome,
        "done": verdict.applied.outcome,
        "reasons": [r.text for r in verdict.applied.reasons],
        "policy_version": verdict.decision.policy_version,
    }
    events.emit(events.DECISION, item.id, **payload)
    if verdict.applied.shadow:
        events.emit(events.WOULD_HAVE, item.id, **payload)
    if verdict.applied.outcome == REVIEW and newly_held:
        events.emit(events.NEEDS_YOU, item.id, **payload)


def outcome_values(
    result: Verdict,
    now: datetime,
    *,
    recommendation: Recommendation | None = None,
    flags: list[str] | None = None,
    duplicate_of: str | None = None,
    reparsing: bool = False,
) -> dict:
    """Every column the end of a run writes, decision and verdict together.

    One dict, so the pipeline and the hourly re-check can each hand it to a
    conditional UPDATE instead of setting attributes on a row that may have
    moved underneath them.
    """
    values = decision_values(result, now)
    if recommendation is not None:
        values["auto_recommendation"] = recommendation
        if recommendation == Recommendation.REJECT and "error_message" not in values:
            values["error_message"] = "; ".join(flags or [])
    if duplicate_of:
        values["auto_recommendation"] = Recommendation.REJECT
        values["error_message"] = f"Duplicate: {duplicate_of}"
    elif reparsing and "error_message" not in values:
        values["error_message"] = None
    return values


async def _write_checked_result(session, item_id: int, values: dict) -> bool:
    """End the item's CHECKING phase, but only if it is still CHECKING.

    Everything between entering CHECKING and getting here is network time, and
    CHECKING is a status the operator can reject from. If they did, this UPDATE matches
    nothing and the result this run reached is thrown away: the item is not
    touched again, and nothing queues a search for it.
    """
    written = await session.execute(
        update(MediaItem)
        .where(MediaItem.id == item_id, MediaItem.status == ItemStatus.CHECKING)
        .values(**values)
        .returning(MediaItem.id)
    )
    if written.scalar_one_or_none() is None:
        await session.rollback()
        return False
    await session.commit()
    return True


def _needed(media_type: MediaType) -> set[str]:
    return needed_signals(settings.config.policy, media_type.value)


async def _decide(
    session,
    item: MediaItem,
    tmdb_result: TMDBResult | None,
    parents_guide: ParentsGuide | None,
    book: BookVerdict | None,
    duplicate_of: str | None,
    now: datetime,
    twin_of: str | None = None,
) -> Verdict:
    """Gather the item's signals, evaluate the policy, and say what to do.

    With auto-approval off nothing that costs a request is asked: the
    decision is still made and recorded, from what was looked up already.
    """
    cfg = settings.config
    policy = cfg.policy
    snapshot = await gather_signals(
        session, item, cfg,
        tmdb=tmdb_result, guide=parents_guide, book=book,
        book_looked_up=book is not None,
        duplicate_of=duplicate_of, twin_of=twin_of, now=now,
        lookups=policy.auto_approve != "off",
    )
    decision = evaluate(
        item, snapshot, policy,
        now=now, default_min_votes=cfg.integrations.imdb_guide.min_votes,
    )
    return Verdict(decision, act(decision, policy.auto_approve), snapshot)


async def _lookup_book(item: MediaItem) -> BookVerdict | None:
    if item.media_type not in _BOOK_LOOKUP_TYPES or not item.title:
        return None
    if settings.config.policy.auto_approve == "off":
        return None
    if not _needed(item.media_type) & _CATALOGUE_SIGNALS:
        return None
    try:
        return await check_book(
            item.title, settings.google_books_api_key or None, settings.config.release_words.format_tags,
        )
    except Exception:
        log.exception("Book lookup failed for item %d", item.id)
        return BookVerdict(error="network error")


def _source_key(item: MediaItem) -> str | None:
    """The key of the source whose formats read this item.

    Its own; a row from before sources existed has none and is read as its
    chat's source, or else as the first userbot source.
    """
    if item.source_key:
        return item.source_key
    source = source_for(None, item.telegram_chat_id)
    return source.key if source is not None else None


@dryrun.about_item("item_id")
async def process_item(item_id: int) -> None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if not item or item.status != ItemStatus.PENDING_PARSE:
            return

        # A post is read with the formats of the source it came from.
        source = _source_key(item)
        parsed = parse_message(item.raw_text, source=source)
        if item.title and item.media_type != MediaType.UNKNOWN:
            # Intake already named it (a torrent's name, an id link, a typed
            # title): that wins, and the parser fills in the rest.
            parsed.title, parsed.media_type = item.title, item.media_type
            parsed.year = item.year or parsed.year
            parsed.season = item.season or parsed.season
        item.media_type = parsed.media_type
        item.title = parsed.title
        item.year = parsed.year
        item.quality = parsed.quality
        item.source = parsed.source
        item.season = parsed.season
        item.size_text = parsed.size_text
        item.release_group = parsed.release_group
        item.external_links = parsed.external_links

        if not parsed.is_valid:
            non_media = classify_non_media(item.raw_text, source=source)
            if non_media:
                # The operator's own notes and the links they drop in the same group.
                # Terminal: never reviewed, never re-parsed, never deleted.
                item.status = ItemStatus.IGNORED
                item.error_message = non_media
                await session.commit()
                log.info("Ignoring non-media message (id=%d): %s", item.id, non_media)
                return
            item.status = ItemStatus.PARSE_FAILED
            if item.error_message == OP_REPARSE:
                item.error_message = "Reparse failed"
            await session.commit()
            return

        item.status = ItemStatus.PARSED
        await session.commit()

        tmdb_result: TMDBResult | None = None
        parents_guide: ParentsGuide | None = None
        recommendation: Recommendation | None = None
        flags: list[str] = []
        # Only a video is parked in CHECKING, and CHECKING is the only status
        # this run passes through that a human can act on meanwhile.
        checking = item.media_type in _VIDEO_TYPES and bool(item.title)
        if checking:
            tmdb_result, parents_guide = await _check_content(session, item)

        dup_match = await _check_duplicate(session, item)
        twin_of = None if dup_match else await _check_in_flight_twin(session, item)
        book = None if dup_match else await _lookup_book(item)

        if dup_match:
            log.info("Duplicate detected for '%s' (id=%d): %s", item.title, item.id, dup_match)
        elif twin_of:
            log.info(
                "Another post of '%s' is still being processed; item %d goes to review",
                item.title, item.id,
            )

        now = _now()
        result = await _decide(
            session, item, tmdb_result, parents_guide, book, dup_match, now, twin_of=twin_of,
        )
        if checking:
            recommendation, flags = flags_of(result.decision)
        values = outcome_values(
            result, now,
            recommendation=recommendation,
            flags=flags,
            duplicate_of=dup_match,
            reparsing=item.error_message == OP_REPARSE,
        )

        if checking:
            if not await _write_checked_result(session, item.id, values):
                log.info(
                    "Item %d ('%s') was decided by hand while it was being "
                    "checked; dropping the %s this run reached",
                    item.id, item.title, result.decision.outcome,
                )
                return
        else:
            for column, value in values.items():
                setattr(item, column, value)
            await session.commit()

        emit_decision(item, result)
        log.info(
            "Item %d ('%s'): %s, %s (%s)",
            item.id, item.title, result.decision.outcome, result.applied.outcome,
            "; ".join(r.text for r in result.applied.reasons),
        )
        if values["status"] == ItemStatus.PENDING_REVIEW:
            await held_for_review(item.id)
        elif values["status"] == ItemStatus.REJECTED:
            await _decline_request(item)


async def _decline_request(item: MediaItem) -> None:
    """A rejection the policy made of a Seerr request is passed on to Seerr: the request is declined.

    Never raises: the decision is written, and a Seerr that cannot be told
    now is told by the source's next poll, which finds the request still
    pending beside a rejected item.
    """
    asked = seerr_api.request_of(item.origin_key)
    if asked is None:
        return
    try:
        await delivery.decline_request(item.id, asked[1])
    except Exception:
        log.exception("Could not decline the Seerr request of item %d", item.id)


async def _check_content(
    session, item: MediaItem
) -> tuple[TMDBResult, ParentsGuide]:
    """The pipeline's content check: park the item in CHECKING, then look it up.

    Nothing about the verdict is written to the item here. What this returns is
    written once, at the end, through a conditional UPDATE -- which is the only
    way a rejection taken mid-lookup survives. Moving from PARSED into CHECKING
    needs no condition: PARSED is not a status anybody can act on.
    """
    item.status = ItemStatus.CHECKING
    await session.commit()
    return await _run_content_check(session, item)


class LookupMissed(Exception):
    """The title lookup found nothing where the item's stored check has a match.

    An outage, a refused key and a rate limit all come back as "no match". A
    caller that asked to keep a stored match (a person's re-run) writes
    nothing then, instead of deciding the item on data it has just lost.
    """


@dryrun.about_item("item")
async def _run_content_check(
    session, item: MediaItem, *, keep_match: bool = False
) -> tuple[TMDBResult, ParentsGuide]:
    """Look the title up and store the content check. Never touches status.

    The caller owns the item's status and error message, because the two
    callers own them differently: the pipeline holds a loaded row, while the
    hourly re-check writes its result through a conditional UPDATE so a human
    decision taken mid-lookup is never overwritten.

    With ``keep_match``, a lookup that comes back empty for an item whose
    stored check has a match raises ``LookupMissed`` and stores nothing.
    """
    tmdb_result = TMDBResult()
    parents_guide = ParentsGuide()

    # A Seerr request names its title by TMDB id, and that id is what gets
    # approved there: the record is read by it, never searched for by name.
    asked = tmdb_link(item.raw_text) if seerr_api.request_of(item.origin_key) else None
    try:
        if asked is not None:
            tmdb_result = await tmdb_client.by_id(asked[1], item.media_type)
        else:
            tmdb_result = await tmdb_client.search(item.title, item.year, item.media_type)
    except Exception:
        log.exception("TMDB search failed for item %d", item.id)

    # The parents guide is asked for only while a rule reads it.
    cfg = settings.config
    wants_guide = cfg.integrations.enabled("imdb_guide") and any(
        name.startswith("imdb.") for name in _needed(item.media_type)
    )
    if tmdb_result.imdb_id and wants_guide:
        try:
            parents_guide = await fetch_parents_guide(tmdb_result.imdb_id)
        except Exception:
            log.exception("IMDb parents guide failed for %s", tmdb_result.imdb_id)

    title_flags = title_rule_hits(cfg.policy, item.media_type.value, tmdb_result, cfg)

    existing = (
        await session.execute(
            select(ContentCheck).where(ContentCheck.media_item_id == item.id)
        )
    ).scalar_one_or_none()
    if keep_match and existing is not None and existing.tmdb_id and not tmdb_result.tmdb_id:
        raise LookupMissed(item.id)
    check = existing or ContentCheck(media_item_id=item.id)
    check.tmdb_id = tmdb_result.tmdb_id
    check.tmdb_title = tmdb_result.title
    check.imdb_id = tmdb_result.imdb_id
    check.poster_url = tmdb_result.poster_url
    check.backdrop_url = tmdb_result.backdrop_url
    check.genres = tmdb_result.genres
    check.keywords = tmdb_result.keywords
    check.plot = tmdb_result.plot
    check.certifications = tmdb_result.certifications
    check.sex_rating = parents_guide.sex_rating
    check.violence_rating = parents_guide.violence_rating
    check.profanity_rating = parents_guide.profanity_rating
    check.frightening_rating = parents_guide.frightening_rating
    check.alcohol_rating = parents_guide.alcohol_rating
    check.guide_votes = dict(parents_guide.votes)
    # The 0.11 content flag column, now: did a keyword or genre rule hit.
    check.title_rule_hit = bool(title_flags)
    check.title_rule_words = title_flags
    if existing is None:
        session.add(check)
    await session.commit()

    log.info(
        "Content check done for '%s' (id=%d): title flags=%d guide_fetched=%s",
        item.title, item.id, len(title_flags), parents_guide.fetched,
    )
    return tmdb_result, parents_guide


async def _claim_waiting_item(session, item_id: int) -> MediaItem | None:
    """Take an item out of the waiting queue, or return None if somebody beat us.

    One statement, so two workers cannot both believe they have it: only the
    UPDATE that still saw AWAITING_DATA returns a row.
    """
    claimed = await session.execute(
        update(MediaItem)
        .where(
            MediaItem.id == item_id,
            MediaItem.status == ItemStatus.AWAITING_DATA,
        )
        .values(status=ItemStatus.CHECKING, error_message=OP_RECHECK)
        .returning(MediaItem.id)
    )
    if claimed.scalar_one_or_none() is None:
        return None
    await session.commit()
    return await session.get(MediaItem, item_id)


async def _release_waiting_item(item_id: int) -> None:
    """Put a claim back after a failed re-check, if it is still ours."""
    async with async_session() as session:
        await session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == item_id,
                MediaItem.status == ItemStatus.CHECKING,
                MediaItem.error_message == OP_RECHECK,
            )
            .values(status=ItemStatus.AWAITING_DATA, error_message=None)
        )
        await session.commit()


async def recover_stranded_rechecks() -> int:
    """Put back items a restart left mid-re-check. Run once at start-up.

    Only rows carrying this worker's own marker, so an item the ingestion
    pipeline was checking is left exactly where the pipeline left it.
    """
    async with async_session() as session:
        result = await session.execute(
            update(MediaItem)
            .where(
                MediaItem.status == ItemStatus.CHECKING,
                MediaItem.error_message == OP_RECHECK,
            )
            .values(status=ItemStatus.AWAITING_DATA, error_message=None)
        )
        await session.commit()
        return result.rowcount or 0


async def _write_recheck_result(
    session,
    item_id: int,
    result: Verdict,
    flags: list[str],
    duplicate_of: str | None,
    now: datetime,
) -> bool:
    """Write the decision, but only if the item is still the one we claimed.

    Everything between the claim and here is network time. If the operator rejected
    the item while TMDB and IMDb were being asked, their decision is the one that
    counts and this UPDATE matches nothing.
    """
    values = decision_values(result, now)
    if duplicate_of:
        values["error_message"] = f"Duplicate: {duplicate_of}"
        values["auto_recommendation"] = Recommendation.REJECT
    elif values.get("status") not in (ItemStatus.APPROVED, ItemStatus.REJECTED):
        values["error_message"] = "; ".join(flags) if flags else None

    written = await session.execute(
        update(MediaItem)
        .where(
            MediaItem.id == item_id,
            MediaItem.status == ItemStatus.CHECKING,
            MediaItem.error_message == OP_RECHECK,
        )
        .values(**values)
        .returning(MediaItem.id)
    )
    if written.scalar_one_or_none() is None:
        await session.rollback()
        return False
    await session.commit()
    return True


async def recheck_waiting_items(
    limit: int = RECHECK_BATCH,
    *,
    only_ids: Collection[int] | None = None,
    decisions: list[str] | None = None,
    push_held: bool = True,
    keep_match: bool = False,
    missed: list[int] | None = None,
) -> int:
    """Re-decide items parked waiting for parental-guide data.

    Bounded per run. Each item is claimed with one atomic statement and its
    result written back conditionally on that claim, so neither a second worker
    nor a human decision taken mid-lookup can be overwritten.

    `only_ids` narrows the pick to those items, and `decisions` collects what
    was done with every item this run wrote (approve, review, reject, wait).
    Both are for the backlog re-run, which also passes `push_held=False`: its
    items were pushed to the reviewers when they were first held, and one that
    is still held must not reach them a second time.

    `keep_match` is for a person's re-run of an item that was already looked
    up: when the title lookup now comes back empty, nothing is written (not
    the check, not a decision), the item stays waiting and its id is added to
    `missed`.
    """
    pick = select(MediaItem.id).where(MediaItem.status == ItemStatus.AWAITING_DATA)
    if only_ids is not None:
        pick = pick.where(MediaItem.id.in_(list(only_ids)))
    async with async_session() as session:
        ids = (
            await session.execute(pick.order_by(MediaItem.created_at).limit(limit))
        ).scalars().all()

    count = 0
    for item_id in ids:
        async with async_session() as session:
            item = await _claim_waiting_item(session, item_id)
            if item is None:
                continue
            try:
                tmdb_result: TMDBResult | None = None
                parents_guide: ParentsGuide | None = None
                flags: list[str] = []
                if item.media_type in _VIDEO_TYPES and item.title:
                    tmdb_result, parents_guide = await _run_content_check(
                        session, item, keep_match=keep_match
                    )
                # Asked again, not carried over: a title approved by hand in the
                # meantime must not be downloaded a second time.
                duplicate_of = await _check_duplicate(session, item)
                book = None if duplicate_of else await _lookup_book(item)
                result = await _decide(
                    session, item, tmdb_result, parents_guide, book, duplicate_of, _now()
                )
                if item.media_type in _VIDEO_TYPES:
                    _rec, flags = flags_of(result.decision)
                if await _write_recheck_result(
                    session, item_id, result, flags, duplicate_of, _now()
                ):
                    count += 1
                    if decisions is not None:
                        decisions.append(result.applied.outcome)
                    emit_decision(item, result, newly_held=push_held)
                    log.info(
                        "Re-checked waiting item %d: %s (%s)",
                        item_id, result.applied.outcome,
                        "; ".join(r.text for r in result.applied.reasons),
                    )
                    if result.applied.outcome == REVIEW and push_held:
                        await held_for_review(item_id)
                    elif result.applied.outcome == REJECT:
                        await _decline_request(item)
                else:
                    log.info(
                        "Waiting item %d was decided elsewhere; leaving it alone",
                        item_id,
                    )
            except LookupMissed:
                log.warning(
                    "Re-check of item %d found no title where one is stored; nothing written",
                    item_id,
                )
                await session.rollback()
                await _release_waiting_item(item_id)
                if missed is not None:
                    missed.append(item_id)
            except Exception:
                log.exception("Re-check failed for waiting item %d", item_id)
                await session.rollback()
                await _release_waiting_item(item_id)
    return count


@dataclass(frozen=True)
class RerunSummary:
    requeued: int = 0
    redecided: int = 0
    auto_approved: int = 0
    still_review: int = 0
    waiting: int = 0
    rejected: int = 0
    #: Set when nothing was looked at, with the reason.
    skipped: str | None = None

    def text(self) -> str:
        if self.skipped:
            return f"Re-run: {self.skipped}"
        if not self.requeued:
            return (
                "Re-run: nothing in the review queue is there for a setting that has changed since."
            )
        lines = [
            f"Re-run: {self.requeued} re-queued, {self.redecided} re-decided.",
            f"Auto-approved: {self.auto_approved}",
            f"Still review: {self.still_review}",
            f"Waiting for data: {self.waiting}",
        ]
        if self.rejected:
            lines.append(f"Rejected: {self.rejected}")
        left = self.requeued - self.redecided
        if left:
            lines.append(
                f"Not re-decided here: {left} (decided by hand meanwhile, failed, "
                "or past the loop bound; the hourly re-check takes any still waiting)"
            )
        return "\n".join(lines)


def _requeue_filter():
    """Never touched by a human, and sent to review by the policy itself."""
    return (
        MediaItem.status == ItemStatus.PENDING_REVIEW,
        MediaItem.reviewed_at.is_(None),
        MediaItem.auto_decision == AutoDecision.NEEDS_REVIEW,
    )


async def rerun_pending_reviews(max_loops: int = RERUN_MAX_LOOPS) -> RerunSummary:
    """Decide again every review a setting made, where that setting has moved.

    Items whose first stored reason is a setting (auto-approval off, shadow
    mode, a type held by its mode) go back to AWAITING_DATA in one
    conditional UPDATE, and are then re-decided through the hourly re-check's
    own claim and conditional write. A human decision taken at any point
    wins: the UPDATE skips anything already reviewed, and the claim skips
    anything no longer waiting. Rule hits, duplicates and wrong matches are
    never re-run.

    Only what could end somewhere else is re-queued (`worth_rerunning`):
    nothing while auto-approval is off, no shadow review of this same policy
    while still in shadow, no item whose type is still held by its mode. A
    row 0.11 decided is read by its own words: the switch being off and a
    type outside its list are settings, anything else is not re-run.
    """
    policy = settings.config.policy
    if policy.auto_approve == "off":
        return RerunSummary(
            skipped="auto-approval is off, so every item would be held again. "
            "Set policy.auto_approve to shadow or on first."
        )
    async with async_session() as session:
        rows = (
            await session.execute(
                select(
                    MediaItem.id, MediaItem.reasons, MediaItem.auto_reasons,
                    MediaItem.policy_version, MediaItem.media_type,
                ).where(*_requeue_filter())
            )
        ).all()
        candidates = [
            item_id for item_id, reasons, auto_reasons, version, media_type in rows
            if worth_rerunning(reasons, auto_reasons, version, media_type, policy)
        ]
        if not candidates:
            return RerunSummary()
        requeued = list(
            (
                await session.execute(
                    update(MediaItem)
                    .where(MediaItem.id.in_(candidates), *_requeue_filter())
                    .values(status=ItemStatus.AWAITING_DATA)
                    .returning(MediaItem.id)
                )
            ).scalars().all()
        )
        await session.commit()
    log.info("Re-run: %d policy reviews re-queued", len(requeued))

    # Batch by batch over our own ids, never the whole waiting queue: an item
    # that waits again stays AWAITING_DATA, so "until nothing was re-checked"
    # would re-ask TMDB for it on every loop.
    decisions: list[str] = []
    pending = requeued
    loops = 0
    while pending and loops < max_loops:
        batch, pending = pending[:RECHECK_BATCH], pending[RECHECK_BATCH:]
        loops += 1
        await recheck_waiting_items(
            len(batch), only_ids=batch, decisions=decisions, push_held=False,
        )

    summary = RerunSummary(
        requeued=len(requeued),
        redecided=len(decisions),
        auto_approved=decisions.count(APPROVE),
        still_review=decisions.count(REVIEW),
        waiting=decisions.count(WAIT),
        rejected=decisions.count(REJECT),
    )
    log.info("%s", summary.text().replace("\n", "; "))
    return summary


async def process_queue(queue) -> None:
    """Worker that processes items from the ingestion queue."""
    while True:
        item_id = await queue.get()
        try:
            await process_item(item_id)
        except Exception:
            log.exception("Pipeline failed for item %d", item_id)
        finally:
            queue.task_done()


async def reprocess_failed() -> int:
    """Re-parse items that previously failed parsing."""
    async with async_session() as session:
        result = await session.execute(
            select(MediaItem.id).where(MediaItem.status == ItemStatus.PARSE_FAILED)
        )
        ids = result.scalars().all()

    count = 0
    for item_id in ids:
        async with async_session() as session:
            result = await session.execute(
                update(MediaItem)
                .where(
                    MediaItem.id == item_id,
                    MediaItem.status == ItemStatus.PARSE_FAILED,
                )
                .values(
                    status=ItemStatus.PENDING_PARSE,
                    error_message=OP_REPARSE,
                )
                .returning(MediaItem.id)
            )
            if result.scalar_one_or_none() is None:
                continue
            await session.commit()
        try:
            await process_item(item_id)
        except Exception:
            log.exception("Reparse failed for item %d", item_id)
            async with async_session() as session:
                await session.execute(
                    update(MediaItem)
                    .where(
                        MediaItem.id == item_id,
                        MediaItem.error_message == OP_REPARSE,
                    )
                    .values(
                        status=ItemStatus.ERROR,
                        error_message="Reparse failed; recheck item",
                    )
                )
                await session.commit()
        count += 1
    return count
