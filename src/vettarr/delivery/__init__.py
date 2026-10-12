"""Where an approved item goes: ``deliver`` in config.yaml.

Each media type is delivered to one target: ``qbittorrent``, ``radarr``,
``sonarr``, ``seerr`` or ``none``. A type that is not listed is not delivered,
and an approved item of that type comes back to a person.

``deliver()`` is the one entry point, called by the search worker for every
approved item. The download client keeps the path it always had (the post's
linked torrent, its own torrent, a search): the worker hands that path in and
this module only decides whether it runs. The other targets take a title, not
a torrent: Radarr and Sonarr are told to add it and search for it themselves,
Seerr is sent a request. So a torrent the post carried or linked to is not
what arrives there, and the item's reasons say so.

After the hand-over the scheduler asks the target whether the files are there
(``poll``), and rejecting the item takes back what the hand-over made
(``withdraw``). Nothing here guesses: an item is completed only when the
target says it has the files, and a target that refuses, loses or declines it
puts the item back in front of a person.

One kind of item has no entry in ``deliver``: a request a ``seerr`` source
took from Seerr. Its delivery is Seerr itself. Approving it approves the
request there (``_answer_request``), rejecting it declines the request
(``decline_request``), and a request somebody decided in Seerr first is
written on the item and left as it is (``reflect_request``).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from vettarr import dryrun, events, ingest
from vettarr.checker.policy import META_IDENTITY, legacy_reasons
from vettarr.config import settings
from vettarr.config.schema import Config
from vettarr.database import async_session
from vettarr.delivery import seerr as seerr_api
from vettarr.delivery.http import DeliveryError
from vettarr.delivery.radarr import RadarrClient
from vettarr.delivery.seerr import SeerrClient
from vettarr.delivery.sonarr import SonarrClient, season_numbers, seasons_complete
from vettarr.models import (
    OP_APPROVAL,
    REVIEWABLE_STATUSES,
    AutoDecision,
    ContentCheck,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
    record_delivery,
)
from vettarr.searcher import providers

log = logging.getLogger(__name__)

QBITTORRENT = "qbittorrent"
RADARR = "radarr"
SONARR = "sonarr"
SEERR = "seerr"
NONE = "none"
#: The targets that take a title and fetch it themselves.
REMOTE = (RADARR, SONARR, SEERR)
LABELS = {QBITTORRENT: "qBittorrent", RADARR: "Radarr", SONARR: "Sonarr", SEERR: "Seerr"}

# What `deliver()` answers, beside the download client's own 'downloading',
# 'error' and 'no_match'.
#: Radarr, Sonarr or Seerr has it now; the scheduler watches for the files.
DELIVERED = "delivered"
#: The target already has the files. Nothing was added; the item is completed.
IN_LIBRARY = "in_library"
#: ``deliver`` says none for the type, or does not list it.
NOT_DELIVERED = "not_delivered"
ERROR = "error"
#: The item is a Seerr request that somebody declined or removed in Seerr
#: before the approval got there. The item is rejected, with that reason.
DECIDED_ELSEWHERE = "decided_elsewhere"
#: The item is a Seerr request and Seerr could not be asked. The item stays
#: approved and the worker comes back to it (``held_back``).
RETRY_LATER = "retry_later"

#: How every reason starts that reports a decision somebody took in Seerr.
DECIDED_IN_SEERR = "decided in Seerr"

#: The kind of an item reason this module writes.
REASON_KIND = "delivery"

_SEASON = re.compile(r"S(\d{1,2})", re.IGNORECASE)
_SEASON_RANGE = re.compile(r"S(\d{1,2})\s*-\s*S(\d{1,2})", re.IGNORECASE)


def _type_name(media_type: Any) -> str:
    return str(getattr(media_type, "value", media_type) or "unknown")


def target_for(media_type: Any, config: Config | None = None) -> str:
    """The target of a media type: its entry in ``deliver``, or ``none``.

    An item whose type could not be read has no entry of its own. A person
    can still approve it, and it then goes to the download client when at
    least one type is delivered there, as it always did. Otherwise it is not
    delivered: no other target can take an item without a type.
    """
    config = config or settings.config
    name = _type_name(media_type)
    if name == "unknown":
        return QBITTORRENT if QBITTORRENT in config.deliver.values() else NONE
    return config.deliver_to(name)


def seasons_of(season: str | None) -> list[int] | None:
    """The season numbers a post names (``S02``, ``S01E03``), or None for the whole series.

    ``S01-S03`` names the seasons between the two as well.
    """
    text = season or ""
    numbers = {int(n) for n in _SEASON.findall(text)}
    for first, last in _SEASON_RANGE.findall(text):
        numbers.update(range(int(first), int(last) + 1))
    return sorted(numbers) or None


# --- the clients ------------------------------------------------------------

_clients: dict[tuple, Any] = {}


def client_for(target: str, config: Config | None = None):
    """The client of an arr or of Seerr, or None while its integration is off. One per URL and key."""
    config = config or settings.config
    if target not in REMOTE or not config.integrations.enabled(target):
        return None
    block = getattr(config.integrations, target)
    api_key = block.api_key.get_secret_value()
    if target == SEERR:
        key = (target, block.url, api_key, block.user_id)
        build = lambda: SeerrClient(block.url, api_key, block.user_id)  # noqa: E731
    else:
        key = (target, block.url, api_key)
        build = lambda: (RadarrClient if target == RADARR else SonarrClient)(block.url, api_key)  # noqa: E731
    if key not in _clients:
        _clients[key] = build()
    return _clients[key]


async def test_connection(target: str, config: Config | None = None) -> str:
    """Test connection, for the Setup page: the service's version, or a ``DeliveryError``."""
    client = client_for(target, config)
    if client is None:
        raise DeliveryError(f"integrations.{target} is not enabled")
    return await client.test()


_tmdb = None


async def _tvdb_id(tmdb_id: int, *, required: bool = True) -> int | None:
    """The TVDB id of a series, from TMDB's external ids."""
    global _tmdb
    if _tmdb is None:
        from vettarr.checker.tmdb_client import TMDBClient

        _tmdb = TMDBClient()
    try:
        tvdb = await _tmdb.tvdb_id(tmdb_id)
    except Exception as exc:
        # Never the error's text: a TMDB address carries the API key.
        log.warning("TMDB could not be asked for the TVDB id of series %s: %s", tmdb_id, type(exc).__name__)
        tvdb = None
    if tvdb is None and required:
        raise DeliveryError("TMDB gave no TVDB id for this series, and Sonarr adds a series by it")
    return tvdb


