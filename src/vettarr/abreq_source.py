"""An ``abreq`` source: AudioBookRequest's requests become items, and the policy decides them.

AudioBookRequest (ABR) lets its users ask for audiobooks by Audible ASIN. A
user it does not trust can only ask; somebody trusted, or a rule, starts the
download. A source of kind ``abreq`` makes this app the one who decides:
every book that waits in ABR becomes an item, goes through the same policy as
a post, and the decision is carried out in ABR by ``delivery`` (download it,
delete the request, or leave it waiting for a reviewer).

ABR knows no pending, approved or declined. A book waits while somebody asks
for it and it is not downloaded. So approving asks ABR to download it, and
rejecting deletes every user's request for it. One item stands for a book,
however many users asked for it.

Two ways in, one item:

- the webhook (``hook``): an ABR notification on ``onNewRequest`` POSTs
  ``{"event": "{eventType}", "asin": "{bookASIN}", "user": "{eventUser}"}`` to
  ``/hooks/abreq/<source key>``. ABR sends it once, and runs its
  notifications one after another, stopping at the first that fails; the
  route answers at once and does the work after.
- the poll (``poll``): every ``poll_minutes`` ABR's list is read, in one
  call. This is the guarantee: whatever the webhook missed is found here.

Both end in ``take``, which works from the book as ABR's own list gives it:
the webhook's body only says which book to look at, so nobody who learns the
secret can make an item of a book nobody asked for.

The poll is also where the two sides are compared:

- a book still waiting in ABR whose item is rejected here is deleted when
  that rejection's delete failed or was never sent. Once the request left
  ABR, by that delete, by its user or by an ABR admin, the book waits again
  because somebody asked for it again: the old item is retired, and the new
  request becomes an item of its own;
- a book still waiting whose item the pipeline never finished is handed to
  the pipeline again;
- an item that still waits here (in error too) whose book was downloaded
  in ABR, or that nobody asks for any more, was decided there, and the item
  is written to match.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from vettarr import delivery, dryrun, events, ingest
from vettarr.config import settings
from vettarr.database import async_session
from vettarr.delivery import abreq as abreq_api
from vettarr.delivery.http import DeliveryError
from vettarr.models import REVIEWABLE_STATUSES, ItemStatus, MediaItem, MediaType

if TYPE_CHECKING:
    from vettarr.config.schema import Config, Source

log = logging.getLogger(__name__)

#: ``telegram_chat_id`` of an item that is an AudioBookRequest request. No
#: Telegram chat id is this far from zero. Its ``telegram_message_id`` is a
#: hash of the book's ASIN, and the item's own id negated once it is retired.
ABREQ_CHAT = -(1 << 62)
#: What ABR's own test button sends: its placeholders, not filled in. It
#: fills only ``{eventType}``. A manual request (a book not on Audible) fills
#: the user but has no ASIN either, so the two are told apart by the user.
TEST_ASIN = "{bookASIN}"
TEST_USER = "{eventUser}"
NEW_REQUEST = "onnewrequest"
#: An Audible product page, which works for any ASIN.
AUDIBLE = "https://www.audible.com/pd/{asin}"


def message_id(asin: str) -> int:
    """A stable positive 63-bit number for a book: the unique column its item has to fill.

    The ASIN alone, not the source: two abreq sources on one ABR then make one
    item of a book, the first source's, as two seerr sources do of a request.
    """
    return int.from_bytes(hashlib.sha256(f"abreq:{asin}".encode()).digest()[:8], "big") >> 1


# ---------------------------------------------------------------------------
# The webhook's body
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hook:
    """What a webhook call says: which notification, about which book, or that it is ABR's test."""

    event: str
    asin: str | None
    test: bool = False


def read_hook(payload: Any) -> Hook:
    """Read the body the docs tell the operator to set.

    ``{"event": "{eventType}", "asin": "{bookASIN}", "user": "{eventUser}"}``.
    ABR's test button fills in only the event, so a literal ``{bookASIN}``
    beside a literal ``{eventUser}`` is the test. A literal ``{bookASIN}``
    with a user is a manual request, which has no ASIN, and is ignored.
    """
    if not isinstance(payload, dict):
        return Hook("", None)
    event = str(payload.get("event") or "").strip()
    raw = str(payload.get("asin") or "").strip()
    if raw == TEST_ASIN and str(payload.get("user") or "").strip() == TEST_USER:
        return Hook(event, None, test=True)
    asin = raw.upper()
    return Hook(event.lower(), asin if abreq_api.ASIN.fullmatch(asin) else None)


# ---------------------------------------------------------------------------
# A book becomes an item
# ---------------------------------------------------------------------------


def _names(values: Any) -> list[str]:
    return [str(v).strip() for v in values or [] if str(v).strip()] if isinstance(values, list) else []


