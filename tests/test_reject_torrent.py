"""Rejecting an item must not delete somebody else's copy.

qBittorrent answers an add with the hash of a torrent it was already holding.
The service therefore had a hash for torrents it never added, and rejecting
such an item removed the torrent *and its files* -- a library copy that
existed before the request was ever made.
"""

import asyncio
import logging
import os
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock

logging.disable(logging.CRITICAL)

for name, value in {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "test",
    "TELEGRAM_PHONE": "+10000000000",
    "TELEGRAM_GROUP_ID": "-1",
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_OWNER_ID": "1",
    "TMDB_API_KEY": "test",
    "PROWLARR_URL": "http://prowlarr",
    "PROWLARR_API_KEY": "test",
    "QBITTORRENT_URL": "http://qbittorrent",
}.items():
    os.environ.setdefault(name, value)

from vettarr import review, scheduler
from vettarr.models import (
    COMPLETABLE_STATUSES,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
    record_torrent_origin,
)

HASH = "abc123def456"


class CountingQBittorrent:
    """A qBittorrent that only counts, so a stray delete cannot hide."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def remove_torrent(self, torrent_hash: str) -> bool:
        self.deleted.append(torrent_hash)
        return True


class _Scalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value

    def all(self):
        return [] if self._value is None else [self._value]


class _Result:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return _Scalars(self._value)

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Serves one item and at most one download row."""

    def __init__(self, item: MediaItem, download: Download | None) -> None:
        self.item = item
        self.download = download
        self.commits = 0

    async def get(self, _model, pk, **_kwargs):
        return self.item if self.item.id == pk else None

    async def execute(self, _statement):
        return _Result(self.download)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_item(status: ItemStatus = ItemStatus.DOWNLOADING) -> MediaItem:
    item = MediaItem(
        id=7,
        telegram_message_id=4242,
        telegram_chat_id=-1,
        raw_text="Some Film (2024) 1080p WEB-DL-GROUP",
        status=status,
    )
    return item


def make_download(**kwargs) -> Download:
    row = Download(
        id=1,
        media_item_id=7,
        qbittorrent_hash=HASH,
        created_torrent=True,
        status=DownloadStatus.DOWNLOADING,
        progress=0.3,
    )
    row.started_at = datetime.now(timezone.utc)
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


def reject(item: MediaItem, download: Download | None):
    session = FakeSession(item, download)
    with mock.patch.object(review, "async_session", lambda: session):
        return asyncio.run(review.reject_item(item.id))


class TorrentOwnershipTests(unittest.TestCase):
    def test_a_torrent_this_item_added_is_dropped(self) -> None:
        item = make_item()
        outcome = reject(item, make_download(created_torrent=True))
        self.assertEqual(outcome.torrent_hash, HASH)

        qbt = CountingQBittorrent()
        asyncio.run(review.finish_rejection(outcome, None, qbt))
        self.assertEqual(qbt.deleted, [HASH])

    def test_a_torrent_that_was_already_there_is_left_alone(self) -> None:
        """The whole point: this hash came back from qBittorrent's own
        duplicate handling, so the files belong to a copy we did not add."""
        item = make_item()
        outcome = reject(item, make_download(created_torrent=False))
        self.assertIsNone(outcome.torrent_hash)

        qbt = CountingQBittorrent()
        asyncio.run(review.finish_rejection(outcome, None, qbt))
        self.assertEqual(qbt.deleted, [], "nothing of ours, nothing to delete")

    def test_an_item_with_no_download_row_deletes_nothing(self) -> None:
        item = make_item(ItemStatus.APPROVED)
        outcome = reject(item, None)
        self.assertIsNone(outcome.torrent_hash)

        qbt = CountingQBittorrent()
        asyncio.run(review.finish_rejection(outcome, None, qbt))
        self.assertEqual(qbt.deleted, [])

    def test_the_item_is_still_rejected_either_way(self) -> None:
        for created in (True, False):
            with self.subTest(created_torrent=created):
                item = make_item()
                outcome = reject(item, make_download(created_torrent=created))
                self.assertTrue(outcome.rejected)
                self.assertEqual(item.status, ItemStatus.REJECTED)