# --- the hand-over ----------------------------------------------------------


@dataclass
class Handed:
    """What a target answered to a hand-over."""

    #: The target already holds the files; nothing was added.
    in_library: bool = False
    #: Radarr's movie id, Sonarr's series id, Seerr's request id.
    remote_id: Any = None
    #: True when this hand-over added the entry, turned its monitoring on or
    #: made the request. False when the target already had all of that.
    created: bool = False
    #: Sonarr only: which monitoring flags this hand-over turned on
    #: (``Download.remote_changes``).
    changes: dict[str, Any] | None = None


def _need(block: Any, target: str, field: str) -> str:
    value = getattr(block, field)
    if not value:
        raise DeliveryError(f"integrations.{target}.{field} is not set")
    return value


async def _to_radarr(item: Any, config: Config) -> Handed:
    client, block = client_for(RADARR, config), config.integrations.radarr
    movie = await client.find(item.tmdb_id)
    if movie is None:
        root = _need(block, RADARR, "root_folder")
        profile = await client.profile_id(block.quality_profile)
        found = await client.lookup(item.tmdb_id)
        if found is None:
            raise DeliveryError(f"Radarr finds no movie for TMDB id {item.tmdb_id}")
        try:
            added = await client.add(found, root, profile)
        except DeliveryError as exc:
            # Radarr refuses a movie it already has. Somebody added it between
            # the two calls, and a movie that is there is not a failure.
            movie = await client.find(item.tmdb_id) if exc.status == 400 else None
            if movie is None:
                raise
        else:
            return Handed(remote_id=added["id"], created=True)
    if movie.get("hasFile"):
        return Handed(in_library=True, remote_id=movie["id"])
    if movie.get("monitored"):
        return Handed(remote_id=movie["id"])
    # There, with no file, and nobody is looking for one.
    await client.set_monitored(movie, True)
    await client.search(movie["id"])
    return Handed(remote_id=movie["id"], created=True)


def _check_seasons(series: dict[str, Any], wanted: list[int] | None) -> None:
    # Every season Sonarr lists, the specials too: a post may name S00.
    listed = {s.get("seasonNumber") for s in series.get("seasons") or [] if isinstance(s, dict)}
    unknown = [n for n in wanted or [] if n not in listed]
    if unknown:
        raise DeliveryError(
            "Sonarr lists no season " + ", ".join(str(n) for n in unknown) + " of this series"
        )


def _to_turn_on(series: dict[str, Any], wanted: list[int] | None) -> dict[str, Any]:
    """The monitoring flags that are off and that the wanted seasons need on.

    A series has one flag of its own and one per season. What is on already
    is somebody else's doing, and stays out of what this item may take back.
    """
    numbers = set(season_numbers(series) if wanted is None else wanted)
    return {
        "series": not series.get("monitored"),
        "seasons": sorted(
            s["seasonNumber"] for s in series.get("seasons") or []
            if isinstance(s, dict) and s.get("seasonNumber") in numbers and not s.get("monitored")
        ),
    }


async def _to_sonarr(item: Any, config: Config) -> Handed:
    client, block = client_for(SONARR, config), config.integrations.sonarr
    tvdb_id = await _tvdb_id(item.tmdb_id)
    wanted = seasons_of(item.season)
    series = await client.find(tvdb_id)
    if series is None:
        root = _need(block, SONARR, "root_folder")
        profile = await client.profile_id(block.quality_profile)
        found = await client.lookup(tvdb_id)
        if found is None:
            raise DeliveryError(f"Sonarr finds no series for TVDB id {tvdb_id}")
        _check_seasons(found, wanted)
        try:
            added = await client.add(found, root, profile, wanted)
        except DeliveryError as exc:
            # Sonarr refuses a series it already has, as Radarr does a movie.
            series = await client.find(tvdb_id) if exc.status == 400 else None
            if series is None:
                raise
        else:
            # The entry is new, so every flag that is on is this hand-over's.
            seasons = sorted(season_numbers(found) if wanted is None else wanted)
            return Handed(remote_id=added["id"], created=True, changes={"series": True, "seasons": seasons})
    _check_seasons(series, wanted)
    if seasons_complete(series, wanted):
        return Handed(in_library=True, remote_id=series["id"])
    off = _to_turn_on(series, wanted)
    if not off["series"] and not off["seasons"]:
        return Handed(remote_id=series["id"])
    await client.monitor(series, wanted)
    return Handed(remote_id=series["id"], created=True, changes=off)


def _seerr_available(info: dict[str, Any], wanted: list[int] | None, key: str = "status") -> bool:
    """Whether Seerr says the title, or each season asked for, is available.

    ``key`` is ``status4k`` for a 4K request: Seerr keeps that copy's status apart.
    """
    if info.get(key) == seerr_api.AVAILABLE:
        return True
    if not wanted:
        return False
    seasons = {s.get("seasonNumber"): s.get(key) for s in info.get("seasons") or [] if isinstance(s, dict)}
    return all(seasons.get(n) == seerr_api.AVAILABLE for n in wanted)


def _open_requests(info: dict[str, Any], wanted: list[int] | None = None) -> list[dict[str, Any]]:
    """The requests Seerr still works on: pending or approved, for the seasons asked.

    A declined, failed or completed request is over and is nothing to wait
    on. With seasons named, a request for other seasons is not this item's.
    """
    live = [
        r for r in info.get("requests") or []
        if isinstance(r, dict) and r.get("status") in (seerr_api.REQUEST_PENDING, seerr_api.REQUEST_APPROVED)
    ]
    if not wanted:
        return live
    return [
        r for r in live
        if any(isinstance(s, dict) and s.get("seasonNumber") in wanted for s in r.get("seasons") or [])
    ]


async def _to_seerr(item: Any, config: Config) -> Handed:
    client, block = client_for(SEERR, config), config.integrations.seerr
    if block.user_id is None:
        raise DeliveryError(seerr_api.NO_USER)
    series = _type_name(item.media_type) == "series"
    kind = "tv" if series else "movie"
    wanted = seasons_of(item.season) if series else None
    info = await client.media(kind, item.tmdb_id)
    if _seerr_available(info, wanted):
        return Handed(in_library=True)
    tvdb_id = await _tvdb_id(item.tmdb_id, required=False) if series else None
    made = await client.request(kind, item.tmdb_id, tvdb_id=tvdb_id, seasons=wanted)
    if made is not None:
        return Handed(remote_id=made["id"], created=True)
    # Seerr made no request. It answers so when somebody already asked, and
    # also when every season asked for has a status there (available, partly
    # available) with no request at all. Only an open request is something
    # to wait on, so ask again: the answer above is from before the request.
    info = await client.media(kind, item.tmdb_id)
    if _seerr_available(info, wanted):
        return Handed(in_library=True)
    existing = _open_requests(info, wanted)
    if not existing:
        raise DeliveryError(
            "Seerr took no request and holds no open one for it: what was asked for already has "
            "a status there (available or partly available). Request what is missing in Seerr itself"
        )
    return Handed(remote_id=existing[0].get("id"))