async def _known(source: Source, asin: str) -> SimpleNamespace | None:
    """The item this book already has, under its live key."""
    async with async_session() as session:
        found = (await session.execute(
            select(MediaItem.id, MediaItem.origin_key, MediaItem.status, MediaItem.reasons)
            .where(MediaItem.origin_key == abreq_api.request_key(source.key, asin)).limit(1)
        )).one_or_none()
    if found is None:
        return None
    item_id, origin_key, status, reasons = found
    return SimpleNamespace(id=item_id, origin_key=origin_key, status=status, reasons=reasons)


def asked_again(held: Any) -> bool:
    """Whether a book still waiting in ABR is a new request: its item is rejected and its request left ABR.

    The request left by this app's delete (which removed every request for
    the book), or because its user or an ABR admin removed it. Whatever ABR
    lists for the book now was asked for after that. A rejection whose delete
    failed or was never sent is not this: the request it rejected may still
    be the one listed, and the poll deletes it.
    """
    return held.status == ItemStatus.REJECTED and delivery.abreq_request_left(held)


async def _retire(source: Source, held: SimpleNamespace, asin: str) -> None:
    """Free the book's key for the request that has it now.

    The item keeps the key of an AudioBookRequest request, one no book has,
    so nothing it decides is sent anywhere.
    """
    key = held.origin_key or abreq_api.request_key(source.key, asin)
    async with async_session() as session:
        await session.execute(
            update(MediaItem).where(MediaItem.id == held.id)
            .values(origin_key=abreq_api.retired_key(key, held.id), telegram_message_id=-held.id)
        )
        await session.commit()
    log.info(
        "Source %s: book %s was asked for again after item %d's request left AudioBookRequest; "
        "the item is retired and the request is taken as a new one", source.key, asin, held.id,
    )


async def take(source: Source, entry: dict[str, Any], config: Config | None = None) -> str:
    """Make the item of one book that waits in ABR, as ABR's list gives it.

    Answers 'added', 'known' (it is an item already: the webhook and the poll
    both saw it) or 'skipped' (downloaded already, nobody asks for it, or it
    has no title).
    """
    config = config or settings.config
    asin = abreq_api.asin_of(entry)
    if asin is None or abreq_api.fulfilled(entry):
        return "skipped"
    requests = [r for r in entry.get("requests") or [] if isinstance(r, dict)]
    book = entry["book"]
    name = str(book.get("title") or "").strip()
    if not requests or not name or not ingest.sender_allowed(source, None, config):
        return "skipped"
    held = await _known(source, asin)
    if held is not None:
        if not asked_again(held):
            return "known"
        await _retire(source, held, asin)

    authors, narrators = _names(book.get("authors")), _names(book.get("narrators"))
    released = abreq_api.when(book.get("release_date"))
    year = released.year if released is not None else None
    asked = sorted(t for t in (abreq_api.when(r.get("updated_at")) for r in requests) if t is not None)
    # What a reviewer reads as the post. The requesters are never in it: the
    # pipeline reads this text as a post, and a username is the user's to
    # write ("2160p 30 GB" would be a resolution and a size).
    lines = [f"AudioBookRequest request {asin}: {name}" + (f" ({year})" if year else "")]
    subtitle = str(book.get("subtitle") or "").strip()
    if subtitle:
        lines.append(subtitle)
    if authors:
        lines.append("By " + ", ".join(authors))
    if narrators:
        lines.append("Narrated by " + ", ".join(narrators))
    lines.append(AUDIBLE.format(asin=asin))
    origin_key = abreq_api.request_key(source.key, asin)
    values: dict[str, Any] = {
        "telegram_message_id": message_id(asin),
        "telegram_chat_id": ABREQ_CHAT,
        "raw_text": "\n".join(lines),
        "status": ItemStatus.PENDING_PARSE,
        "source_key": source.key,
        "origin_key": origin_key,
        "sender_id": None,
        "posted_at": asked[0] if asked else None,
        # "Title - Author": the book lookup splits on the last " - ", and
        # matches nothing without an author.
        "title": (f"{name} - {authors[0]}" if authors else name)[:500],
        "year": year,
        "media_type": MediaType.AUDIOBOOK,
    }
    item_id = await ingest.add(values)
    if item_id is None:
        return "known"
    log.info("Source %s: AudioBookRequest book %s taken as item %d", source.key, asin, item_id)
    return "added"


# ---------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------


@dataclass
class Polled:
    """What one poll found and did."""

    waiting: int = 0
    added: int = 0
    #: Rejections ABR had not been told of, deleted now.
    declined: int = 0
    #: Items written to match what happened to their book in ABR.
    reflected: int = 0
    #: Items the pipeline never finished, handed to it again.
    requeued: int = 0


