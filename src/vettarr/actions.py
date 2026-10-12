"""What a person can do to an item: approve, reject, search, grab, skip and rerun.

The web UI and the Telegram bot each carried their own copy of every action,
and the copies drifted: the bot searched without the season, `/rescan` sent
the quality where the season belongs, only the bot wrote a download's
category, and a missing title read as "already being processed". Each action
now lives here once. It does the database work and the calls out, and returns
a small result the caller renders as an htmx fragment, a 409 or a Telegram
message. Nothing here knows about either surface.

The search worker shares the pieces underneath (the one search, `search_for`,
the stored release list, adding a release and the download row) through
`searcher.search_service`, so an approval and a person's own grab write the
same rows the same way.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from sqlalchemy import and_, or_, select, update

from vettarr import delivery, dryrun, ingest
from vettarr.database import async_session
from vettarr.delivery import seerr as seerr_api
from vettarr.models import (
    OP_MANUAL_DOWNLOAD,
    OP_MANUAL_SEARCH,
    REVIEWABLE_STATUSES,
    ContentCheck,
    ItemStatus,
    MediaItem,
    TorrentSearch,
)
from vettarr.review import finish_rejection, reject_item
from vettarr.searcher import search_service

if TYPE_CHECKING:
    from vettarr.ingester.telegram_reader import TelegramIngester

log = logging.getLogger(__name__)

BUSY_MESSAGE = "This item is already being processed."
NOT_FOUND_MESSAGE = "This item no longer exists."
# A post nothing could read carries no title: approving it would queue a
# download of nothing.
NO_TITLE_MESSAGE = (
    "This post has no title, so there is nothing to approve. "
    "Teach a format in Setup, or dismiss the post."
)

# Statuses no action takes an item out of. Rejecting one is refused with what
# it is, not with "already being processed": nothing is processing it.
_FINISHED_STATUSES = frozenset({
    ItemStatus.REJECTED,
    ItemStatus.COMPLETED,
    ItemStatus.IGNORED,
})


class Status(str, enum.Enum):
    OK = "ok"
    #: Another action or the worker holds the item, or it is already finished
    #: (rejected, completed). The web answers 409.
    BUSY = "busy"
    NOT_FOUND = "not_found"
    #: The action ran, and something outside said no (Prowlarr, qBittorrent).
    FAILED = "failed"


@dataclass
class ActionResult:
    item_id: int
    status: Status
    #: One line a person reads: a toast on the web, a message in the bot.
    message: str
    title: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    @property
    def conflict(self) -> bool:
        return self.status is Status.BUSY


@dataclass
class RejectResult(ActionResult):
    #: True when the item's post was deleted by this rejection.
    deletes_post: bool = False
    #: What the rejection did at Radarr, Sonarr or Seerr, when the item had
    #: been delivered there. Also the end of `message`.
    remote_note: str | None = None
    #: True when that target could not be told and still has the title.
    remote_failed: bool = False
    #: True when the source's on_reject could not be done to the post: it is still there.
    post_unchanged: bool = False


# What a rejection says, by what its source's on_reject did to the post.
_REJECTED = {
    "deleted": "Rejected, and its post was deleted.",
    "reacted": "Rejected, and its post got a reaction.",
    "none": "Rejected. Its post was left in place.",
}
_REJECTED_POST_UNCHANGED = "Rejected. Its post could not be changed and is still there."
# The same under dry_run: Telegram was not told, so the post is as it was.
_REJECTED_DRY_RUN = {
    "deleted": "Rejected. Dry run: its post would have been deleted, and is still there.",
    "reacted": "Rejected. Dry run: its post would have got a reaction, and has none.",
}


def queued_word() -> str:
    """How an approval's toast starts: under ``dry_run`` nothing reaches the target."""
    return "Would have queued" if dryrun.active() else "Queued"


@dataclass
class SearchOutcome(ActionResult):
    #: None when nothing was stored: the search never ran, or no search completed.
    search_id: int | None = None
    #: The stored releases, best first, as the release list shows them.
    releases: list[dict] = field(default_factory=list)
    #: True once the item was claimed and searched for: its status says how it went.
    ran: bool = False


NO_SEARCH_MESSAGE = "No search is set up. Add Prowlarr or turn on a provider's own search in Setup."


