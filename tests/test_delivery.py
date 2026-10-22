"""`deliver` in config.yaml: where an approved item goes, and what happens there.

The map used to be read by nothing: every approved item went to the download
client. These tests drive the places an item is handed on (the search worker,
the download poll, a rejection, a person's own grab) against fake Radarr,
Sonarr and Seerr servers that answer in the real shapes, and an in-memory
store that runs the real SQL statements.
"""

import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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

import httpx

import delivery_fakes as fakes
from delivery_fakes import API_KEY, TVDB_ID, FakeRadarr, FakeSeerr, FakeSonarr
from test_actions import Store, make_item

from vettarr import actions, delivery, events, ingest, review, scheduler, search_jobs
from vettarr.checker import gather
from vettarr.checker.tmdb_client import TMDBClient, TMDBResult
from vettarr.config import settings
from vettarr.config.schema import Config, warnings
from vettarr.delivery.http import DeliveryError
from vettarr.delivery.radarr import RadarrClient
from vettarr.delivery.seerr import NO_USER, SeerrClient
from vettarr.delivery.sonarr import SonarrClient, seasons_complete
from vettarr.models import (
    OP_APPROVAL,
    OP_MANUAL_DOWNLOAD,
    REVIEWABLE_STATUSES,
    AutoDecision,
    ContentCheck,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
    TorrentSearch,
)
from vettarr.searcher import search_service
from vettarr.searcher.qbittorrent_client import AddOutcome
from vettarr.searcher.search_service import ItemRef

_ROOT = pathlib.Path(__file__).resolve().parents[1]
TMDB_ID = 603
SERIES_TMDB_ID = 1396
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Some.Film.1999.1080p"


def config(deliver: dict, *, seerr_user: int | None = 2, **extra) -> Config:
    integrations = {
        "qbittorrent": {"url": "http://qbittorrent:8080"},
        "radarr": {
            "url": "http://radarr:7878", "api_key": API_KEY,
            "root_folder": "/movies", "quality_profile": "HD-1080p",
        },
        "sonarr": {
            "url": "http://sonarr:8989", "api_key": API_KEY,
            "root_folder": "/tv", "quality_profile": 4,
        },
        "seerr": {"url": "http://seerr:5055", "api_key": API_KEY, "user_id": seerr_user},
    }
    return Config.model_validate({"integrations": integrations, "deliver": deliver, **extra})


def run(coro):
    return asyncio.run(coro)


def drain() -> list[tuple[str, int | None, dict]]:
    out = []
    while events.pending():
        _at, kind, item_id, payload = events._pending.get_nowait()
        out.append((kind, item_id, payload))
    return out


class DeliveryCase(unittest.TestCase):
    """The store, the three fake servers and the download client's path, patched in."""

    DELIVER = {"movie": "radarr", "series": "sonarr"}
    SEERR_USER: int | None = 2

    def setUp(self) -> None:
        self.store = Store()
        self.radarr, self.sonarr, self.seerr = FakeRadarr(), FakeSonarr(), FakeSeerr()
        self.cfg = config(self.DELIVER, seerr_user=self.SEERR_USER)
        self.clients = {
            "radarr": RadarrClient("http://radarr:7878", API_KEY, http=self.radarr.http()),
            "sonarr": SonarrClient("http://sonarr:8989", API_KEY, http=self.sonarr.http()),
            "seerr": SeerrClient("http://seerr:5055", API_KEY, self.SEERR_USER, http=self.seerr.http()),
        }
        self.tvdb = mock.AsyncMock(return_value=TVDB_ID)
        # The download client's path, as the worker hands it to deliver().
        self.add_direct = mock.AsyncMock(return_value=None)
        self.search_and_download = mock.AsyncMock(return_value="downloading")
        self.qbt = SimpleNamespace(
            confirm_torrent_healthy=mock.AsyncMock(return_value=True),
            get_torrent_progress=mock.AsyncMock(return_value=None),
            remove_torrent=mock.AsyncMock(return_value=True),
            add_torrent=mock.AsyncMock(),
            add_torrent_file=mock.AsyncMock(),
        )
        self.posts = mock.AsyncMock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.torrents = pathlib.Path(tmp.name)
        for patcher in (
            *(mock.patch.object(module, "async_session", self.store.session)
              for module in (delivery, search_jobs, scheduler, review, actions)),
            mock.patch.object(settings, "_config", self.cfg),
            mock.patch.object(delivery, "client_for", lambda target, config=None: self.clients.get(target)),
            mock.patch.object(delivery, "_tvdb_id", self.tvdb),
            mock.patch.object(search_jobs, "add_direct", self.add_direct),
            mock.patch.object(search_jobs, "search_and_download", self.search_and_download),
            mock.patch.object(search_jobs, "qbittorrent", self.qbt),
            mock.patch.object(search_jobs, "_recorded_torrent_hash", mock.AsyncMock(return_value="abc")),
            mock.patch.object(scheduler, "qbt", self.qbt),
            mock.patch.object(search_service, "qbittorrent", self.qbt),
            mock.patch.object(ingest, "TORRENTS_DIR", self.torrents),
            mock.patch.dict(scheduler._unanswered, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        drain()
        self.addCleanup(drain)

    # --- fixtures ---

    def claimed(self, item_id: int = 1, *, media_type=MediaType.MOVIE, tmdb_id=TMDB_ID, **fields):
        """An approved item the worker has claimed, and the ref it carries."""
        values = dict(
            media_type=media_type, title="Some Film", year=1999, season=None,
            raw_text="Some Film (1999) 1080p WEB-DL", status=ItemStatus.SEARCHING,
            error_message=OP_APPROVAL,
        )
        values.update(fields)
        item = make_item(self.store, item_id, **values)
        if tmdb_id:
            self.store.put(ContentCheck(id=item_id, media_item_id=item_id, tmdb_id=tmdb_id))
        ref = ItemRef(
            id=item.id, title=item.title, year=item.year, quality=item.quality, season=item.season,
            media_type=item.media_type, telegram_message_id=item.telegram_message_id,
            tmdb_id=tmdb_id, external_links=list(item.external_links or []),
        )
        return item, ref

    def series(self, season: str | None = "S02", **fields):
        return self.claimed(
            media_type=MediaType.SERIES, tmdb_id=SERIES_TMDB_ID, title="Some Show", season=season, **fields,
        )

    def work(self, ref: ItemRef) -> None:
        """What the search worker does with one claimed item."""
        run(search_jobs._process_item(ref, self.posts))

    def download(self) -> Download | None:
        rows = self.store.all(Download)
        self.assertLessEqual(len(rows), 1)
        return rows[0] if rows else None

    def reasons(self, item: MediaItem) -> list[str]:
        return [r["text"] for r in item.reasons or [] if r["kind"] == delivery.REASON_KIND]

    def assert_download_client_untouched(self) -> None:
        self.add_direct.assert_not_awaited()
        self.search_and_download.assert_not_awaited()
        self.qbt.add_torrent.assert_not_awaited()
        self.qbt.add_torrent_file.assert_not_awaited()

    def poll(self) -> None:
        run(scheduler._check_downloads(self.posts))

    def approve_again(self, item: MediaItem) -> None:
        """A person approves the item, and the worker claims it."""
        self.assertTrue(run(actions.approve(item.id)).ok)
        item.status, item.error_message = ItemStatus.SEARCHING, OP_APPROVAL


# ---------------------------------------------------------------------------
# The clients, each against its fake server
# ---------------------------------------------------------------------------


class RadarrClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeRadarr()
        self.client = RadarrClient("http://radarr:7878/", API_KEY, http=self.server.http())

    def test_the_connection_test_answers_with_the_version(self) -> None:
        self.assertEqual(run(self.client.test()), "5.14.0.9383")
        self.assertEqual([c.path for c in self.server.calls], ["/api/v3/system/status"])

    def test_the_key_travels_in_a_header_and_never_in_an_address(self) -> None:
        async def everything():
            await self.client.test()
            await self.client.find(TMDB_ID)
            found = await self.client.lookup(TMDB_ID)
            added = await self.client.add(found, "/movies", await self.client.profile_id("HD-1080p"))
            await self.client.set_monitored(added, False)
            await self.client.search(added["id"])

        run(everything())
        self.assertGreater(len(self.server.calls), 5)
        for call in self.server.calls:
            self.assertEqual(call.headers["x-api-key"], API_KEY)
            self.assertNotIn(API_KEY, call.url)
            self.assertNotIn("apikey", call.url.lower())

    def test_a_refused_key_is_an_error_that_says_so_without_the_key(self) -> None:
        self.server.accepts = "another-key"
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.test())
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("refused the API key", str(caught.exception))
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_a_timeout_is_an_error_with_no_status(self) -> None:
        self.server.down = httpx.ReadTimeout
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.test())
        self.assertIsNone(caught.exception.status)
        self.assertIn("did not answer in time", str(caught.exception))

    def test_a_refused_connection_is_an_error_too(self) -> None:
        self.server.down = httpx.ConnectError
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.find(TMDB_ID))
        self.assertIn("could not be reached", str(caught.exception))

    def test_a_profile_is_found_by_name_without_case(self) -> None:
        self.assertEqual(run(self.client.profile_id("hd-1080P")), 4)

    def test_a_profile_given_as_a_number_is_the_id_and_asks_nothing(self) -> None:
        self.assertEqual(run(self.client.profile_id(7)), 7)
        self.assertEqual(self.server.calls, [])

    def test_digits_that_name_no_profile_are_the_id(self) -> None:
        self.assertEqual(run(self.client.profile_id("9")), 9)

    def test_an_unknown_profile_name_lists_the_ones_there_are(self) -> None:
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.profile_id("Ultra"))
        self.assertIn("Any, HD-1080p", str(caught.exception))

    def test_no_profile_configured_is_an_error_before_any_request(self) -> None:
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.profile_id(None))
        self.assertIn("integrations.radarr.quality_profile is not set", str(caught.exception))
        self.assertEqual(self.server.calls, [])

    def test_the_lookup_asks_by_tmdb_id(self) -> None:
        found = run(self.client.lookup(TMDB_ID))
        self.assertEqual(found["title"], "Some Film")
        self.assertEqual(self.server.calls[0].params, {"term": f"tmdb:{TMDB_ID}"})

    def test_a_lookup_that_finds_nothing_is_none(self) -> None:
        self.assertIsNone(run(self.client.lookup(999)))

    def test_the_add_carries_folder_profile_monitoring_and_the_search(self) -> None:
        found = run(self.client.lookup(TMDB_ID))
        added = run(self.client.add(found, "/movies", 4))
        [post] = self.server.sent("POST", "/api/v3/movie")
        self.assertEqual(post.body["tmdbId"], TMDB_ID)
        self.assertEqual(post.body["rootFolderPath"], "/movies")
        self.assertEqual(post.body["qualityProfileId"], 4)
        self.assertIs(post.body["monitored"], True)
        self.assertEqual(post.body["addOptions"], {"searchForMovie": True})
        self.assertEqual(post.body["minimumAvailability"], "released")
        self.assertEqual(added["id"], 11)

    def test_unmonitoring_is_a_put_and_deletes_nothing(self) -> None:
        movie = self.server.hold(has_file=False)
        run(self.client.set_monitored(movie, False))
        [put] = self.server.writes()
        self.assertEqual((put.method, put.path), ("PUT", f"/api/v3/movie/{movie['id']}"))
        self.assertIs(put.body["monitored"], False)
        self.assertIn(movie["id"], self.server.movies)

    def test_the_library_answer_has_jellyfin_s_three_shapes(self) -> None:
        self.assertIs(run(self.client.has_tmdb_id(TMDB_ID)), False)
        self.server.hold(has_file=False)
        self.assertIs(run(self.client.has_tmdb_id(TMDB_ID)), False)
        self.server.movies.clear()
        self.server.hold(has_file=True)
        self.assertIs(run(self.client.has_tmdb_id(TMDB_ID)), True)
        self.server.down = httpx.ReadTimeout
        self.assertIsNone(run(self.client.has_tmdb_id(TMDB_ID)))


class SonarrClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeSonarr()
        self.client = SonarrClient("http://sonarr:8989", API_KEY, http=self.server.http())

    def test_the_connection_test_answers_with_the_version(self) -> None:
        self.assertEqual(run(self.client.test()), "4.0.11.2680")

    def test_a_refused_key_and_a_timeout_are_errors(self) -> None:
        self.server.accepts = "another-key"
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.test())
        self.assertEqual(caught.exception.status, 401)
        self.server.accepts, self.server.down = API_KEY, httpx.ReadTimeout
        with self.assertRaises(DeliveryError):
            run(self.client.test())

    def test_the_lookup_asks_by_tvdb_id(self) -> None:
        self.assertEqual(run(self.client.lookup(TVDB_ID))["title"], "Some Show")
        self.assertEqual(self.server.calls[0].params, {"term": f"tvdb:{TVDB_ID}"})
        self.assertIsNone(run(self.client.lookup(5)))

    def test_named_seasons_are_the_only_ones_monitored(self) -> None:
        found = run(self.client.lookup(TVDB_ID))
        run(self.client.add(found, "/tv", 4, [2]))
        [post] = self.server.sent("POST", "/api/v3/series")
        self.assertEqual(
            {s["seasonNumber"]: s["monitored"] for s in post.body["seasons"]},
            {0: False, 1: False, 2: True, 3: False},
        )
        self.assertEqual(post.body["rootFolderPath"], "/tv")
        self.assertEqual(post.body["qualityProfileId"], 4)
        self.assertIs(post.body["monitored"], True)
        self.assertEqual(post.body["addOptions"], {"searchForMissingEpisodes": True})
        self.assertEqual(post.headers["x-api-key"], API_KEY)
        self.assertNotIn(API_KEY, post.url)

    def test_no_season_named_monitors_all_but_the_specials(self) -> None:
        found = run(self.client.lookup(TVDB_ID))
        run(self.client.add(found, "/tv", 4, None))
        [post] = self.server.sent("POST", "/api/v3/series")
        self.assertEqual(
            {s["seasonNumber"]: s["monitored"] for s in post.body["seasons"]},
            {0: False, 1: True, 2: True, 3: True},
        )

    def test_unmonitoring_named_seasons_leaves_the_others_and_the_series(self) -> None:
        series = self.server.hold()
        run(self.client.unmonitor(series, [2]))
        [put] = self.server.writes()
        self.assertEqual(put.method, "PUT")
        self.assertIs(put.body["monitored"], True)
        self.assertEqual(
            {s["seasonNumber"]: s["monitored"] for s in put.body["seasons"]},
            {0: False, 1: True, 2: False, 3: True},
        )

    def test_unmonitoring_the_series_too_leaves_the_other_seasons_as_they_were(self) -> None:
        series = self.server.hold()
        run(self.client.unmonitor(series, [2, 3], series_too=True))
        [put] = self.server.writes()
        self.assertIs(put.body["monitored"], False)
        self.assertEqual(
            {s["seasonNumber"]: s["monitored"] for s in put.body["seasons"]},
            {0: False, 1: True, 2: False, 3: False},
        )
        self.assertIn(series["id"], self.server.series)


class SeasonStatisticsTests(unittest.TestCase):
    """Completion is read from Sonarr's own season statistics."""

    def setUp(self) -> None:
        self.server = FakeSonarr()

    def test_a_season_with_every_aired_episode_is_complete(self) -> None:
        series = self.server.hold(files={2: fakes.EPISODES})
        self.assertTrue(seasons_complete(series, [2]))

    def test_a_season_one_episode_short_is_not(self) -> None:
        series = self.server.hold(files={2: fakes.EPISODES - 1})
        self.assertFalse(seasons_complete(series, [2]))

    def test_the_whole_series_needs_every_season(self) -> None:
        series = self.server.hold(files={1: fakes.EPISODES, 2: fakes.EPISODES})
        self.assertFalse(seasons_complete(series, None))
        self.server.set_files(series["id"], 3, fakes.EPISODES)
        self.assertTrue(seasons_complete(series, None))

    def test_the_specials_are_never_waited_for(self) -> None:
        series = self.server.hold(files={1: fakes.EPISODES, 2: fakes.EPISODES, 3: fakes.EPISODES})
        self.assertTrue(seasons_complete(series, None))

    def test_an_unmonitored_season_with_some_files_is_not_complete(self) -> None:
        """Sonarr counts only the episodes it has files for there, so its own
        count would read as complete: the total is what it is measured against."""
        series = self.server.hold(monitored_seasons=(1,), files={2: 3})
        self.assertEqual(series["seasons"][2]["statistics"]["episodeCount"], 3)
        self.assertFalse(seasons_complete(series, [2]))

    def test_a_season_that_has_not_aired_is_left_out(self) -> None:
        series = self.server.hold(files={1: fakes.EPISODES, 2: fakes.EPISODES, 3: 0})
        series["seasons"][3]["statistics"].update(episodeCount=0, totalEpisodeCount=0)
        self.assertTrue(seasons_complete(series, None))

    def test_nothing_to_measure_is_not_complete(self) -> None:
        series = self.server.hold()
        self.assertFalse(seasons_complete(series, [9]))


class SeerrClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeSeerr()
        self.client = SeerrClient("http://seerr:5055", API_KEY, 2, http=self.server.http())

    def test_the_connection_test_needs_the_key(self) -> None:
        self.assertEqual(run(self.client.test()), "3.5.0")
        self.assertEqual(self.server.calls[0].path, "/api/v1/settings/about")
        self.server.accepts = "another-key"
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.test())
        self.assertEqual(caught.exception.status, 401)

    def test_a_timeout_is_an_error(self) -> None:
        self.server.down = httpx.ReadTimeout
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.media("movie", TMDB_ID))
        self.assertIn("did not answer in time", str(caught.exception))

    def test_a_request_goes_as_the_configured_user(self) -> None:
        made = run(self.client.request("movie", TMDB_ID))
        [post] = self.server.sent("POST", "/api/v1/request")
        self.assertEqual(post.headers["x-api-key"], API_KEY)
        self.assertEqual(post.headers["x-api-user"], "2")
        self.assertNotIn(API_KEY, post.url)
        self.assertEqual(post.body, {"mediaType": "movie", "mediaId": TMDB_ID})
        # As that user it waits for Seerr's own approval; as the admin it would not.
        self.assertEqual(made["status"], fakes.REQUEST_PENDING)
        self.assertEqual(made["requestedBy"]["id"], 2)

    def test_without_a_user_nothing_is_sent(self) -> None:
        client = SeerrClient("http://seerr:5055", API_KEY, None, http=self.server.http())
        with self.assertRaises(DeliveryError) as caught:
            run(client.request("movie", TMDB_ID))
        self.assertEqual(str(caught.exception), NO_USER)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.server.requests, {})

    def test_a_series_request_carries_the_tvdb_id_and_its_seasons(self) -> None:
        run(self.client.request("tv", SERIES_TMDB_ID, tvdb_id=TVDB_ID, seasons=[1, 2]))
        [post] = self.server.sent("POST")
        self.assertEqual(
            post.body, {"mediaType": "tv", "mediaId": SERIES_TMDB_ID, "tvdbId": TVDB_ID, "seasons": [1, 2]},
        )

    def test_a_series_with_no_season_named_asks_for_all(self) -> None:
        run(self.client.request("tv", SERIES_TMDB_ID, tvdb_id=TVDB_ID))
        self.assertEqual(self.server.sent("POST")[0].body["seasons"], "all")

    def test_a_request_seerr_already_has_is_not_an_error(self) -> None:
        self.server.add_request("movie", TMDB_ID, user=5)
        self.assertIsNone(run(self.client.request("movie", TMDB_ID)))
        self.server.add_request("tv", SERIES_TMDB_ID, user=5, seasons=[1])
        self.assertIsNone(run(self.client.request("tv", SERIES_TMDB_ID, seasons=[1])))

    def test_media_of_a_title_seerr_has_never_seen_is_empty(self) -> None:
        self.assertEqual(run(self.client.media("movie", TMDB_ID)), {})

    def test_a_write_passes_seerr_s_csrf_protection(self) -> None:
        # Seen live: with CSRF Protection on, Seerr refused every approve
        # "403 invalid csrf token", so no request the policy cleared got through.
        self.server.csrf = True
        pending = self.server.add_request("movie", TMDB_ID, user=5)
        run(self.client.approve(pending["id"]))
        self.assertEqual(self.server.requests[pending["id"]]["status"], fakes.REQUEST_APPROVED)
        first, read, again = self.server.calls
        self.assertEqual((first.method, read.method, again.method), ("POST", "GET", "POST"))
        self.assertNotIn("x-xsrf-token", first.headers)
        self.assertEqual(again.headers["x-xsrf-token"], fakes.CSRF_TOKEN)
        self.assertEqual(again.headers["x-api-key"], API_KEY)

    def test_csrf_cookies_marked_secure_still_go_back_over_http(self) -> None:
        # Seen live on Seerr 3.5.1: both cookies came back Secure on a plain
        # http answer, the jar kept _csrf to itself, and every approve was
        # refused "403 invalid csrf token" again on the retry.
        self.server.csrf = True
        pending = self.server.add_request("movie", TMDB_ID, user=5)
        run(self.client.approve(pending["id"]))
        self.assertEqual(self.server.requests[pending["id"]]["status"], fakes.REQUEST_APPROVED)
        again = self.server.calls[-1]
        self.assertIn(f"{fakes.CSRF_SECRET_COOKIE}={fakes.CSRF_SECRET}", again.headers["cookie"])
        self.assertIn(f"{fakes.CSRF_COOKIE}={fakes.CSRF_TOKEN}", again.headers["cookie"])

    def test_csrf_cookies_without_secure_still_pass(self) -> None:
        # Seerr 3.5.0 and before: the same cookies, not marked Secure.
        self.server.csrf, self.server.csrf_secure = True, False
        pending = self.server.add_request("movie", TMDB_ID, user=5)
        run(self.client.approve(pending["id"]))
        self.assertEqual(self.server.requests[pending["id"]]["status"], fakes.REQUEST_APPROVED)

    def test_a_csrf_refusal_with_no_secret_cookie_is_an_error(self) -> None:
        self.server.csrf = True
        pending = self.server.add_request("movie", TMDB_ID, user=5)
        with mock.patch.object(fakes, "CSRF_SECRET_COOKIE", "other"):
            with self.assertRaises(DeliveryError) as caught:
                run(self.client.approve(pending["id"]))
        self.assertIn("sent no _csrf cookie", str(caught.exception))
        self.assertNotIn(fakes.CSRF_TOKEN, str(caught.exception))
        self.assertEqual(self.server.requests[pending["id"]]["status"], fakes.REQUEST_PENDING)

    def test_a_request_through_csrf_still_goes_as_the_user(self) -> None:
        self.server.csrf = True
        made = run(self.client.request("movie", TMDB_ID))
        self.assertEqual(made["requestedBy"]["id"], 2)
        self.assertEqual(self.server.sent("POST", "/api/v1/request")[-1].headers["x-api-user"], "2")

    def test_decline_and_withdraw_pass_csrf_too(self) -> None:
        self.server.csrf = True
        declined = self.server.add_request("movie", TMDB_ID, user=5)
        run(self.client.decline(declined["id"]))
        self.assertEqual(self.server.requests[declined["id"]]["status"], fakes.REQUEST_DECLINED)
        mine = self.server.add_request("tv", SERIES_TMDB_ID, user=2, seasons=[1])
        run(self.client.withdraw(mine["id"]))
        self.assertNotIn(mine["id"], self.server.requests)

    def test_without_csrf_a_write_is_sent_once(self) -> None:
        pending = self.server.add_request("movie", TMDB_ID, user=5)
        run(self.client.approve(pending["id"]))
        self.assertEqual([c.method for c in self.server.calls], ["POST"])

    def test_another_403_is_not_retried(self) -> None:
        pending = self.server.add_request("movie", TMDB_ID, user=5)
        client = SeerrClient("http://seerr:5055", API_KEY, 2, http=self.server.http())
        with self.assertRaises(DeliveryError) as caught:
            run(client._write("POST", f"/api/v1/request/{pending['id']}/approve", headers={"X-API-User": "2"}))
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.server.calls), 1)


