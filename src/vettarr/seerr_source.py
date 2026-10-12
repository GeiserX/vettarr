"""A ``seerr`` source: Seerr's pending requests become items, and the policy decides them.

Seerr has users who ask and admins who approve. It has no rule that approves.
A source of kind ``seerr`` makes this app the one who decides: every request
that waits for approval in Seerr becomes an item, goes through the same
policy as a post, and the decision is carried out in Seerr by ``delivery``
(approve, decline, or leave it pending for a reviewer).

Two ways in, one item:

- the webhook (``hook``): Seerr's webhook agent POSTs each notification to
  ``/hooks/seerr/<source key>``. Seerr sends it once, ignores the answer and
  never sends it again, so this is only the fast way.
- the poll (``poll``): every ``poll_minutes`` the list of pending requests is
  read. This is the guarantee: whatever the webhook missed is found here.

Both end in ``take``, which works from the request as Seerr's own API gives
it. The webhook's body only says which request to look at. Its template is
the operator's to edit, it has no field for the requester's id (which the
``sender`` signals need), and reading the request back means nobody who
learns the secret can make an item out of a request that is not pending.

The poll is also where the two sides are compared, so that nothing depends
on a call that was made once:

- a request still pending in Seerr whose item is rejected here is declined:
  the decline that could not be sent when the item was rejected;
- a request still pending in Seerr whose item the pipeline never finished
  (the process stopped, or the web UI on its own took the webhook) is handed
  to the pipeline again;
- an item that still waits here whose request is no longer pending in Seerr
  was decided there by somebody else, and the item is written to match.

An item is its request by more than the id. Seerr starts its ids again when
its database does, so the title's TMDB id and the request's date are
compared too (``delivery.same_request``). An item whose id now names another
request is retired, and that request becomes an item of its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update

from vettarr import delivery, dryrun, events, ingest
from vettarr.config import settings
from vettarr.database import async_session
from vettarr.delivery import seerr as seerr_api
from vettarr.delivery.http import DeliveryError
from vettarr.models import (
    REVIEWABLE_STATUSES,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
)

if TYPE_CHECKING:
    from vettarr.config.schema import Config, Source

log = logging.getLogger(__name__)

#: ``telegram_chat_id`` of an item that is a Seerr request: no Telegram chat
#: has this id. Its ``telegram_message_id`` is the request's id in Seerr.
SEERR_CHAT = 0
#: The longest ``season`` an item holds (the column's width).
_SEASON_MAX = 20

PENDING = "MEDIA_PENDING"
TEST = "TEST_NOTIFICATION"
#: The notifications about a request this app already holds that change its item.
_UPDATES = ("MEDIA_AVAILABLE", "MEDIA_DECLINED", "MEDIA_FAILED")


def _int(value: Any) -> int | None:
    """A whole number Seerr sent as a number or, from a webhook template, as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    # ASCII digits only: "²" is a digit to str.isdigit and no number to int.
    return int(text) if text.isascii() and text.isdigit() else None


# ---------------------------------------------------------------------------
# The webhook's body
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hook:
    """What a webhook call says: which notification, about which request."""

    event: str
    request_id: int | None


def read_hook(payload: Any) -> Hook:
    """Read Seerr's webhook JSON, in the default template's shape or a flat one.

    The default template nests the request's fields under ``request``
    (``{"notification_type": ..., "request": {"request_id": ...}}``). A flat
    one carries them at the top, under Seerr's own variable names
    (``{"notification_type": ..., "request_id": ...}``). ``request`` is null
    for a notification about no request (the test one).
    """
    if not isinstance(payload, dict):
        return Hook("", None)
    event = str(payload.get("notification_type") or "").strip().upper()
    nested = payload.get("request")
    request_id = _int(nested.get("request_id")) if isinstance(nested, dict) else None
    if request_id is None:
        request_id = _int(payload.get("request_id"))
    return Hook(event, request_id)


# ---------------------------------------------------------------------------
# A request becomes an item
# ---------------------------------------------------------------------------