@dataclass
class SkipResult(ActionResult):
    #: The item to show next, or None when nothing else is waiting.
    next_item: MediaItem | None = None


def _busy(item_id: int, cls=ActionResult, **extra) -> ActionResult:
    return cls(item_id=item_id, status=Status.BUSY, message=BUSY_MESSAGE, **extra)


def _not_found(item_id: int, cls=ActionResult, message: str = NOT_FOUND_MESSAGE):
    return cls(item_id=item_id, status=Status.NOT_FOUND, message=message)


def _approve_statement(item_ids: Iterable[int]):
    # A rejection the policy made is a person's to overrule: nobody has
    # looked at the item, and its post is still there. One a person made
    # (`reviewed_at` is set) stays final.
    rejected_by_policy = and_(
        MediaItem.status == ItemStatus.REJECTED,
        MediaItem.reviewed_at.is_(None),
        MediaItem.policy_outcome == "reject",
    )
    return (
        update(MediaItem)
        .where(
            MediaItem.id.in_(list(item_ids)),
            or_(MediaItem.status.in_(REVIEWABLE_STATUSES), rejected_by_policy),
        )
        .values(
            status=ItemStatus.APPROVED,
            reviewed_at=datetime.now(timezone.utc),
            error_message=None,
        )
        .returning(MediaItem.id, MediaItem.title, MediaItem.media_type, MediaItem.origin_key)
    )


def _approved_message(title: str | None, media_type, origin_key: str | None = None) -> str:
    """What approving answers: where `deliver` sends the item's type."""
    what = title or "the item"
    if seerr_api.request_of(origin_key) is not None:
        return f"Approved {what}. Its Seerr request is approved next, and Seerr fetches it."
    target = delivery.target_for(media_type)
    # Approved here all the same; only what reaches the target is a would-have.
    queued = queued_word()
    if target == delivery.QBITTORRENT:
        return f"{queued} {what} for download."
    if target in delivery.REMOTE:
        return f"{queued} {what} for {delivery.LABELS[target]}."
    unlisted = delivery.no_target_reason(media_type)
    if unlisted:
        return f"Approved {what}. It will come back: {unlisted}."
    return f"Approved {what}. Nothing is fetched: deliver says none for {getattr(media_type, 'value', media_type)}."


def approvable(item: MediaItem) -> bool:
    """An item has something to fetch: a title to search for, or a torrent of its own."""
    return bool(item.title) or ingest.direct_add_of(item) is not None


async def _without_title(item_ids: list[int]) -> set[int]:
    async with async_session() as session:
        items = (await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids)))).scalars().all()
    return {item.id for item in items if not approvable(item)}


async def approve(item_id: int) -> ActionResult:
    """Queue the item for the worker, which hands it to its `deliver` target."""
    if await _without_title([item_id]):
        return ActionResult(item_id=item_id, status=Status.BUSY, message=NO_TITLE_MESSAGE)
    async with async_session() as session:
        row = (await session.execute(_approve_statement([item_id]))).one_or_none()
        if row is None:
            exists = await session.get(MediaItem, item_id)
            return _busy(item_id) if exists is not None else _not_found(item_id)
        await session.commit()
    title = row.title
    return ActionResult(
        item_id=item_id,
        status=Status.OK,
        message=_approved_message(title, row.media_type, row.origin_key),
        title=title,
    )


async def approve_many(item_ids: Iterable[int]) -> list[int]:
    """Approve every item that may be approved; return the ids that were."""
    ids = [int(i) for i in item_ids]
    if not ids:
        return []
    untitled = await _without_title(ids)
    ids = [i for i in ids if i not in untitled]
    if not ids:
        return []
    async with async_session() as session:
        rows = (await session.execute(_approve_statement(ids))).all()
        await session.commit()
    return [row.id for row in rows]


