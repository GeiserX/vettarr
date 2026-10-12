import asyncio
import logging
import os
import unittest
from typing import Self
from unittest.mock import AsyncMock, patch

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

import httpx

from vettarr import search_jobs
from vettarr.models import MediaType
from vettarr.searcher.qbittorrent_client import QBittorrentClient
from vettarr.searcher.search_service import ItemRef

_MESSAGE_ID = 4242

# These items carry no torrent of their own, so the search always runs; the
# direct add has its own tests (test_sources.py).
_no_direct_add = patch.object(search_jobs, "add_direct", AsyncMock(return_value=None))


def setUpModule() -> None:
    _no_direct_add.start()


def tearDownModule() -> None:
    _no_direct_add.stop()


def _ref(item_id: int = 7) -> ItemRef:
    return ItemRef(
        id=item_id,
        title="Some Release",
        year=2024,
        quality="1080p",
        season=None,
        media_type=MediaType.MOVIE,
        telegram_message_id=_MESSAGE_ID,
    )


def _healthy_qbittorrent(healthy: bool = True) -> AsyncMock:
    qbt = AsyncMock()
    qbt.confirm_torrent_healthy.return_value = healthy
    return qbt


class ProcessItemDeletionTests(unittest.TestCase):
    """The group post is the only copy of the request, so it may only be
    deleted once qBittorrent confirms it is really holding the torrent."""

    def test_failed_search_keeps_group_post(self) -> None:
        ingester = AsyncMock()
        failure = RuntimeError("prowlarr unreachable")

        with (
            patch.object(
                search_jobs, "search_and_download", AsyncMock(side_effect=failure)
            ),
            patch.object(search_jobs, "_mark_failed", AsyncMock()) as mark_failed,
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        ingester.after.assert_not_awaited()
        mark_failed.assert_awaited_once_with(7, failure)

    def test_no_match_keeps_group_post(self) -> None:
        ingester = AsyncMock()

        with (
            patch.object(
                search_jobs, "search_and_download", AsyncMock(return_value="no_match")
            ),
            patch.object(
                search_jobs, "_finish_no_match_if_needed", AsyncMock()
            ) as finish,
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        ingester.after.assert_not_awaited()
        finish.assert_awaited_once_with(7)

    def test_error_result_keeps_group_post(self) -> None:
        ingester = AsyncMock()

        with patch.object(
            search_jobs, "search_and_download", AsyncMock(return_value="error")
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        ingester.after.assert_not_awaited()

    def test_confirmed_download_deletes_group_post_once(self) -> None:
        ingester = AsyncMock()
        qbt = _healthy_qbittorrent()

        with (
            patch.object(
                search_jobs,
                "search_and_download",
                AsyncMock(return_value="downloading"),
            ),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value="abc123")
            ),
            patch.object(search_jobs, "qbittorrent", qbt),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        qbt.confirm_torrent_healthy.assert_awaited_once_with("abc123")
        ingester.after.assert_awaited_once_with("download", 7)

    def test_unhealthy_torrent_keeps_group_post(self) -> None:
        ingester = AsyncMock()
        qbt = _healthy_qbittorrent(healthy=False)

        with (
            patch.object(
                search_jobs,
                "search_and_download",
                AsyncMock(return_value="downloading"),
            ),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value="abc123")
            ),
            patch.object(search_jobs, "qbittorrent", qbt),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        ingester.after.assert_not_awaited()

    def test_unverifiable_download_keeps_group_post(self) -> None:
        """qBittorrent accepted the add but reported no hash, so nothing can
        be confirmed and the post has to stay."""
        ingester = AsyncMock()
        qbt = _healthy_qbittorrent()

        with (
            patch.object(
                search_jobs,
                "search_and_download",
                AsyncMock(return_value="downloading"),
            ),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value=None)
            ),
            patch.object(search_jobs, "qbittorrent", qbt),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        qbt.confirm_torrent_healthy.assert_not_awaited()
        ingester.after.assert_not_awaited()

    def test_unreachable_qbittorrent_keeps_group_post(self) -> None:
        ingester = AsyncMock()
        qbt = AsyncMock()
        qbt.confirm_torrent_healthy.side_effect = OSError("connection refused")

        with (
            patch.object(
                search_jobs,
                "search_and_download",
                AsyncMock(return_value="downloading"),
            ),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value="abc123")
            ),
            patch.object(search_jobs, "qbittorrent", qbt),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        ingester.after.assert_not_awaited()

    def test_search_runs_before_any_deletion(self) -> None:
        calls: list[str] = []
        ingester = AsyncMock()
        ingester.after.side_effect = lambda *_: calls.append("delete")
        qbt = _healthy_qbittorrent()

        async def _search(_ref_arg):
            calls.append("search")
            return "downloading"

        with (
            patch.object(search_jobs, "search_and_download", _search),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value="abc123")
            ),
            patch.object(search_jobs, "qbittorrent", qbt),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        self.assertEqual(calls, ["search", "delete"])

    def test_delete_failure_is_swallowed(self) -> None:
        ingester = AsyncMock()
        ingester.after.side_effect = RuntimeError("telegram down")
        qbt = _healthy_qbittorrent()

        with (
            patch.object(
                search_jobs,
                "search_and_download",
                AsyncMock(return_value="downloading"),
            ),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value="abc123")
            ),
            patch.object(search_jobs, "qbittorrent", qbt),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

        ingester.after.assert_awaited_once_with("download", 7)