class DownloadRowTests(unittest.TestCase):
    """A rejected item must not be able to come back as completed."""

    def test_a_live_download_is_cancelled(self) -> None:
        row = make_download(status=DownloadStatus.DOWNLOADING)
        reject(make_item(), row)
        self.assertEqual(row.status, DownloadStatus.CANCELLED)

    def test_a_pending_download_is_cancelled(self) -> None:
        row = make_download(status=DownloadStatus.PENDING)
        reject(make_item(), row)
        self.assertEqual(row.status, DownloadStatus.CANCELLED)

    def test_a_failed_download_keeps_its_own_story(self) -> None:
        row = make_download(status=DownloadStatus.ERROR)
        reject(make_item(), row)
        self.assertEqual(row.status, DownloadStatus.ERROR)

    def test_the_poller_never_looks_at_a_cancelled_download(self) -> None:
        """Compiled from the real query the scheduler runs."""
        sql = str(
            scheduler._live_downloads_query().compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        self.assertIn("DOWNLOADING", sql)
        self.assertNotIn("CANCELLED", sql)


class _AddResult:
    def __init__(self, torrent_hash, created):
        self.hash = torrent_hash
        self.created = created


class TorrentOriginHelperTests(unittest.TestCase):
    """The two lines that decide whether a torrent is ours to delete.

    They used to be copied at four call sites. Replacing the ownership line
    with a bare True survived the suite at three of them, so the pair now lives
    in one function and is pinned here.
    """

    def test_an_add_that_created_the_torrent_is_ours(self) -> None:
        row = Download(media_item_id=7)
        record_torrent_origin(row, _AddResult(HASH, True))
        self.assertTrue(row.created_torrent)
        self.assertEqual(row.qbittorrent_hash, HASH)

    def test_an_add_that_found_it_already_there_is_not_ours(self) -> None:
        """This is the case the bare True destroys: qBittorrent answers with
        the hash of a torrent it was already holding."""
        row = Download(media_item_id=7)
        record_torrent_origin(row, _AddResult(HASH, False))
        self.assertFalse(row.created_torrent)
        self.assertEqual(row.qbittorrent_hash, HASH)

    def test_a_failed_add_owns_nothing_and_records_no_hash(self) -> None:
        row = Download(media_item_id=7)
        record_torrent_origin(row, None)
        self.assertFalse(row.created_torrent)
        self.assertIsNone(row.qbittorrent_hash)

    def test_the_helper_and_the_rejection_agree(self) -> None:
        """End to end: what the helper writes is what decides the delete."""
        for created, expected in ((True, [HASH]), (False, [])):
            with self.subTest(created=created):
                row = make_download()
                record_torrent_origin(row, _AddResult(HASH, created))
                outcome = reject(make_item(), row)
                qbt = CountingQBittorrent()
                asyncio.run(
                    review.finish_rejection(outcome, mock.AsyncMock(), qbt)
                )
                self.assertEqual(qbt.deleted, expected)


class TorrentOriginCallSiteTests(unittest.TestCase):
    """Every path that adds a torrent has to go through the one helper.

    A behavioural test cannot catch a call site being written out longhand
    again, and that is exactly the regression: the line was inlined four times
    and three copies were unprotected. So this reads the source.
    """

    SITES = ("searcher/search_service.py",)

    #: The surfaces a person acts from. They add torrents only through
    #: `actions.grab`, so none of them may write a download row itself.
    SURFACES = ("web/app.py", "bot/handlers.py", "actions.py")

    @staticmethod
    def _source(relative: str) -> str:
        import vettarr

        return (pathlib.Path(vettarr.__file__).parent / relative).read_text()

    def test_no_call_site_sets_the_ownership_flag_by_hand(self) -> None:
        for relative in self.SITES:
            with self.subTest(module=relative):
                self.assertNotIn("created_torrent =", self._source(relative))

    def test_every_call_site_goes_through_the_helper(self) -> None:
        for relative in self.SITES:
            with self.subTest(module=relative):
                self.assertIn("record_torrent_origin(dl, add_result)",
                              self._source(relative))

    def test_the_search_worker_uses_it_on_both_of_its_paths(self) -> None:
        """The linked torrent and the search's best release (Prowlarr's or a
        direct tracker search's) write the download row through
        `record_download`, which is the one caller."""
        source = self._source("searcher/search_service.py")
        self.assertEqual(source.count("record_torrent_origin(dl, add_result)"), 1)
        self.assertEqual(source.count("await record_download("), 2)

    def test_a_person_s_grab_goes_through_the_same_helper(self) -> None:
        self.assertEqual(
            self._source("actions.py").count("await search_service.record_download("), 1,
        )

    def test_no_surface_writes_a_download_row_itself(self) -> None:
        for relative in self.SURFACES:
            with self.subTest(module=relative):
                source = self._source(relative)
                self.assertNotIn("record_torrent_origin", source)
                self.assertNotIn("Download(", source)

    def test_the_flag_is_written_in_exactly_one_place(self) -> None:
        import vettarr

        root = pathlib.Path(vettarr.__file__).parent
        writers = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "created_torrent =" in path.read_text()
        ]
        self.assertEqual(writers, ["models.py"])


