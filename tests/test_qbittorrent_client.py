import asyncio
import base64
import hashlib
import logging
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

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

from vettarr.config import runtime
from vettarr.config.schema import Config
from vettarr.searcher.qbittorrent_client import (
    QBittorrentClient,
    _add_was_accepted,
    _added_torrent_hash,
    _magnet_info_hash,
    torrent_info_hash,
)
from vettarr.models import MediaType


class ResponseTests(unittest.TestCase):
    def test_accepts_qbittorrent_52_success_response(self) -> None:
        response = httpx.Response(
            200,
            json={
                "success_count": 1,
                "pending_count": 0,
                "failure_count": 0,
            },
        )
        self.assertTrue(_add_was_accepted(response))
        self.assertIsNone(_added_torrent_hash(response))

    def test_extracts_added_torrent_hash(self) -> None:
        response = httpx.Response(
            200,
            json={
                "success_count": 1,
                "pending_count": 0,
                "failure_count": 0,
                "added_torrent_ids": ["ABCDEF"],
            },
        )
        self.assertEqual(_added_torrent_hash(response), "abcdef")

    def test_does_not_treat_pending_as_downloading(self) -> None:
        response = httpx.Response(
            202,
            json={
                "success_count": 0,
                "pending_count": 1,
                "failure_count": 0,
            },
        )
        self.assertFalse(_add_was_accepted(response))
        self.assertFalse(_add_was_accepted(httpx.Response(202, text="Ok.")))

    def test_rejects_conflict_response(self) -> None:
        self.assertFalse(_add_was_accepted(httpx.Response(409, text="Conflict")))


class InfoHashTests(unittest.TestCase):
    def test_hashes_raw_torrent_info_dictionary(self) -> None:
        info = b"d4:name4:test6:lengthi1ee"
        torrent = b"d4:info" + info + b"e"
        self.assertEqual(torrent_info_hash(torrent), hashlib.sha1(info).hexdigest())

    def test_a_file_nested_deeper_than_any_torrent_is_not_one(self) -> None:
        """Whoever posts a file chooses its bytes: 200000 nested lists must
        read as "not a torrent", not raise RecursionError."""
        deep = b"d4:info" + b"l" * 200_000 + b"e" * 200_000 + b"e"
        self.assertIsNone(torrent_info_hash(deep))

    def test_extracts_hex_and_base32_magnet_hashes(self) -> None:
        raw = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        hex_hash = raw.hex()
        base32_hash = base64.b32encode(raw).decode()

        self.assertEqual(
            _magnet_info_hash(f"magnet:?xt=urn:btih:{hex_hash}"),
            hex_hash,
        )
        self.assertEqual(
            _magnet_info_hash(f"magnet:?xt=urn:btih:{base32_hash}"),
            hex_hash,
        )


