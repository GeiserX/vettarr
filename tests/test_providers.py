"""Providers: everything tracker-specific comes from config.yaml.

Hosts a session may go to, link shapes, the logged-out marker, thank and
keep-alive, the User-Agent, categories, and the one search every caller uses.
The HTTP tests drive a real `requests.Session` against a real local server, so
"no request was sent" and "the cookie was not sent" are read off the wire.
"""

from __future__ import annotations

import asyncio
import http.server
import io
import logging
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

import httpx
import requests
from pydantic import ValidationError

logging.disable(logging.CRITICAL)

for _name, _value in {
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
    os.environ.setdefault(_name, _value)

from vettarr import scheduler
from vettarr.config import runtime
from vettarr.config.loader import load_text
from vettarr.config.schema import Config, Provider
from vettarr.models import MediaType
from vettarr.searcher import providers, search_service
from vettarr.searcher.prowlarr_client import ProwlarrClient, SearchResult, TorrentResult
from vettarr.searcher.qbittorrent_client import AddOutcome, QBittorrentClient
from vettarr.searcher.unit3d_client import Unit3dClient

SRC = Path(__file__).resolve().parents[1] / "src" / "vettarr"
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
TORRENT = b"d8:announce10:http://x/a4:infod6:lengthi1e4:name1:x12:piece lengthi1e6:pieces0:ee"
COOKIE_EXPIRY = int(time.time()) + 10 * 365 * 24 * 3600


def provider(**fields) -> Provider:
    return Provider.model_validate({"key": "tracker-a", "base_url": "https://tracker-a.example", **fields})


def example_providers() -> list[Provider]:
    """The commented xbtit, NexusPHP and UNIT3D templates of config.example.yaml."""
    lines = EXAMPLE.read_text().splitlines()
    start = lines.index("providers: []")
    block = []
    for line in lines[start + 1:]:
        if not line.startswith("#"):
            break
        if not line.startswith("##"):
            block.append(line[1:])
    text = (
        "integrations:\n  prowlarr:\n    url: http://prowlarr:9696\n    api_key: k\n"
        "providers:\n" + "\n".join(block)
    )
    return load_text(text, "example", {"TRACKER_C_PASSWORD": "x"}).providers


# --- a tracker on localhost ------------------------------------------------


class FakeTracker:
    """A tracker on 127.0.0.1 that records every request it gets.

    `pages` maps a path (with its query) to (status, body, headers).
    """

    def __init__(self, pages: dict[str, tuple[int, bytes, dict]]) -> None:
        self.pages = pages
        self.requests: list[dict] = []
        tracker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                tracker.requests.append({
                    "method": self.command, "path": self.path,
                    "headers": dict(self.headers), "body": self.rfile.read(length) if length else b"",
                })
                status, body, headers = tracker.pages.get(self.path, (404, b"not found", {}))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _answer

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self, method: str | None = None) -> list[str]:
        return [r["path"] for r in self.requests if method is None or r["method"] == method]


class RecordingAdapter(requests.adapters.BaseAdapter):
    """Answers by (host, path) and records the Cookie header each request carried."""

    def __init__(self, pages) -> None:
        super().__init__()
        self.pages = pages
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        self.sent.append((parts.hostname, parts.path, request.headers.get("Cookie")))
        status, body, headers = self.pages.get((parts.hostname, parts.path), (404, b"", {}))
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO(body)
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass


class WireAdapter(requests.adapters.BaseAdapter):
    """Answers from `handler(request) -> (status, body, headers)` and keeps every request it was sent."""

    def __init__(self, handler) -> None:
        super().__init__()
        self.handler = handler
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        status, body, headers = self.handler(request)
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO(body)
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass

    def to(self, host: str) -> list[requests.PreparedRequest]:
        return [r for r in self.requests if urlsplit(r.url).hostname == host]


LOGGED_IN = b'<html><head><meta name="csrf-token" content="tok123"></head><body>torrents</body></html>'
LOGGED_OUT = b'<html><form><input name="password" type="password"></form></html>'
LOGIN_FORM = (
    b'<html><head><meta name="csrf-token" content="tok123"></head>'
    b'<body><form><input name="password" type="password"></form></body></html>'
)
PROWLARR = {"prowlarr": {"url": "http://prowlarr", "api_key": "k"}}


class WiredProviderCase(unittest.TestCase):
    """One provider of config.yaml, as `init_trackers` builds its client, on a fake wire.

    The client is the app's own (guarded, with the provider's session rule),
    so what a test reads off the wire is what the app would send.
    """

    def build(self, handler, **fields):
        cfg = Config.model_validate({"integrations": PROWLARR, "providers": [{
            "key": "tracker-a", "base_url": "https://tracker-a.example", **fields,
        }]})
        search_service._trackers.clear()
        search_service._trackers_initialized = False
        self.addCleanup(search_service._trackers.clear)
        self.addCleanup(setattr, search_service, "_trackers_initialized", False)
        with mock.patch.object(runtime, "_config", cfg):
            search_service.init_trackers()
        [client] = search_service.get_trackers()
        wire = WireAdapter(handler)
        client.session.mount("https://", wire)
        client.session.mount("http://", wire)
        return cfg.providers[0], client, wire


