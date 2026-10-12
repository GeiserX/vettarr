"""Which Prowlarr indexer holds the session for a tracker a post links to."""

import asyncio
import logging
import os
import unittest
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

from vettarr.config import runtime
from vettarr.config.schema import Config, Provider
from vettarr.models import MediaType
from vettarr.searcher.prowlarr_client import ProwlarrClient


def indexer(name, base, cookie=None, enable=True):
    fields = [{"name": "baseUrl", "value": base}]
    if cookie is not None:
        fields.append({"name": "cookie", "value": cookie})
    return {"name": name, "enable": enable, "indexerUrls": [base], "fields": fields}


INDEXERS = [
    indexer("Tracker A (API)", "https://tracker-a.invalid/"),
    indexer("Some Tracker", "https://www.sometracker.invalid/", cookie="uid=1; pass=abc"),
]


def provider(base_url, aliases=()):
    return Provider.model_validate({"key": "p", "base_url": base_url, "aliases": list(aliases)})


def cookie_for(base_url, indexers=INDEXERS, aliases=()):
    client = ProwlarrClient()
    resp = SimpleNamespace(json=lambda: indexers, raise_for_status=lambda: None)
    with mock.patch.object(client._client, "get", mock.AsyncMock(return_value=resp)) as get:
        result = asyncio.run(client.cookie_for(provider(base_url, aliases)))
    return result, get


class CookieForProviderTests(unittest.TestCase):
    """Prowlarr's cookie goes to the provider whose hosts hold the indexer, exactly."""

    def test_the_indexer_on_the_base_url_host_answers(self) -> None:
        result, _ = cookie_for("https://www.sometracker.invalid")
        self.assertEqual(result, "uid=1; pass=abc")

    def test_the_scheme_and_case_do_not_matter(self) -> None:
        result, _ = cookie_for("http://WWW.SomeTracker.invalid/")
        self.assertEqual(result, "uid=1; pass=abc")

    def test_an_alias_answers(self) -> None:
        result, _ = cookie_for("https://sometracker.invalid", aliases=["www.sometracker.invalid"])
        self.assertEqual(result, "uid=1; pass=abc")

    def test_the_same_host_without_www_is_another_host(self) -> None:
        """No `www.` folding: a host that is not base_url's or an alias gets nothing."""
        result, _ = cookie_for("https://sometracker.invalid")
        self.assertIsNone(result)

    def test_a_host_that_only_carries_the_indexer_name_gets_no_session(self) -> None:
        """The old name match handed this tracker's session, and the link's id,
        to any host whose letters contained the indexer's name."""
        for host in ("sometracker.oldtld", "evil-sometracker.invalid",
                     "www.sometracker.invalid.evil.example", "some-tracker.invalid"):
            with self.subTest(host=host):
                result, _ = cookie_for(f"https://{host}")
                self.assertIsNone(result)

    def test_a_subdomain_is_not_the_same_host(self) -> None:
        result, _ = cookie_for("https://cdn.www.sometracker.invalid")
        self.assertIsNone(result)

    def test_an_indexer_without_a_cookie_is_no_session(self) -> None:
        result, _ = cookie_for("https://tracker-a.invalid")
        self.assertIsNone(result)

    def test_an_unknown_host_is_no_session(self) -> None:
        result, _ = cookie_for("https://elsewhere.invalid")
        self.assertIsNone(result)

    def test_a_disabled_indexer_is_no_session(self) -> None:
        disabled = [indexer("Some Tracker", "https://sometracker.invalid/", cookie="x", enable=False)]
        result, _ = cookie_for("https://sometracker.invalid", disabled)
        self.assertIsNone(result)

    def test_the_indexer_list_is_asked_for_once(self) -> None:
        client = ProwlarrClient()
        resp = SimpleNamespace(json=lambda: INDEXERS, raise_for_status=lambda: None)
        with mock.patch.object(client._client, "get", mock.AsyncMock(return_value=resp)) as get:
            asyncio.run(client.cookie_for(provider("https://www.sometracker.invalid")))
            asyncio.run(client.cookie_for(provider("https://elsewhere.invalid")))
        self.assertEqual(get.await_count, 1)

    def test_a_failed_list_is_no_session_and_no_crash(self) -> None:
        client = ProwlarrClient()
        with mock.patch.object(client._client, "get", mock.AsyncMock(side_effect=OSError("down"))):
            self.assertIsNone(asyncio.run(client.cookie_for(provider("https://sometracker.invalid"))))


def searched_url(media_type):
    """The URL a search for `media_type` actually puts on the wire."""
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=[])

    client = ProwlarrClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(client.search("Una Pelicula 2024", media_type))
    [url] = seen
    return url


class CategoryTests(unittest.TestCase):
    """httpx replaces a query written into the URL with `params`, so the
    categories the old code put in the URL never reached Prowlarr."""

    def test_the_media_type_s_categories_are_sent(self) -> None:
        url = searched_url(MediaType.MOVIE)
        self.assertEqual(url.params.get_list("categories"), ["2000"])
        self.assertEqual(url.params.get("query"), "Una Pelicula 2024")
        self.assertEqual(url.params.get("limit"), "25")

    def test_each_type_sends_its_own(self) -> None:
        for media_type, wanted in (
            (MediaType.SERIES, ["5000"]),
            (MediaType.EBOOK, ["7020"]),
            (MediaType.AUDIOBOOK, ["3030"]),
        ):
            with self.subTest(media_type=media_type):
                self.assertEqual(searched_url(media_type).params.get_list("categories"), wanted)

    def test_an_unknown_type_searches_every_category(self) -> None:
        url = searched_url(MediaType.UNKNOWN)
        self.assertEqual(url.params.get_list("categories"), [])
        self.assertEqual(url.params.get("query"), "Una Pelicula 2024")


class ConfiguredCategoryTests(unittest.TestCase):
    """integrations.prowlarr.categories decides what each type sends."""

    def setUp(self) -> None:
        cfg = Config.model_validate({
            "integrations": {"prowlarr": {
                "url": "http://prowlarr", "api_key": "k",
                "categories": {"movie": [2040, 2045], "ebook": []},
            }},
        })
        patcher = mock.patch.object(runtime, "_config", cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_listed_type_sends_its_configured_categories(self) -> None:
        self.assertEqual(searched_url(MediaType.MOVIE).params.get_list("categories"), ["2040", "2045"])

    def test_an_empty_list_sends_none(self) -> None:
        self.assertEqual(searched_url(MediaType.EBOOK).params.get_list("categories"), [])

    def test_a_type_not_listed_sends_its_standard_category(self) -> None:
        self.assertEqual(searched_url(MediaType.SERIES).params.get_list("categories"), ["5000"])


if __name__ == "__main__":
    unittest.main()