async def poll(source: Source, config: Config | None = None) -> Polled:
    """Read ABR's list once and settle the difference with this source's items.

    Raises ``DeliveryError`` when the list cannot be read. That says nothing
    about any book: the next poll asks again.
    """
    config = config or settings.config
    done = Polled()
    client = delivery.client_for(delivery.ABREQ, config)
    if client is None:
        return done
    listed = await client.wishlist()
    waiting = {asin: entry for asin, entry in listed.items() if not abreq_api.fulfilled(entry)}
    done.waiting = len(waiting)

    keys = {abreq_api.request_key(source.key, asin): asin for asin in waiting}
    async with async_session() as session:
        rows = (await session.execute(
            select(MediaItem.id, MediaItem.origin_key, MediaItem.status, MediaItem.reasons)
            .where(MediaItem.origin_key.in_(list(keys)))
        )).all() if keys else []
        undecided = (await session.execute(
            select(MediaItem.id, MediaItem.origin_key)
            .where(MediaItem.source_key == source.key, MediaItem.status.in_(list(REVIEWABLE_STATUSES)))
        )).all()
    items = {
        keys[origin_key]: SimpleNamespace(id=item_id, status=status, reasons=reasons)
        for item_id, origin_key, status, reasons in rows if origin_key in keys
    }

    for asin, entry in waiting.items():
        try:
            held = items.get(asin)
            if held is None or asked_again(held):
                done.added += (await take(source, entry, config)) == "added"
            elif held.status == ItemStatus.REJECTED:
                if dryrun.active() and await delivery.declined_by_dry_run(held.id):
                    # A dry run's delete is never sent, so ABR keeps listing the
                    # book: it was recorded once, and is not again.
                    continue
                # Rejected here and still waiting there: the delete never arrived.
                taken = await delivery.decline_abreq(held.id, asin, config, entry=entry)
                done.declined += not taken.failed
            elif held.status in ingest.UNFINISHED:
                done.requeued += await ingest.add_again(held.id, held.status)
        except Exception:
            log.exception("Source %s: AudioBookRequest book %s could not be handled", source.key, asin)

    for item_id, origin_key in undecided:
        book = abreq_api.request_of(origin_key)
        if book is None:
            continue
        entry = None if abreq_api.retired(origin_key) else listed.get(book[1])
        if entry is not None and not abreq_api.fulfilled(entry):
            continue
        try:
            done.reflected += (await delivery.reflect_abreq(item_id, downloaded=entry is not None)) is not None
        except Exception:
            log.exception("Source %s: item %d could not be brought up to date", source.key, item_id)
    return done


async def poll_job(source_key: str) -> None:
    """The scheduler's poll of one source. Never raises: an ABR that is down is asked again next time."""
    source = next(
        (s for s in settings.config.sources if s.key == source_key and s.kind == "abreq"), None,
    )
    if source is None:
        return
    try:
        done = await poll(source)
    except DeliveryError as exc:
        log.warning("Source %s: AudioBookRequest's requests could not be read: %s", source_key, exc)
        return
    except Exception:
        log.exception("Source %s: the poll of AudioBookRequest failed", source_key)
        return
    if done.added or done.declined or done.reflected or done.requeued:
        log.info(
            "Source %s: %d waiting in AudioBookRequest, %d new, %d deleted late, %d decided there, %d queued again",
            source_key, done.waiting, done.added, done.declined, done.reflected, done.requeued,
        )


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------


def triage(source: Source, payload: Any) -> tuple[str, str | None]:
    """What the route answers at once, and the ASIN to take after the answer (None for nothing).

    'test' for ABR's test button, 'queued' for a new request, 'ignored' for
    anything else. The book itself is read after the answer: ABR waits on
    each notification before it sends the next.
    """
    seen = read_hook(payload)
    if seen.test:
        events.emit(events.WEBHOOK_TEST, source=source.key, app="AudioBookRequest")
        log.info("Source %s: AudioBookRequest's test notification arrived", source.key)
        return "test", None
    if seen.asin is None or seen.event != NEW_REQUEST:
        return "ignored", None
    return "queued", seen.asin


async def hook(source: Source, asin: str, config: Config | None = None) -> str:
    """Take the book one webhook call named. Answers what it did; never raises.

    'added' or 'known' for a book that waits, 'ignored' for one that does
    not (downloaded, or gone), and 'deferred' when ABR could not be asked
    just now: the poll picks the book up.
    """
    config = config or settings.config
    client = delivery.client_for(delivery.ABREQ, config)
    if client is None:
        return "ignored"
    try:
        entry = await client.entry(asin)
        if entry is None:
            return "ignored"
        taken = await take(source, entry, config)
        return "ignored" if taken == "skipped" else taken
    except DeliveryError as exc:
        log.warning(
            "Source %s: AudioBookRequest could not be asked about book %s (%s); the poll picks it up",
            source.key, asin, exc,
        )
        return "deferred"
    except Exception:
        log.exception("Source %s: a webhook call about book %s could not be handled", source.key, asin)
        return "deferred"
