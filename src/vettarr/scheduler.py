from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, update

from vettarr import delivery
from vettarr.config import settings
from vettarr.config.schema import Config, KeepAlive
from vettarr.database import async_session
from vettarr.models import (
    COMPLETABLE_STATUSES,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
)
from vettarr.searcher import search_service
from vettarr.searcher.qbittorrent_client import QBittorrentClient

log = logging.getLogger(__name__)

qbt = QBittorrentClient()

_GRACE_PERIOD = timedelta(hours=2)
# The first keep-alive visit after a start, so a session that died while the
# app was down is found within a minute, not after a whole interval.
_FIRST_KEEP_ALIVE = timedelta(minutes=1)
# The first read of a seerr source's pending requests after a start.
_FIRST_SEERR_POLL = timedelta(seconds=20)
# How many checks in a row Radarr, Sonarr or Seerr may leave unanswered before
# the item goes back to a person: an hour, at one check every five minutes.
# Long enough for a restart or an upgrade of the target, short enough that a
# rotated API key is not a silent stall.
UNANSWERED_LIMIT = 12
# Download id -> checks in a row its target left unanswered. In memory: a
# restart starts the hour again.
_unanswered: dict[int, int] = {}


def keep_alive_wait(
    keep: KeepAlive, uniform: Callable[[float, float], float] | None = None
) -> timedelta:
    """How long until the next visit: the interval, moved by the jitter.

    ``every_minutes`` plus or minus a uniform random value up to
    ``jitter_minutes``, and never less than a minute however wide the jitter.
    """
    minutes = float(keep.every_minutes)
    if keep.jitter_minutes:
        minutes += (uniform or random.uniform)(-keep.jitter_minutes, keep.jitter_minutes)
    return timedelta(minutes=max(1.0, minutes))


class JitteredInterval(IntervalTrigger):
    """An interval trigger whose every wait is drawn again by ``keep_alive_wait``."""

    def __init__(self, keep: KeepAlive) -> None:
        super().__init__(minutes=keep.every_minutes)
        self.keep = keep

    def get_next_fire_time(self, previous_fire_time, now):
        if previous_fire_time is None:
            return super().get_next_fire_time(previous_fire_time, now)
        return previous_fire_time + keep_alive_wait(self.keep)


def _live_downloads_query():
    """The downloads the poller is allowed to advance.

    Only a live download. A cancelled one belongs to an item somebody
    rejected, and completing it here would put that item back to COMPLETED.
    """
    return select(Download).where(Download.status == DownloadStatus.DOWNLOADING)


async def _complete_item(session, item_id: int) -> bool:
    """Mark the item done, unless somebody moved it out of DOWNLOADING. True when it was marked.

    A poll iteration takes as long as qBittorrent takes to answer, and a
    rejection can land inside it. Rejecting cancels the download row, but this
    poller is already holding the list it read before that happened.
    """
    moved = await session.execute(
        update(MediaItem)
        .where(
            MediaItem.id == item_id,
            MediaItem.status.in_(list(COMPLETABLE_STATUSES)),
        )
        .values(status=ItemStatus.COMPLETED)
        .returning(MediaItem.id)
    )
    return moved.scalar_one_or_none() is not None


async def _complete(dl: Download) -> bool:
    """Mark a live download and its item done. False when a rejection got there first.

    The row is completed only while it is still DOWNLOADING: one a rejection
    cancelled during this poll stays cancelled, and nothing is reported as
    completed for an item that was rejected.
    """
    async with async_session() as session:
        live = await session.execute(
            update(Download)
            .where(Download.id == dl.id, Download.status == DownloadStatus.DOWNLOADING)
            .values(status=DownloadStatus.COMPLETED, progress=1.0)
            .returning(Download.id)
        )
        done = live.scalar_one_or_none() is not None and await _complete_item(session, dl.media_item_id)
        await session.commit()
    return done


async def _left_unanswered(dl: Download, exc: Exception) -> None:
    """The target could not be asked. After ``UNANSWERED_LIMIT`` checks in a row, a person is told."""
    count = _unanswered.get(dl.id, 0) + 1
    if count < UNANSWERED_LIMIT:
        _unanswered[dl.id] = count
        log.warning(
            "Could not ask %s about item %d (%d in a row): %s", dl.delivery_target, dl.media_item_id, count, exc,
        )
        return
    _unanswered.pop(dl.id, None)
    await delivery.stalled(dl, delivery.RemoteState(
        problem=f"it could not be asked for {count} checks in a row, an hour ({exc})",
    ))


async def _check_remote(dl: Download, posts, silent: dict[str, Exception]) -> None:
    """A delivery to Radarr, Sonarr or Seerr: completed only when the target has the files.

    Never by the clock. A target that could not be asked says nothing about
    the item and is asked again at the next check; after an hour of that the
    item goes back to a person. One that lost, unmonitored or declined it
    hands the item back at once.

    ``silent`` holds the targets that did not answer, or refused the key,
    earlier in this same check. They are not asked again for the next item:
    each request to a target that is down waits out its whole timeout.
    """
    target = dl.delivery_target
    if target in silent:
        await _left_unanswered(dl, silent[target])
        return
    try:
        state = await delivery.poll(dl)
    except delivery.DeliveryError as exc:
        if exc.status in (None, 401, 403):
            silent[target] = exc
        await _left_unanswered(dl, exc)
        return
    await _settle(dl, state, posts)