_HAND_OVER: dict[str, Callable[[Any, Config], Awaitable[Handed]]] = {
    RADARR: _to_radarr, SONARR: _to_sonarr, SEERR: _to_seerr,
}


def _own_torrent(row: Any, item: Any, config: Config) -> str | None:
    """The torrent this post would have downloaded as it is, in words, or None."""
    carried = ingest.direct_add_of(row, config) if row is not None else None
    if carried:
        return f"{carried} this post carries"
    if providers.linked_torrent(config.providers, getattr(item, "external_links", None)) is not None:
        return "torrent this post links to"
    return None


def _doubted_match(row: Any) -> str | None:
    """Why the policy doubts the item's TMDB match, or None when it does not."""
    identity = (getattr(row, "signals", None) or {}).get(META_IDENTITY)
    if isinstance(identity, dict) and identity.get("ok") is False:
        return str(identity.get("text") or "no detail")
    return None


def _approved_by_a_person(row: Any) -> bool:
    """Whether the approval being delivered is a person's, not the policy's own.

    The policy stamps ``reviewed_at`` with the moment it decided
    (``auto_decided_at``). A person's approval stamps a later one, and an
    item the policy did not approve can only have been approved by a person.
    """
    if getattr(row, "auto_decision", None) != AutoDecision.AUTO_APPROVE:
        return True
    return getattr(row, "reviewed_at", None) != getattr(row, "auto_decided_at", None)


def _check_identity(row: Any, label: str) -> str | None:
    """Refuse a TMDB id the policy doubts, unless a person approved the item.

    The download client is searched by the post's own title, so a wrong TMDB
    match changes nothing there. Here the id is all the target is given. The
    policy sends such an item to a person where it reads the title; a type it
    approves without reading it arrives here on the policy's word alone, and
    is refused. A person saw the doubt on the card and decided: that approval
    goes through, and the doubt comes back as a reason to write on the item.
    """
    doubt = _doubted_match(row)
    if doubt is None:
        return None
    if _approved_by_a_person(row):
        return f"sent on a person's approval: the policy doubted the TMDB match ({doubt})"
    raise DeliveryError(
        f"the policy doubts the TMDB match ({doubt}), and the id is all {label} is given. "
        "Approve the item yourself to send that match, or reject it"
    )


def _with_reasons(row: Any, texts: list[str]) -> list[dict[str, Any]]:
    """The item's stored reasons with the delivery's own added, each once."""
    stored = [
        r.to_json()
        for r in legacy_reasons(getattr(row, "reasons", None), getattr(row, "auto_reasons", None))
    ]
    known = {r["text"] for r in stored if r["kind"] == REASON_KIND}
    return stored + [
        {"kind": REASON_KIND, "rule": None, "text": text} for text in texts if text not in known
    ]


def _claimed(item_id: int):
    """The item, while the worker's approval still holds it."""
    return update(MediaItem).where(
        MediaItem.id == item_id,
        MediaItem.status == ItemStatus.SEARCHING,
        MediaItem.error_message == OP_APPROVAL,
    )


def _emit(kind: str, item: Any, target: str, **payload: Any) -> None:
    events.emit(
        kind, item.id, title=getattr(item, "title", None),
        media_type=_type_name(getattr(item, "media_type", None)), target=target, **payload,
    )


def no_target_reason(media_type: Any, config: Config | None = None) -> str | None:
    """Why nothing can take this type, when ``deliver`` does not say ``none`` for it. Else None.

    ``none`` is a decision: record the approval and fetch nothing. A type the
    map leaves out is more likely a gap in the file than a decision.
    """
    config = config or settings.config
    name = _type_name(media_type)
    if target_for(media_type, config) != NONE or name in config.deliver:
        return None
    if name == "unknown":
        return (
            "its type could not be read, and deliver sends no type to the download client, "
            "the only target that takes an item without one"
        )
    return f"deliver lists no target for {name}"


async def _no_target(item: Any, why: str) -> str:
    """A type ``deliver`` leaves out: the item goes back to a person, and can be approved again.

    Never completed: a completed item cannot be approved again or rejected,
    and it holds every later post of the title as a duplicate.
    """
    message = (
        f"Not delivered: {why}. List the type under deliver (none for a type that is only "
        "decided), then approve the item again"
    )
    async with async_session() as session:
        await session.execute(_claimed(item.id).values(status=ItemStatus.ERROR, error_message=message))
        await session.commit()
    _emit(events.DELIVERY_FAILED, item, NONE, reason=why)
    _emit(events.NEEDS_YOU, item, NONE, reasons=[message])
    log.warning("Item %d ('%s') was not delivered: %s", item.id, item.title, why)
    return ERROR


async def _not_delivered(item: Any, config: Config) -> str:
    name = _type_name(item.media_type)
    why = f"not delivered: deliver says none for {name}"
    async with async_session() as session:
        row = await session.get(MediaItem, item.id)
        await session.execute(
            _claimed(item.id).values(
                status=ItemStatus.COMPLETED, error_message=None, reasons=_with_reasons(row, [why]),
            )
        )
        await session.commit()
    # The post's own torrent is not needed: nothing is added anywhere.
    ingest.forget_torrent(item.id)
    _emit(events.COMPLETED, item, NONE, note=why)
    log.info("Item %d ('%s') %s", item.id, item.title, why)
    return NOT_DELIVERED


async def _failed(item: Any, target: str, problem: str) -> str:
    """The target would not take it: the item goes back to a person, with why."""
    message = f"{LABELS[target]} delivery failed: {problem}"
    async with async_session() as session:
        await session.execute(_claimed(item.id).values(status=ItemStatus.ERROR, error_message=message))
        await session.commit()
    _emit(events.DELIVERY_FAILED, item, target, reason=problem)
    _emit(events.NEEDS_YOU, item, target, reasons=[message])
    log.warning("Delivery of item %d ('%s') to %s failed: %s", item.id, item.title, target, problem)
    return ERROR