async def reject(item_id: int, ingester: TelegramIngester | None) -> RejectResult:
    """Reject the item, drop the torrent it added and act on its post as its source says."""
    outcome = await reject_item(item_id)
    if not outcome.rejected:
        async with async_session() as session:
            item = await session.get(MediaItem, item_id)
        if item is None:
            return _not_found(item_id, RejectResult)
        if item.status in _FINISHED_STATUSES:
            # Nothing holds it; it is past the point a rejection could change.
            return RejectResult(
                item_id=item_id, status=Status.BUSY,
                message=f"This item is already {item.status.value}.",
            )
        return _busy(item_id, RejectResult)
    done = await finish_rejection(outcome, ingester, search_service.qbittorrent)
    message = _REJECTED.get(done, _REJECTED_POST_UNCHANGED)
    if dryrun.active():
        message = _REJECTED_DRY_RUN.get(done, message)
    if outcome.seerr_request is not None:
        # A Seerr request has no post to speak of.
        message = "Rejected."
    if outcome.remote_note:
        message = f"{message} {outcome.remote_note}"
    return RejectResult(
        item_id=item_id,
        status=Status.OK,
        message=message,
        deletes_post=done == "deleted",
        remote_note=outcome.remote_note,
        remote_failed=outcome.remote_failed,
        post_unchanged=outcome.seerr_request is None and done not in _REJECTED,
    )


async def search(
    item_id: int,
    *,
    from_statuses: Iterable[ItemStatus] = REVIEWABLE_STATUSES,
    op: str = OP_MANUAL_SEARCH,
) -> SearchOutcome:
    """Search Prowlarr for the item and store what came back for a person to pick.

    The query is the worker's: the season, never the quality, and no year for
    books. `/rescan` passes NO_MATCH as the only status it may claim from.
    """
    statuses = list(from_statuses)
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if item is None:
            return _not_found(item_id, SearchOutcome)
        if not item.title:
            return SearchOutcome(
                item_id=item_id, status=Status.FAILED,
                message="This item has no title to search for.",
            )
        if not search_service.has_search():
            # It could only fail: nothing is claimed, stored or changed.
            return SearchOutcome(item_id=item_id, status=Status.FAILED, message=NO_SEARCH_MESSAGE)
        claim = await session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == item_id,
                MediaItem.status.in_(statuses),
            )
            .values(status=ItemStatus.SEARCHING, error_message=op)
            .returning(
                MediaItem.title,
                MediaItem.year,
                MediaItem.season,
                MediaItem.media_type,
            )
        )
        claimed = claim.one_or_none()
        if claimed is None:
            return _busy(item_id, SearchOutcome)
        # What the worker's ItemRef carries, so a person's search asks the
        # trackers the same question: a direct search filters by these ids.
        check = (
            await session.execute(
                select(ContentCheck.tmdb_title, ContentCheck.tmdb_id, ContentCheck.imdb_id)
                .where(ContentCheck.media_item_id == item_id)
            )
        ).one_or_none()
        tmdb_title, tmdb_id, imdb_id = check or (None, None, None)
        await session.commit()

    result = await search_service.search_for(search_service.SearchItem(
        claimed.title, claimed.year, claimed.season, claimed.media_type,
        tmdb_title=tmdb_title, tmdb_id=tmdb_id, imdb_id=imdb_id,
    ))
    failure = search_service.search_failure(result)

    async with async_session() as session:
        # A search that did not complete stores nothing: the last one that did
        # stays the item's latest, with its releases still on offer.
        ts = None if failure else search_service.store_search(session, item_id, result)
        await session.flush()
        final_status = (
            ItemStatus.PENDING_REVIEW
            if result.results
            else ItemStatus.ERROR if result.error else ItemStatus.NO_MATCH
        )
        await session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == item_id,
                MediaItem.status == ItemStatus.SEARCHING,
                MediaItem.error_message == op,
            )
            .values(
                status=final_status,
                error_message=failure,
            )
        )
        await session.commit()
        search_id = ts.id if ts is not None else None
        releases = list(ts.prowlarr_results or []) if ts is not None else []

    if failure:
        return SearchOutcome(
            item_id=item_id, status=Status.FAILED, title=claimed.title,
            message=f"{failure}.", search_id=search_id, ran=True,
        )
    count = len(result.results)
    return SearchOutcome(
        item_id=item_id,
        status=Status.OK,
        title=claimed.title,
        message=(
            f"Found {count} release{'s' if count != 1 else ''}."
            if count else "No releases found."
        ),
        search_id=search_id,
        releases=releases,
        ran=True,
    )