class _Returned:
    """What RETURNING id answers: the id of the row the UPDATE matched, or nothing."""

    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StatementSession:
    """Applies a real UPDATE statement to one in-memory item.

    It reads the conditions off the statement instead of re-stating them, so a
    condition deleted from the source really does change what this does. An
    earlier version of this test decided for itself what the rule was, and a
    mutation that removed the status check from the query sailed through it.
    """

    def __init__(self, item: MediaItem) -> None:
        self.item = item
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        for criterion in statement._where_criteria:
            column = getattr(criterion.left, "name", None)
            wanted = getattr(criterion.right, "value", None)
            actual = getattr(self.item, column, None)
            if getattr(criterion.right, "expanding", False):
                if actual not in wanted:
                    return _Returned(None)
            elif actual != wanted:
                return _Returned(None)
        for column, value in statement._values.items():
            setattr(self.item, column.name, getattr(value, "value", value))
        return _Returned(self.item.id)

    async def commit(self) -> None:
        pass


class PollerCompletionTests(unittest.TestCase):
    """A rejection landing inside a poll iteration is not undone.

    The poller reads the live downloads, then spends as long as qBittorrent
    takes on each one. A reject in that window cancels the download row, but
    this iteration is already holding the list it read beforehand -- and it
    used to mark the item COMPLETED by id, with no condition at all.
    """

    def _complete(self, status: ItemStatus):
        item = make_item(status=status)
        session = _StatementSession(item)
        moved = asyncio.run(scheduler._complete_item(session, item.id))
        # It says whether it marked the item: the poller reports a
        # completion only when it did.
        self.assertIs(moved, item.status is ItemStatus.COMPLETED and status is ItemStatus.DOWNLOADING)
        return item, session

    def test_only_a_download_that_is_running_can_complete(self) -> None:
        self.assertEqual(COMPLETABLE_STATUSES, frozenset({ItemStatus.DOWNLOADING}))

    def test_a_rejected_item_is_not_a_status_a_download_completes_from(self) -> None:
        self.assertNotIn(ItemStatus.REJECTED, COMPLETABLE_STATUSES)

    def test_a_rejection_mid_poll_survives_the_completion(self) -> None:
        item, _session = self._complete(ItemStatus.REJECTED)
        self.assertEqual(item.status, ItemStatus.REJECTED)

    def test_a_running_download_still_completes(self) -> None:
        """The positive control: the same statement does land when it should."""
        item, _session = self._complete(ItemStatus.DOWNLOADING)
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_an_item_someone_already_moved_on_is_left_alone(self) -> None:
        for status in (ItemStatus.PENDING_REVIEW, ItemStatus.ERROR,
                       ItemStatus.NO_MATCH):
            with self.subTest(status=status):
                item, _session = self._complete(status)
                self.assertEqual(item.status, status)

    def test_the_statement_carries_a_status_condition(self) -> None:
        """Not just any second condition: one that reads the status column."""
        _item, session = self._complete(ItemStatus.DOWNLOADING)
        columns = [
            getattr(c.left, "name", None)
            for c in session.statements[0]._where_criteria
        ]
        self.assertIn("status", columns)
        self.assertIn("id", columns)

    def test_the_condition_reaches_the_sql(self) -> None:
        _item, session = self._complete(ItemStatus.DOWNLOADING)
        sql = str(
            session.statements[0].compile(compile_kwargs={"literal_binds": True})
        )
        self.assertIn("media_items.status IN ('DOWNLOADING')", sql)
        self.assertNotIn("REJECTED", sql)

    def test_the_poller_writes_to_media_items_in_one_place_only(self) -> None:
        source = pathlib.Path(scheduler.__file__).read_text()
        self.assertEqual(source.count("update(MediaItem)"), 1)


if __name__ == "__main__":
    unittest.main()