async def _deliver_remote(item: Any, target: str, config: Config) -> str:
    label = LABELS[target]
    async with async_session() as session:
        row = await session.get(MediaItem, item.id)
    try:
        if not item.tmdb_id:
            raise DeliveryError(
                f"the item has no TMDB id, and {label} is given a title by it "
                "(integrations.tmdb must be on, and must have found the title)"
            )
        overruled = _check_identity(row, label)
        handed = await _HAND_OVER[target](item, config)
    except DeliveryError as exc:
        return await _failed(item, target, str(exc))

    notes = [overruled] if overruled else []
    own = _own_torrent(row, item, config)
    if own:
        notes.append(f"delivered by title: {label} picks its own release, not the {own}")
    if handed.in_library:
        notes.append(f"already in the library ({label})")
    return await _record_handed(item, row, target, handed, notes, by_title=bool(own))


async def _record_handed(
    item: Any, row: Any, target: str, handed: Handed, notes: list[str], *, by_title: bool = False,
) -> str:
    """Write what a hand-over did: the download row a poll reads, the item's status, the event."""
    label = LABELS[target]
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        values: dict[str, Any] = {"error_message": None}
        if notes:
            values["reasons"] = _with_reasons(row, notes)
        if handed.in_library:
            values["status"] = ItemStatus.COMPLETED
        else:
            dl = (
                await session.execute(select(Download).where(Download.media_item_id == item.id))
            ).scalar_one_or_none()
            if dl is None:
                dl = Download(media_item_id=item.id)
                session.add(dl)
            dl.torrent_search_id = None
            dl.magnet_url = None
            dl.download_url = None
            dl.category = None
            # A dry run's delivery is marked: the poll finds it done (`poll`).
            remote = dryrun.mark(handed.remote_id) if dryrun.active() else handed.remote_id
            record_delivery(dl, target, remote, handed.created, handed.changes)
            dl.status = DownloadStatus.DOWNLOADING
            dl.progress = 0.0
            dl.started_at = now
            values["status"] = ItemStatus.DOWNLOADING
            values["reviewed_at"] = now
        await session.execute(_claimed(item.id).values(**values))
        await session.commit()

    remote_id = str(handed.remote_id) if handed.remote_id is not None else None
    if handed.in_library:
        # The files are there, so the post's own torrent is not needed. A
        # delivery that only started keeps it until the files arrive
        # (`arrived`): the target can still decline, fail or find nothing.
        ingest.forget_torrent(item.id)
        _emit(events.COMPLETED, item, target, remote_id=remote_id, note=notes[-1])
        log.info("Item %d ('%s') is already in the library (%s)", item.id, item.title, label)
        return IN_LIBRARY
    _emit(events.DELIVERED, item, target, remote_id=remote_id, by_title=by_title, created=handed.created)
    log.info("Item %d ('%s') delivered to %s (id %s)", item.id, item.title, label, remote_id)
    return DELIVERED


async def deliver(
    item: Any,
    config: Config | None = None,
    *,
    download_client: Callable[[Any], Awaitable[str]],
) -> str:
    """Hand an approved item to the target ``deliver`` names for its type.

    ``item`` is the worker's ``ItemRef``. ``download_client`` is the path to
    the download client, run as it is when the target is ``qbittorrent``.
    Answers 'downloading', 'no_match' or 'error' from that path, and
    ``DELIVERED``, ``IN_LIBRARY``, ``NOT_DELIVERED`` or ``ERROR`` from the
    others. The item's row is written by the time it returns.

    An item that is a Seerr request (a ``seerr`` source made it) is not sent
    anywhere by ``deliver``: its request is approved in Seerr. That answers
    ``DELIVERED``, ``ERROR``, ``DECIDED_ELSEWHERE`` or ``RETRY_LATER``.

    ``none`` completes the item with nothing fetched. A type the map does
    not list at all is not completed: it comes back to a person (``ERROR``).
    """
    config = config or settings.config
    asked = seerr_api.request_of(getattr(item, "origin_key", None))
    if asked is not None:
        # A Seerr request: its delivery is Seerr itself, whatever `deliver` says for the type.
        return await _answer_request(item, asked[1], config)
    target = target_for(item.media_type, config)
    if target == QBITTORRENT:
        result = await download_client(item)
        if result == "downloading":
            _emit(events.DELIVERED, item, target)
        elif result == "error":
            _emit(events.DELIVERY_FAILED, item, target)
        return result
    if target == NONE:
        unlisted = no_target_reason(item.media_type, config)
        if unlisted:
            return await _no_target(item, unlisted)
        return await _not_delivered(item, config)
    return await _deliver_remote(item, target, config)


# --- an item that is a Seerr request ----------------------------------------

#: The wait before an approval Seerr could not be asked about is tried again,
#: doubled after each try that fails, up to the longest.
RETRY_FIRST_SECONDS = 30.0
RETRY_MAX_SECONDS = 1800.0
# Item id -> (tries that failed, when the next one may run). In memory: a
# restart tries every waiting approval at once, and starts counting again.
_retries: dict[int, tuple[int, float]] = {}
_clock = time.monotonic

# The statuses an item waits for a decision in, and so the ones a decision
# taken in Seerr may still be written on.
_UNDECIDED = (ItemStatus.PENDING_REVIEW, ItemStatus.AWAITING_DATA)


def held_back() -> list[int]:
    """The approved items the worker leaves alone for now: Seerr did not answer for them."""
    now = _clock()
    return [item_id for item_id, (_tries, at) in _retries.items() if at > now]


def _is_outage(exc: DeliveryError) -> bool:
    """No answer, or a server's own failure: nothing about the request, so worth asking again."""
    return exc.status is None or exc.status == 429 or exc.status >= 500


#: How far apart Seerr's date of a request and the item's own may be and still be the same request.
_SAME_MOMENT = timedelta(seconds=1)


def same_request(item: Any, request: dict[str, Any]) -> bool:
    """Whether the request Seerr has under an id is the one this item was made from.

    Seerr numbers its requests from 1 again when its database starts again:
    a reinstall, an older backup put back, another Seerr behind the same
    address. The id alone then names somebody else's request. An item knows
    two more things about its own: the title's TMDB id, in the link its text
    carries, and when it was asked for. Either one differing makes it
    another request.
    """
    link = ingest.tmdb_link(getattr(item, "raw_text", None))
    if link is not None:
        media = request.get("media") if isinstance(request.get("media"), dict) else {}
        kind = request.get("type") or media.get("mediaType")
        if kind != link[0] or str(media.get("tmdbId")) != str(link[1]):
            return False
    asked_at, posted_at = seerr_api.created_at(request), getattr(item, "posted_at", None)
    if asked_at is not None and isinstance(posted_at, datetime):
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        return abs(asked_at - posted_at) <= _SAME_MOMENT
    return True