def season_text(numbers: list[int]) -> str | None:
    """The seasons a request names, as an item's ``season`` (``S02``, ``S01-S03 S05``).

    Runs are written as ranges, which is what ``delivery.seasons_of`` reads
    back. None for a set that does not fit the column even without padding:
    the item then stands for the whole series.
    """
    runs: list[list[int]] = []
    for number in sorted(set(numbers)):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    for width in (2, 1):
        text = " ".join(
            f"S{first:0{width}d}" if first == last else f"S{first:0{width}d}-S{last:0{width}d}"
            for first, last in runs
        )
        if text and len(text) <= _SEASON_MAX:
            return text
    return None


def _own_delivery(sender_id: int | None, config: Config) -> bool:
    """Whether a request is one this app made itself, for ``deliver: seerr``.

    Those are asked for as ``integrations.seerr.user_id`` and their items
    already wait on them. Taken as new items they would be held as their own
    duplicates.
    """
    block = config.integrations.seerr
    return (
        sender_id is not None and block is not None and block.user_id == sender_id
        and delivery.SEERR in config.deliver.values()
    )


_IDENTITY = (MediaItem.id, MediaItem.origin_key, MediaItem.raw_text, MediaItem.posted_at)


async def _known(source: Source, request_id: int) -> SimpleNamespace | None:
    """The item this request id already has, by its key or by the row an earlier source wrote."""
    async with async_session() as session:
        found = (await session.execute(
            select(*_IDENTITY).where(or_(
                MediaItem.origin_key == seerr_api.request_key(source.key, request_id),
                and_(MediaItem.telegram_chat_id == SEERR_CHAT, MediaItem.telegram_message_id == request_id),
            )).limit(1)
        )).one_or_none()
    if found is None:
        return None
    item_id, origin_key, raw_text, posted_at = found
    return SimpleNamespace(id=item_id, origin_key=origin_key, raw_text=raw_text, posted_at=posted_at)


async def _retire(source: Source, held: SimpleNamespace, request_id: int) -> None:
    """Free a request id for the request that has it now.

    The item's own request is gone: Seerr's ids started again, and this one
    names another title or another day's request. The item keeps the key of
    a Seerr request, one no request in Seerr has, so nothing it decides is
    sent anywhere. If it still waited for a decision, the poll writes it as
    removed.
    """
    key = held.origin_key or seerr_api.request_key(source.key, request_id)
    async with async_session() as session:
        await session.execute(
            update(MediaItem).where(MediaItem.id == held.id)
            .values(origin_key=seerr_api.retired_key(key, held.id), telegram_message_id=-held.id)
        )
        await session.commit()
    log.warning(
        "Source %s: Seerr request %d is not the request item %d was made from (its ids started again); "
        "the item is retired and the request is taken as a new one", source.key, request_id, held.id,
    )