class TvdbIdTests(unittest.TestCase):
    """The TVDB id Sonarr and Seerr know a series by comes from TMDB's external ids."""

    def _client(self, handler) -> TMDBClient:
        client = TMDBClient()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    def test_it_is_read_from_external_ids(self) -> None:
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"id": SERIES_TMDB_ID, "imdb_id": "tt1", "tvdb_id": TVDB_ID})

        self.assertEqual(run(self._client(handler).tvdb_id(SERIES_TMDB_ID)), TVDB_ID)
        self.assertEqual(seen, [f"/3/tv/{SERIES_TMDB_ID}/external_ids"])

    def test_a_series_tmdb_lists_none_for_is_none(self) -> None:
        client = self._client(lambda request: httpx.Response(200, json={"tvdb_id": None}))
        self.assertIsNone(run(client.tvdb_id(SERIES_TMDB_ID)))

    def test_a_tmdb_failure_never_reaches_the_log_with_its_address(self) -> None:
        tmdb = SimpleNamespace(tvdb_id=mock.AsyncMock(side_effect=RuntimeError("https://x/?api_key=SECRET")))
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with mock.patch.object(delivery, "_tmdb", tmdb), self.assertLogs(delivery.log, "WARNING") as logs:
            with self.assertRaises(DeliveryError):
                run(delivery._tvdb_id(SERIES_TMDB_ID))
            self.assertIsNone(run(delivery._tvdb_id(SERIES_TMDB_ID, required=False)))
        self.assertNotIn("SECRET", "\n".join(logs.output))


class ClientForTests(unittest.TestCase):
    def test_each_enabled_integration_gets_its_client_once(self) -> None:
        cfg = config({"movie": "radarr"})
        with mock.patch.dict(delivery._clients, clear=True):
            radarr = delivery.client_for("radarr", cfg)
            self.assertIsInstance(radarr, RadarrClient)
            self.assertIs(delivery.client_for("radarr", cfg), radarr)
            self.assertIsInstance(delivery.client_for("sonarr", cfg), SonarrClient)
            self.assertIsInstance(delivery.client_for("seerr", cfg), SeerrClient)

    def test_a_disabled_integration_and_the_download_client_have_none(self) -> None:
        cfg = Config.model_validate({"integrations": {"radarr": {"enabled": False}}})
        self.assertIsNone(delivery.client_for("radarr", cfg))
        self.assertIsNone(delivery.client_for("sonarr", cfg))
        self.assertIsNone(delivery.client_for("qbittorrent", config({})))

    def test_the_setup_page_s_connection_test_goes_through_the_client(self) -> None:
        server = FakeSonarr()
        client = SonarrClient("http://sonarr:8989", API_KEY, http=server.http())
        with mock.patch.object(delivery, "client_for", return_value=client):
            self.assertEqual(run(delivery.test_connection("sonarr", config({}))), "4.0.11.2680")
        with mock.patch.object(delivery, "client_for", return_value=None):
            with self.assertRaises(DeliveryError):
                run(delivery.test_connection("sonarr", config({})))


# ---------------------------------------------------------------------------
# The map is read
# ---------------------------------------------------------------------------


class TargetTests(unittest.TestCase):
    def test_a_listed_type_goes_where_the_map_says(self) -> None:
        cfg = config({"movie": "radarr", "series": "seerr", "ebook": "qbittorrent", "music": "none"})
        self.assertEqual(delivery.target_for(MediaType.MOVIE, cfg), "radarr")
        self.assertEqual(delivery.target_for(MediaType.SERIES, cfg), "seerr")
        self.assertEqual(delivery.target_for(MediaType.EBOOK, cfg), "qbittorrent")
        self.assertEqual(delivery.target_for(MediaType.MUSIC, cfg), "none")

    def test_a_type_not_listed_goes_nowhere(self) -> None:
        self.assertEqual(delivery.target_for(MediaType.COMIC, config({"movie": "radarr"})), "none")

    def test_an_unread_type_goes_to_the_download_client_only_when_something_does(self) -> None:
        self.assertEqual(delivery.target_for(MediaType.UNKNOWN, config({"ebook": "qbittorrent"})), "qbittorrent")
        self.assertEqual(delivery.target_for(MediaType.UNKNOWN, config({"movie": "radarr"})), "none")
        self.assertEqual(delivery.target_for(MediaType.UNKNOWN, config({})), "none")

    def test_the_seasons_a_post_names(self) -> None:
        self.assertEqual(delivery.seasons_of("S02"), [2])
        self.assertEqual(delivery.seasons_of("S01E03"), [1])
        self.assertEqual(delivery.seasons_of("S01-S03"), [1, 2, 3])
        self.assertEqual(delivery.seasons_of("S01E01-E03"), [1])
        self.assertEqual(delivery.seasons_of("S00"), [0])
        self.assertIsNone(delivery.seasons_of(None))
        self.assertIsNone(delivery.seasons_of(""))


class DownloadClientPathTests(DeliveryCase):
    """A type delivered to qbittorrent keeps exactly the path it had."""

    DELIVER = {"movie": "qbittorrent"}

    def test_the_post_s_own_torrent_is_tried_then_the_search(self) -> None:
        _item, ref = self.claimed()
        self.work(ref)
        self.add_direct.assert_awaited_once_with(ref)
        self.search_and_download.assert_awaited_once_with(ref)
        self.posts.after.assert_awaited_once_with("download", 1)
        self.assertEqual(self.radarr.calls + self.sonarr.calls + self.seerr.calls, [])

    def test_a_direct_add_skips_the_search(self) -> None:
        self.add_direct.return_value = "downloading"
        _item, ref = self.claimed()
        self.work(ref)
        self.search_and_download.assert_not_awaited()

    def test_nothing_about_the_item_is_written_by_the_routing(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.SEARCHING)
        self.assertIsNone(item.reasons)
        self.assertIsNone(self.download())

    def test_no_match_and_error_come_back_as_they_were(self) -> None:
        for result in ("no_match", "error"):
            with self.subTest(result=result):
                self.search_and_download.return_value = result
                _item, ref = self.claimed()
                self.assertEqual(
                    run(delivery.deliver(ref, self.cfg, download_client=search_jobs._to_download_client)),
                    result,
                )

    def test_an_unread_type_a_person_approved_still_downloads(self) -> None:
        _item, ref = self.claimed(media_type=MediaType.UNKNOWN, tmdb_id=None)
        self.work(ref)
        self.search_and_download.assert_awaited_once_with(ref)

    def test_the_hand_over_and_a_failure_are_events(self) -> None:
        _item, ref = self.claimed()
        self.work(ref)
        self.search_and_download.return_value = "error"
        self.work(ref)
        self.assertEqual(
            [(kind, payload["target"]) for kind, _id, payload in drain()],
            [(events.DELIVERED, "qbittorrent"), (events.DELIVERY_FAILED, "qbittorrent")],
        )