async def request_status(client: SeerrClient, request_id: int, item: Any = None) -> int | None:
    """Seerr's status of the request an item is.

    None once it is gone from Seerr, which it also is when Seerr has given
    its id to another request (``same_request``): nothing this item decides
    is carried out on that one.
    """
    request = await client.get_request(request_id)
    if not isinstance(request, dict) or (item is not None and not same_request(item, request)):
        return None
    return request.get("status")


async def _retry_later(item: Any, problem: str) -> str:
    """Seerr could not be asked: the item stays approved, and the worker comes back to it.

    Never an error and never completed. An approval that only this app knows
    of is not one: the request still waits in Seerr, so the item keeps
    asking, with a longer wait each time.
    """
    tries = _retries.get(item.id, (0, 0.0))[0] + 1
    wait = min(RETRY_FIRST_SECONDS * 2 ** min(tries - 1, 16), RETRY_MAX_SECONDS)
    _retries[item.id] = (tries, _clock() + wait)
    message = f"Seerr could not be told yet ({problem}). Its request is still pending there; this is tried again"
    async with async_session() as session:
        await session.execute(_claimed(item.id).values(status=ItemStatus.APPROVED, error_message=message))
        await session.commit()
    if tries == 1:
        # Once an outage, not once a try.
        _emit(events.DELIVERY_FAILED, item, SEERR, reason=problem, retrying=True)
    log.warning(
        "Seerr could not be asked about the request of item %d (try %d, next in %.0f s): %s",
        item.id, tries, wait, problem,
    )
    return RETRY_LATER


_APPROVED_IN_SEERR = f"{DECIDED_IN_SEERR}: its request was approved there"
# What a rejection of the item wrote when its decline went through, and what
# an approval that comes after it says: the decline was this app's own.
_DECLINED = "Its Seerr request was declined."
# What a rejection writes under dry_run: Seerr was not told, and the request
# still waits there. The source's poll leaves it alone once this is said.
_WOULD_DECLINE = "Dry run: its Seerr request would have been declined; nothing was sent, and it still waits in Seerr."
_DECLINED_HERE = (
    "its Seerr request was declined when this item was rejected. Seerr approves no declined "
    "request, so it has to be asked for again"
)


def _declined_here(row: Any) -> bool:
    """Whether the item's own rejection is what declined its request."""
    return any(
        r["kind"] == REASON_KIND and r["text"] == f"rejected: {_DECLINED}" for r in _with_reasons(row, [])
    )


def _decided(status: int | None) -> str:
    """The reason for a request somebody declined or removed in Seerr."""
    if status is None:
        return f"{DECIDED_IN_SEERR}: its request was removed there"
    return (
        f"{DECIDED_IN_SEERR}: its request is declined there. Seerr approves no declined "
        "request, so it has to be asked for again"
    )


async def _answer_request(item: Any, request_id: int, config: Config) -> str:
    """Carry out the approval of an item that is a Seerr request.

    The request is read first and approved only while it is still pending.
    One that somebody approved in Seerr meanwhile is waited on like any
    delivery, and one that was declined or removed there rejects the item:
    in both cases nothing is sent, and the item says who decided.

    Seerr approves as its admin here (no ``X-API-User``): deciding a request
    takes the manage-requests permission.
    """
    client = client_for(SEERR, config)
    if client is None:
        return await _failed(item, SEERR, "integrations.seerr is off, so its request cannot be approved")
    async with async_session() as session:
        row = await session.get(MediaItem, item.id)
    ours = False
    try:
        status = await request_status(client, request_id, row)
        if status == seerr_api.REQUEST_PENDING:
            try:
                await client.approve(request_id)
            except DeliveryError as exc:
                if exc.status not in (404, 409):
                    raise
                # Somebody decided it between the two calls.
                status = await request_status(client, request_id, row)
            else:
                ours, status = True, seerr_api.REQUEST_APPROVED
    except DeliveryError as exc:
        if _is_outage(exc):
            return await _retry_later(item, str(exc))
        _retries.pop(item.id, None)
        return await _failed(item, SEERR, str(exc))
    _retries.pop(item.id, None)
    if status == seerr_api.REQUEST_PENDING:
        return await _failed(item, SEERR, "Seerr would not approve the request, and it is still pending there")

    if status is None or status == seerr_api.REQUEST_DECLINED:
        reason = _decided(status)
        if status == seerr_api.REQUEST_DECLINED and _declined_here(row):
            # A person approved an item the policy had rejected: nobody in Seerr declined it.
            reason = _DECLINED_HERE
        async with async_session() as session:
            await session.execute(_claimed(item.id).values(
                status=ItemStatus.REJECTED, error_message=reason[0].upper() + reason[1:],
                reviewed_at=datetime.now(timezone.utc), reasons=_with_reasons(row, [reason]),
            ))
            await session.commit()
        _emit(events.DECLINED, item, SEERR, remote_id=str(request_id), reason=reason)
        log.info("Item %d ('%s'): %s", item.id, item.title, reason)
        return DECIDED_ELSEWHERE
    note = "its Seerr request was approved" if ours else _APPROVED_IN_SEERR
    # Not this item's making: the request was there first, so a later
    # rejection takes nothing back from Seerr.
    return await _record_handed(item, row, SEERR, Handed(remote_id=request_id), [note])


_GONE = "Its Seerr request was already gone from Seerr."
_NOT_PENDING = "Its Seerr request is no longer pending, so it stays as it is in Seerr."


async def decline_request(
    item_id: int, request_id: int, config: Config | None = None,
) -> TakenBack:
    """Decline in Seerr the request a rejected item is, and write on the item what that did.

    Only a pending request is declined. The request is read first and the
    decline is sent only while it still waits: Seerr itself refuses to
    decline a request that is no longer pending since 3.5.0, and before
    that it set whatever it was told. One that is no longer pending
    (somebody decided it there, or this app approved it before the item was
    rejected) stays as it is, and the answer says so. When Seerr cannot be
    told, the request still waits there: the source's next poll finds it
    pending beside a rejected item and declines it then.
    """
    client = client_for(SEERR, config)
    if client is None:
        taken = TakenBack("Seerr is off, so its request could not be declined and still waits there.", failed=True)
    else:
        async with async_session() as session:
            row = await session.get(MediaItem, item_id)
        try:
            status = await request_status(client, request_id, row)
            if status == seerr_api.REQUEST_PENDING:
                await client.decline(request_id)
        except DeliveryError as exc:
            if exc.status == 404:
                taken = TakenBack(_GONE)
            elif exc.status == 409:
                taken = TakenBack(_NOT_PENDING)
            else:
                log.warning("Could not decline the Seerr request of item %d: %s", item_id, exc)
                taken = TakenBack(
                    f"Seerr could not be told, and its request still waits there: {exc}. "
                    "It is declined at the next poll that reaches Seerr.",
                    failed=True,
                )
        else:
            if status is None:
                taken = TakenBack(_GONE)
            elif status != seerr_api.REQUEST_PENDING:
                taken = TakenBack(_NOT_PENDING)
            else:
                taken = TakenBack(_WOULD_DECLINE if dryrun.active() else _DECLINED)
    await _note_taken_back(item_id, taken, SEERR, str(request_id))
    return taken