async def latest_search(item_id: int) -> TorrentSearch | None:
    """The item's most recent stored search, if it has one."""
    async with async_session() as session:
        return (
            await session.execute(
                select(TorrentSearch)
                .where(TorrentSearch.media_item_id == item_id)
                .order_by(TorrentSearch.searched_at.desc(), TorrentSearch.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


@dryrun.about_item("item_id")
async def grab(item_id: int, search_id: int, index: int) -> ActionResult:
    """Add the release a person picked from a stored search to qBittorrent.

    Only for a type `deliver` sends to the download client. Radarr, Sonarr
    and Seerr pick their own release, and a type delivered nowhere gets none.
    """
    async with async_session() as session:
        ts = await session.get(TorrentSearch, search_id)
        item = await session.get(MediaItem, item_id)
    releases = (ts.prowlarr_results or []) if ts is not None else []
    if ts is None or ts.media_item_id != item_id or not 0 <= index < len(releases):
        return _not_found(
            item_id, message="That release is no longer listed. Search again.",
        )
    refusal = (
        delivery.grab_refusal(item.media_type, origin_key=item.origin_key) if item is not None else None
    )
    if refusal:
        return ActionResult(item_id=item_id, status=Status.FAILED, title=item.title, message=refusal)
    selected = releases[index]
    magnet = selected.get("magnet_url")
    download_url = selected.get("download_url")

    async with async_session() as session:
        claim = await session.execute(
            update(MediaItem)
            .where(
                MediaItem.id == item_id,
                MediaItem.status.in_(REVIEWABLE_STATUSES),
            )
            .values(status=ItemStatus.SEARCHING, error_message=OP_MANUAL_DOWNLOAD)
            .returning(MediaItem.media_type, MediaItem.title)
        )
        claimed = claim.one_or_none()
        if claimed is None:
            return _busy(item_id)
        await session.commit()

    add_result = await search_service.add_release(selected, claimed.media_type)

    async with async_session() as session:
        await search_service.record_download(
            session, item_id, add_result, op=OP_MANUAL_DOWNLOAD,
            media_type=claimed.media_type,
            download_url=download_url, magnet_url=magnet, torrent_search_id=search_id,
        )

    title = selected.get("title") or claimed.title
    if add_result:
        return ActionResult(
            item_id=item_id, status=Status.OK, title=title,
            message=f"{'Would have downloaded' if dryrun.active() else 'Downloading'} {title or 'the release'}.",
        )
    return ActionResult(
        item_id=item_id, status=Status.FAILED, title=title,
        message=search_service.refusal_of(add_result),
    )


async def next_for_review(after: MediaItem | None = None) -> MediaItem | None:
    """The next item waiting for a decision, oldest first.

    With `after`, the first one queued after it, wrapping round to the oldest;
    `after` itself is never the answer.
    """
    query = (
        select(MediaItem)
        .where(MediaItem.status == ItemStatus.PENDING_REVIEW)
        .order_by(MediaItem.created_at.asc(), MediaItem.id.asc())
        .limit(1)
    )
    async with async_session() as session:
        if after is None:
            return (await session.execute(query)).scalar_one_or_none()
        query = query.where(MediaItem.id != after.id)
        if after.created_at is not None:
            later = (
                await session.execute(
                    query.where(
                        or_(
                            MediaItem.created_at > after.created_at,
                            and_(
                                MediaItem.created_at == after.created_at,
                                MediaItem.id > after.id,
                            ),
                        )
                    )
                )
            ).scalar_one_or_none()
            if later is not None:
                return later
        return (await session.execute(query)).scalar_one_or_none()


async def skip(item_id: int) -> SkipResult:
    """Leave the item as it is and move on to the one queued after it.

    Skipping changes nothing about the item. It used to answer with the oldest
    pending item, which is the one just skipped, so Skip showed it again.
    """
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
    nxt = await next_for_review(item if item is not None else MediaItem(id=item_id))
    return SkipResult(
        item_id=item_id,
        status=Status.OK,
        message="Skipped." if nxt is not None else "Skipped. Nothing else is waiting for you.",
        title=item.title if item is not None else None,
        next_item=nxt,
    )


# What a re-run says, by what the policy did with the item this time.
_RERUN = {
    "approve": "The policy approved it. It is queued for download.",
    "review": "The policy still leaves it to you.",
    "wait": "The policy is still waiting for data.",
    "reject": "The policy rejected it.",
}
RERUN_LOOKUP_MISSED = (
    "TMDB did not find the title this time, so nothing was changed: "
    "the item keeps its data and its verdict. Try again later."
)
RERUN_NOT_RUN = "The policy could not be run again just now. Nothing was changed."
RERUN_STILL_WAITING = (
    "The policy could not be run again just now. "
    "The item waits for data and is tried again within the hour."
)
RERUN_NO_MATCH = " TMDB has no match for the title, so it was decided without that data."
RERUN_SEERR_DECLINED = (
    "The policy's rejection declined its Seerr request, and Seerr approves no declined request. "
    "Nothing was changed: the title has to be asked for again in Seerr."
)


async def _put_back(item_id: int, status: ItemStatus, error_message: str | None) -> None:
    """Return an item `rerun` moved to waiting to where it was, unless something took it since."""
    async with async_session() as session:
        await session.execute(
            update(MediaItem)
            .where(MediaItem.id == item_id, MediaItem.status == ItemStatus.AWAITING_DATA)
            .values(status=status, error_message=error_message)
        )
        await session.commit()


async def rerun(item_id: int) -> ActionResult:
    """Decide the item again, under the policy as it is now.

    Only an item no person has decided: one in the review queue, one waiting
    for data, or one the policy rejected. It goes back to waiting and through
    the hourly re-check's own claim and conditional write, so its lookups are
    asked again and a decision a person takes meanwhile is the one that
    stands. The reviewers are not told a second time about an item that is
    held again.

    Not a Seerr request the policy rejected: that rejection declined the
    request (or the next poll does), and a declined request cannot be
    approved. Deciding the item again would only set it against Seerr.

    A re-run that decides nothing changes nothing. TMDB answers an outage, a
    refused key and a rate limit the way it answers "no such title", so an
    item that had a match and gets none now keeps its stored data, its
    reasons and its place: deciding it on the empty answer would erase what
    the reviewer reads, and could approve a title held for what that data said.
    """
    from vettarr.pipeline import recheck_waiting_items

    rejected_by_policy = and_(
        MediaItem.status == ItemStatus.REJECTED,
        MediaItem.reviewed_at.is_(None),
        MediaItem.policy_outcome == "reject",
    )
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if item is None:
            return _not_found(item_id)
        if (
            seerr_api.request_of(item.origin_key) is not None and item.status is ItemStatus.REJECTED
            and item.reviewed_at is None and item.policy_outcome == "reject"
        ):
            return ActionResult(item_id=item_id, status=Status.FAILED, title=item.title, message=RERUN_SEERR_DECLINED)
        before, error_before = item.status, item.error_message
        row = (
            await session.execute(
                update(MediaItem)
                .where(
                    MediaItem.id == item_id,
                    MediaItem.status == before,
                    or_(
                        MediaItem.status.in_([ItemStatus.PENDING_REVIEW, ItemStatus.AWAITING_DATA]),
                        rejected_by_policy,
                    ),
                )
                .values(status=ItemStatus.AWAITING_DATA, error_message=None)
                .returning(MediaItem.title)
            )
        ).one_or_none()
        if row is None:
            return _busy(item_id)
        await session.commit()

    decisions: list[str] = []
    missed: list[int] = []
    await recheck_waiting_items(
        1, only_ids=[item_id], decisions=decisions, push_held=False, keep_match=True, missed=missed,
    )
    if not decisions:
        await _put_back(item_id, before, error_before)
        if missed:
            message = RERUN_LOOKUP_MISSED
        elif before is ItemStatus.AWAITING_DATA:
            message = RERUN_STILL_WAITING
        else:
            message = RERUN_NOT_RUN
        return ActionResult(item_id=item_id, status=Status.FAILED, title=row.title, message=message)

    message = _RERUN.get(decisions[0], "The policy was run again.")
    async with async_session() as session:
        looked_up = (
            await session.execute(
                select(ContentCheck.tmdb_id).where(ContentCheck.media_item_id == item_id)
            )
        ).one_or_none()
    # A check row with no match: the title was looked up and TMDB had nothing.
    if looked_up is not None and looked_up[0] is None:
        message += RERUN_NO_MATCH
    return ActionResult(item_id=item_id, status=Status.OK, title=row.title, message=message)