class NotDeliveredTests(DeliveryCase):
    DELIVER = {"movie": "none"}

    def test_a_type_mapped_to_none_is_recorded_and_nothing_is_fetched(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        self.assert_download_client_untouched()
        self.assertEqual(self.radarr.calls + self.sonarr.calls + self.seerr.calls, [])
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertIsNone(item.error_message)
        self.assertEqual(self.reasons(item), ["not delivered: deliver says none for movie"])
        self.assertIsNone(self.download())
        [(kind, item_id, payload)] = drain()
        self.assertEqual((kind, item_id, payload["target"]), (events.COMPLETED, 1, "none"))
        self.posts.after.assert_not_awaited()

    def test_a_type_not_listed_comes_back_to_a_person(self) -> None:
        """It used to be completed for good: not approvable again, not
        rejectable, and a duplicate for every later post of the title."""
        item, ref = self.claimed(media_type=MediaType.SERIES)
        kept = self.torrents / "1.torrent"
        kept.write_bytes(b"d4:infod4:name1:xee")
        self.work(ref)
        self.assert_download_client_untouched()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn(item.status, REVIEWABLE_STATUSES)
        self.assertIn("deliver lists no target for series", item.error_message)
        self.assertEqual(self.reasons(item), [])
        self.assertEqual(
            [(kind, payload["target"]) for kind, _id, payload in drain()],
            [(events.DELIVERY_FAILED, "none"), (events.NEEDS_YOU, "none")],
        )
        self.posts.after.assert_not_awaited()
        # Listing the type later must still find what the post carried.
        self.assertTrue(kept.exists())

    def test_it_is_delivered_once_the_map_lists_its_type(self) -> None:
        item, ref = self.claimed(media_type=MediaType.SERIES)
        self.work(ref)
        self.cfg.deliver["series"] = "qbittorrent"
        self.approve_again(item)
        self.work(ref)
        self.search_and_download.assert_awaited_once_with(ref)

    def test_an_unread_type_comes_back_too_when_nothing_goes_to_the_download_client(self) -> None:
        item, ref = self.claimed(media_type=MediaType.UNKNOWN, tmdb_id=None)
        self.work(ref)
        self.assert_download_client_untouched()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("its type could not be read", item.error_message)

    def test_the_reasons_the_policy_gave_are_kept(self) -> None:
        item, ref = self.claimed(reasons=[{"kind": "clear", "rule": None, "text": "nothing hit"}])
        self.work(ref)
        self.assertEqual([r["kind"] for r in item.reasons], ["clear", "delivery"])

    def test_a_row_from_before_structured_reasons_keeps_its_words(self) -> None:
        item, ref = self.claimed(reasons=None, auto_reasons=["approved by hand"])
        self.work(ref)
        self.assertEqual(
            [(r["kind"], r["text"]) for r in item.reasons][0], ("legacy", "approved by hand"),
        )

    def test_the_post_s_kept_torrent_is_removed(self) -> None:
        _item, ref = self.claimed()
        kept = self.torrents / "1.torrent"
        kept.write_bytes(b"d4:infod4:name1:xee")
        self.work(ref)
        self.assertFalse(kept.exists())


# ---------------------------------------------------------------------------
# Radarr
# ---------------------------------------------------------------------------


class RadarrDeliveryTests(DeliveryCase):
    def test_a_new_movie_is_added_and_the_item_waits_on_it(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        self.assert_download_client_untouched()
        [post] = self.radarr.sent("POST", "/api/v3/movie")
        self.assertEqual(post.body["tmdbId"], TMDB_ID)
        self.assertEqual(post.body["rootFolderPath"], "/movies")
        self.assertIs(post.body["monitored"], True)
        self.assertEqual(post.body["addOptions"], {"searchForMovie": True})
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertIsNone(item.error_message)
        dl = self.download()
        self.assertEqual((dl.delivery_target, dl.remote_id), ("radarr", "11"))
        self.assertEqual(dl.status, DownloadStatus.DOWNLOADING)
        self.assertTrue(dl.created_torrent)
        self.assertIsNone(dl.qbittorrent_hash)
        [(kind, item_id, payload)] = drain()
        self.assertEqual((kind, item_id), (events.DELIVERED, 1))
        self.assertEqual((payload["target"], payload["remote_id"]), ("radarr", "11"))

    def test_the_profile_named_in_the_config_is_sent_as_its_id(self) -> None:
        _item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.radarr.sent("GET", "/api/v3/qualityprofile")[0].params, {})
        self.assertEqual(self.radarr.sent("POST", "/api/v3/movie")[0].body["qualityProfileId"], 4)

    def test_the_post_is_left_until_radarr_has_the_file(self) -> None:
        """on_download ran at the hand-over, while Radarr had found nothing yet."""
        _item, ref = self.claimed()
        self.work(ref)
        self.posts.after.assert_not_awaited()
        # The download client is not asked to confirm a torrent it never got.
        self.qbt.confirm_torrent_healthy.assert_not_awaited()
        self.poll()
        self.posts.after.assert_not_awaited()
        self.radarr.movies[11]["hasFile"] = True
        self.poll()
        self.posts.after.assert_awaited_once_with("download", 1)

    def test_a_movie_already_there_without_a_file_is_a_success(self) -> None:
        held = self.radarr.hold(has_file=False, monitored=True)
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.radarr.writes(), [])
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual(dl.remote_id, str(held["id"]))
        # Radarr was already looking for it: nothing there is this item's.
        self.assertFalse(dl.created_torrent)

    def test_a_movie_there_and_unmonitored_is_monitored_and_searched(self) -> None:
        held = self.radarr.hold(has_file=False, monitored=False)
        _item, ref = self.claimed()
        self.work(ref)
        put, command = self.radarr.writes()
        self.assertEqual((put.method, put.body["monitored"]), ("PUT", True))
        self.assertEqual(command.body, {"name": "MoviesSearch", "movieIds": [held["id"]]})
        self.assertTrue(self.download().created_torrent)

    def test_a_movie_already_there_with_its_file_completes_the_item(self) -> None:
        self.radarr.hold(has_file=True)
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.radarr.writes(), [])
        self.assert_download_client_untouched()
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertEqual(self.reasons(item), ["already in the library (Radarr)"])
        self.assertIsNone(self.download())
        [(kind, _id, payload)] = drain()
        self.assertEqual((kind, payload["target"]), (events.COMPLETED, "radarr"))
        self.posts.after.assert_awaited_once_with("download", 1)

    def test_a_movie_added_between_the_two_calls_is_a_success_too(self) -> None:
        """Radarr answers 400 to a movie it already has."""
        held = self.radarr.hold(has_file=False)
        self.radarr.hide_first_find = True
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(len(self.radarr.sent("POST", "/api/v3/movie")), 1)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(self.download().remote_id, str(held["id"]))

    def _assert_needs_a_person(self, item: MediaItem, fragment: str) -> None:
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn(fragment, item.error_message)
        self.assertTrue(item.error_message.startswith("Radarr delivery failed: "))
        self.assertIsNone(self.download())
        kinds = [(kind, item_id) for kind, item_id, _payload in drain()]
        self.assertEqual(kinds, [(events.DELIVERY_FAILED, 1), (events.NEEDS_YOU, 1)])
        self.posts.after.assert_not_awaited()
        self.assert_download_client_untouched()

    def test_a_lookup_that_finds_nothing_needs_a_person(self) -> None:
        self.radarr.catalog.clear()
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.radarr.sent("POST"), [])
        self._assert_needs_a_person(item, f"finds no movie for TMDB id {TMDB_ID}")

    def test_a_refused_key_needs_a_person_and_never_shows_the_key(self) -> None:
        self.radarr.accepts = "another-key"
        item, ref = self.claimed()
        self.work(ref)
        self._assert_needs_a_person(item, "refused the API key (HTTP 401)")
        self.assertNotIn(API_KEY, item.error_message)

    def test_a_timeout_needs_a_person(self) -> None:
        self.radarr.down = httpx.ReadTimeout
        item, ref = self.claimed()
        self.work(ref)
        self._assert_needs_a_person(item, "did not answer in time")

    def test_an_item_with_no_tmdb_id_is_not_sent(self) -> None:
        item, ref = self.claimed(tmdb_id=None)
        self.work(ref)
        self.assertEqual(self.radarr.calls, [])
        self._assert_needs_a_person(item, "no TMDB id")

    DOUBTED = {"_identity": {"ok": False, "text": "TMDB matched a 2019 film"}}

    def _approved_by_the_policy(self, **fields):
        """An item the policy approved by itself: both stamps are its own."""
        decided = datetime(2026, 10, 1, tzinfo=timezone.utc)
        return self.claimed(
            auto_decision=AutoDecision.AUTO_APPROVE, auto_decided_at=decided, reviewed_at=decided, **fields,
        )

    def test_a_tmdb_match_the_policy_doubts_is_not_sent_on_the_policy_s_own_approval(self) -> None:
        """The download client is searched by the post's title; Radarr gets only the id."""
        item, ref = self._approved_by_the_policy(signals=self.DOUBTED)
        self.work(ref)
        self.assertEqual(self.radarr.calls, [])
        self._assert_needs_a_person(item, "TMDB matched a 2019 film")

    def test_a_person_s_approval_sends_the_match_the_policy_doubted(self) -> None:
        """The policy held it for a person to decide; the delivery refused that decision too."""
        item, ref = self.claimed(signals=self.DOUBTED, auto_decision=AutoDecision.NEEDS_REVIEW)
        self.work(ref)
        self.assertEqual(len(self.radarr.sent("POST", "/api/v3/movie")), 1)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(
            self.reasons(item),
            ["sent on a person's approval: the policy doubted the TMDB match (TMDB matched a 2019 film)"],
        )

    def test_a_person_overrules_the_refusal_of_the_policy_s_own_approval(self) -> None:
        item, ref = self._approved_by_the_policy(signals=self.DOUBTED)
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.approve_again(item)
        self.work(ref)
        self.assertEqual(len(self.radarr.sent("POST", "/api/v3/movie")), 1)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)

    def test_the_card_says_which_match_approving_sends(self) -> None:
        item, _ref = self.claimed(signals=self.DOUBTED, status=ItemStatus.PENDING_REVIEW)
        self.assertIn(
            "The policy doubts the TMDB match (TMDB matched a 2019 film); approving sends that match.",
            delivery.where(item),
        )

    def test_a_missing_root_folder_needs_a_person_before_anything_is_added(self) -> None:
        self.cfg.integrations.radarr.root_folder = None
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.radarr.writes(), [])
        self._assert_needs_a_person(item, "integrations.radarr.root_folder is not set")

    def test_an_item_that_failed_can_be_delivered_again(self) -> None:
        self.radarr.down = httpx.ReadTimeout
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.radarr.down = None
        item.status, item.error_message = ItemStatus.SEARCHING, OP_APPROVAL
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)