async def _settle(dl: Download, state: delivery.RemoteState, posts) -> None:
    """Act on what the target answered about a live delivery."""
    _unanswered.pop(dl.id, None)
    if state.done:
        if await _complete(dl):
            await delivery.completed(dl)
            await delivery.arrived(dl, posts)
            log.info("%s has the files of item %d", dl.delivery_target, dl.media_item_id)
    elif state.problem:
        await delivery.stalled(dl, state)


async def check_delivery(dl: Download, posts=None) -> None:
    """One check of one live delivery, now: Seerr's webhook said something happened to its request.

    Raises ``DeliveryError`` when the target cannot be asked. That silence
    is not counted: only the five-minute check counts toward the hour an
    item is given, so a run of webhook calls during an outage cannot use
    the hour up in seconds.
    """
    await _settle(dl, await delivery.poll(dl), posts)


async def _check_downloads(posts=None) -> None:
    """One check of every live download. ``posts`` runs a post's on_download once its files arrive."""
    async with async_session() as session:
        result = await session.execute(_live_downloads_query())
        downloads = result.scalars().all()

    live = {dl.id for dl in downloads}
    for gone in [dl_id for dl_id in _unanswered if dl_id not in live]:
        del _unanswered[gone]
    silent: dict[str, Exception] = {}
    # The download client's torrents first: a target that is down must not
    # hold up reading their progress.
    for dl in sorted(downloads, key=lambda d: d.delivery_target in delivery.REMOTE):
        try:
            if dl.delivery_target in delivery.REMOTE:
                await _check_remote(dl, posts, silent)
            elif dl.qbittorrent_hash:
                progress = await qbt.get_torrent_progress(dl.qbittorrent_hash)
                if progress is None:
                    continue
                if progress >= 1.0:
                    if await _complete(dl):
                        log.info("Download complete for item %d", dl.media_item_id)
                        await delivery.completed(dl)
                    continue
                async with async_session() as session:
                    await session.execute(
                        update(Download).where(Download.id == dl.id)
                        .values(progress=progress)
                    )
                    await session.commit()
            else:
                if dl.started_at and datetime.now(timezone.utc) - dl.started_at > _GRACE_PERIOD:
                    if await _complete(dl):
                        await delivery.completed(dl)
                        log.info("Marked hashless download as complete for item %d (past grace period)", dl.media_item_id)
        except Exception:
            log.exception("Error checking download %d", dl.id)


async def _recheck_waiting() -> None:
    """Give items parked without parental-guide data another chance."""
    from vettarr.pipeline import recheck_waiting_items

    try:
        count = await recheck_waiting_items()
        if count:
            log.info("Re-checked %d items waiting for parental-guide data", count)
    except Exception:
        log.exception("Waiting-item re-check failed")


async def _flush_events() -> None:
    """Write queued events and hand each to the notifier."""
    from vettarr.events import flush

    try:
        await flush()
    except Exception:
        log.exception("Event flush failed")


class Scheduler:
    def __init__(self, posts=None) -> None:
        self._scheduler = AsyncIOScheduler()
        # What on_download does to a post, for the deliveries whose files
        # arrive long after the hand-over.
        self._posts = posts

    def start(self) -> None:
        self._scheduler.add_job(
            _check_downloads, "interval", minutes=5, id="check_downloads", args=[self._posts],
        )
        self._scheduler.add_job(
            _recheck_waiting, "interval", hours=1, id="recheck_waiting",
        )
        self._scheduler.add_job(_flush_events, "interval", seconds=5, id="flush_events")
        kept = self.add_keep_alive_jobs(settings.config)
        polled = self.add_seerr_poll_jobs(settings.config)
        self._scheduler.start()
        log.info(
            "Scheduler started (download check every 5 min, waiting re-check hourly, "
            "session keep-alive for %d provider(s), poll of %d Seerr source(s))", kept, polled,
        )

    def add_seerr_poll_jobs(self, config: Config) -> int:
        """One recurring read of Seerr's pending requests per `seerr` source, every `poll_minutes`.

        The first runs soon after the start, so what the webhook sent while
        the app was down is not left waiting a whole interval.
        """
        from vettarr import seerr_source

        count = 0
        for source in config.sources:
            if source.kind != "seerr":
                continue
            self._scheduler.add_job(
                seerr_source.poll_job, "interval", minutes=source.poll_minutes, args=[source.key],
                next_run_time=datetime.now(timezone.utc) + _FIRST_SEERR_POLL,
                id=f"seerr_poll_{source.key}",
            )
            log.info("Source %s: Seerr's pending requests are read every %d min", source.key, source.poll_minutes)
            count += 1
        return count

    def add_keep_alive_jobs(self, config: Config) -> int:
        """One recurring visit per provider whose `keep_alive` is enabled."""
        count = 0
        for provider in config.providers:
            keep = provider.keep_alive
            if not keep.enabled:
                continue
            # No jitter is the plain fixed interval it always was.
            trigger = JitteredInterval(keep) if keep.jitter_minutes else IntervalTrigger(minutes=keep.every_minutes)
            self._scheduler.add_job(
                search_service.keep_alive, trigger, args=[provider.key],
                next_run_time=datetime.now(timezone.utc) + _FIRST_KEEP_ALIVE,
                id=f"keep_alive_{provider.key}",
            )
            log.info(
                "Provider %s: keep-alive every %d min%s", provider.key, keep.every_minutes,
                f", plus or minus up to {keep.jitter_minutes} min" if keep.jitter_minutes else "",
            )
            count += 1
        return count

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