async def take(source: Source, request: dict[str, Any], config: Config | None = None) -> str:
    """Make the item of one pending request, as Seerr's API gives the request.

    Answers 'added', 'known' (it is an item already: the webhook and the poll
    both saw it) or 'skipped' (not pending any more, not for this source, or
    TMDB could not name it, in which case the next poll tries again).
    """
    config = config or settings.config
    request_id = _int(request.get("id"))
    if request_id is None or request.get("status") != seerr_api.REQUEST_PENDING:
        return "skipped"
    media = request.get("media") if isinstance(request.get("media"), dict) else {}
    kind = request.get("type") or media.get("mediaType")
    tmdb_id = _int(media.get("tmdbId"))
    if kind not in ("movie", "tv") or tmdb_id is None:
        log.warning("Source %s: Seerr request %d names no movie or series by TMDB id; not taken", source.key, request_id)
        return "skipped"
    requester = request.get("requestedBy") if isinstance(request.get("requestedBy"), dict) else {}
    sender_id = _int(requester.get("id"))
    if _own_delivery(sender_id, config) or not ingest.sender_allowed(source, sender_id, config):
        return "skipped"
    held = await _known(source, request_id)
    if held is not None:
        if delivery.same_request(held, request):
            return "known"
        await _retire(source, held, request_id)

    resolved = await ingest.resolve_id(kind, str(tmdb_id))
    if resolved is None or not resolved.title:
        log.warning(
            "Source %s: TMDB did not name Seerr request %d (integrations.tmdb must be on); "
            "it is tried again at the next poll", source.key, request_id,
        )
        return "skipped"
    seasons = sorted({
        n for n in (_int(s.get("seasonNumber")) for s in request.get("seasons") or [] if isinstance(s, dict))
        if n is not None
    })
    # What a reviewer reads as the post. The TMDB link is also where the
    # content check and the availability poll take the id from.
    lines = [
        f"Seerr request {request_id}: {resolved.title}" + (f" ({resolved.year})" if resolved.year else "")
        + (" 4K" if request.get("is4k") else ""),
        f"https://www.themoviedb.org/{kind}/{tmdb_id}",
    ]
    if kind == "tv" and seasons:
        lines.append("Seasons: " + ", ".join(str(n) for n in seasons))
    # The requester by id, never by name. The pipeline reads this text as a
    # post, and a display name is the requester's to write: "Cam" would be a
    # release source and "2160p 30 GB" a resolution and a size.
    if sender_id is not None:
        lines.append(f"Requested by Seerr user {sender_id}")
    values: dict[str, Any] = {
        "telegram_message_id": request_id,
        "telegram_chat_id": SEERR_CHAT,
        "raw_text": "\n".join(lines),
        "status": ItemStatus.PENDING_PARSE,
        "source_key": source.key,
        "origin_key": seerr_api.request_key(source.key, request_id),
        "sender_id": sender_id,
        "posted_at": seerr_api.created_at(request),
        "title": resolved.title[:500],
        "year": resolved.year,
        "media_type": resolved.media_type,
    }
    if kind == "tv" and seasons:
        values["season"] = season_text(seasons)
    item_id = await ingest.add(values)
    if item_id is None:
        return "known"
    log.info("Source %s: Seerr request %d taken as item %d", source.key, request_id, item_id)
    return "added"


# ---------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------


@dataclass
class Polled:
    """What one poll found and did."""

    pending: int = 0
    added: int = 0
    #: Rejections Seerr had not been told of, declined now.
    declined: int = 0
    #: Items written to match a decision somebody took in Seerr.
    reflected: int = 0
    #: Items the pipeline never finished, handed to it again.
    requeued: int = 0


async def poll(source: Source, config: Config | None = None) -> Polled:
    """Read Seerr's pending requests once and settle the difference with this source's items.

    Raises ``DeliveryError`` when the list cannot be read. That says nothing
    about any request: the next poll asks again.
    """
    config = config or settings.config
    done = Polled()
    client = delivery.client_for(delivery.SEERR, config)
    if client is None:
        return done
    pending = {
        request_id: request for request in await client.pending_requests()
        if (request_id := _int(request.get("id"))) is not None
    }
    done.pending = len(pending)

    keys = {seerr_api.request_key(source.key, request_id): request_id for request_id in pending}
    async with async_session() as session:
        rows = (await session.execute(
            select(*_IDENTITY, MediaItem.status, MediaItem.error_message)
            .where(MediaItem.origin_key.in_(list(keys)))
        )).all() if keys else []
        waiting = (await session.execute(
            select(*_IDENTITY)
            .where(MediaItem.source_key == source.key, MediaItem.status.in_(list(REVIEWABLE_STATUSES)))
        )).all()
    items = {
        keys[origin_key]: SimpleNamespace(
            id=item_id, raw_text=raw_text, posted_at=posted_at, status=status, error_message=error_message,
        )
        for item_id, origin_key, raw_text, posted_at, status, error_message in rows if origin_key in keys
    }

    for request_id, request in pending.items():
        try:
            held = items.get(request_id)
            if held is None or not delivery.same_request(held, request):
                # New, or the id of an item that was made from another request.
                done.added += (await take(source, request, config)) == "added"
            elif held.status == ItemStatus.REJECTED:
                if dryrun.active() and await delivery.declined_by_dry_run(held.id):
                    # A dry run's decline is never sent, so Seerr keeps listing
                    # the request: it was recorded once, and is not again.
                    continue
                # Rejected here and still waiting there: the decline never arrived.
                taken = await delivery.decline_request(held.id, request_id, config)
                done.declined += not taken.failed
            elif held.status in ingest.UNFINISHED:
                # Inserted and never decided: the queue it was on is gone.
                done.requeued += await ingest.add_again(held.id, held.status)
        except Exception:
            log.exception("Source %s: Seerr request %d could not be handled", source.key, request_id)

    for item_id, origin_key, raw_text, posted_at in waiting:
        asked = seerr_api.request_of(origin_key)
        if asked is None:
            continue
        mine = SimpleNamespace(raw_text=raw_text, posted_at=posted_at)
        listed = pending.get(asked[1])
        if listed is not None and delivery.same_request(mine, listed):
            continue
        try:
            # Pending under this id is another request: this item's own is gone.
            status = None if listed is not None else await delivery.request_status(client, asked[1], mine)
            done.reflected += (await delivery.reflect_request(item_id, status)) is not None
        except DeliveryError as exc:
            log.warning("Source %s: Seerr could not be asked about request %d: %s", source.key, asked[1], exc)
    return done