class DirectAddToAnArrTests(DeliveryCase):
    """A post's own torrent cannot be handed to a target that takes a title."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config(self.DELIVER, sources=[{
            "key": "inbox", "kind": "bot", "chat": "private", "direct_adds": "anyone",
        }], telegram={"bot_token": "123:abc"})
        patcher = mock.patch.object(settings, "_config", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_magnet_is_delivered_by_title_and_the_item_says_so(self) -> None:
        item, ref = self.claimed(raw_text=MAGNET, source_key="inbox", sender_id=5)
        self.assertEqual(ingest.direct_add_of(item, self.cfg), "magnet")
        self.work(ref)
        self.assert_download_client_untouched()
        self.assertEqual(len(self.radarr.sent("POST", "/api/v3/movie")), 1)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(
            self.reasons(item),
            ["delivered by title: Radarr picks its own release, not the magnet this post carries"],
        )
        [(_kind, _id, payload)] = drain()
        self.assertIs(payload["by_title"], True)

    def test_a_torrent_file_is_delivered_by_title_and_kept_until_the_files_are_there(self) -> None:
        """It was removed at the hand-over, while the delivery could still fail."""
        item, ref = self.claimed(source_key="inbox", sender_id=5)
        kept = self.torrents / "1.torrent"
        kept.write_bytes(b"d4:infod4:name1:xee")
        self.work(ref)
        self.assert_download_client_untouched()
        self.assertIn("torrent file this post carries", self.reasons(item)[0])
        self.assertTrue(kept.exists())
        self.radarr.movies[11]["hasFile"] = True
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertFalse(kept.exists())

    def test_a_post_without_its_own_torrent_carries_no_such_reason(self) -> None:
        item, ref = self.claimed(source_key="inbox", sender_id=5)
        self.work(ref)
        self.assertEqual(self.reasons(item), [])

    def test_the_page_says_so_before_the_item_is_approved(self) -> None:
        item, _ref = self.claimed(
            raw_text=MAGNET, source_key="inbox", sender_id=5, status=ItemStatus.PENDING_REVIEW,
        )
        line = delivery.where(item)
        self.assertIn("Approving sends the title to Radarr", line)
        self.assertIn("magnet this post carries is not what downloads", line)

    def test_the_review_card_no_longer_promises_the_post_s_own_torrent(self) -> None:
        """The card said approving downloads the magnet as it is. Radarr never gets it."""
        from vettarr.bot import handlers

        item, _ref = self.claimed(
            raw_text=MAGNET, source_key="inbox", sender_id=5, status=ItemStatus.PENDING_REVIEW,
        )
        bot = mock.AsyncMock()
        with mock.patch.object(handlers, "_bot_app", SimpleNamespace(bot=bot)), \
                mock.patch.object(handlers, "async_session", self.store.session):
            run(handlers._send_review(5, item))
        card = bot.send_message.await_args.kwargs["text"]
        self.assertIn("<b>Delivery:</b> Approving sends the title to Radarr", card)
        self.assertNotIn("Direct add", card)
        self.assertNotIn("Nothing is searched", card)


# ---------------------------------------------------------------------------
# Sonarr
# ---------------------------------------------------------------------------


class SonarrDeliveryTests(DeliveryCase):
    def test_a_new_series_is_added_with_the_season_the_post_names(self) -> None:
        item, ref = self.series("S02")
        self.work(ref)
        self.tvdb.assert_awaited_once_with(SERIES_TMDB_ID)
        self.assertEqual(self.sonarr.sent("GET", "/api/v3/series/lookup")[0].params, {"term": f"tvdb:{TVDB_ID}"})
        [post] = self.sonarr.sent("POST", "/api/v3/series")
        self.assertEqual(
            {s["seasonNumber"]: s["monitored"] for s in post.body["seasons"]},
            {0: False, 1: False, 2: True, 3: False},
        )
        self.assertEqual(post.body["addOptions"], {"searchForMissingEpisodes": True})
        self.assertEqual(post.body["qualityProfileId"], 4)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual((dl.delivery_target, dl.remote_id, dl.created_torrent), ("sonarr", "21", True))
        self.assertEqual(dl.remote_changes, {"series": True, "seasons": [2]})
        self.assert_download_client_untouched()

    def test_an_episode_post_monitors_its_season(self) -> None:
        _item, ref = self.series("S01E03")
        self.work(ref)
        seasons = self.sonarr.sent("POST", "/api/v3/series")[0].body["seasons"]
        self.assertEqual([s["seasonNumber"] for s in seasons if s["monitored"]], [1])

    def test_no_season_named_monitors_the_whole_series(self) -> None:
        _item, ref = self.series(None)
        self.work(ref)
        seasons = self.sonarr.sent("POST", "/api/v3/series")[0].body["seasons"]
        self.assertEqual([s["seasonNumber"] for s in seasons if s["monitored"]], [1, 2, 3])

    def test_a_series_there_with_the_season_complete_completes_the_item(self) -> None:
        self.sonarr.hold(files={2: fakes.EPISODES})
        item, ref = self.series("S02")
        self.work(ref)
        self.assertEqual(self.sonarr.writes(), [])
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertEqual(self.reasons(item), ["already in the library (Sonarr)"])

    def test_a_series_there_and_monitored_without_files_is_waited_on(self) -> None:
        held = self.sonarr.hold()
        item, ref = self.series("S02")
        self.work(ref)
        self.assertEqual(self.sonarr.writes(), [])
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual((dl.remote_id, dl.created_torrent), (str(held["id"]), False))

    def test_a_series_there_with_the_season_unmonitored_gets_it_monitored_and_searched(self) -> None:
        held = self.sonarr.hold(monitored_seasons=(1,))
        _item, ref = self.series("S02")
        self.work(ref)
        put, command = self.sonarr.writes()
        self.assertEqual(
            {s["seasonNumber"]: s["monitored"] for s in put.body["seasons"]},
            {0: False, 1: True, 2: True, 3: False},
        )
        self.assertEqual(command.body, {"name": "SeriesSearch", "seriesId": held["id"]})
        self.assertTrue(self.download().created_torrent)
        # The series flag was on already; season 2 is all this delivery turned on.
        self.assertEqual(self.download().remote_changes, {"series": False, "seasons": [2]})

    def test_a_series_added_between_the_two_calls_is_a_success_too(self) -> None:
        """Sonarr answers 400 to a series it already has, as Radarr does to a movie."""
        held = self.sonarr.hold()
        self.sonarr.hide_first_find = True
        item, ref = self.series("S02")
        self.work(ref)
        self.assertEqual(len(self.sonarr.sent("POST", "/api/v3/series")), 1)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual((dl.remote_id, dl.created_torrent), (str(held["id"]), False))

    def test_the_specials_can_be_asked_for_by_name(self) -> None:
        """S00 was refused with "Sonarr lists no season 0", which it does list."""
        item, ref = self.series("S00")
        self.work(ref)
        [post] = self.sonarr.sent("POST", "/api/v3/series")
        self.assertEqual([s["seasonNumber"] for s in post.body["seasons"] if s["monitored"]], [0])
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)

    def test_a_season_sonarr_does_not_list_needs_a_person(self) -> None:
        item, ref = self.series("S09")
        self.work(ref)
        self.assertEqual(self.sonarr.sent("POST"), [])
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("Sonarr lists no season 9", item.error_message)

    def test_no_tvdb_id_needs_a_person(self) -> None:
        self.tvdb.side_effect = DeliveryError("TMDB gave no TVDB id for this series")
        item, ref = self.series("S02")
        self.work(ref)
        self.assertEqual(self.sonarr.calls, [])
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("no TVDB id", item.error_message)

    def test_a_lookup_that_finds_nothing_a_refused_key_and_a_timeout_need_a_person(self) -> None:
        for name, breakage, fragment in (
            ("nothing found", lambda: self.sonarr.catalog.clear(), f"finds no series for TVDB id {TVDB_ID}"),
            ("401", lambda: setattr(self.sonarr, "accepts", "another-key"), "refused the API key"),
            ("timeout", lambda: setattr(self.sonarr, "down", httpx.ReadTimeout), "did not answer in time"),
        ):
            with self.subTest(name):
                breakage()
                item, ref = self.series("S02")
                self.work(ref)
                self.assertEqual(item.status, ItemStatus.ERROR)
                self.assertIn(fragment, item.error_message)
                self.assertEqual(self.sonarr.sent("POST"), [])


# ---------------------------------------------------------------------------
# Seerr
# ---------------------------------------------------------------------------


class SeerrDeliveryTests(DeliveryCase):
    DELIVER = {"movie": "seerr", "series": "seerr"}

    def test_a_movie_is_requested_as_the_configured_user(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        [post] = self.seerr.sent("POST", "/api/v1/request")
        self.assertEqual(post.body, {"mediaType": "movie", "mediaId": TMDB_ID})
        self.assertEqual(post.headers["x-api-key"], API_KEY)
        self.assertEqual(post.headers["x-api-user"], "2")
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual((dl.delivery_target, dl.remote_id, dl.created_torrent), ("seerr", "31", True))
        self.assertEqual(self.seerr.requests[31]["status"], fakes.REQUEST_PENDING)
        self.assert_download_client_untouched()
        # The request still waits for Seerr's own approval: the post stays.
        self.posts.after.assert_not_awaited()

    def test_a_declined_request_leaves_the_post_and_its_torrent(self) -> None:
        """on_download: delete used to remove the post before Seerr had said yes."""
        item, ref = self.claimed()
        kept = self.torrents / "1.torrent"
        kept.write_bytes(b"d4:infod4:name1:xee")
        self.work(ref)
        self.seerr.set_request(31, fakes.REQUEST_DECLINED)
        self.poll()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.posts.after.assert_not_awaited()
        self.assertTrue(kept.exists())

    def test_a_series_seerr_takes_no_request_for_needs_a_person(self) -> None:
        """Seerr answers 202 when every season asked for already has a status
        there, with or without a request. That read as "somebody requested it"."""
        info = self.seerr.info("tv", SERIES_TMDB_ID)
        info["status"] = fakes.MEDIA_PARTIAL
        info["seasons"] = [
            {"seasonNumber": 1, "status": fakes.MEDIA_AVAILABLE},
            {"seasonNumber": 2, "status": fakes.MEDIA_PARTIAL},
            {"seasonNumber": 3, "status": fakes.MEDIA_PARTIAL},
        ]
        item, ref = self.series(None)
        self.work(ref)
        self.assertEqual(len(self.seerr.sent("POST", "/api/v1/request")), 1)
        self.assertEqual(self.seerr.requests, {})
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("Seerr took no request", item.error_message)
        self.assertIsNone(self.download())
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DELIVERY_FAILED, events.NEEDS_YOU])
        self.posts.after.assert_not_awaited()

    def test_a_request_for_the_seasons_asked_is_waited_on(self) -> None:
        theirs = self.seerr.add_request("tv", SERIES_TMDB_ID, user=5, seasons=[2])
        item, ref = self.series("S02")
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual((dl.remote_id, dl.created_torrent), (str(theirs["id"]), False))

    def test_a_request_for_other_seasons_is_not_this_item_s_to_wait_on(self) -> None:
        self.seerr.add_request("tv", SERIES_TMDB_ID, user=5, seasons=[1])
        info = self.seerr.info("tv", SERIES_TMDB_ID)
        info["seasons"] = [{"seasonNumber": 2, "status": fakes.MEDIA_PARTIAL}]
        item, ref = self.series("S02")
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIsNone(self.download())

    def test_a_completed_request_is_not_one_to_wait_on(self) -> None:
        self.seerr.add_request("tv", SERIES_TMDB_ID, user=5, status=fakes.REQUEST_COMPLETED, seasons=[1, 2, 3])
        info = self.seerr.info("tv", SERIES_TMDB_ID)
        info["status"] = fakes.MEDIA_PARTIAL
        info["seasons"] = [{"seasonNumber": n, "status": fakes.MEDIA_PARTIAL} for n in (1, 2, 3)]
        item, ref = self.series(None)
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("Seerr took no request", item.error_message)
        self.assertIsNone(self.download())

    def test_a_series_is_requested_with_its_tvdb_id_and_seasons(self) -> None:
        _item, ref = self.series("S02")
        self.work(ref)
        [post] = self.seerr.sent("POST", "/api/v1/request")
        self.assertEqual(
            post.body, {"mediaType": "tv", "mediaId": SERIES_TMDB_ID, "tvdbId": TVDB_ID, "seasons": [2]},
        )

    def test_a_series_is_still_requested_when_tmdb_gives_no_tvdb_id(self) -> None:
        self.tvdb.return_value = None
        _item, ref = self.series(None)
        self.work(ref)
        [post] = self.seerr.sent("POST", "/api/v1/request")
        self.assertEqual(post.body, {"mediaType": "tv", "mediaId": SERIES_TMDB_ID, "seasons": "all"})

    def test_a_request_seerr_already_has_is_a_success_and_not_this_item_s(self) -> None:
        theirs = self.seerr.add_request("movie", TMDB_ID, user=5)
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download()
        self.assertEqual((dl.remote_id, dl.created_torrent), (str(theirs["id"]), False))
        self.assertEqual(len(self.seerr.requests), 1)

    def test_media_seerr_calls_available_completes_the_item(self) -> None:
        self.seerr.info("movie", TMDB_ID)["status"] = fakes.MEDIA_AVAILABLE
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.seerr.sent("POST"), [])
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertEqual(self.reasons(item), ["already in the library (Seerr)"])

    def test_a_refused_key_and_a_timeout_need_a_person(self) -> None:
        for breakage, fragment in (
            (lambda: setattr(self.seerr, "accepts", "another-key"), "refused the API key"),
            (lambda: setattr(self.seerr, "down", httpx.ReadTimeout), "did not answer in time"),
        ):
            with self.subTest(fragment):
                breakage()
                item, ref = self.claimed()
                self.work(ref)
                self.assertEqual(item.status, ItemStatus.ERROR)
                self.assertIn(fragment, item.error_message)
                self.assertEqual(self.seerr.requests, {})


class SeerrWithoutAUserTests(DeliveryCase):
    DELIVER = {"movie": "seerr"}
    SEERR_USER = None

    def test_no_request_is_sent_and_the_item_says_why(self) -> None:
        """With the key alone Seerr would approve the request as its admin."""
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(self.seerr.calls, [])
        self.assertEqual(self.seerr.requests, {})
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("integrations.seerr.user_id is not set", item.error_message)
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DELIVERY_FAILED, events.NEEDS_YOU])


# ---------------------------------------------------------------------------
# Completion: only when the target says the files are there
# ---------------------------------------------------------------------------


class CompletionCase(DeliveryCase):
    def delivered(self, ref: ItemRef) -> Download:
        self.work(ref)
        drain()
        return self.download()


class RadarrCompletionTests(CompletionCase):
    def test_a_movie_without_a_file_keeps_waiting_however_long(self) -> None:
        """The hashless grace period completed any download after two hours."""
        item, ref = self.claimed()
        dl = self.delivered(ref)
        dl.started_at = datetime.now(timezone.utc) - timedelta(hours=30)
        self.poll()
        self.assertEqual(dl.status, DownloadStatus.DOWNLOADING)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(drain(), [])
        self.qbt.get_torrent_progress.assert_not_awaited()

    def test_has_file_completes_the_item(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.radarr.movies[11]["hasFile"] = True
        self.poll()
        self.assertEqual(self.radarr.calls[-1].path, "/api/v3/movie/11")
        self.assertEqual((dl.status, dl.progress), (DownloadStatus.COMPLETED, 1.0))
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        [(kind, item_id, payload)] = drain()
        self.assertEqual((kind, item_id, payload["target"]), (events.COMPLETED, 1, "radarr"))

    def test_a_movie_removed_from_radarr_needs_a_person(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.radarr.movies.clear()
        self.poll()
        self.assertEqual(dl.status, DownloadStatus.ERROR)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertEqual(item.error_message, "Radarr: Radarr no longer has this movie")
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DELIVERY_FAILED, events.NEEDS_YOU])

    def test_a_movie_unmonitored_there_before_its_file_needs_a_person(self) -> None:
        item, ref = self.claimed()
        self.delivered(ref)
        self.radarr.movies[11]["monitored"] = False
        self.poll()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("unmonitored in Radarr", item.error_message)

    def test_a_radarr_that_does_not_answer_changes_nothing(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        for failure in (httpx.ReadTimeout, httpx.ConnectError):
            self.radarr.down = failure
            self.poll()
        self.radarr.down, self.radarr.accepts = None, "another-key"
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.DOWNLOADING, ItemStatus.DOWNLOADING))
        self.assertEqual(drain(), [])

    def test_a_radarr_silent_for_an_hour_hands_the_item_back(self) -> None:
        """A refused key at poll time was one log line every five minutes, for ever."""
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.radarr.accepts = "another-key"
        for _ in range(scheduler.UNANSWERED_LIMIT - 1):
            self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.DOWNLOADING, ItemStatus.DOWNLOADING))
        self.assertEqual(drain(), [])
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.ERROR, ItemStatus.ERROR))
        self.assertIn(f"{scheduler.UNANSWERED_LIMIT} checks in a row", item.error_message)
        self.assertIn("refused the API key", item.error_message)
        self.assertNotIn(API_KEY, item.error_message)
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DELIVERY_FAILED, events.NEEDS_YOU])
        asked = len(self.radarr.calls)
        self.poll()
        self.assertEqual(len(self.radarr.calls), asked)

    def test_an_answer_starts_the_count_again(self) -> None:
        item, ref = self.claimed()
        self.delivered(ref)
        for _ in range(2):
            self.radarr.down = httpx.ReadTimeout
            for _ in range(scheduler.UNANSWERED_LIMIT - 1):
                self.poll()
            self.radarr.down = None
            self.poll()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(drain(), [])
        self.assertEqual(scheduler._unanswered, {})

    def test_a_target_that_is_down_is_asked_once_a_check_and_after_the_download_client(self) -> None:
        """Each delivered item waited out its own 15 s timeout before any torrent was read."""
        self.radarr.catalog[604] = fakes.radarr_lookup(604, "Other Film")
        _item, first = self.claimed(1)
        _item, second = self.claimed(2, tmdb_id=604, title="Other Film")
        for ref in (first, second):
            self.work(ref)
        self.claimed(3, status=ItemStatus.DOWNLOADING)
        self.store.put(Download(
            id=5, media_item_id=3, qbittorrent_hash="abc", status=DownloadStatus.DOWNLOADING,
            progress=0.0, started_at=datetime.now(timezone.utc),
        ))
        self.radarr.down = httpx.ReadTimeout
        asked = len(self.radarr.calls)
        seen = []
        self.qbt.get_torrent_progress.side_effect = lambda _hash: seen.append(len(self.radarr.calls) - asked) or 0.5
        self.poll()
        self.assertEqual(seen, [0])
        self.assertEqual(len(self.radarr.calls) - asked, 1)
        # Both items were left unanswered, the one that was not asked too.
        self.assertEqual(sorted(scheduler._unanswered.values()), [1, 1])

    def test_a_rejection_during_the_poll_is_not_reported_as_completed(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.radarr.movies[11]["hasFile"] = True
        asked = delivery.poll

        async def rejected_meanwhile(download, config=None):
            state = await asked(download, config)
            self.radarr.down = httpx.ReadTimeout
            self.assertTrue((await actions.reject(1, None)).ok)
            return state

        with mock.patch.object(delivery, "poll", rejected_meanwhile):
            self.poll()
        self.assertEqual((item.status, dl.status), (ItemStatus.REJECTED, DownloadStatus.CANCELLED))
        self.assertNotIn(events.COMPLETED, [kind for kind, _id, _p in drain()])
        self.posts.after.assert_not_awaited()

    def test_a_rejection_that_landed_meanwhile_is_not_overwritten(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.radarr.movies.clear()
        item.status = ItemStatus.REJECTED
        run(delivery.stalled(dl, delivery.RemoteState(problem="gone")))
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(drain(), [])

    def test_a_torrent_in_the_download_client_is_still_polled_by_its_hash(self) -> None:
        item, _ref = self.claimed(status=ItemStatus.DOWNLOADING)
        dl = self.store.put(Download(
            id=5, media_item_id=1, qbittorrent_hash="abc", status=DownloadStatus.DOWNLOADING,
            progress=0.0, started_at=datetime.now(timezone.utc),
        ))
        self.qbt.get_torrent_progress.return_value = 1.0
        self.poll()
        self.qbt.get_torrent_progress.assert_awaited_once_with("abc")
        self.assertEqual(self.radarr.calls, [])
        self.assertEqual((dl.status, item.status), (DownloadStatus.COMPLETED, ItemStatus.COMPLETED))
        [(kind, _id, payload)] = drain()
        self.assertEqual((kind, payload["target"]), (events.COMPLETED, "qbittorrent"))

    def test_a_hashless_download_client_row_keeps_its_grace_period(self) -> None:
        item, _ref = self.claimed(status=ItemStatus.DOWNLOADING)
        dl = self.store.put(Download(
            id=5, media_item_id=1, status=DownloadStatus.DOWNLOADING, progress=0.0,
            started_at=datetime.now(timezone.utc) - timedelta(hours=3),
        ))
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.COMPLETED, ItemStatus.COMPLETED))
        self.assertEqual([(kind, p["target"]) for kind, _id, p in drain()], [(events.COMPLETED, "qbittorrent")])


class SonarrCompletionTests(CompletionCase):
    def test_the_season_is_complete_when_every_episode_has_a_file(self) -> None:
        item, ref = self.series("S02")
        dl = self.delivered(ref)
        self.sonarr.set_files(21, 2, fakes.EPISODES - 1)
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.DOWNLOADING, ItemStatus.DOWNLOADING))
        self.sonarr.set_files(21, 2, fakes.EPISODES)
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.COMPLETED, ItemStatus.COMPLETED))
        self.assertEqual([kind for kind, _id, _p in drain()], [events.COMPLETED])

    def test_files_of_another_season_do_not_complete_it(self) -> None:
        item, ref = self.series("S02")
        self.delivered(ref)
        self.sonarr.set_files(21, 1, fakes.EPISODES)
        self.poll()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)

    def test_the_whole_series_waits_for_every_season(self) -> None:
        item, ref = self.series(None)
        self.delivered(ref)
        for season in (1, 2):
            self.sonarr.set_files(21, season, fakes.EPISODES)
        self.poll()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.sonarr.set_files(21, 3, fakes.EPISODES)
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_season_unmonitored_there_before_its_files_needs_a_person(self) -> None:
        item, ref = self.series("S02")
        self.delivered(ref)
        self.sonarr.series[21]["seasons"][2]["monitored"] = False
        self.poll()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("unmonitored in Sonarr", item.error_message)

    def test_a_series_removed_from_sonarr_needs_a_person(self) -> None:
        item, ref = self.series("S02")
        self.delivered(ref)
        self.sonarr.series.clear()
        self.poll()
        self.assertEqual(item.error_message, "Sonarr: Sonarr no longer has this series")
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DELIVERY_FAILED, events.NEEDS_YOU])


class SeerrCompletionTests(CompletionCase):
    DELIVER = {"movie": "seerr", "series": "seerr"}

    def test_the_item_completes_when_seerr_calls_the_media_available(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.seerr.set_request(31, fakes.REQUEST_APPROVED)
        self.seerr.info("movie", TMDB_ID)["status"] = fakes.MEDIA_PROCESSING
        self.poll()
        self.assertEqual(self.seerr.calls[-1].path, f"/api/v1/movie/{TMDB_ID}")
        self.assertEqual((dl.status, item.status), (DownloadStatus.DOWNLOADING, ItemStatus.DOWNLOADING))
        self.seerr.info("movie", TMDB_ID)["status"] = fakes.MEDIA_AVAILABLE
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.COMPLETED, ItemStatus.COMPLETED))

    def test_a_series_is_read_from_tv_and_its_seasons(self) -> None:
        item, ref = self.series("S02")
        self.delivered(ref)
        info = self.seerr.info("tv", SERIES_TMDB_ID)
        info["status"] = fakes.MEDIA_PARTIAL
        info["seasons"] = [{"seasonNumber": 1, "status": fakes.MEDIA_AVAILABLE}]
        self.poll()
        self.assertEqual(self.seerr.calls[-1].path, f"/api/v1/tv/{SERIES_TMDB_ID}")
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        info["seasons"].append({"seasonNumber": 2, "status": fakes.MEDIA_AVAILABLE})
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_declined_request_is_an_event_and_needs_a_person(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.seerr.set_request(31, fakes.REQUEST_DECLINED)
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.ERROR, ItemStatus.ERROR))
        self.assertEqual(item.error_message, "Seerr: the request was declined in Seerr")
        got = drain()
        self.assertEqual([kind for kind, _id, _p in got], [events.DECLINED, events.NEEDS_YOU])
        self.assertEqual(got[0][2]["remote_id"], "31")

    def test_a_request_removed_there_is_declined_too(self) -> None:
        item, ref = self.claimed()
        self.delivered(ref)
        self.seerr.remove_request(31)
        self.poll()
        self.assertEqual(item.error_message, "Seerr: the request was removed in Seerr")
        self.assertEqual(drain()[0][0], events.DECLINED)

    def test_a_request_seerr_could_not_pass_on_is_a_failure(self) -> None:
        item, ref = self.claimed()
        self.delivered(ref)
        self.seerr.set_request(31, fakes.REQUEST_FAILED)
        self.poll()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertEqual(drain()[0][0], events.DELIVERY_FAILED)

    def test_a_failed_request_clears_itself_once_seerr_has_the_files(self) -> None:
        # Seen live: Seerr failed the request (Radarr already held the title),
        # then found the files; the item stayed an error for days.
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.seerr.set_request(31, fakes.REQUEST_FAILED)
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.ERROR, ItemStatus.ERROR))
        drain()
        self.poll()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.seerr.info("movie", TMDB_ID)["status"] = fakes.MEDIA_AVAILABLE
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.COMPLETED, ItemStatus.COMPLETED))
        self.assertIsNone(item.error_message)
        self.assertEqual([kind for kind, _id, _p in drain()], [events.COMPLETED])
        self.posts.after.assert_awaited_once_with("download", item.id)

    def test_an_error_a_person_moved_on_is_left_alone(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.seerr.set_request(31, fakes.REQUEST_FAILED)
        self.poll()
        item.status = ItemStatus.REJECTED
        drain()
        self.seerr.info("movie", TMDB_ID)["status"] = fakes.MEDIA_AVAILABLE
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.ERROR, ItemStatus.REJECTED))
        self.assertEqual(drain(), [])

    def test_a_pending_request_keeps_waiting_however_long(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        dl.started_at = datetime.now(timezone.utc) - timedelta(days=9)
        self.poll()
        self.assertEqual((dl.status, item.status), (DownloadStatus.DOWNLOADING, ItemStatus.DOWNLOADING))


# ---------------------------------------------------------------------------
# Rejecting a delivered item
# ---------------------------------------------------------------------------


class RejectTests(CompletionCase):
    def reject(self):
        return run(actions.reject(1, None))

    def test_radarr_is_told_to_stop_monitoring_and_deletes_nothing(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        before = len(self.radarr.calls)
        result = self.reject()
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(dl.status, DownloadStatus.CANCELLED)
        get, put = self.radarr.calls[before:]
        self.assertEqual((get.method, put.method, put.path), ("GET", "PUT", "/api/v3/movie/11"))
        self.assertIs(put.body["monitored"], False)
        self.assertEqual(self.radarr.sent("DELETE"), [])
        self.assertIn(11, self.radarr.movies)
        self.assertIn("Radarr no longer monitors it", result.message)
        self.qbt.remove_torrent.assert_not_awaited()

    def test_a_movie_radarr_was_already_looking_for_is_left_as_it_is(self) -> None:
        self.radarr.hold(has_file=False, monitored=True)
        _item, ref = self.claimed()
        self.delivered(ref)
        result = self.reject()
        self.assertEqual(self.radarr.writes(), [])
        self.assertIn("Radarr had it before this item did", result.message)

    def test_sonarr_stops_monitoring_the_seasons_the_post_named(self) -> None:
        _item, ref = self.series("S02")
        self.delivered(ref)
        result = self.reject()
        put = self.sonarr.writes()[-1]
        self.assertEqual(put.method, "PUT")
        # The series is this item's own add, so its flag goes off with the season.
        self.assertEqual(self.flags(), {"series": False, 1: False, 2: False, 3: False})
        self.assertEqual(self.sonarr.sent("DELETE"), [])
        self.assertIn("Sonarr no longer monitors it", result.message)

    def test_sonarr_stops_monitoring_the_series_when_no_season_was_named(self) -> None:
        _item, ref = self.series(None)
        self.delivered(ref)
        self.reject()
        self.assertEqual(self.flags(), {"series": False, 1: False, 2: False, 3: False})
        self.assertIn(21, self.sonarr.series)

    def flags(self, series_id: int = 21) -> dict:
        series = self.sonarr.series[series_id]
        return {
            "series": series["monitored"],
            **{s["seasonNumber"]: s["monitored"] for s in series["seasons"] if s["seasonNumber"] > 0},
        }

    def test_a_series_the_operator_monitors_keeps_its_flag_and_its_own_seasons(self) -> None:
        """Rejecting a whole-series item turned the series flag off, which was
        the operator's, and left on the seasons the delivery had turned on."""
        self.sonarr.hold(monitored=True, monitored_seasons=(1,), files={1: fakes.EPISODES})
        before = self.flags()
        self.assertEqual(before, {"series": True, 1: True, 2: False, 3: False})
        _item, ref = self.series(None)
        dl = self.delivered(ref)
        self.assertEqual(self.flags(), {"series": True, 1: True, 2: True, 3: True})
        result = self.reject()
        self.assertEqual(self.flags(), before)
        self.assertIn("Sonarr no longer monitors seasons 2 and 3", result.message)
        self.assertEqual(self.sonarr.sent("DELETE"), [])
        self.assertEqual(dl.remote_changes, {"series": False, "seasons": [2, 3]})

    def test_a_series_flag_this_delivery_turned_on_goes_off_again(self) -> None:
        self.sonarr.hold(monitored=False, monitored_seasons=(1,))
        before = self.flags()
        _item, ref = self.series("S02")
        self.delivered(ref)
        self.assertEqual(self.flags(), {"series": True, 1: True, 2: True, 3: False})
        self.reject()
        self.assertEqual(self.flags(), before)

    def test_a_series_sonarr_was_already_looking_for_is_left_as_it_is(self) -> None:
        self.sonarr.hold()
        _item, ref = self.series("S02")
        self.delivered(ref)
        result = self.reject()
        self.assertEqual(self.sonarr.writes(), [])
        self.assertIn("Sonarr had it before this item did", result.message)

    def test_approving_again_keeps_what_the_first_hand_over_made(self) -> None:
        """The second hand-over finds the movie monitored and wrote "not this item's"."""
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.assertTrue(dl.created_torrent)
        radarr = self.clients.pop("radarr")
        self.poll()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.clients["radarr"] = radarr
        self.approve_again(item)
        self.work(ref)
        self.assertEqual((item.status, dl.remote_id), (ItemStatus.DOWNLOADING, "11"))
        self.assertTrue(dl.created_torrent)
        self.reject()
        self.assertIs(self.radarr.movies[11]["monitored"], False)

    def test_what_happened_at_the_target_is_kept_where_a_person_looks_later(self) -> None:
        """Only the bot's answer said it; the item page read "rejected after it was sent"."""
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.radarr.down = httpx.ReadTimeout
        result = self.reject()
        self.assertTrue(result.remote_failed)
        self.assertIn("Radarr could not be told", delivery.where(item, dl))
        [(kind, item_id, payload)] = drain()
        self.assertEqual((kind, item_id, payload["target"]), (events.DELIVERY_FAILED, 1, "radarr"))
        self.assertIn("could not be told", payload["reason"])

    def test_a_withdrawal_that_worked_is_no_failure(self) -> None:
        item, ref = self.claimed()
        dl = self.delivered(ref)
        result = self.reject()
        self.assertFalse(result.remote_failed)
        self.assertIn("Radarr no longer monitors it", delivery.where(item, dl))
        self.assertEqual(drain(), [])

    def _web(self):
        from fastapi.testclient import TestClient

        from vettarr.web import app as web_app
        from vettarr.web import cards, shell

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real_path = pathlib.Path

        def _path(*args):
            return real_path(tmp.name) if args == ("/data/photos",) else real_path(*args)

        for patcher in (
            mock.patch.object(web_app, "Path", _path),
            mock.patch.object(shell, "failing", mock.AsyncMock(return_value=[])),
            # What the answer draws beside its toast: the panel and the rows, read again.
            mock.patch.object(cards, "load_detail", mock.AsyncMock(return_value=None)),
            mock.patch.object(cards, "load_rows", mock.AsyncMock(
                return_value=([], {tab.key: 0 for tab in cards.TABS}))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return TestClient(web_app.create_web_app(ingester=None))

    def test_the_web_answer_says_what_happened_at_the_target(self) -> None:
        """The page answered a fixed "Rejected" while Radarr kept the movie monitored."""
        _item, ref = self.claimed()
        self.delivered(ref)
        self.radarr.down = httpx.ReadTimeout
        resp = self._web().post("/item/1/reject", headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        toast = json.loads(resp.headers["HX-Trigger"])["toast"]
        self.assertIn("Radarr could not be told", toast["text"])
        self.assertEqual(toast["kind"], "error")

    def test_the_bulk_reject_says_it_too(self) -> None:
        """Rejected here, but Radarr could not be told and still has it: the toast says so, as an error."""
        _item, ref = self.claimed()
        self.delivered(ref)
        self.radarr.down = httpx.ReadTimeout
        resp = self._web().post("/bulk/reject", data={"ids": ["1"]}, headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        toast = json.loads(resp.headers["HX-Trigger"])["toast"]
        self.assertTrue(toast["text"].startswith("Rejected 1. "), toast)
        self.assertIn("Radarr could not be told", toast["text"])
        self.assertEqual(toast["kind"], "error")

    def test_a_bulk_reject_the_target_took_back_is_no_error(self) -> None:
        _item, ref = self.claimed()
        self.delivered(ref)
        resp = self._web().post("/bulk/reject", data={"ids": ["1"]}, headers={"HX-Request": "true"})
        self.assertEqual(json.loads(resp.headers["HX-Trigger"])["toast"], {"text": "Rejected 1.", "kind": "ok"})

    def test_a_target_that_does_not_answer_does_not_stop_the_rejection(self) -> None:
        item, ref = self.claimed()
        self.delivered(ref)
        self.radarr.down = httpx.ReadTimeout
        result = self.reject()
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertIn("Radarr could not be told", result.message)

    def test_an_item_not_delivered_yet_touches_no_target(self) -> None:
        make_item(self.store, 1, media_type=MediaType.MOVIE, status=ItemStatus.PENDING_REVIEW)
        result = self.reject()
        self.assertTrue(result.ok)
        self.assertEqual(self.radarr.calls + self.sonarr.calls + self.seerr.calls, [])


class SeerrRejectTests(CompletionCase):
    DELIVER = {"movie": "seerr"}

    def test_a_pending_request_is_withdrawn_as_the_user_who_made_it(self) -> None:
        _item, ref = self.claimed()
        self.delivered(ref)
        result = run(actions.reject(1, None))
        [delete] = self.seerr.sent("DELETE")
        self.assertEqual(delete.path, "/api/v1/request/31")
        self.assertEqual(delete.headers["x-api-user"], "2")
        self.assertEqual(self.seerr.requests, {})
        self.assertIn("Its Seerr request was withdrawn", result.message)

    def test_an_approved_request_stays(self) -> None:
        _item, ref = self.claimed()
        self.delivered(ref)
        self.seerr.set_request(31, fakes.REQUEST_APPROVED)
        result = run(actions.reject(1, None))
        self.assertEqual(self.seerr.sent("DELETE"), [])
        self.assertIn("no longer pending", result.message)

    def test_somebody_else_s_request_is_never_withdrawn(self) -> None:
        self.seerr.add_request("movie", TMDB_ID, user=5)
        _item, ref = self.claimed()
        self.delivered(ref)
        result = run(actions.reject(1, None))
        self.assertEqual(self.seerr.sent("DELETE"), [])
        self.assertIn("Seerr had it before this item did", result.message)


class BackToTheDownloadClientTests(CompletionCase):
    """An item handed to an arr first and added to the download client later.

    The add left the row saying radarr, so the torrent was never polled by its
    hash and a rejection unmonitored a movie the operator had added himself.
    """

    def _picked_by_hand(self):
        self.radarr.hold(has_file=False, monitored=True)
        item, ref = self.claimed()
        dl = self.delivered(ref)
        self.assertEqual((dl.delivery_target, dl.remote_id, dl.created_torrent), ("radarr", "11", False))
        item.status, item.error_message = ItemStatus.SEARCHING, OP_MANUAL_DOWNLOAD

        async def add() -> None:
            async with self.store.session() as session:
                await search_service.record_download(
                    session, 1, AddOutcome("abc", True), op=OP_MANUAL_DOWNLOAD,
                    media_type=MediaType.MOVIE, download_url=None, magnet_url=MAGNET,
                )

        run(add())
        return item, dl

    def test_the_row_is_the_download_client_s_again(self) -> None:
        _item, dl = self._picked_by_hand()
        self.assertEqual(
            (dl.delivery_target, dl.remote_id, dl.qbittorrent_hash, dl.created_torrent),
            (None, None, "abc", True),
        )

    def test_the_torrent_is_polled_by_its_hash(self) -> None:
        item, _dl = self._picked_by_hand()
        asked = len(self.radarr.calls)
        self.qbt.get_torrent_progress.return_value = 1.0
        self.poll()
        self.qbt.get_torrent_progress.assert_awaited_once_with("abc")
        self.assertEqual(len(self.radarr.calls), asked)
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_rejecting_it_removes_the_torrent_and_leaves_the_operator_s_movie(self) -> None:
        self._picked_by_hand()
        run(actions.reject(1, None))
        self.assertEqual(self.radarr.writes(), [])
        self.assertIs(self.radarr.movies[11]["monitored"], True)
        self.qbt.remove_torrent.assert_awaited_once_with("abc")


class ApproveAnswerTests(DeliveryCase):
    """Approving answered "Queued ... for download." whatever the target."""

    DELIVER = {"movie": "radarr", "series": "seerr", "music": "none", "ebook": "qbittorrent"}

    def _answer(self, media_type) -> str:
        make_item(self.store, 1, media_type=media_type, title="Some Title", status=ItemStatus.PENDING_REVIEW)
        return run(actions.approve(1)).message

    def test_it_says_where_the_item_goes(self) -> None:
        for media_type, said in (
            (MediaType.EBOOK, "Queued Some Title for download."),
            (MediaType.MOVIE, "Queued Some Title for Radarr."),
            (MediaType.SERIES, "Queued Some Title for Seerr."),
            (MediaType.MUSIC, "Approved Some Title. Nothing is fetched: deliver says none for music."),
            (MediaType.COMIC, "Approved Some Title. It will come back: deliver lists no target for comic."),
        ):
            with self.subTest(media_type=media_type):
                self.assertEqual(self._answer(media_type), said)


class DownloadClientRejectTests(DeliveryCase):
    DELIVER = {"movie": "qbittorrent"}

    def test_the_torrent_this_item_added_is_still_removed(self) -> None:
        make_item(self.store, 1, media_type=MediaType.MOVIE, status=ItemStatus.DOWNLOADING)
        self.store.put(Download(
            id=5, media_item_id=1, qbittorrent_hash="abc", created_torrent=True,
            status=DownloadStatus.DOWNLOADING,
        ))
        result = run(actions.reject(1, None))
        self.qbt.remove_torrent.assert_awaited_once_with("abc")
        self.assertEqual(result.message, "Rejected. Its post was left in place.")


# ---------------------------------------------------------------------------
# A person's own pick, the library signal, the config check
# ---------------------------------------------------------------------------


class GrabTests(DeliveryCase):
    def _searched(self, media_type):
        item = make_item(self.store, 1, media_type=media_type, status=ItemStatus.PENDING_REVIEW)
        self.store.put(TorrentSearch(id=50, media_item_id=1, result_count=1, prowlarr_results=[
            {"title": "Release A", "magnet_url": "magnet:?xt=urn:btih:a", "indexer": "tracker-a"},
        ]))
        return item

    def test_a_release_picked_for_a_type_radarr_takes_is_refused(self) -> None:
        item = self._searched(MediaType.MOVIE)
        result = run(actions.grab(1, 50, 0))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertIn("goes to Radarr, which picks its own release", result.message)
        self.qbt.add_torrent.assert_not_awaited()
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertIsNone(self.download())

    def test_the_bot_answers_a_refused_pick_with_the_reason(self) -> None:
        """It used to answer every failed pick with "Failed to add torrent to qBittorrent"."""
        from vettarr.bot import handlers

        self._searched(MediaType.MOVIE)
        respond = mock.AsyncMock()
        query = SimpleNamespace(message=SimpleNamespace(chat_id=5))
        with mock.patch.object(handlers, "_respond", respond), \
                mock.patch.object(handlers, "_send_next", mock.AsyncMock()):
            run(handlers._handle_torrent_select(query, 1, 0, 50))
        self.assertIn("goes to Radarr, which picks its own release", respond.await_args.kwargs["text"])

    def test_a_release_picked_for_a_type_delivered_nowhere_is_refused(self) -> None:
        self._searched(MediaType.EBOOK)
        result = run(actions.grab(1, 50, 0))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertIn("Nothing is delivered for ebook", result.message)
        self.qbt.add_torrent.assert_not_awaited()


class LibrarySignalTests(DeliveryCase):
    def _signal(self, item, tmdb_id=TMDB_ID):
        return run(gather.costly_signals(
            self.store.session(), item, self.cfg, {"in_library"},
            tmdb=TMDBResult(tmdb_id=tmdb_id, title="Some Film"), now=datetime.now(timezone.utc),
        ))["in_library"]

    def setUp(self) -> None:
        super().setUp()
        gather.forget_kept()
        self.addCleanup(gather.forget_kept)

    def test_radarr_s_library_answers_without_jellyfin(self) -> None:
        item, _ref = self.claimed()
        self.assertIs(self._signal(item), False)
        self.radarr.hold(has_file=True)
        self.assertIs(self._signal(item), True)

    def test_sonarr_answers_for_the_seasons_the_post_names(self) -> None:
        item, _ref = self.series("S02")
        self.sonarr.hold(files={1: fakes.EPISODES})
        self.assertIs(self._signal(item, SERIES_TMDB_ID), False)
        self.sonarr.set_files(21, 2, fakes.EPISODES)
        self.assertIs(self._signal(item, SERIES_TMDB_ID), True)

    def test_an_arr_that_does_not_answer_is_missing_not_absent(self) -> None:
        item, _ref = self.claimed()
        self.radarr.down = httpx.ReadTimeout
        self.assertEqual(self._signal(item), {"missing": "Radarr did not answer"})

    def test_a_type_not_delivered_to_an_arr_still_needs_jellyfin(self) -> None:
        item, _ref = self.claimed()
        with mock.patch.object(settings, "_config", config({"movie": "qbittorrent"})):
            self.cfg = settings.config
            self.assertEqual(self._signal(item), {"unavailable": "jellyfin"})

    def test_jellyfin_saying_yes_is_enough(self) -> None:
        item, _ref = self.claimed()
        jellyfin = mock.Mock(has_tmdb_id=mock.AsyncMock(return_value=True))
        with mock.patch("vettarr.checker.jellyfin_client.client_for", return_value=jellyfin):
            self.assertIs(self._signal(item), True)
        self.assertEqual(self.radarr.calls, [])


class ConfigCheckTests(unittest.TestCase):
    def _messages(self, cfg: Config) -> list[str]:
        return [f"{'.'.join(map(str, loc))}: {text}" for loc, text in warnings(cfg)]

    def test_an_empty_map_beside_a_download_client_is_warned_about(self) -> None:
        messages = self._messages(config({}))
        self.assertTrue(any(m.startswith("deliver: deliver is empty") for m in messages), messages)

    EVERY_TYPE = ("movie", "series", "audiobook", "ebook", "magazine", "comic", "music")

    def test_a_map_that_lists_every_type_is_not(self) -> None:
        for target in ("qbittorrent", "none"):
            deliver = {name: target for name in self.EVERY_TYPE}
            self.assertFalse(any(m.startswith("deliver:") for m in self._messages(config(deliver))))

    def test_the_types_a_map_leaves_out_are_warned_about(self) -> None:
        """The example's own lines: movie and series only. Every book then came
        back undelivered, and neither `config check` nor the start said so."""
        messages = self._messages(config({"movie": "qbittorrent", "series": "qbittorrent"}))
        self.assertIn(
            "deliver: deliver lists no target for audiobook, ebook, magazine, comic, music, so an approved "
            "item of those types comes back to you undelivered. Write none for a type that should only be decided",
            messages,
        )

    def test_the_start_logs_the_same_warning(self) -> None:
        from vettarr import __main__ as app

        cfg = config({"movie": "qbittorrent", "series": "qbittorrent"})
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with mock.patch.object(app, "_load_config", return_value=cfg), \
                self.assertLogs("vettarr", "WARNING") as logs:
            app.boot()
        self.assertIn("deliver lists no target for audiobook, ebook, magazine, comic, music", "\n".join(logs.output))

    def test_the_start_says_nothing_about_a_map_that_lists_every_type(self) -> None:
        from vettarr import __main__ as app

        cfg = config({name: "qbittorrent" for name in self.EVERY_TYPE})
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with mock.patch.object(app, "_load_config", return_value=cfg), \
                self.assertLogs("vettarr", "INFO") as logs:
            app.boot()
        self.assertNotIn("deliver lists no target", "\n".join(logs.output))

    def test_an_arr_target_without_folder_or_profile_is_warned_about(self) -> None:
        cfg = config({"movie": "radarr", "series": "sonarr"})
        cfg.integrations.radarr.root_folder = None
        cfg.integrations.sonarr.quality_profile = None
        messages = self._messages(cfg)
        self.assertTrue(any(m.startswith("integrations.radarr:") and "root_folder" in m for m in messages))
        self.assertTrue(any(m.startswith("integrations.sonarr:") and "quality_profile" in m for m in messages))

    def test_a_complete_arr_block_is_not(self) -> None:
        messages = self._messages(config({"movie": "radarr", "series": "sonarr"}))
        self.assertFalse(any(m.startswith("integrations.") for m in messages), messages)

    def test_the_example_file_shows_every_target(self) -> None:
        example = (_ROOT / "config.example.yaml").read_text()
        for line in ("movie: qbittorrent", "movie: radarr", "series: sonarr", "movie: seerr", "music: none"):
            self.assertIn(f"#   {line}", example)

    def test_the_docs_no_longer_call_deliver_unenforced(self) -> None:
        doc = (_ROOT / "docs" / "CONFIG.md").read_text()
        pending = doc.split("## Not enforced yet", 1)[1]
        self.assertNotIn("`deliver`", pending)
        self.assertNotIn("integrations.radarr", pending)


# ---------------------------------------------------------------------------
# What a person is shown
# ---------------------------------------------------------------------------


class WhereTests(DeliveryCase):
    def test_a_delivered_item_says_where_it_went(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(
            delivery.where(item, self.download()), "Radarr: it has the title and is fetching it (id 11)",
        )

    def test_an_item_already_in_the_library_says_so(self) -> None:
        self.radarr.hold(has_file=True)
        item, ref = self.claimed()
        self.work(ref)
        self.assertEqual(delivery.where(item, None), "already in the library (Radarr)")

    def test_an_item_for_the_download_client_says_nothing_new(self) -> None:
        item = make_item(self.store, 1, media_type=MediaType.MOVIE)
        with mock.patch.object(settings, "_config", config({"movie": "qbittorrent"})):
            self.assertIsNone(delivery.where(item))
            self.assertIsNone(delivery.where(item, Download(media_item_id=1, qbittorrent_hash="abc")))

    def test_a_waiting_item_says_where_approving_sends_it(self) -> None:
        item = make_item(self.store, 1, media_type=MediaType.MOVIE)
        self.assertEqual(
            delivery.where(item), "Approving sends the title to Radarr, which picks its own release.",
        )
        ebook = make_item(self.store, 2, media_type=MediaType.EBOOK)
        self.assertIn("deliver lists no target for ebook", delivery.where(ebook))
        with mock.patch.object(settings, "_config", config({"ebook": "none"})):
            self.assertIn("fetches nothing: deliver says none for ebook", delivery.where(ebook))

    def test_the_item_page_carries_the_line(self) -> None:
        from fastapi.testclient import TestClient

        from vettarr.web import app as web_app
        from vettarr.web import cards, shell

        item, ref = self.claimed(auto_recommendation=Recommendation.NEUTRAL)
        self.work(ref)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real_path = pathlib.Path

        def _path(*args):
            return real_path(tmp.name) if args == ("/data/photos",) else real_path(*args)

        # The layout's banner reads the events table; no database runs here.
        with mock.patch.object(web_app, "Path", _path), \
                mock.patch.object(shell, "failing", mock.AsyncMock(return_value=[])), \
                mock.patch.object(ingest, "direct_add_of", return_value="magnet"), \
                mock.patch.object(cards, "async_session", self.store.session):
            resp = TestClient(web_app.create_web_app(ingester=None)).get("/item/1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Delivery:</strong> Radarr: it has the title and is fetching it (id 11)", resp.text)
        # The post's magnet is not what Radarr fetches, so the page does not promise it,
        # and offers no release of its own to grab.
        self.assertNotIn("Approving downloads the", resp.text)
        self.assertNotIn("/download/", resp.text)


# ---------------------------------------------------------------------------
# Migration 0005
# ---------------------------------------------------------------------------


def _revision_0005():
    spec = importlib.util.spec_from_file_location(
        "_revision_0005", _ROOT / "alembic" / "versions" / "0005_delivery_targets.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out: list = []
    with mock.patch.object(module.op, "execute", out.append):
        module.upgrade()
    return [" ".join(str(s).split()) for s in out], module


class DeliveryMigrationTests(unittest.TestCase):
    def test_it_follows_the_sender_migration(self) -> None:
        _sql, module = _revision_0005()
        self.assertEqual(module.revision, "0005_delivery_targets")
        self.assertEqual(module.down_revision, "0004_sender_and_post_time")

    def test_it_adds_three_nullable_columns_and_nothing_else(self) -> None:
        sql, _module = _revision_0005()
        self.assertEqual(len(sql), 1)
        self.assertIn("ALTER TABLE downloads", sql[0])
        self.assertIn("ADD COLUMN IF NOT EXISTS delivery_target TEXT", sql[0])
        self.assertIn("ADD COLUMN IF NOT EXISTS remote_id TEXT", sql[0])
        self.assertIn("ADD COLUMN IF NOT EXISTS remote_changes JSONB", sql[0])
        self.assertEqual(sql[0].count("ADD COLUMN"), 3)
        for forbidden in ("UPDATE ", "DELETE ", "DROP ", "NOT NULL", "RENAME", "DEFAULT", "ALTER TYPE"):
            self.assertNotIn(forbidden, sql[0].upper())

    def test_the_model_carries_the_same_three_columns(self) -> None:
        columns = Download.__table__.columns
        for name, kind in (("delivery_target", "TEXT"), ("remote_id", "TEXT"), ("remote_changes", "JSONB")):
            self.assertTrue(columns[name].nullable)
            self.assertEqual(str(columns[name].type), kind)

    def test_the_migrations_are_one_line_with_one_head(self) -> None:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        scripts = ScriptDirectory.from_config(cfg)
        self.assertEqual(scripts.get_heads(), ["0005_delivery_targets"])


if __name__ == "__main__":
    unittest.main()