class TrackerTestCase(unittest.TestCase):
    """A fake tracker, a cookie file for it, and a config naming it."""

    def setUp(self) -> None:
        self.tracker = FakeTracker({"/torrents": (200, LOGGED_IN, {})})
        self.addCleanup(self.tracker.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cookie_file = str(Path(tmp.name) / "cookies.txt")
        Path(self.cookie_file).write_text(
            "# Netscape HTTP Cookie File\n"
            f"127.0.0.1\tFALSE\t/\tFALSE\t{COOKIE_EXPIRY}\tsession\tsecret-value\n"
        )
        search_service._trackers.clear()
        search_service._trackers_initialized = False
        self.addCleanup(search_service._trackers.clear)
        self.addCleanup(setattr, search_service, "_trackers_initialized", False)

    def use(self, **fields) -> Provider:
        cfg = Config.model_validate({"providers": [{
            "key": "tracker-a", "base_url": self.tracker.base, "search": "unit3d",
            "auth": {"from": "cookie_file", "cookie_file": self.cookie_file},
            **fields,
        }]})
        patcher = mock.patch.object(runtime, "_config", cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cfg.providers[0]


# --- hosts -----------------------------------------------------------------


class HostMatchTests(unittest.TestCase):
    """The session goes to base_url's host and the aliases, exactly."""

    p = provider(aliases=["old.tracker-a.example", "tracker-a.example:8443"])

    def test_the_base_url_host_matches(self) -> None:
        self.assertTrue(providers.on_provider(self.p, "https://tracker-a.example/torrents/1"))

    def test_an_alias_matches(self) -> None:
        self.assertTrue(providers.on_provider(self.p, "https://old.tracker-a.example/details.php?id=1"))

    def test_the_scheme_is_ignored(self) -> None:
        self.assertTrue(providers.on_provider(self.p, "http://tracker-a.example/x"))
        self.assertTrue(providers.on_provider(self.p, "HTTP://Tracker-A.Example/x"))

    def test_a_subdomain_does_not_match(self) -> None:
        for url in ("https://cdn.tracker-a.example/x", "https://www.tracker-a.example/x",
                    "https://x.old.tracker-a.example/x"):
            with self.subTest(url=url):
                self.assertFalse(providers.on_provider(self.p, url))

    def test_a_host_carrying_the_name_does_not_match(self) -> None:
        for url in ("https://tracker-a.example.evil.test/x", "https://evil-tracker-a.example/x",
                    "https://evil.test/?u=tracker-a.example", "https://tracker-a.examples/x"):
            with self.subTest(url=url):
                self.assertFalse(providers.on_provider(self.p, url))

    def test_an_alias_with_a_port_matches_that_port_only(self) -> None:
        self.assertTrue(providers.on_provider(self.p, "https://tracker-a.example:8443/x"))
        self.assertFalse(
            providers.on_provider(provider(base_url="https://a.example", aliases=["b.example:8443"]),
                                  "https://b.example:9000/x"),
        )

    def test_a_base_url_written_with_its_scheme_s_own_port_is_the_same_host(self) -> None:
        p = provider(base_url="https://tracker-a.example:443")
        self.assertTrue(providers.on_provider(p, "https://tracker-a.example/torrents/1"))
        self.assertTrue(providers.carries_session(p, "https://tracker-a.example/torrents/1"))

    def test_a_url_the_two_parsers_read_differently_is_nobody_s(self) -> None:
        """`requests` connects to evil.test for this one; `urlsplit` reads tracker-a.example."""
        url = "https://evil.test\\@tracker-a.example/x"
        self.assertEqual(urlsplit(url).hostname, "tracker-a.example")
        self.assertEqual(urlsplit(requests.Request("GET", url).prepare().url).hostname, "evil.test")
        self.assertFalse(providers.on_provider(self.p, url))
        self.assertFalse(providers.carries_session(self.p, url))


class SessionScopeTests(WiredProviderCase):
    """Where the session may travel: the provider's host, its port, and never down to http."""

    def test_the_rule(self) -> None:
        p = provider(aliases=["tracker-a.example:8443", "old.tracker-a.example"])
        for url, allowed in (
            ("https://tracker-a.example/torrents", True),
            ("https://tracker-a.example:443/torrents", True),
            ("https://old.tracker-a.example/torrents", True),
            ("https://tracker-a.example:8443/torrents", True),
            ("http://tracker-a.example/torrents", False),
            ("http://tracker-a.example:8443/torrents", False),
            ("https://tracker-a.example:8080/torrents", False),
            ("https://old.tracker-a.example:8443/torrents", False),
            ("https://cdn.tracker-a.example/torrents", False),
            ("ftp://tracker-a.example/torrents", False),
            ("//tracker-a.example/torrents", False),
        ):
            with self.subTest(url=url):
                self.assertEqual(providers.carries_session(p, url), allowed)

    def test_a_provider_on_plain_http_may_be_reached_over_https_too(self) -> None:
        p = provider(base_url="http://tracker-a.example")
        self.assertTrue(providers.carries_session(p, "http://tracker-a.example/x"))
        self.assertTrue(providers.carries_session(p, "https://tracker-a.example/x"))
        self.assertFalse(providers.carries_session(p, "http://tracker-a.example:8080/x"))

    def redirected(self, location: str):
        def handler(request):
            path = urlsplit(request.url).path
            if path == "/torrents":
                return 200, LOGGED_IN, {}
            if path == "/torrents/download/7":
                return 301, b"", {"Location": location}
            return 200, TORRENT, {}

        _, client, wire = self.build(handler)
        client.use_cookie_header("session=secret-value")
        self.assertEqual(client.fetch_torrent("https://tracker-a.example/torrents/download/7"), TORRENT)
        download, hop = wire.requests[-2:]
        # Positive control: the request that was redirected did carry the cookie.
        self.assertIn("secret-value", download.headers.get("Cookie", ""))
        self.assertEqual(hop.url, location)
        return hop

    def test_a_redirect_down_to_http_leaves_without_the_cookie(self) -> None:
        hop = self.redirected("http://tracker-a.example/file")
        self.assertIsNone(hop.headers.get("Cookie"))

    def test_a_redirect_to_another_port_leaves_without_the_cookie(self) -> None:
        for location in ("https://tracker-a.example:8080/file", "http://tracker-a.example:8080/file"):
            with self.subTest(location=location):
                self.assertIsNone(self.redirected(location).headers.get("Cookie"))

    def test_prowlarr_s_cookie_reaches_an_alias_on_another_domain(self) -> None:
        def handler(request):
            return (200, LOGGED_IN, {}) if urlsplit(request.url).path == "/torrents" else (200, TORRENT, {})

        _, client, wire = self.build(handler, aliases=["tracker-a.test"])
        client.use_cookie_header("session=secret-value")
        self.assertEqual(client.fetch_torrent("https://tracker-a.test/download.php?id=1"), TORRENT)
        [request] = wire.to("tracker-a.test")
        self.assertIn("secret-value", request.headers.get("Cookie", ""))

    def test_a_url_read_two_ways_is_never_fetched(self) -> None:
        _, client, wire = self.build(lambda request: (200, TORRENT, {}))
        client.use_cookie_header("session=secret-value")
        self.assertIsNone(client.fetch_torrent("https://evil.test\\@tracker-a.example/x"))
        self.assertEqual(wire.requests, [])


class RedirectedBodyTests(WiredProviderCase):
    """A 307 or 308 repeats the method, the body and the headers at the new address."""

    PASSWORD = "not-a-real-password"

    def test_a_redirected_login_never_repeats_the_password_elsewhere(self) -> None:
        for status in (307, 308):
            with self.subTest(status=status):
                def handler(request, status=status):
                    if request.method == "POST":
                        return status, b"", {"Location": "https://www.tracker-a.example/login"}
                    return 200, LOGIN_FORM, {}

                _, client, wire = self.build(
                    handler, search="unit3d",
                    auth={"from": "login", "username": "me", "password": self.PASSWORD},
                )
                self.assertFalse(client.login())
                self.assertEqual(wire.to("www.tracker-a.example"), [])
                # Positive control: the form did go to the provider, with the password.
                [post] = [r for r in wire.to("tracker-a.example") if r.method == "POST"]
                self.assertIn(self.PASSWORD, post.body)

    def thank(self, status: int) -> list[requests.PreparedRequest]:
        def handler(request):
            parts = urlsplit(request.url)
            if parts.hostname != "tracker-a.example":
                return 200, b"ok", {}
            if request.method == "POST":
                return status, b"", {"Location": "https://cdn.tracker-a.example/thanks"}
            return 200, LOGGED_IN, {}

        _, client, wire = self.build(handler)
        client.use_cookie_header("session=secret-value")
        client.thank("42", "/thanks.php", "POST", "id={id}")
        # Positive control: the thank went to the provider with its token and its cookie.
        [post] = [r for r in wire.to("tracker-a.example") if r.method == "POST"]
        self.assertEqual(post.headers.get("X-CSRF-TOKEN"), "tok123")
        self.assertIn("secret-value", post.headers.get("Cookie", ""))
        return wire.to("cdn.tracker-a.example")

    def test_a_thank_redirected_with_its_body_is_not_sent_elsewhere(self) -> None:
        for status in (307, 308):
            with self.subTest(status=status):
                self.assertEqual(self.thank(status), [])

    def test_a_thank_redirected_to_a_get_leaves_without_the_token(self) -> None:
        [hop] = self.thank(302)
        self.assertEqual(hop.method, "GET")
        self.assertIsNone(hop.body)
        self.assertIsNone(hop.headers.get("X-CSRF-TOKEN"))
        self.assertIsNone(hop.headers.get("Cookie"))


class LoginSessionTests(WiredProviderCase):
    """`auth.from: login` with no cookie_file: the session lives in memory."""

    def test_one_login_serves_every_later_call(self) -> None:
        signed_in = []

        def handler(request):
            path = urlsplit(request.url).path
            if path == "/login" and request.method == "POST":
                signed_in.append(True)
                return 200, LOGGED_IN, {}
            if path == "/login":
                return 200, LOGIN_FORM, {}
            return 200, LOGGED_IN if signed_in else LOGGED_OUT, {}

        _, client, wire = self.build(
            handler, search="unit3d", auth={"from": "login", "username": "me", "password": "pw"},
        )
        for _ in range(3):
            client.search("Some Film")
            self.assertIsNone(client.last_search_error)
        searches = [r for r in wire.requests if "name=Some" in r.url]
        self.assertEqual(len(searches), 3)
        self.assertEqual(len([r for r in wire.requests if r.method == "POST"]), 1)

    def test_a_session_that_dies_later_is_one_more_login(self) -> None:
        state = {"in": False}

        def handler(request):
            path = urlsplit(request.url).path
            if path == "/login" and request.method == "POST":
                state["in"] = True
                return 200, LOGGED_IN, {}
            if path == "/login":
                return 200, LOGIN_FORM, {}
            return 200, LOGGED_IN if state["in"] else LOGGED_OUT, {}

        _, client, wire = self.build(
            handler, search="unit3d", auth={"from": "login", "username": "me", "password": "pw"},
        )
        client.search("Some Film")
        state["in"] = False
        client.search("Some Film")
        self.assertIsNone(client.last_search_error)
        self.assertEqual(len([r for r in wire.requests if r.method == "POST"]), 2)


class SessionHostTests(TrackerTestCase):
    """On the wire: the cookie reaches the provider's host and nothing else."""

    def test_the_cookie_goes_to_the_provider_s_host(self) -> None:
        p = self.use()
        client = Unit3dClient(p.base_url, self.cookie_file, allowed=lambda u: providers.on_provider(p, u))
        self.assertTrue(client.check_session())
        self.assertIn("secret-value", self.tracker.requests[0]["headers"].get("Cookie", ""))

    def test_a_redirect_to_a_subdomain_leaves_without_the_cookie(self) -> None:
        """`requests` itself sends a host's cookie to its subdomains; the provider session does not."""
        p = provider()
        client = Unit3dClient(p.base_url, "", allowed=lambda u: providers.on_provider(p, u))
        client.use_cookie_header("session=secret-value")
        adapter = RecordingAdapter({
            ("tracker-a.example", "/torrents"): (200, LOGGED_IN, {}),
            ("tracker-a.example", "/torrents/download/7"): (
                302, b"", {"Location": "https://cdn.tracker-a.example/file"},
            ),
            ("cdn.tracker-a.example", "/file"): (200, TORRENT, {}),
        })
        client.session.mount("https://", adapter)
        self.assertEqual(client.download_torrent(7), TORRENT)
        sent = {(host, path): cookie for host, path, cookie in adapter.sent}
        self.assertIn("secret-value", sent[("tracker-a.example", "/torrents/download/7")] or "")
        self.assertIsNone(sent[("cdn.tracker-a.example", "/file")])

    def test_the_same_redirect_without_the_guard_would_leak(self) -> None:
        """Positive control: a plain session sends the cookie to the subdomain."""
        jar = requests.cookies.RequestsCookieJar()
        jar.set("session", "secret-value", domain="tracker-a.example", path="/")
        session = requests.Session()
        session.cookies = jar
        adapter = RecordingAdapter({
            ("tracker-a.example", "/x"): (302, b"", {"Location": "https://cdn.tracker-a.example/file"}),
            ("cdn.tracker-a.example", "/file"): (200, TORRENT, {}),
        })
        session.mount("https://", adapter)
        session.get("https://tracker-a.example/x")
        sent = {(host, path): cookie for host, path, cookie in adapter.sent}
        self.assertIn("secret-value", sent[("cdn.tracker-a.example", "/file")] or "")

    def test_a_torrent_url_off_the_provider_is_never_fetched_with_the_session(self) -> None:
        p = self.use()
        client = Unit3dClient(p.base_url, self.cookie_file, allowed=lambda u: providers.on_provider(p, u))
        self.assertIsNone(client.fetch_torrent("http://localhost:1/download.php?id=1"))
        self.assertEqual(self.tracker.requests, [])


# --- links -----------------------------------------------------------------


class LinkTests(unittest.TestCase):
    """The shipped templates' examples turn into the right download URLs."""

    WANTED = {
        "tracker-a": "https://tracker-a.example/download.php?id=0123456789abcdef0123456789abcdef01234567",
        "tracker-b": "https://tracker-b.example/download.php?id=42",
        "tracker-c": "https://tracker-c.example/torrents/download/42",
    }

    def test_each_software_s_example_becomes_its_download_url(self) -> None:
        templates = example_providers()
        self.assertEqual([p.search for p in templates], ["xbtit", "nexusphp", "unit3d"])
        for p in templates:
            for shape in p.links:
                for example in shape.examples:
                    with self.subTest(provider=p.key, url=example.url):
                        linked = providers.linked_torrent(templates, [example.url])
                        self.assertIsNotNone(linked)
                        self.assertEqual(linked.provider.key, p.key)
                        self.assertEqual(linked.torrent_id, example.id)
                        self.assertEqual(linked.download_url, self.WANTED[p.key])

    def test_an_alias_link_downloads_from_base_url(self) -> None:
        templates = example_providers()
        url = "https://www.tracker-a.example/index.php?page=torrent-details&id=" + "ab" * 20
        linked = providers.linked_torrent(templates, [url])
        self.assertEqual(linked.download_url, "https://tracker-a.example/download.php?id=" + "ab" * 20)

    def test_a_matching_path_on_another_host_is_not_a_link(self) -> None:
        templates = example_providers()
        self.assertIsNone(providers.linked_torrent(templates, ["https://elsewhere.example/details.php?id=42"]))
        self.assertIsNone(providers.linked_torrent(templates, ["https://cdn.tracker-b.example/details.php?id=42"]))

    def test_the_first_link_a_pattern_reads_wins(self) -> None:
        templates = example_providers()
        linked = providers.linked_torrent(
            templates,
            ["https://instagram.com/x", "https://tracker-b.example/details.php?id=7",
             "https://tracker-c.example/torrents/8"],
        )
        self.assertEqual((linked.provider.key, linked.torrent_id), ("tracker-b", "7"))

    def test_a_prowlarr_release_s_details_link_names_the_torrent(self) -> None:
        [_, _, unit3d] = example_providers()
        self.assertEqual(providers.torrent_id(unit3d, "https://tracker-c.example/torrents/42"), "42")
        self.assertIsNone(providers.torrent_id(unit3d, "https://other.example/torrents/42"))

    #: A pattern that passes validation and reads far more than an id.
    GREEDY = {
        "pattern": r"/torrents/(?P<id>[^\s]+)",
        "download_url": "{base}/torrents/download/{id}",
        "examples": [{"url": "https://tracker-a.example/torrents/42", "id": "42"}],
    }

    def test_an_id_that_would_steer_the_request_is_not_read(self) -> None:
        """The id goes into the download URL, the thank path and the thank body as it is."""
        p = provider(links=[self.GREEDY])
        # Positive control: the pattern does read a plain id.
        self.assertEqual(
            providers.linked_torrent([p], ["https://tracker-a.example/torrents/42"]).download_url,
            "https://tracker-a.example/torrents/download/42",
        )
        for link in (
            "https://tracker-a.example/torrents/42/../../../users/me/settings?x=1&role=admin#",
            "https://tracker-a.example/torrents/..",
            "https://tracker-a.example/torrents/42?x=1",
            "https://tracker-a.example/torrents/42&role=admin",
            "https://tracker-a.example/torrents/42%2F..%2Fusers",
        ):
            with self.subTest(link=link):
                self.assertIsNone(providers.linked_torrent([p], [link]))
                self.assertIsNone(providers.torrent_id(p, link))

    def test_a_pattern_that_matches_without_its_id_reads_nothing(self) -> None:
        p = provider(links=[{
            "pattern": r"/torrents/(?P<id>\d+)|/t/\d+",
            "download_url": "{base}/torrents/download/{id}",
            "examples": [{"url": "https://tracker-a.example/torrents/42", "id": "42"}],
        }])
        self.assertIsNone(providers.linked_torrent([p], ["https://tracker-a.example/t/9"]))
        self.assertIsNone(providers.torrent_id(p, "https://tracker-a.example/t/9"))
        self.assertEqual(providers.torrent_id(p, "https://tracker-a.example/torrents/9"), "9")

    def test_an_example_whose_id_is_not_an_id_stops_the_load(self) -> None:
        link = {**self.GREEDY, "examples": [{"url": "https://tracker-a.example/torrents/42/../x", "id": "42/../x"}]}
        with self.assertRaises(ValidationError) as raised:
            provider(links=[link])
        self.assertIn("an id is letters, digits, - and _ only", str(raised.exception))

    def test_a_site_path_starts_with_a_slash(self) -> None:
        """`base_url + path` without the slash names another host."""
        for block in (
            {"thank": {"enabled": True, "path": ".evil.test/{id}"}},
            {"thank": {"enabled": True, "path": "torrents/{id}/thank"}},
            {"keep_alive": {"enabled": True, "path": "torrents"}},
        ):
            with self.subTest(block=block), self.assertRaises(ValidationError):
                provider(**block)
        ok = provider(thank={"enabled": True, "path": "/torrents/{id}/thank"}, keep_alive={"path": "/torrents"})
        self.assertEqual((ok.thank.path, ok.keep_alive.path), ("/torrents/{id}/thank", "/torrents"))


class LogTests(unittest.TestCase):
    def test_a_url_in_a_log_line_has_no_password_and_no_passkey(self) -> None:
        self.assertEqual(
            providers.shown("https://user:pw@tracker-a.example:8443/download.php?id=42&passkey=abcdef"),
            "https://tracker-a.example:8443/download.php?id=42&passkey=...",
        )
        self.assertEqual(
            providers.shown("https://tracker-a.example/torrents/download/7"),
            "https://tracker-a.example/torrents/download/7",
        )

    def test_a_download_logs_neither(self) -> None:
        client = Unit3dClient("https://user:pw@tracker-a.example", "")
        client.use_cookie_header("session=secret-value")
        # Signed in, and the download answers a page instead of a torrent.
        client.session.mount("https://", WireAdapter(
            lambda request: (200, LOGGED_IN if urlsplit(request.url).path == "/torrents" else LOGGED_OUT, {}),
        ))
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        with self.assertLogs("vettarr.searcher.unit3d_client", level="INFO") as logs:
            client.fetch_torrent("https://user:pw@tracker-a.example/download.php?id=42&passkey=abcdef")
        text = "\n".join(logs.output)
        self.assertIn("download.php?id=42", text)
        self.assertNotIn("pw@", text)
        self.assertNotIn("abcdef", text)


# --- the logged-out marker -------------------------------------------------


class LoggedOutMarkerTests(TrackerTestCase):
    def test_the_configured_marker_reads_a_page_as_logged_out(self) -> None:
        self.tracker.pages["/torrents"] = (200, b"<p>Bitte anmelden</p>", {})
        p = self.use(logged_out_marker="Bitte anmelden")
        search_service.init_trackers()
        [client] = search_service.get_trackers()
        self.assertEqual(client.logged_out_marker, "Bitte anmelden")
        self.assertFalse(client.check_session())

    def test_a_login_page_instead_of_a_torrent_is_no_torrent(self) -> None:
        self.tracker.pages["/torrents/download/9"] = (200, LOGGED_OUT, {"Content-Type": "application/x-bittorrent"})
        p = self.use()
        client = Unit3dClient(p.base_url, self.cookie_file, allowed=lambda u: providers.on_provider(p, u))
        self.assertIsNone(client.download_torrent(9))


# --- thank -----------------------------------------------------------------


class ThankTests(TrackerTestCase):
    def thank(self, **fields) -> None:
        self.tracker.pages["/thanks.php"] = (200, b"ok", {})
        p = self.use(**fields)
        asyncio.run(search_service.thank_after_download(p, "42"))

    def test_off_by_default_nothing_is_sent(self) -> None:
        self.thank()
        self.assertEqual(self.tracker.requests, [])

    def test_off_when_disabled_even_with_a_path(self) -> None:
        self.thank(thank={"enabled": False, "path": "/thanks.php"})
        self.assertEqual(self.tracker.requests, [])

    def test_on_it_sends_the_configured_request(self) -> None:
        self.thank(thank={"enabled": True, "path": "/thanks.php", "method": "POST", "body": "id={id}"})
        [post] = [r for r in self.tracker.requests if r["method"] == "POST"]
        self.assertEqual(post["path"], "/thanks.php")
        self.assertEqual(post["body"], b"id=42")
        self.assertEqual(post["headers"].get("X-CSRF-TOKEN"), "tok123")
        self.assertIn("secret-value", post["headers"].get("Cookie", ""))

    def test_the_id_goes_into_the_path_and_a_mapping_body(self) -> None:
        self.tracker.pages["/t/42/thank"] = (200, b"ok", {})
        self.thank(thank={"enabled": True, "path": "/t/{id}/thank", "method": "PUT", "body": {"torrent": "{id}"}})
        [put] = [r for r in self.tracker.requests if r["method"] == "PUT"]
        self.assertEqual(put["path"], "/t/42/thank")
        self.assertEqual(put["body"], b"torrent=42")

    def test_a_download_this_app_added_is_thanked_once(self) -> None:
        self.tracker.pages["/thanks.php"] = (200, b"ok", {})
        p = self.use(
            thank={"enabled": True, "path": "/thanks.php", "body": "id={id}"},
            links=[{
                "pattern": r"/torrents/(?P<id>\d+)",
                "download_url": "{base}/torrents/download/{id}",
                "examples": [{"url": "https://x.example/torrents/1", "id": "1"}],
            }],
        )
        release = TorrentResult(
            guid=f"{p.base_url}/torrents/42", title="A", info_url=f"{p.base_url}/torrents/42",
            download_url="http://prowlarr/1/download",
        )
        for created, thanks in ((True, 1), (False, 1)):
            with self.subTest(created=created):
                qbt = SimpleNamespace(add_torrent=mock.AsyncMock(return_value=AddOutcome("h", created)))
                with mock.patch.object(search_service, "qbittorrent", qbt):
                    asyncio.run(search_service.add_release(release, MediaType.MOVIE))
                self.assertEqual(self.tracker.paths("POST"), ["/thanks.php"] * thanks)


# --- keep-alive ------------------------------------------------------------


class KeepAliveTests(TrackerTestCase):
    def jobs(self, cfg: Config):
        sched = scheduler.Scheduler()
        sched.add_keep_alive_jobs(cfg)
        return {job.id: job for job in sched._scheduler.get_jobs()}

    def test_off_by_default_no_job(self) -> None:
        cfg = Config.model_validate({"providers": [{"key": "a", "base_url": "https://a.example"}]})
        self.assertEqual(self.jobs(cfg), {})

    def test_on_it_runs_on_the_configured_interval(self) -> None:
        cfg = Config.model_validate({"providers": [
            {"key": "a", "base_url": "https://a.example", "keep_alive": {"enabled": True, "every_minutes": 45}},
            {"key": "b", "base_url": "https://b.example"},
        ]})
        jobs = self.jobs(cfg)
        self.assertEqual(list(jobs), ["keep_alive_a"])
        job = jobs["keep_alive_a"]
        self.assertEqual(job.trigger.interval, timedelta(minutes=45))
        self.assertEqual(job.args, ("a",))
        self.assertIs(job.func, search_service.keep_alive)

    def test_no_jitter_is_the_fixed_interval(self) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        cfg = Config.model_validate({"providers": [
            {"key": "a", "base_url": "https://a.example", "keep_alive": {"enabled": True, "every_minutes": 45}},
        ]})
        keep = cfg.providers[0].keep_alive
        self.assertEqual(keep.jitter_minutes, 0)
        self.assertIs(type(self.jobs(cfg)["keep_alive_a"].trigger), IntervalTrigger)
        never = mock.Mock(side_effect=AssertionError("no jitter draws nothing"))
        self.assertEqual(scheduler.keep_alive_wait(keep, never), timedelta(minutes=45))

    def test_each_wait_is_the_interval_plus_or_minus_up_to_the_jitter(self) -> None:
        keep = provider(keep_alive={"enabled": True, "every_minutes": 240, "jitter_minutes": 90}).keep_alive
        draws = []

        def lowest(low, high):
            draws.append((low, high))
            return low

        self.assertEqual(scheduler.keep_alive_wait(keep, lowest), timedelta(minutes=150))
        self.assertEqual(scheduler.keep_alive_wait(keep, lambda low, high: high), timedelta(minutes=330))
        self.assertEqual(scheduler.keep_alive_wait(keep, lambda low, high: 12.5), timedelta(minutes=252.5))
        self.assertEqual(draws, [(-90, 90)])
        # And with the real generator, every wait is inside the band and they are not all the same.
        waits = {scheduler.keep_alive_wait(keep) for _ in range(200)}
        self.assertGreater(len(waits), 100)
        self.assertGreaterEqual(min(waits), timedelta(minutes=150))
        self.assertLessEqual(max(waits), timedelta(minutes=330))

    def test_a_wait_is_never_below_one_minute(self) -> None:
        keep = provider(keep_alive={"enabled": True, "every_minutes": 5, "jitter_minutes": 60}).keep_alive
        self.assertEqual(scheduler.keep_alive_wait(keep, lambda low, high: low), timedelta(minutes=1))
        self.assertGreaterEqual(min(scheduler.keep_alive_wait(keep) for _ in range(200)), timedelta(minutes=1))

    def test_the_job_draws_its_next_visit_from_the_one_before(self) -> None:
        cfg = Config.model_validate({"providers": [
            {"key": "a", "base_url": "https://a.example",
             "keep_alive": {"enabled": True, "every_minutes": 240, "jitter_minutes": 90}},
        ]})
        before = datetime.now(timezone.utc)
        job = self.jobs(cfg)["keep_alive_a"]
        # The first visit is still a minute after the start, whatever the jitter.
        self.assertLess(abs(job.next_run_time - before - timedelta(minutes=1)), timedelta(seconds=5))
        self.assertEqual(job.trigger.interval, timedelta(minutes=240))
        previous = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(scheduler.random, "uniform", return_value=-30.0) as uniform:
            self.assertEqual(
                job.trigger.get_next_fire_time(previous, previous), previous + timedelta(minutes=210),
            )
        uniform.assert_called_once_with(-90, 90)

    def test_a_jitter_as_wide_as_the_interval_is_warned_about(self) -> None:
        """Then a large share of the waits is the 1-minute floor: visit after visit."""
        from vettarr.config.schema import warnings

        def about_keep_alive(every: int, jitter: int, enabled: bool = True) -> list:
            cfg = Config.model_validate({"providers": [
                {"key": "a", "base_url": "https://a.example",
                 "keep_alive": {"enabled": enabled, "every_minutes": every, "jitter_minutes": jitter}},
            ]})
            return [(loc, text) for loc, text in warnings(cfg) if loc == ("providers", 0, "keep_alive")]

        self.assertEqual(about_keep_alive(240, 90), [])
        self.assertEqual(about_keep_alive(240, 239), [])
        self.assertEqual(about_keep_alive(5, 60, enabled=False), [])
        for every, jitter in ((240, 240), (5, 60)):
            with self.subTest(every=every, jitter=jitter):
                ((_, text),) = about_keep_alive(every, jitter)
                self.assertIn("jitter_minutes is not below every_minutes", text)

    def test_a_negative_jitter_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            provider(keep_alive={"enabled": True, "jitter_minutes": -1})

    def test_the_doc_and_the_example_name_the_jitter(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertIn("| `keep_alive.jitter_minutes` | int ≥ 0 | `0` |", (root / "docs" / "CONFIG.md").read_text())
        self.assertIn("jitter_minutes: 0", (root / "config.example.yaml").read_text())

    def test_an_environment_boot_schedules_none(self) -> None:
        """Neither is opt-in if booting from the environment turns it on."""
        env = {name: os.environ[name] for name in runtime.REQUIRED_ENV}
        env.update({
            "TRACKER_1_URL": "https://tracker-a.example",
            "TRACKER_2_URL": "https://tracker-b.example", "TRACKER_2_COOKIE_PATH": "/data/cookies/b.txt",
            "TRACKER_3_URL": "https://tracker-c.example", "TRACKER_3_USERNAME": "me", "TRACKER_3_PASSWORD": "pw",
        })
        cfg = runtime.from_environment(env)
        self.assertEqual([p.auth.from_ for p in cfg.providers], ["prowlarr", "cookie_file", "login"])
        self.assertEqual([(p.thank.enabled, p.keep_alive.enabled) for p in cfg.providers], [(False, False)] * 3)
        self.assertEqual(self.jobs(cfg), {})
        # The tracker with nothing but a URL has no way to be contacted at all.
        url_only = cfg.providers[0]
        self.assertEqual((url_only.search, url_only.links), ("none", []))

    def test_the_visit_goes_to_the_configured_path(self) -> None:
        self.tracker.pages["/account"] = (200, LOGGED_IN, {})
        self.use(keep_alive={"enabled": True, "path": "/account"})
        self.assertTrue(asyncio.run(search_service.keep_alive("tracker-a")))
        self.assertEqual(self.tracker.paths(), ["/torrents", "/account"])


# --- the User-Agent --------------------------------------------------------


class UserAgentTests(TrackerTestCase):
    def test_the_project_user_agent_is_sent(self) -> None:
        self.assertRegex(
            providers.USER_AGENT, r"^vettarr/\S+ \(\+https://github\.com/GeiserX/vettarr\)$",
        )
        p = self.use()
        client = Unit3dClient(p.base_url, self.cookie_file, allowed=lambda u: providers.on_provider(p, u))
        client.check_session()
        self.assertEqual(self.tracker.requests[0]["headers"]["User-Agent"], providers.USER_AGENT)

    def test_no_browser_user_agent_is_spoofed_by_the_tracker_code(self) -> None:
        for path in (SRC / "searcher").glob("*.py"):
            with self.subTest(module=path.name):
                text = path.read_text()
                self.assertIsNone(re.search(r"Mozilla/\d", text))
                self.assertNotIn("Chrome/", text)


# --- qBittorrent categories and save paths ---------------------------------


class QBittorrentConfigTests(unittest.TestCase):
    def added_with(self, cfg: Config, media_type: MediaType) -> dict:
        client = QBittorrentClient.__new__(QBittorrentClient)
        client._login = mock.AsyncMock()
        client._paths_cache = None
        client._request = mock.AsyncMock(return_value=httpx.Response(200, text="Ok."))
        with mock.patch.object(runtime, "_config", cfg):
            asyncio.run(client.add_torrent_file(TORRENT, media_type))
        [post] = [c for c in client._request.await_args_list if c.args[:2] == ("POST", "/api/v2/torrents/add")]
        return post.kwargs["data"]

    def test_category_and_save_path_come_from_config(self) -> None:
        cfg = Config.model_validate({"integrations": {"qbittorrent": {
            "url": "http://qbittorrent",
            "categories": {"movie": "films"}, "save_paths": {"movie": "/media/films"},
        }}})
        self.assertEqual(self.added_with(cfg, MediaType.MOVIE), {"category": "films", "savepath": "/media/films"})

    def test_a_type_not_listed_gets_neither(self) -> None:
        cfg = Config.model_validate({"integrations": {"qbittorrent": {
            "url": "http://qbittorrent", "categories": {"movie": "films"},
        }}})
        self.assertEqual(self.added_with(cfg, MediaType.EBOOK), {})

    def test_no_category_name_lives_in_code(self) -> None:
        """With none configured, no type is added under a category or a path of the code's own."""
        cfg = Config.model_validate({"integrations": {"qbittorrent": {"url": "http://qbittorrent"}}})
        types = [t for t in MediaType if t is not MediaType.UNKNOWN]
        self.assertGreaterEqual(len(types), 7)
        for media_type in types:
            with self.subTest(media_type=media_type):
                self.assertEqual(self.added_with(cfg, media_type), {})


# --- one search for every caller -------------------------------------------


class SearchForTests(unittest.TestCase):
    """The worker, actions.search (the web UI and the bot) all search through search_for."""

    def test_the_worker_searches_through_search_for(self) -> None:
        item = search_service.ItemRef(
            id=1, title="Some Film", year=2024, quality=None, season=None,
            media_type=MediaType.MOVIE, telegram_message_id=1,
        )
        search_for = mock.AsyncMock(return_value=SearchResult(error=None))
        session = _NullSession()
        with (
            mock.patch.object(search_service, "search_for", search_for),
            mock.patch.object(search_service, "fetch_linked_torrent", mock.AsyncMock(return_value=None)),
            mock.patch.object(search_service, "init_trackers", lambda: None),
            mock.patch.object(search_service, "async_session", lambda: session),
        ):
            self.assertEqual(asyncio.run(search_service.search_and_download(item)), "no_match")
        search_for.assert_awaited_once_with(item)

    def test_actions_search_goes_through_search_for(self) -> None:
        source = (SRC / "actions.py").read_text()
        self.assertIn("search_service.search_for(", source)
        self.assertNotIn("search_service.find_releases(", source)

    def test_the_web_ui_and_the_bot_search_through_actions_search(self) -> None:
        for relative in ("web/routes/items.py", "bot/handlers.py"):
            with self.subTest(surface=relative):
                source = (SRC / relative).read_text()
                self.assertIn("actions.search(", source)
                self.assertNotIn("find_releases", source)
                self.assertNotIn("prowlarr.search", source)

    def test_a_direct_search_runs_when_prowlarr_finds_nothing(self) -> None:
        cfg = Config.model_validate({"integrations": PROWLARR, "providers": [
            {"key": "quiet", "base_url": "https://quiet.example"},
            {"key": "direct", "base_url": "https://direct.example", "search": "nexusphp",
             "auth": {"from": "cookie_file", "cookie_file": "/nonexistent"}},
        ]})
        hit = SimpleNamespace(
            torrent_id="7", name="Some Film 2024", size_bytes=10, seeders=3, leechers=0,
            download_url="https://direct.example/download.php?id=7",
            detail_url="https://direct.example/details.php?id=7",
        )
        client = SimpleNamespace(name="direct", search_with_status=mock.Mock(return_value=([hit], None)))
        prowlarr = SimpleNamespace(
            build_query=lambda *a: "q", search=mock.AsyncMock(return_value=SearchResult()),
        )
        item = search_service.SearchItem(
            "Some Film", 2024, None, MediaType.MOVIE, imdb_id="tt0133093", tmdb_id=603,
        )
        with (
            mock.patch.object(search_service, "prowlarr", prowlarr),
            mock.patch.object(search_service, "_trackers", [client]),
            mock.patch.object(search_service, "init_trackers", lambda: None),
        ):
            result = asyncio.run(search_service.search_for(item, cfg))
        prowlarr.search.assert_awaited_once()
        [release] = result.results
        self.assertEqual((release.provider, release.provider_id), ("direct", "7"))
        self.assertEqual(release.download_url, "https://direct.example/download.php?id=7")
        self.assertIsNone(result.error)
        # The tracker is asked for this title's ids, not for the name alone.
        client.search_with_status.assert_called_once_with("Some Film", imdb_id="tt0133093", tmdb_id=603)

    def test_prowlarr_is_not_asked_when_it_is_not_configured(self) -> None:
        """No request, and no "Prowlarr search failed" traceback per search."""
        hit = SimpleNamespace(
            torrent_id="7", name="Some Film 2024", size_bytes=10, seeders=3, leechers=0,
            download_url="https://direct.example/download.php?id=7",
            detail_url="https://direct.example/details.php?id=7",
        )
        direct = {"key": "direct", "base_url": "https://direct.example", "search": "nexusphp",
                  "auth": {"from": "cookie_file", "cookie_file": "/nonexistent"}}
        item = search_service.SearchItem("Some Film", 2024, None, MediaType.MOVIE)
        for integrations in ({}, {"prowlarr": {"enabled": False}}):
            for found, error in (([hit], None), ([], None), ([], "session expired")):
                with self.subTest(integrations=integrations, found=len(found), error=error):
                    cfg = Config.model_validate({"integrations": integrations, "providers": [direct]})
                    client = SimpleNamespace(
                        name="direct", search_with_status=mock.Mock(return_value=(found, error)),
                    )
                    prowlarr = SimpleNamespace(
                        build_query=ProwlarrClient.build_query,
                        search=mock.AsyncMock(return_value=SearchResult(error="ConnectError")),
                    )
                    with (
                        mock.patch.object(search_service, "prowlarr", prowlarr),
                        mock.patch.object(search_service, "_trackers", [client]),
                        mock.patch.object(search_service, "init_trackers", lambda: None),
                    ):
                        result = asyncio.run(search_service.search_for(item, cfg))
                    prowlarr.search.assert_not_awaited()
                    self.assertEqual(len(result.results), len(found))
                    self.assertEqual(result.error, search_service.ALL_UNAVAILABLE if error else None)

    def test_nothing_to_search_with_says_so(self) -> None:
        prowlarr = SimpleNamespace(build_query=ProwlarrClient.build_query, search=mock.AsyncMock())
        item = search_service.SearchItem("Some Film", 2024, None, MediaType.MOVIE)
        with mock.patch.object(search_service, "prowlarr", prowlarr):
            result = asyncio.run(search_service.search_for(item, Config.model_validate({})))
        prowlarr.search.assert_not_awaited()
        self.assertEqual(result.error, search_service.NO_SEARCH)
        self.assertEqual(search_service.search_failure(result), "No search is configured")

    def test_one_wording_for_a_search_that_did_not_complete(self) -> None:
        for error, shown in (
            (None, None),
            ("ConnectError", "Prowlarr search unavailable"),
            (search_service.ALL_UNAVAILABLE, "All search providers unavailable"),
            (search_service.NO_SEARCH, "No search is configured"),
        ):
            with self.subTest(error=error):
                self.assertEqual(search_service.search_failure(SearchResult(error=error)), shown)

    def test_a_direct_release_is_fetched_with_its_provider_s_session(self) -> None:
        cfg = Config.model_validate({"providers": [
            {"key": "direct", "base_url": "https://direct.example", "search": "nexusphp",
             "auth": {"from": "cookie_file", "cookie_file": "/nonexistent"}},
        ]})
        stored = {
            "title": "Some Film", "download_url": "https://direct.example/download.php?id=7",
            "provider": "direct", "provider_id": "7",
        }
        client = SimpleNamespace(name="direct", fetch_torrent=mock.Mock(return_value=TORRENT))
        qbt = SimpleNamespace(
            add_torrent=mock.AsyncMock(), add_torrent_file=mock.AsyncMock(return_value=AddOutcome("h", True)),
        )
        with (
            mock.patch.object(runtime, "_config", cfg),
            mock.patch.object(search_service, "_trackers", [client]),
            mock.patch.object(search_service, "init_trackers", lambda: None),
            mock.patch.object(search_service, "qbittorrent", qbt),
        ):
            outcome = asyncio.run(search_service.add_release(stored, MediaType.MOVIE))
        self.assertTrue(outcome.created)
        client.fetch_torrent.assert_called_once_with("https://direct.example/download.php?id=7")
        qbt.add_torrent_file.assert_awaited_once_with(TORRENT, MediaType.MOVIE)
        qbt.add_torrent.assert_not_awaited()

    def test_a_tracker_refusing_the_torrent_is_said_as_such(self) -> None:
        """A refused .torrent read "qBittorrent did not take the torrent", which sent
        a person to the download client when the fix was the tracker's cookies."""
        cfg = Config.model_validate({"providers": [
            {"key": "direct", "base_url": "https://direct.example", "search": "nexusphp",
             "auth": {"from": "cookie_file", "cookie_file": "/nonexistent"}},
        ]})
        stored = {"title": "Some Film", "download_url": "https://direct.example/d?id=7",
                  "provider": "direct", "provider_id": "7"}
        client = SimpleNamespace(name="direct", fetch_torrent=mock.Mock(return_value=None))
        qbt = SimpleNamespace(add_torrent=mock.AsyncMock(), add_torrent_file=mock.AsyncMock())
        with (
            mock.patch.object(runtime, "_config", cfg),
            mock.patch.object(search_service, "_trackers", [client]),
            mock.patch.object(search_service, "init_trackers", lambda: None),
            mock.patch.object(search_service, "qbittorrent", qbt),
        ):
            outcome = asyncio.run(search_service.add_release(stored, MediaType.MOVIE))
        self.assertFalse(outcome)
        self.assertIn("The tracker refused the .torrent", search_service.refusal_of(outcome))
        self.assertIn("direct", outcome.reason)
        self.assertIn("Paste new cookies in Setup", outcome.reason)
        qbt.add_torrent_file.assert_not_awaited()
        # The download client saying no is the client's sentence.
        self.assertEqual(search_service.refusal_of(None), search_service.CLIENT_REFUSED)

    def test_every_search_failing_is_an_error(self) -> None:
        cfg = Config.model_validate({"integrations": PROWLARR, "providers": [
            {"key": "direct", "base_url": "https://direct.example", "search": "unit3d",
             "auth": {"from": "cookie_file", "cookie_file": "/nonexistent"}},
        ]})
        client = SimpleNamespace(name="direct", search_with_status=mock.Mock(return_value=([], "session expired")))
        prowlarr = SimpleNamespace(
            build_query=lambda *a: "q", search=mock.AsyncMock(return_value=SearchResult(error="ConnectError")),
        )
        item = search_service.SearchItem("Some Film", 2024, None, MediaType.MOVIE)
        with (
            mock.patch.object(search_service, "prowlarr", prowlarr),
            mock.patch.object(search_service, "_trackers", [client]),
            mock.patch.object(search_service, "init_trackers", lambda: None),
        ):
            result = asyncio.run(search_service.search_for(item, cfg))
        self.assertEqual(result.error, search_service.ALL_UNAVAILABLE)


class _NullSession:
    def add(self, obj) -> None:
        obj.id = 1

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, _statement):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# --- NexusPHP and xbtit result lists ---------------------------------------


NEXUSPHP_LIST = """
<table class="torrents"><tr><td class="colhead">Type</td></tr>
<tr>
  <td><img alt="Movies"></td>
  <td><table class="torrentname"><tr><td><a title="Some Film 2024 1080p" href="details.php?id=101&amp;hit=1"><b>Some Film</b></a></td>
      <td><a href="download.php?id=101"><img alt="download"></a></td></tr></table></td>
  <td>3</td>
  <td>2026-10-01</td>
  <td>4.20<br>GB</td>
  <td><b><a href="details.php?id=101#peers">17</a></b></td>
  <td>2</td>
  <td>40</td>
</tr></table>
"""

XBTIT_LIST = """
<table><tr>
  <td>Movies</td>
  <td><a href="index.php?page=torrent-details&amp;id=0123456789abcdef0123456789abcdef01234567">Some Film 2024</a></td>
  <td><a href="download.php?id=0123456789abcdef0123456789abcdef01234567&amp;f=Some.torrent">DL</a></td>
  <td>01/10/2026</td>
  <td>700.5 MB</td>
  <td>9</td>
  <td>1</td>
</tr></table>
"""


class ListingParserTests(unittest.TestCase):
    def test_a_nexusphp_list(self) -> None:
        client = Unit3dClient("https://tracker-b.example", "", software="nexusphp")
        [row] = client._parse_listing(NEXUSPHP_LIST)
        self.assertEqual(row.torrent_id, "101")
        self.assertEqual(row.name, "Some Film 2024 1080p")
        self.assertEqual(row.download_url, "https://tracker-b.example/download.php?id=101")
        self.assertEqual((row.size_str, row.seeders, row.leechers), ("4.20 GB", 17, 2))

    def test_an_xbtit_list(self) -> None:
        client = Unit3dClient("https://tracker-a.example", "", software="xbtit")
        [row] = client._parse_listing(XBTIT_LIST)
        self.assertEqual(row.torrent_id, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(row.name, "Some Film 2024")
        self.assertEqual((row.size_str, row.seeders, row.leechers), ("700.5 MB", 9, 1))


if __name__ == "__main__":
    unittest.main()