class LoginTests(unittest.TestCase):
    def test_accepts_auth_bypass_204(self) -> None:
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._base = "http://qbittorrent"
        client._user = "admin"
        client._pass = ""
        client._authenticated = False
        client._auth_lock = asyncio.Lock()
        client._client = AsyncMock()
        client._client.post.return_value = httpx.Response(204)

        asyncio.run(client._login())

        self.assertTrue(client._authenticated)

    def test_reauthenticates_once_after_forbidden_response(self) -> None:
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._base = "http://qbittorrent"
        client._user = "admin"
        client._pass = ""
        client._authenticated = True
        client._auth_lock = asyncio.Lock()
        client._client = AsyncMock()
        client._client.request.side_effect = [
            httpx.Response(403),
            httpx.Response(200, json=[]),
        ]
        client._client.post.return_value = httpx.Response(204)

        response = asyncio.run(client._request("GET", "/api/v2/torrents/info"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client._client.post.await_count, 1)


class PendingTorrentTests(unittest.TestCase):
    def test_resolves_pending_torrent_by_new_hash_and_title(self) -> None:
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._request = AsyncMock(
            return_value=httpx.Response(
                200,
                json=[{"hash": "newhash", "name": "Expected Release Title"}],
            )
        )

        with patch("vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(
                client._wait_for_pending_torrent(
                    {"oldhash"},
                    expected_title="Expected Release Title",
                )
            )

        self.assertEqual(result, "newhash")

    def test_reuses_existing_torrent_with_exact_release_title(self) -> None:
        """The name match is what is left when the .torrent cannot be fetched
        and so the link has no identity of its own."""
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._base = "http://qbittorrent"
        client._authenticated = True
        client._login = AsyncMock()
        client._indexer_client = _indexer(error=httpx.ConnectError("indexer down"))
        client._request = AsyncMock(
            return_value=httpx.Response(
                200,
                json=[{
                    "hash": "existinghash",
                    "name": "Expected Release Title",
                    "category": "books",
                }],
            )
        )
        # The match is within the type's category, as config.yaml names it.
        cfg = Config.model_validate({"integrations": {"qbittorrent": {
            "url": "http://qbittorrent", "categories": {"ebook": "books"},
        }}})

        with patch.object(runtime, "_config", cfg):
            result = asyncio.run(
                client.add_torrent(
                    MediaType.EBOOK,
                    download_url="http://prowlarr/download/1",
                    expected_title="Expected Release Title",
                )
            )

        self.assertEqual(result.hash, "existinghash")
        self.assertFalse(
            result.created,
            "a torrent qBittorrent was already holding is not ours to delete",
        )
        self.assertEqual(client._request.await_count, 1)


class ConfirmHealthyTests(unittest.TestCase):
    @staticmethod
    def _client(responses) -> QBittorrentClient:
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._login = AsyncMock()
        client._request = AsyncMock(side_effect=responses)
        return client

    @staticmethod
    def _torrents(state: str, torrent_hash: str = "abc123") -> httpx.Response:
        return httpx.Response(200, json=[{"hash": torrent_hash, "state": state}])

    def test_downloading_torrent_is_healthy(self) -> None:
        client = self._client([self._torrents("downloading")])

        self.assertTrue(asyncio.run(client.confirm_torrent_healthy("abc123")))

    def test_queued_stalled_and_checking_are_healthy(self) -> None:
        for state in ("queuedDL", "stalledDL", "checkingDL", "metaDL", "uploading"):
            with self.subTest(state=state):
                client = self._client([self._torrents(state)])
                self.assertTrue(asyncio.run(client.confirm_torrent_healthy("abc123")))

    def test_error_and_missing_files_are_not_healthy(self) -> None:
        for state in ("error", "missingFiles"):
            with self.subTest(state=state):
                client = self._client([self._torrents(state)])
                self.assertFalse(asyncio.run(client.confirm_torrent_healthy("abc123")))

    def test_absent_torrent_is_not_healthy(self) -> None:
        client = self._client([httpx.Response(200, json=[])] * 3)

        with patch("vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(client.confirm_torrent_healthy("abc123", attempts=3))

        self.assertFalse(result)
        self.assertEqual(client._request.await_count, 3)

    def test_waits_for_a_torrent_that_registers_late(self) -> None:
        client = self._client([
            httpx.Response(200, json=[]),
            self._torrents("unknown"),
            self._torrents("downloading"),
        ])

        with patch("vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(client.confirm_torrent_healthy("abc123", attempts=3))

        self.assertTrue(result)

    def test_matches_hash_case_insensitively(self) -> None:
        client = self._client([self._torrents("downloading", torrent_hash="ABC123")])

        self.assertTrue(asyncio.run(client.confirm_torrent_healthy("abc123")))

    def test_ignores_an_unrelated_torrent(self) -> None:
        client = self._client(
            [self._torrents("downloading", torrent_hash="otherhash")] * 2
        )

        with patch("vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(client.confirm_torrent_healthy("abc123", attempts=2))

        self.assertFalse(result)


# Invented, in the shape real releases take: Prowlarr's release title and the
# name qBittorrent ends up with share no normalized form, so nothing but the
# info hash can identify the torrent that is already there.
_PROWLARR_TITLE = "Trail and Tide Edición   Junio 2026   Nº 312 MULTi/SPANiSH"
_QBITTORRENT_NAME = "Trail and Tide Edicion - Junio 2026 - Nº 312.pdf"
_INFO = b"d4:name4:test6:lengthi1ee"
_TORRENT = b"d4:info" + _INFO + b"e"
_TORRENT_HASH = hashlib.sha1(_INFO).hexdigest()
_DOWNLOAD_URL = "http://prowlarr/3/download?apikey=k&link=abc"

_PENDING_BODY = {
    "added_torrent_ids": [],
    "failure_count": 0,
    "pending_count": 1,
    "success_count": 0,
}


_ACCEPTED_BODY = {
    "added_torrent_ids": [_TORRENT_HASH],
    "failure_count": 0,
    "pending_count": 0,
    "success_count": 1,
}


def _posts(client: QBittorrentClient) -> list[dict]:
    """The keyword arguments of every add the client posted."""
    return [
        call.kwargs for call in client._request.await_args_list
        if call.args[0] == "POST"
    ]


def _list_params(client: QBittorrentClient) -> list[dict | None]:
    """The query of every /torrents/info the client asked for."""
    return [
        call.kwargs.get("params") for call in client._request.await_args_list
        if call.args[0] == "GET" and call.args[1] == "/api/v2/torrents/info"
    ]


def _bounded(params: dict | None) -> bool:
    """True when the query cannot return the whole library."""
    return bool(params) and ("hashes" in params or "limit" in params)


def _seeding_duplicate() -> list[dict]:
    return [{
        "hash": _TORRENT_HASH,
        "name": _QBITTORRENT_NAME,
        "category": "magazines",
        "state": "stalledUP",
    }]


def _indexer(response: httpx.Response | None = None, error: Exception | None = None) -> AsyncMock:
    """Stand in for the client the .torrent is fetched with."""
    indexer = AsyncMock()
    if error is not None:
        indexer.get.side_effect = error
    else:
        indexer.get.return_value = response or httpx.Response(200, content=_TORRENT)
    return indexer


def _url_add_client(add_response: httpx.Response, listed: list[dict]) -> QBittorrentClient:
    """A client whose qBittorrent answers every add with add_response and
    every /torrents/info with listed."""
    client = QBittorrentClient.__new__(QBittorrentClient)
    client._base = "http://qbittorrent"
    client._authenticated = True
    client._login = AsyncMock()

    async def _request(method: str, _path: str, **_kwargs) -> httpx.Response:
        if method == "POST":
            return add_response
        return httpx.Response(200, json=listed)

    client._request = AsyncMock(side_effect=_request)
    client._client = AsyncMock()
    client._indexer_client = _indexer()
    return client


class DuplicateUrlAddTests(unittest.TestCase):
    """qBittorrent 5.x drops a duplicate URL add behind the same 202 body it
    uses for a link it has not finished resolving, so the add itself can never
    say which happened -- only the torrent list can."""

    def _add(self, client: QBittorrentClient) -> str | None:
        with patch(
            "vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()
        ):
            return asyncio.run(
                client.add_torrent(
                    MediaType.MAGAZINE,
                    download_url=_DOWNLOAD_URL,
                    expected_title=_PROWLARR_TITLE,
                )
            )

    def test_pending_duplicate_returns_the_existing_hash(self) -> None:
        client = _url_add_client(
            httpx.Response(202, json=_PENDING_BODY), _seeding_duplicate()
        )

        outcome = self._add(client)
        self.assertEqual(outcome.hash, _TORRENT_HASH)
        self.assertFalse(outcome.created, "the torrent was already in the client")
        client._indexer_client.get.assert_awaited_once_with(
            _DOWNLOAD_URL, follow_redirects=True
        )
        client._client.get.assert_not_awaited()

    def test_fresh_add_fetches_the_torrent_once_and_uploads_it(self) -> None:
        client = _url_add_client(httpx.Response(200, json=_ACCEPTED_BODY), [])

        outcome = self._add(client)
        self.assertEqual(outcome.hash, _TORRENT_HASH)
        self.assertTrue(outcome.created, "this add is what put the torrent there")
        client._indexer_client.get.assert_awaited_once_with(
            _DOWNLOAD_URL, follow_redirects=True
        )
        (post,) = _posts(client)
        self.assertEqual(
            post["files"]["torrents"][1], _TORRENT,
            "qBittorrent gets the file we fetched, so the indexer is hit once",
        )
        self.assertNotIn("urls", post["data"])

    def test_a_link_that_cannot_be_fetched_is_handed_over_as_a_link(self) -> None:
        client = _url_add_client(httpx.Response(200, json=_ACCEPTED_BODY), [])
        client._indexer_client = _indexer(error=httpx.ConnectError("indexer down"))

        outcome = self._add(client)
        self.assertEqual(outcome.hash, _TORRENT_HASH)
        (post,) = _posts(client)
        self.assertEqual(post["data"]["urls"], _DOWNLOAD_URL)
        self.assertNotIn("files", post)

    def test_an_unparseable_link_is_treated_as_not_fetched(self) -> None:
        client = _url_add_client(httpx.Response(200, json=_ACCEPTED_BODY), [])
        client._indexer_client = _indexer(error=httpx.InvalidURL("bad redirect"))

        self.assertIsNotNone(self._add(client), "the add must not die on the fetch")

    def test_add_fails_when_the_torrent_never_appears(self) -> None:
        client = _url_add_client(httpx.Response(202, json=_PENDING_BODY), [])

        self.assertIsNone(self._add(client))

    def test_add_fails_when_the_torrent_file_cannot_be_fetched(self) -> None:
        client = _url_add_client(httpx.Response(202, json=_PENDING_BODY), [])
        client._indexer_client = _indexer(error=httpx.ConnectError("indexer down"))

        self.assertIsNone(self._add(client))

    def test_add_fails_when_the_link_does_not_serve_a_torrent(self) -> None:
        client = _url_add_client(httpx.Response(202, json=_PENDING_BODY), [])
        client._indexer_client = _indexer(httpx.Response(200, text="<html>login</html>"))

        self.assertIsNone(self._add(client))


class WholeListTests(unittest.TestCase):
    """On 2026-10-04 a batch of twenty posts took the process from 140 MB past
    its 2 GB limit: every link add asked qBittorrent for its whole list
    thirty-one times, 18 MB an answer, and then recorded the torrent it had
    just added as one the client already held. A link add must never read the
    whole library, whichever way it ends."""

    def _add(self, client: QBittorrentClient):
        with patch(
            "vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()
        ):
            return asyncio.run(
                client.add_torrent(
                    MediaType.MAGAZINE,
                    download_url=_DOWNLOAD_URL,
                    expected_title=_PROWLARR_TITLE,
                )
            )

    def _assert_never_the_whole_list(self, client: QBittorrentClient) -> None:
        asked = _list_params(client)
        self.assertTrue(asked, "the add is expected to look at qBittorrent at least once")
        for params in asked:
            self.assertTrue(_bounded(params), f"asked for the whole list: {params!r}")

    def test_a_fresh_link_add_never_asks_for_the_whole_list(self) -> None:
        client = _url_add_client(httpx.Response(200, json=_ACCEPTED_BODY), [])

        self.assertIsNotNone(self._add(client))
        self._assert_never_the_whole_list(client)

    def test_a_duplicate_link_add_never_asks_for_the_whole_list(self) -> None:
        client = _url_add_client(
            httpx.Response(202, json=_PENDING_BODY), _seeding_duplicate()
        )

        outcome = self._add(client)
        self.assertFalse(outcome.created)
        self._assert_never_the_whole_list(client)
        self.assertLessEqual(
            len(_list_params(client)), 2,
            "a duplicate is settled by asking about one hash, not by polling",
        )

    def test_waiting_for_a_known_hash_asks_about_that_hash_alone(self) -> None:
        client = _url_add_client(httpx.Response(200, json=_ACCEPTED_BODY), [])

        with patch(
            "vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()
        ):
            asyncio.run(
                client._wait_for_pending_torrent(set(), expected_hash=_TORRENT_HASH)
            )

        asked = _list_params(client)
        self.assertEqual(len(asked), 30)
        self.assertEqual(asked[0], {"hashes": _TORRENT_HASH})

    def test_waiting_for_an_unknown_torrent_reads_only_the_newest(self) -> None:
        client = _url_add_client(httpx.Response(200, json=_ACCEPTED_BODY), [])

        with patch(
            "vettarr.searcher.qbittorrent_client.asyncio.sleep", new=AsyncMock()
        ):
            asyncio.run(
                client._wait_for_pending_torrent({"old"}, expected_title="Some Release")
            )

        asked = _list_params(client)
        self.assertEqual(len(asked), 30, "the wait is expected to poll, or this proves nothing")
        for params in asked:
            self.assertEqual(params["sort"], "added_on")
            self.assertEqual(params["reverse"], "true")
            self.assertIn("limit", params)

    def test_only_real_names_come_back(self) -> None:
        """Each object is reduced to its name and category while it is parsed. A torrent
        with no name is left out instead of failing the whole refresh."""
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._login = AsyncMock()
        client._request = AsyncMock(
            return_value=httpx.Response(
                200,
                json=[
                    {"hash": "a", "name": "First", "category": "books", "size": 1},
                    {"hash": "b", "name": "Segundo Ñ", "category": "Series", "size": 2},
                    {"hash": "c", "name": None, "category": "Series"},
                    {"hash": "d", "category": "Series"},
                ],
                request=httpx.Request("GET", "http://qbittorrent/api/v2/torrents/info"),
            )
        )

        self.assertEqual(
            asyncio.run(client.list_all_names()), [("First", "books"), ("Segundo Ñ", "Series")]
        )


class OwnershipTests(unittest.TestCase):
    """Who put the torrent there. Rejecting an item deletes the files, so
    "qBittorrent gave us a hash" must never be read as "we added it"."""

    _MAGNET = f"magnet:?xt=urn:btih:{_TORRENT_HASH}&dn=Something"
    _ACCEPTED = {
        "added_torrent_ids": [_TORRENT_HASH],
        "failure_count": 0,
        "pending_count": 0,
        "success_count": 1,
    }

    @staticmethod
    def _listed(name: str) -> list[dict]:
        return [{
            "hash": _TORRENT_HASH,
            "name": name,
            "category": "magazines",
            "state": "stalledUP",
        }]

    def test_a_magnet_already_in_the_client_was_not_added_by_us(self) -> None:
        client = _url_add_client(
            httpx.Response(200, json=self._ACCEPTED), self._listed("Already Here")
        )

        outcome = asyncio.run(
            client.add_torrent(MediaType.MOVIE, magnet_url=self._MAGNET)
        )

        self.assertEqual(outcome.hash, _TORRENT_HASH)
        self.assertFalse(
            outcome.created,
            "the client was holding this magnet before the add",
        )

    def test_a_magnet_the_client_did_not_have_is_ours(self) -> None:
        client = _url_add_client(httpx.Response(200, json=self._ACCEPTED), [])

        outcome = asyncio.run(
            client.add_torrent(MediaType.MOVIE, magnet_url=self._MAGNET)
        )

        self.assertTrue(outcome.created)

    def test_a_url_add_that_returns_a_hash_from_the_baseline_is_not_ours(self) -> None:
        """qBittorrent reports success and hands back a torrent it already
        had. Only the list taken before the add can tell."""
        client = _url_add_client(
            httpx.Response(200, json=self._ACCEPTED),
            self._listed("A Completely Different Name"),
        )

        outcome = asyncio.run(
            client.add_torrent(
                MediaType.MAGAZINE,
                download_url=_DOWNLOAD_URL,
                expected_title="A Release Nobody Has",
            )
        )

        self.assertEqual(outcome.hash, _TORRENT_HASH)
        self.assertFalse(outcome.created)

    def test_a_failed_add_reports_nothing_at_all(self) -> None:
        client = _url_add_client(httpx.Response(500, text="boom"), [])

        self.assertIsNone(
            asyncio.run(client.add_torrent(MediaType.MOVIE, magnet_url=self._MAGNET))
        )


class AddTorrentFileTests(unittest.TestCase):
    def test_records_computed_info_hash(self) -> None:
        info = b"d4:name4:test6:lengthi1ee"
        torrent = b"d4:info" + info + b"e"
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._login = AsyncMock()
        client._request = AsyncMock(return_value=httpx.Response(200, text="Ok."))

        result = asyncio.run(client.add_torrent_file(torrent, MediaType.MOVIE))

        self.assertEqual(result.hash, hashlib.sha1(info).hexdigest())
        self.assertTrue(result.created)

    def test_duplicate_file_add_returns_the_existing_hash(self) -> None:
        for status in (202, 409):
            with self.subTest(status=status):
                client = _url_add_client(
                    httpx.Response(status, json=_PENDING_BODY), _seeding_duplicate()
                )

                result = asyncio.run(
                    client.add_torrent_file(_TORRENT, MediaType.MAGAZINE)
                )

                self.assertEqual(result.hash, _TORRENT_HASH)
                self.assertFalse(
                    result.created,
                    "a .torrent qBittorrent already held was not added by us",
                )

    def test_a_file_add_the_client_already_held_is_not_ours(self) -> None:
        """qBittorrent accepts the add and reports success even though the
        torrent was already seeding. Deleting it on a reject would take the
        existing files with it."""
        client = _url_add_client(
            httpx.Response(200, text="Ok."), _seeding_duplicate()
        )

        result = asyncio.run(client.add_torrent_file(_TORRENT, MediaType.MAGAZINE))

        self.assertEqual(result.hash, _TORRENT_HASH)
        self.assertFalse(result.created)

    def test_file_add_fails_when_the_torrent_is_absent(self) -> None:
        client = _url_add_client(httpx.Response(202, json=_PENDING_BODY), [])

        result = asyncio.run(client.add_torrent_file(_TORRENT, MediaType.MAGAZINE))

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()


def _torrent(torrent_hash: str, content_path: str, **extra) -> dict:
    return {"hash": torrent_hash, "name": content_path.rsplit("/", 1)[-1],
            "content_path": content_path, "save_path": content_path.rsplit("/", 1)[0],
            "state": "stalledUP", **extra}


def _qbittorrent(torrents: list[dict], delete=None):
    """A `_request` that answers like qBittorrent for the removal path.

    `hashes=` returns the matching torrents, a bare /torrents/info returns
    the whole list, and the delete answers `delete` (200 by default).
    """
    async def request(method, path, **kwargs):
        # raise_for_status needs the request attached, as on a real response.
        req = httpx.Request(method, f"http://qbittorrent{path}")
        if method == "GET" and path == "/api/v2/torrents/info":
            params = kwargs.get("params") or {}
            if "hashes" in params:
                wanted = params["hashes"].lower().split("|")
                return httpx.Response(
                    200, json=[t for t in torrents if t["hash"].lower() in wanted], request=req
                )
            return httpx.Response(200, json=torrents, request=req)
        if method == "POST" and path == "/api/v2/torrents/delete":
            if isinstance(delete, Exception):
                raise delete
            return delete or httpx.Response(200, text="")
        raise AssertionError(f"unexpected {method} {path}")
    return AsyncMock(side_effect=request)


def _delete_posts(request: AsyncMock) -> list[dict]:
    return [
        call.kwargs["data"] for call in request.await_args_list
        if call.args[0] == "POST" and call.args[1] == "/api/v2/torrents/delete"
    ]


def _whole_list_reads(request: AsyncMock) -> int:
    return sum(
        1 for call in request.await_args_list
        if call.args[0] == "GET" and call.args[1] == "/api/v2/torrents/info"
        and not _bounded(call.kwargs.get("params"))
    )


class RemoveTorrentTests(unittest.TestCase):
    """Taking back a rejected item has to take the content with it, unless
    another torrent still lives on those files."""

    OURS = _torrent("ABCDEF", "/downloads/series/Show.S01.1080p")

    def _client(self) -> QBittorrentClient:
        client = QBittorrentClient()
        client._authenticated = True
        return client

    def test_delete_is_posted_with_the_files(self) -> None:
        client = self._client()
        request = _qbittorrent([self.OURS, _torrent("111", "/downloads/films/Film")])
        with patch.object(client, "_request", request):
            removed = asyncio.run(client.remove_torrent("ABCDEF"))
        self.assertTrue(removed)
        self.assertEqual(
            _delete_posts(request), [{"hashes": "abcdef", "deleteFiles": "true"}]
        )

    def test_files_can_be_kept(self) -> None:
        client = self._client()
        request = _qbittorrent([self.OURS])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abc", delete_files=False))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")
        self.assertEqual(_whole_list_reads(request), 0, "keeping files needs no check")

    def test_an_empty_hash_asks_qbittorrent_nothing(self) -> None:
        client = self._client()
        request = AsyncMock()
        with patch.object(client, "_request", request):
            removed = asyncio.run(client.remove_torrent(""))
        self.assertFalse(removed)
        request.assert_not_awaited()

    def test_a_refusal_is_reported_not_raised(self) -> None:
        client = self._client()
        request = _qbittorrent([self.OURS], delete=httpx.Response(409, text="nope"))
        with patch.object(client, "_request", request):
            self.assertFalse(asyncio.run(client.remove_torrent("abcdef")))

    def test_a_network_error_is_reported_not_raised(self) -> None:
        client = self._client()
        request = _qbittorrent([self.OURS], delete=httpx.ConnectError("down"))
        with patch.object(client, "_request", request):
            self.assertFalse(asyncio.run(client.remove_torrent("abcdef")))

    def test_files_stay_when_another_torrent_has_the_same_folder(self) -> None:
        # The same release from a second tracker: other hash, same path.
        other = _torrent("222", self.OURS["content_path"])
        client = self._client()
        request = _qbittorrent([self.OURS, other])
        with patch.object(client, "_request", request):
            removed = asyncio.run(client.remove_torrent("abcdef"))
        self.assertTrue(removed, "the torrent itself still goes")
        self.assertEqual(
            _delete_posts(request), [{"hashes": "abcdef", "deleteFiles": "false"}]
        )

    def test_files_stay_when_another_torrent_lives_inside_our_folder(self) -> None:
        episode = _torrent("333", self.OURS["content_path"] + "/Show.S01E02.mkv")
        client = self._client()
        request = _qbittorrent([self.OURS, episode])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")

    def test_files_stay_when_our_file_sits_inside_another_torrents_folder(self) -> None:
        ours = _torrent("ABCDEF", "/downloads/series/Show.S01/Show.S01E02.mkv")
        pack = _torrent("444", "/downloads/series/Show.S01")
        client = self._client()
        request = _qbittorrent([ours, pack])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")

    def test_a_sibling_folder_with_a_common_prefix_is_not_shared(self) -> None:
        sibling = _torrent("555", self.OURS["content_path"] + ".PROPER")
        client = self._client()
        request = _qbittorrent([self.OURS, sibling])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "true")

    def test_files_stay_when_the_check_cannot_be_made(self) -> None:
        client = self._client()

        async def request(method, path, **kwargs):
            if method == "GET":
                raise httpx.ConnectError("down")
            return httpx.Response(200, text="")

        request = AsyncMock(side_effect=request)
        with patch.object(client, "_request", request):
            removed = asyncio.run(client.remove_torrent("abcdef"))
        self.assertTrue(removed)
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")

    def test_files_stay_when_qbittorrent_does_not_know_the_torrent(self) -> None:
        client = self._client()
        request = _qbittorrent([_torrent("999", "/downloads/films/Film")])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")

    def test_a_flat_torrent_in_the_category_root_does_not_hold_every_file(self) -> None:
        # A multi-file torrent with no root folder reports the category root
        # as its content_path. That must not read as "contains ours".
        flat = _torrent("666", "/downloads/series", save_path="/downloads/series")
        client = self._client()
        request = _qbittorrent([self.OURS, flat])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "true")

    def test_our_own_flat_torrent_does_not_hold_the_whole_category(self) -> None:
        ours = _torrent("ABCDEF", "/downloads/series", save_path="/downloads/series")
        other = _torrent("777", "/downloads/series/Other.Show.S02")
        client = self._client()
        request = _qbittorrent([ours, other])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "true")

    def test_two_flat_torrents_in_the_same_root_still_share(self) -> None:
        ours = _torrent("ABCDEF", "/downloads/series", save_path="/downloads/series")
        other = _torrent("888", "/downloads/series", save_path="/downloads/series")
        client = self._client()
        request = _qbittorrent([ours, other])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")

    def test_a_root_content_path_keeps_the_files(self) -> None:
        ours = _torrent("ABCDEF", "/", save_path="/")
        client = self._client()
        request = _qbittorrent([ours, _torrent("999", "/downloads/films/Film")])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_delete_posts(request)[0]["deleteFiles"], "false")

    def test_an_add_inside_the_cache_window_is_seen_by_the_next_reject(self) -> None:
        # Reject X, approve the same release from another tracker, reject that
        # copy: the second reject must see the first torrent is still there.
        client = self._client()
        request = _qbittorrent([self.OURS])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertIsNotNone(client._paths_cache)
        client._login = AsyncMock()
        client._request = AsyncMock(return_value=httpx.Response(200, text="Ok."))
        asyncio.run(client.add_torrent_file(b"d4:info" + b"d4:name4:test6:lengthi1ee" + b"e", MediaType.SERIES))
        self.assertIsNone(client._paths_cache, "an add drops the cached list")

    def test_a_bulk_reject_reads_the_whole_list_once(self) -> None:
        torrents = [
            _torrent(f"{i:06x}", f"/downloads/series/Show.{i}") for i in range(20)
        ]
        client = self._client()
        request = _qbittorrent(torrents)
        with patch.object(client, "_request", request):
            async def bulk():
                for t in torrents:
                    await client.remove_torrent(t["hash"])
            asyncio.run(bulk())
        self.assertEqual(len(_delete_posts(request)), 20)
        self.assertEqual(_whole_list_reads(request), 1)
        self.assertTrue(all(d["deleteFiles"] == "true" for d in _delete_posts(request)))

    def test_the_list_cache_expires(self) -> None:
        client = self._client()
        request = _qbittorrent([self.OURS])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("abcdef"))
            client._paths_cache = (client._paths_cache[0] - 60, client._paths_cache[1])
            asyncio.run(client.remove_torrent("abcdef"))
        self.assertEqual(_whole_list_reads(request), 2)

    def test_the_check_asks_for_our_torrent_by_hash(self) -> None:
        client = self._client()
        request = _qbittorrent([self.OURS])
        with patch.object(client, "_request", request):
            asyncio.run(client.remove_torrent("ABCDEF"))
        bounded = [
            call.kwargs.get("params") for call in request.await_args_list
            if call.args[1] == "/api/v2/torrents/info" and _bounded(call.kwargs.get("params"))
        ]
        self.assertEqual(bounded, [{"hashes": "abcdef"}])