async def declined_by_dry_run(item_id: int) -> bool:
    """Whether a dry run already recorded the decline of this item's Seerr request.

    Seerr was not told, so it still lists the request as pending: the
    source's poll would otherwise decline it again at every poll.
    """
    async with async_session() as session:
        row = await session.get(MediaItem, item_id)
    return row is not None and any(
        r["kind"] == REASON_KIND and r["text"] == f"rejected: {_WOULD_DECLINE}" for r in _with_reasons(row, [])
    )


async def reflect_request(item_id: int, status: int | None) -> str | None:
    """Write on an item that still waits here what somebody decided about its request in Seerr.

    ``status`` is Seerr's own for the request, None when it is gone. A
    request declined or removed there rejects the item. One approved there
    approves it, and the worker then waits for Seerr's files without sending
    anything. Nothing is ever sent to Seerr from here: whoever decided there
    is not argued with. Answers 'rejected', 'approved' or None when the item
    had moved on.
    """
    if status == seerr_api.REQUEST_PENDING:
        return None
    declined = status is None or status == seerr_api.REQUEST_DECLINED
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        row = await session.get(MediaItem, item_id)
        if row is None:
            return None
        if declined:
            reason = _decided(status)
            held = list(REVIEWABLE_STATUSES)
            values: dict[str, Any] = {
                "status": ItemStatus.REJECTED, "error_message": reason[0].upper() + reason[1:],
            }
        else:
            reason = _APPROVED_IN_SEERR
            held = list(_UNDECIDED)
            values = {"status": ItemStatus.APPROVED, "error_message": None}
        moved = await session.execute(
            update(MediaItem)
            .where(MediaItem.id == item_id, MediaItem.status.in_(held))
            .values(reviewed_at=now, reasons=_with_reasons(row, [reason]), **values)
            .returning(MediaItem.id)
        )
        written = moved.scalar_one_or_none() is not None
        await session.commit()
    if not written:
        return None
    if declined:
        events.emit(
            events.DECLINED, item_id, title=getattr(row, "title", None),
            media_type=_type_name(getattr(row, "media_type", None)), target=SEERR, reason=reason,
        )
    log.info("Item %d: %s", item_id, reason)
    return "rejected" if declined else "approved"


def grab_refusal(
    media_type: Any, config: Config | None = None, *, origin_key: str | None = None,
) -> str | None:
    """Why a release a person picked cannot be added for this type, or None when it can.

    A picked release goes to the download client. A type delivered anywhere
    else has no use for it, and saying so beats adding a torrent ``deliver``
    did not ask for. Nor has an item that is a Seerr request (``origin_key``).
    """
    if seerr_api.request_of(origin_key) is not None:
        return "This item is a Seerr request, and Seerr picks its own release. Approve it to approve the request."
    target = target_for(media_type, config)
    if target == QBITTORRENT:
        return None
    name = _type_name(media_type)
    if target == NONE:
        return f"Nothing is delivered for {name}: deliver names no target for it."
    return (
        f"A {name} goes to {LABELS[target]}, which picks its own release. "
        "Approve the item to send it there."
    )


# --- completion -------------------------------------------------------------


@dataclass
class RemoteState:
    """What the target says about a delivered item."""

    #: The target has the files.
    done: bool = False
    #: Set when the delivery cannot finish any more: one line for a person.
    problem: str | None = None
    #: The problem is a refusal by a person at the target, not a failure.
    declined: bool = False


async def _radarr_state(client: RadarrClient, download: Any, item: Any, tmdb_id: int | None) -> RemoteState:
    movie = await client.get(download.remote_id)
    if movie is None:
        return RemoteState(problem="Radarr no longer has this movie")
    if movie.get("hasFile"):
        return RemoteState(done=True)
    if not movie.get("monitored"):
        return RemoteState(problem="the movie was unmonitored in Radarr before it had a file")
    return RemoteState()


async def _sonarr_state(client: SonarrClient, download: Any, item: Any, tmdb_id: int | None) -> RemoteState:
    series = await client.get(download.remote_id)
    if series is None:
        return RemoteState(problem="Sonarr no longer has this series")
    wanted = seasons_of(getattr(item, "season", None))
    if seasons_complete(series, wanted):
        return RemoteState(done=True)
    numbers = set(season_numbers(series) if wanted is None else wanted)
    watched = [s for s in series.get("seasons") or [] if s.get("seasonNumber") in numbers and s.get("monitored")]
    if not series.get("monitored") or not watched:
        return RemoteState(problem="the series was unmonitored in Sonarr before its files arrived")
    return RemoteState()


async def _seerr_state(client: SeerrClient, download: Any, item: Any, tmdb_id: int | None) -> RemoteState:
    if not tmdb_id:
        return RemoteState(problem="the item lost its TMDB id, and Seerr is asked by it")
    series = _type_name(getattr(item, "media_type", None)) == "series"
    info = await client.media("tv" if series else "movie", tmdb_id)
    requests = [r for r in info.get("requests") or [] if isinstance(r, dict)]
    ours = (
        next((r for r in requests if str(r.get("id")) == str(download.remote_id)), None)
        if download.remote_id else None
    )
    # A 4K request (only a Seerr user makes one) is done when the 4K copy is there.
    key = "status4k" if ours is not None and ours.get("is4k") else "status"
    if _seerr_available(info, seasons_of(getattr(item, "season", None)) if series else None, key):
        return RemoteState(done=True)
    if download.remote_id:
        if ours is None:
            return RemoteState(problem="the request was removed in Seerr", declined=True)
        if ours.get("status") == seerr_api.REQUEST_DECLINED:
            return RemoteState(problem="the request was declined in Seerr", declined=True)
        if ours.get("status") == seerr_api.REQUEST_FAILED:
            return RemoteState(problem="Seerr could not hand the request on to its own download service")
    elif not _open_requests(info):
        return RemoteState(problem="Seerr has no open request for it any more", declined=True)
    return RemoteState()


