"""The one place that decides what rejecting an item actually does.

The web UI, the bulk action and the Telegram bot all reject items, and the
rules are the same in all three: which statuses may be rejected, whose
Telegram post may be deleted, and what happens to a torrent that is already
running. Keeping them here means there is a single rule to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from vettarr import delivery, dryrun, ingest
from vettarr.database import async_session
from vettarr.delivery import seerr as seerr_api
from vettarr.models import (
    DELETABLE_ON_REJECT,
    REJECTABLE_STATUSES,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
)

if TYPE_CHECKING:
    from vettarr.ingest import PostActions

log = logging.getLogger(__name__)


@dataclass
class RejectOutcome:
    """What the rejection changed, and what still has to be cleaned up."""

    item_id: int
    rejected: bool = False
    #: Set only when this item's Telegram post may be acted on (on_reject).
    telegram_message_id: int | None = None
    #: Set only when this item's own add put the torrent in qBittorrent, so
    #: dropping it takes away nothing that was there beforehand.
    torrent_hash: str | None = None
    #: Where the item was delivered, when that was Radarr, Sonarr or Seerr,
    #: and what it is called there.
    delivery_target: str | None = None
    remote_id: str | None = None
    #: True only when this item's own delivery made the entry or the request
    #: there, so taking it back undoes nothing that was there beforehand.
    delivery_created: bool = False
    #: Sonarr's finer record of the same: which monitoring flags the delivery
    #: turned on (`Download.remote_changes`).
    delivery_changes: dict | None = None
    #: What was done at the delivery target, in one sentence. Set by
    #: `finish_rejection`.
    remote_note: str | None = None
    #: True when the target could not be told and still has the title.
    remote_failed: bool = False
    #: Seerr's id of the request this item is, when a seerr source made it.
    #: Rejecting the item declines that request.
    seerr_request: int | None = None


async def reject_item(item_id: int) -> RejectOutcome:
    """Mark an item rejected, if its current status allows it.

    An auto-approved item can be taken back right up to the moment the
    download finishes. A PARSE_FAILED item can be rejected, but its Telegram
    post is never deleted: the service could not read that post, so it does not
    get to destroy it.
    """
    async with async_session() as session:
        item = await session.get(MediaItem, item_id, with_for_update=True)
        if item is None or item.status not in REJECTABLE_STATUSES:
            return RejectOutcome(item_id=item_id, rejected=False)

        deletable = item.status in DELETABLE_ON_REJECT
        torrent_hash = None
        delivered: dict = {}

        download = (
            await session.execute(
                select(Download).where(Download.media_item_id == item_id)
            )
        ).scalars().first()
        if download is not None:
            # qBittorrent answers an add with the hash of a torrent it was
            # already holding, so a recorded hash is not proof we put it there.
            # Only a torrent this item created is ours to delete with its files.
            if download.created_torrent:
                torrent_hash = download.qbittorrent_hash
            if download.delivery_target in delivery.REMOTE:
                delivered = {
                    "delivery_target": download.delivery_target,
                    "remote_id": download.remote_id,
                    "delivery_created": bool(download.created_torrent),
                    "delivery_changes": download.remote_changes,
                }
            # Whatever happens to the torrent, the row stops being a live
            # download. Left as DOWNLOADING, the poller would see it finish and
            # flip the rejected item back to completed.
            if download.status in (DownloadStatus.PENDING, DownloadStatus.DOWNLOADING):
                download.status = DownloadStatus.CANCELLED

        message_id = item.telegram_message_id
        asked = seerr_api.request_of(item.origin_key)
        item.status = ItemStatus.REJECTED
        item.reviewed_at = datetime.now(timezone.utc)
        await session.commit()

    return RejectOutcome(
        item_id=item_id,
        rejected=True,
        telegram_message_id=message_id if deletable else None,
        torrent_hash=torrent_hash,
        seerr_request=asked[1] if asked else None,
        **delivered,
    )


@dryrun.about_item("outcome")
async def finish_rejection(
    outcome: RejectOutcome,
    posts: PostActions | None,
    qbittorrent=None,
) -> str:
    """Act on the Telegram post (its source's on_reject) and drop the torrent, where allowed.

    An item delivered to Radarr, Sonarr or Seerr is taken back there instead
    (``delivery.take_back``), and an item that is a Seerr request has that
    request declined (``delivery.decline_request``). ``outcome.remote_note``
    says what was done, and ``outcome.remote_failed`` that the target could
    not be told.

    Returns what happened to the post, as ``PostActions.after`` says it:
    deleted, reacted, none, refused, unavailable or failed.
    """
    if not outcome.rejected:
        return "none"
    # A .torrent the post carried is not needed once the item is rejected.
    ingest.forget_torrent(outcome.item_id)

    if outcome.torrent_hash and qbittorrent is not None:
        try:
            await qbittorrent.remove_torrent(outcome.torrent_hash)
        except Exception:
            log.exception("Could not remove torrent for item %d", outcome.item_id)

    if outcome.seerr_request is not None:
        # The item is a Seerr request: Seerr is told no. A request this app
        # had already approved is past declining, and the answer says so.
        try:
            taken = await delivery.decline_request(outcome.item_id, outcome.seerr_request)
        except Exception:
            log.exception("Could not decline the Seerr request of item %d", outcome.item_id)
        else:
            outcome.remote_note, outcome.remote_failed = taken.note, taken.failed
    elif outcome.delivery_target:
        try:
            taken = await delivery.take_back(
                outcome.item_id, outcome.delivery_target, outcome.remote_id,
                outcome.delivery_created, outcome.delivery_changes,
            )
        except Exception:
            log.exception("Could not take item %d back from %s", outcome.item_id, outcome.delivery_target)
        else:
            if taken is not None:
                outcome.remote_note, outcome.remote_failed = taken.note, taken.failed

    if outcome.telegram_message_id is None or posts is None:
        return "none"
    try:
        return await posts.after("reject", outcome.item_id)
    except Exception:
        log.exception("Telegram on_reject failed for item %d", outcome.item_id)
        return "failed"