class DuplicateDownloadTests(unittest.TestCase):
    """A duplicate is already in qBittorrent and usually finished seeding, so
    it satisfies the same condition a fresh add does: the content is really
    there, and the group post can go."""

    @staticmethod
    def _client(listed: list[dict]) -> QBittorrentClient:
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._login = AsyncMock()
        client._request = AsyncMock(return_value=httpx.Response(200, json=listed))
        return client

    def _run(self, listed: list[dict], ingester: AsyncMock) -> None:
        with (
            patch.object(
                search_jobs,
                "search_and_download",
                AsyncMock(return_value="downloading"),
            ),
            patch.object(
                search_jobs, "_recorded_torrent_hash", AsyncMock(return_value="abc123")
            ),
            patch.object(search_jobs, "qbittorrent", self._client(listed)),
            patch(
                "vettarr.searcher.qbittorrent_client.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            asyncio.run(search_jobs._process_item(_ref(), ingester))

    def test_seeding_duplicate_deletes_group_post_once(self) -> None:
        ingester = AsyncMock()

        self._run([{"hash": "abc123", "state": "stalledUP"}], ingester)

        ingester.after.assert_awaited_once_with("download", 7)

    def test_torrent_that_never_appeared_keeps_group_post(self) -> None:
        ingester = AsyncMock()

        self._run([], ingester)

        ingester.after.assert_not_awaited()


class _RecordingSession:
    """Stands in for async_session(), keeping the statements it was given."""

    def __init__(self) -> None:
        self.statements: list = []

    def __call__(self) -> Self:
        return self

    async def execute(self, statement) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class MarkFailedTests(unittest.TestCase):
    @staticmethod
    def _error_message(item_id: int, *args) -> str:
        recorder = _RecordingSession()
        with patch.object(search_jobs, "async_session", recorder):
            asyncio.run(search_jobs._mark_failed(item_id, *args))
        return recorder.statements[0].compile().params["error_message"]

    def test_error_message_names_the_exception(self) -> None:
        self.assertEqual(
            self._error_message(5, TimeoutError("no answer")),
            "Search/download failed; see service logs (TimeoutError)",
        )

    def test_error_message_without_exception_is_unchanged(self) -> None:
        self.assertEqual(
            self._error_message(5), "Search/download failed; see service logs"
        )

    def test_error_message_is_truncated(self) -> None:
        long_name = type("E" * 500, (Exception,), {})

        self.assertEqual(
            len(self._error_message(5, long_name())),
            search_jobs._ERROR_MESSAGE_LIMIT,
        )


if __name__ == "__main__":
    unittest.main()
