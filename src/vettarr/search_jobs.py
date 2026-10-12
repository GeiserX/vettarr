from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select, text, update

from vettarr import delivery, dryrun
from vettarr.database import async_session, engine
from vettarr.ingest import add_direct
from vettarr.models import (
    OP_APPROVAL,
    OP_RECHECK,
    OP_REPARSE,
    ContentCheck,
    Download,
    ItemStatus,
    MediaItem,
)
from vettarr.searcher.search_service import (
    ItemRef,
    qbittorrent,
    search_and_download,
)

if TYPE_CHECKING:
    from vettarr.ingest import PostActions

log = logging.getLogger(__name__)

_CONCURRENCY = 3
_POLL_SECONDS = 2
_WORKER_LOCK_ID = 0x4D545249414745
_ERROR_MESSAGE_LIMIT = 200


async def _acquire_worker_lock(stop: asyncio.Event):
    while not stop.is_set():
        connection = await engine.connect()
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _WORKER_LOCK_ID},
            )
        ).scalar()
        if acquired:
            return connection
        await connection.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass
    return None


async def _recover_interrupted_jobs() -> int:
    async with async_session() as session:
        approval_result = await session.execute(
            update(MediaItem)
            .where(
                MediaItem.status == ItemStatus.SEARCHING,
                MediaItem.error_message == OP_APPROVAL,
            )
            .values(status=ItemStatus.APPROVED, error_message=None)
        )
        await session.execute(
            update(MediaItem)
            .where(MediaItem.status == ItemStatus.SEARCHING)
            .values(
                status=ItemStatus.ERROR,
                error_message="Interrupted operation; recheck item",
            )
        )
        # Items the hourly re-check had claimed when the process stopped. They
        # go back to waiting, which is exactly where they came from.
        await session.execute(
            update(MediaItem)
            .where(
                MediaItem.status == ItemStatus.CHECKING,
                MediaItem.error_message == OP_RECHECK,
            )
            .values(status=ItemStatus.AWAITING_DATA, error_message=None)
        )
        await session.execute(
            update(MediaItem)
            .where(
                MediaItem.status.in_({
                    ItemStatus.PENDING_PARSE,
                    ItemStatus.PARSED,
                    ItemStatus.CHECKING,
                }),
                MediaItem.error_message == OP_REPARSE,
            )
            .values(
                status=ItemStatus.ERROR,
                error_message="Interrupted reparse; recheck item",
            )
        )
        await session.commit()
        return approval_result.rowcount


async def _claim_approved_items(limit: int) -> list[ItemRef]:
    # A Seerr request whose approval Seerr did not answer waits its turn out
    # here, so an outage is not asked about every two seconds.
    approved = select(MediaItem).where(MediaItem.status == ItemStatus.APPROVED)
    waiting = delivery.held_back()
    if waiting:
        approved = approved.where(MediaItem.id.notin_(waiting))
    async with async_session() as session:
        result = await session.execute(
            approved
            .order_by(MediaItem.reviewed_at, MediaItem.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        items = result.scalars().all()
        if not items:
            return []

        item_ids = [item.id for item in items]
        checks_result = await session.execute(select(ContentCheck).where(ContentCheck.media_item_id.in_(item_ids)))
        checks = {check.media_item_id: check for check in checks_result.scalars()}

        for item in items:
            item.status = ItemStatus.SEARCHING
            item.error_message = OP_APPROVAL
        await session.commit()

        return [
            ItemRef(
                id=item.id,
                title=item.title,
                year=item.year,
                quality=item.quality,
                season=item.season,
                media_type=item.media_type,
                telegram_message_id=item.telegram_message_id,
                tmdb_id=checks[item.id].tmdb_id if item.id in checks else None,
                imdb_id=checks[item.id].imdb_id if item.id in checks else None,
                tmdb_title=checks[item.id].tmdb_title if item.id in checks else None,
                external_links=list(item.external_links or []),
                origin_key=item.origin_key,
            )
            for item in items
        ]


async def _mark_failed(item_id: int, exc: BaseException | None = None) -> None:
    reason = "Search/download failed; see service logs"
    if exc is not None:
        reason = f"{reason} ({type(exc).__name__})"
    async with async_session() as session:
        await session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == item_id,
                MediaItem.status == ItemStatus.SEARCHING,
                MediaItem.error_message == OP_APPROVAL,
            )
            .values(
                status=ItemStatus.ERROR,
                error_message=reason[:_ERROR_MESSAGE_LIMIT],
            )
        )
        await session.commit()


async def _finish_no_match_if_needed(item_id: int) -> None:
    async with async_session() as session:
        await session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == item_id,
                MediaItem.status == ItemStatus.SEARCHING,
                MediaItem.error_message == OP_APPROVAL,
            )
            .values(status=ItemStatus.NO_MATCH)
        )
        await session.commit()


async def _recorded_torrent_hash(item_id: int) -> str | None:
    async with async_session() as session:
        result = await session.execute(
            select(Download.qbittorrent_hash).where(Download.media_item_id == item_id)
        )
        return result.scalars().first()


async def _download_confirmed(item_id: int) -> bool:
    torrent_hash = await _recorded_torrent_hash(item_id)
    if not torrent_hash:
        log.warning("No qBittorrent hash recorded for item %d; keeping group post", item_id)
        return False
    if await qbittorrent.confirm_torrent_healthy(torrent_hash):
        return True
    log.warning(
        "Torrent %s for item %d is not downloading; keeping group post",
        torrent_hash,
        item_id,
    )
    return False


async def _to_download_client(ref: ItemRef) -> str:
    """The download client's path: the post's own torrent, else its linked one, else a search."""
    # A torrent the post carried itself (a .torrent, a magnet) needs no search.
    return await add_direct(ref) or await search_and_download(ref)


@dryrun.about_item("ref")
async def _process_item(
    ref: ItemRef,
    posts: PostActions | None,
) -> None:
    try:
        # `deliver` in config.yaml says where this type goes; the path above
        # runs only when that is the download client.
        result = await delivery.deliver(ref, download_client=_to_download_client)
    except Exception as exc:
        log.exception("Background search/download failed for item %d", ref.id)
        await _mark_failed(ref.id, exc)
        return

    if result == "no_match":
        await _finish_no_match_if_needed(ref.id)
        return
    if posts is None:
        return
    if result == "downloading":
        try:
            confirmed = await _download_confirmed(ref.id)
        except Exception:
            log.exception("qBittorrent confirmation failed for item %d", ref.id)
            return
        if not confirmed:
            return
    elif result != delivery.IN_LIBRARY:
        # Only a target that already has the files makes the post spare now.
        # A title Radarr, Sonarr or Seerr has just taken can still be declined
        # or never found, so its post waits until the files are there: the
        # download poll acts on it then (`delivery.arrived`).
        return

    try:
        await posts.after("download", ref.id)
    except Exception:
        log.exception("Telegram on_download failed for item %d", ref.id)


async def run_search_jobs(
    posts: PostActions | None,
    stop: asyncio.Event,
) -> None:
    connection = await _acquire_worker_lock(stop)
    if connection is None:
        return
    log.info("Acquired singleton search-worker lock")
    try:
        recovered = await _recover_interrupted_jobs()
        if recovered:
            log.warning("Recovered %d interrupted search jobs", recovered)

        while not stop.is_set():
            refs = await _claim_approved_items(_CONCURRENCY)
            if refs:
                await asyncio.gather(*(_process_item(ref, posts) for ref in refs))
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        await connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": _WORKER_LOCK_ID},
        )
        await connection.close()