_STATE = {RADARR: _radarr_state, SONARR: _sonarr_state, SEERR: _seerr_state}


async def poll(download: Any, config: Config | None = None) -> RemoteState:
    """Ask the target of a live delivery whether the files are there.

    Radarr answers with the movie's ``hasFile``, Sonarr with the episode file
    statistics of the seasons the item asked for, Seerr with the media
    status. Raises ``DeliveryError`` when the target could not be asked,
    which says nothing about the item: the next poll asks again.
    """
    config = config or settings.config
    target = download.delivery_target
    if dryrun.simulated(download.remote_id):
        # Handed over by would-have: it arrived as it was handed, with no wait.
        return RemoteState(done=True)
    client = client_for(target, config)
    if client is None:
        return RemoteState(problem=f"integrations.{target} is off, so nothing can say whether it arrived")
    async with async_session() as session:
        item = await session.get(MediaItem, download.media_item_id)
        tmdb_id = (
            await session.execute(
                select(ContentCheck.tmdb_id).where(ContentCheck.media_item_id == download.media_item_id)
            )
        ).scalar_one_or_none()
    if seerr_api.request_of(getattr(item, "origin_key", None)) is not None:
        # A Seerr request names its title itself; the content check may have found nothing.
        link = ingest.tmdb_link(getattr(item, "raw_text", None))
        tmdb_id = link[1] if link else tmdb_id
    return await _STATE[target](client, download, item, tmdb_id)


async def completed(download: Any) -> None:
    """The event of a delivery that finished, whatever its target."""
    async with async_session() as session:
        item = await session.get(MediaItem, download.media_item_id)
    target = download.delivery_target or QBITTORRENT
    events.emit(
        events.COMPLETED, download.media_item_id,
        title=getattr(item, "title", None), media_type=_type_name(getattr(item, "media_type", None)),
        target=target, remote_id=download.remote_id,
    )


async def arrived(download: Any, posts: Any = None) -> None:
    """The target has the files: the post's ``on_download`` runs now, and its kept torrent goes.

    The download client's path acts on the post once its torrent is seen
    downloading. A title handed to Radarr, Sonarr or Seerr can still be
    declined or never found, and the post, with whatever torrent it carried,
    is then all there is to fall back on. So it stays until the files exist.
    """
    ingest.forget_torrent(download.media_item_id)
    if posts is None:
        return
    try:
        await posts.after("download", download.media_item_id)
    except Exception:
        log.exception("on_download failed for item %d", download.media_item_id)


async def stalled(download: Any, state: RemoteState) -> None:
    """A delivery the target will not finish: the item goes back to a person, with why.

    Only while the item is still the live delivery it was: a rejection that
    landed in the meantime stands.
    """
    target = download.delivery_target
    message = f"{LABELS[target]}: {state.problem}"
    async with async_session() as session:
        item = await session.get(MediaItem, download.media_item_id)
        # A Seerr request declined or removed in Seerr after it was approved:
        # somebody decided there, and the item says so. It is not an error
        # for a person to sort out, and nothing is sent back.
        elsewhere = state.declined and seerr_api.request_of(getattr(item, "origin_key", None)) is not None
        values: dict[str, Any] = {"status": ItemStatus.ERROR, "error_message": message}
        if elsewhere:
            reason = f"{DECIDED_IN_SEERR}: {state.problem}"
            values = {
                "status": ItemStatus.REJECTED, "error_message": reason[0].upper() + reason[1:],
                "reviewed_at": datetime.now(timezone.utc), "reasons": _with_reasons(item, [reason]),
            }
        await session.execute(
            update(Download)
            .where(Download.id == download.id, Download.status == DownloadStatus.DOWNLOADING)
            .values(status=DownloadStatus.CANCELLED if elsewhere else DownloadStatus.ERROR)
        )
        moved = await session.execute(
            update(MediaItem)
            .where(MediaItem.id == download.media_item_id, MediaItem.status == ItemStatus.DOWNLOADING)
            .values(**values)
            .returning(MediaItem.id)
        )
        held = moved.scalar_one_or_none() is not None
        await session.commit()
    if not held:
        return
    payload = {
        "title": getattr(item, "title", None),
        "media_type": _type_name(getattr(item, "media_type", None)),
        "target": target,
    }
    events.emit(
        events.DECLINED if state.declined else events.DELIVERY_FAILED, download.media_item_id,
        remote_id=download.remote_id, reason=state.problem, **payload,
    )
    if not elsewhere:
        events.emit(events.NEEDS_YOU, download.media_item_id, reasons=[message], **payload)
    log.warning("Delivery of item %d to %s will not finish: %s", download.media_item_id, target, state.problem)


# --- rejection --------------------------------------------------------------


@dataclass
class TakenBack:
    """What rejecting a delivered item did at its target."""

    #: One sentence for the person.
    note: str
    #: The target could not be told, so what the delivery made is still
    #: there and still fetching.
    failed: bool = False


def _season_words(numbers: list[int]) -> str:
    names = [str(n) for n in numbers]
    if len(names) == 1:
        return f"season {names[0]}"
    return "seasons " + ", ".join(names[:-1]) + " and " + names[-1]


def _said(note: str, label: str) -> str:
    """What taking a delivery back says: under ``dry_run``, that the target was not told."""
    if not dryrun.active():
        return note
    return f"Dry run: {label} was not told. Live, this would say: {note}"