async def poll_job(source_key: str) -> None:
    """The scheduler's poll of one source. Never raises: a Seerr that is down is asked again next time."""
    source = next(
        (s for s in settings.config.sources if s.key == source_key and s.kind == "seerr"), None,
    )
    if source is None:
        return
    try:
        done = await poll(source)
    except DeliveryError as exc:
        log.warning("Source %s: Seerr's pending requests could not be read: %s", source_key, exc)
        return
    except Exception:
        log.exception("Source %s: the poll of Seerr failed", source_key)
        return
    if done.added or done.declined or done.reflected or done.requeued:
        log.info(
            "Source %s: %d pending in Seerr, %d new, %d declined late, %d decided in Seerr, %d queued again",
            source_key, done.pending, done.added, done.declined, done.reflected, done.requeued,
        )


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------


async def _held(source: Source, request_id: int) -> int | None:
    """The item this source holds for a request, or None."""
    async with async_session() as session:
        found = await session.execute(
            select(MediaItem.id)
            .where(MediaItem.origin_key == seerr_api.request_key(source.key, request_id))
            .limit(1)
        )
        return found.scalar_one_or_none()


async def _update(item_id: int, request_id: int, client: Any, posts: Any) -> None:
    """Bring an item up to date with its request, now and not at the next check.

    An item whose request this app approved waits on Seerr like any
    delivery, and the same check that runs every five minutes runs for it
    here. Any other item still waits for a decision, and takes the one
    somebody made in Seerr.

    Raises ``DeliveryError`` when Seerr cannot be asked: nothing was updated.
    """
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        download = (await session.execute(
            select(Download).where(
                Download.media_item_id == item_id,
                Download.status == DownloadStatus.DOWNLOADING,
                Download.delivery_target == delivery.SEERR,
            )
        )).scalar_one_or_none()
    if download is not None:
        from vettarr import scheduler

        await scheduler.check_delivery(download, posts)
        return
    await delivery.reflect_request(item_id, await delivery.request_status(client, request_id, item))


async def hook(source: Source, payload: Any, posts: Any = None, config: Config | None = None) -> str:
    """Act on one webhook call. Answers what it did, for the route to say back.

    'test' for Seerr's test notification, 'added' or 'known' for a pending
    request, 'updated' for news about a request this source holds, 'ignored'
    for everything else, and 'deferred' when Seerr's API could not be asked
    just now: the poll picks the request up.
    """
    config = config or settings.config
    seen = read_hook(payload)
    if seen.event == TEST:
        events.emit(events.WEBHOOK_TEST, source=source.key)
        log.info("Source %s: Seerr's test notification arrived", source.key)
        return "test"
    if seen.request_id is None or seen.event not in (PENDING, *_UPDATES):
        return "ignored"
    client = delivery.client_for(delivery.SEERR, config)
    if client is None:
        return "ignored"
    try:
        if seen.event == PENDING:
            request = await client.get_request(seen.request_id)
            if not isinstance(request, dict):
                return "ignored"
            taken = await take(source, request, config)
            return "ignored" if taken == "skipped" else taken
        item_id = await _held(source, seen.request_id)
        if item_id is None:
            return "ignored"
        await _update(item_id, seen.request_id, client, posts)
        return "updated"
    except DeliveryError as exc:
        log.warning(
            "Source %s: Seerr's API could not be asked about request %d (%s); the poll picks it up",
            source.key, seen.request_id, exc,
        )
        return "deferred"