async def withdraw(
    target: str | None,
    remote_id: str | None,
    created: bool,
    changes: dict[str, Any] | None = None,
    config: Config | None = None,
) -> TakenBack | None:
    """Take back what delivering a now-rejected item made, or None when there is nothing to say.

    Radarr and Sonarr are told to stop monitoring it: a PUT with ``monitored``
    false, which deletes no file and no entry. A Seerr request is deleted
    while it is still pending. Only what this item's own delivery made: an
    entry or a request that was there before is left as it is. In Sonarr
    that goes flag by flag (``changes``): the seasons this delivery turned
    on, and the series' own flag only when this delivery turned that on too.
    """
    if dryrun.simulated(remote_id) and not dryrun.active():
        # A dry run handed it over by would-have: the target never got it,
        # so there is nothing there to take back.
        return None
    # The id a dry run's hand-over was recorded with; none when it only made one up.
    remote_id = dryrun.unmark(remote_id)
    if target not in REMOTE or not remote_id:
        return None
    label = LABELS[target]
    if not created:
        return TakenBack(f"{label} had it before this item did, so it is left as it is there.")
    client = client_for(target, config)
    if client is None:
        return TakenBack(f"{label} is off, so it could not be told and still has it.", failed=True)
    try:
        if target == RADARR:
            movie = await client.get(remote_id)
            if movie is None:
                return None
            await client.set_monitored(movie, False)
            return TakenBack(_said("Radarr no longer monitors it; its entry and files are untouched.", label))
        if target == SONARR:
            series = await client.get(remote_id)
            if series is None:
                return None
            seasons = sorted((changes or {}).get("seasons") or [])
            series_too = bool((changes or {}).get("series"))
            if not seasons and not series_too:
                return TakenBack("Sonarr had it before this item did, so it is left as it is there.")
            await client.unmonitor(series, seasons, series_too=series_too)
            if series_too:
                return TakenBack(_said("Sonarr no longer monitors it; its entry and files are untouched.", label))
            return TakenBack(_said(
                f"Sonarr no longer monitors {_season_words(seasons)} of it. The series was monitored "
                "before this item and stays so; nothing was deleted.", label,
            ))
        request = await client.get_request(remote_id)
        if request is None:
            return None
        if request.get("status") != seerr_api.REQUEST_PENDING:
            return TakenBack("Its Seerr request is no longer pending, so it stays there.")
        await client.withdraw(remote_id)
        return TakenBack(_said("Its Seerr request was withdrawn.", label))
    except DeliveryError as exc:
        log.warning("Could not take the rejected delivery back from %s: %s", target, exc)
        return TakenBack(f"{label} could not be told, and still has it: {exc}.", failed=True)


async def take_back(
    item_id: int,
    target: str | None,
    remote_id: str | None,
    created: bool,
    changes: dict[str, Any] | None = None,
) -> TakenBack | None:
    """``withdraw``, and write what it did where a person looks later.

    The sentence is kept on the item as a delivery reason, so the item page
    says it and not only the answer to the click. A target that could not be
    told still holds the title: that is a ``delivery_failed`` event, which
    notifies.
    """
    taken = await withdraw(target, remote_id, created, changes)
    if taken is None:
        return None
    await _note_taken_back(item_id, taken, target, remote_id)
    return taken


async def _note_taken_back(item_id: int, taken: TakenBack, target: str | None, remote_id: str | None) -> None:
    """Keep what a rejection did at the target on the item, and raise the event when it could not be told.

    The event is raised once for an item and a problem: a decline the poll
    sends again every few minutes, and that fails the same way each time,
    does not notify each time.
    """
    text = f"rejected: {taken.note}"
    async with async_session() as session:
        row = await session.get(MediaItem, item_id)
        reasons = _with_reasons(row, []) if row is not None else []
        said_before = any(r["kind"] == REASON_KIND and r["text"] == text for r in reasons)
        if row is not None and not said_before:
            await session.execute(
                update(MediaItem).where(MediaItem.id == item_id)
                .values(reasons=_with_reasons(row, [text]))
            )
            await session.commit()
    if taken.failed and not said_before:
        events.emit(
            events.DELIVERY_FAILED, item_id,
            title=getattr(row, "title", None), media_type=_type_name(getattr(row, "media_type", None)),
            target=target, remote_id=remote_id, reason=taken.note, rejected=True,
        )


# --- the library ------------------------------------------------------------


def library_of(media_type: Any, config: Config | None = None) -> str | None:
    """The arr this type is delivered to, whose library joins the `in_library` signal."""
    target = target_for(media_type, config)
    return target if target in (RADARR, SONARR) else None


async def in_library(item: Any, tmdb_id: int, config: Config | None = None) -> bool | None:
    """Whether the arr this item would go to already has its files.

    True, False, or None when it could not be asked: the answers of
    ``JellyfinClient.has_tmdb_id``, which the signal already reads.
    """
    target = library_of(getattr(item, "media_type", None), config)
    client = client_for(target, config) if target else None
    if client is None:
        return None
    if target == RADARR:
        return await client.has_tmdb_id(tmdb_id)
    tvdb_id = await _tvdb_id(tmdb_id, required=False)
    if tvdb_id is None:
        return None
    return await client.has_tvdb_id(tvdb_id, seasons_of(getattr(item, "season", None)))


# --- what a person is shown -------------------------------------------------

_STATES = {
    DownloadStatus.DOWNLOADING: "it has the title and is fetching it",
    DownloadStatus.COMPLETED: "the files are there",
    DownloadStatus.ERROR: "it will not finish",
    DownloadStatus.CANCELLED: "rejected after it was sent",
}


def where(item: Any, download: Any = None, config: Config | None = None) -> str | None:
    """One line saying where the item went, or where approving will send it.

    None for the download client before anything is sent: that is the
    default, and the page and the bot say nothing more than they did.
    """
    notes = [
        str(r.get("text")) for r in getattr(item, "reasons", None) or []
        if isinstance(r, dict) and r.get("kind") == REASON_KIND
    ]
    sent = getattr(download, "delivery_target", None)
    if sent in REMOTE:
        line = f"{LABELS[sent]}: {_STATES.get(download.status, 'sent')}"
        if download.remote_id:
            line += f" (id {download.remote_id})"
        return "; ".join([line, *notes])
    if notes:
        return "; ".join(notes)
    if download is not None:
        return None
    asked = seerr_api.request_of(getattr(item, "origin_key", None))
    if asked is not None:
        if getattr(getattr(item, "status", None), "value", None) not in _BEFORE_DELIVERY:
            return None
        return (
            f"Seerr request {asked[1]}: approving approves it in Seerr, and rejecting declines it there. "
            "Seerr fetches it; deliver is not read for it."
        )
    target = target_for(getattr(item, "media_type", None), config)
    if target == QBITTORRENT:
        return None
    if getattr(getattr(item, "status", None), "value", None) not in _BEFORE_DELIVERY:
        return None
    name = _type_name(getattr(item, "media_type", None))
    config = config or settings.config
    if target == NONE:
        unlisted = no_target_reason(getattr(item, "media_type", None), config)
        if unlisted:
            return f"Nothing can take it: {unlisted}. Approving it now brings it back here."
        return f"Approving records the decision and fetches nothing: deliver says none for {name}."
    line = f"Approving sends the title to {LABELS[target]}, which picks its own release."
    own = _own_torrent(item, item, config)
    if own:
        line += f" The {own} is not what downloads."
    doubt = _doubted_match(item)
    if doubt:
        line += f" The policy doubts the TMDB match ({doubt}); approving sends that match."
    return line


_BEFORE_DELIVERY = frozenset({
    "pending_review", "awaiting_data", "no_match", "parse_failed", "error", "approved", "checking",
})
