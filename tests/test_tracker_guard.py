"""Tests for the tracker cooldown guard.

These drive a real `requests.Session` through a fake adapter that counts every
request, so a test that says "no request was sent" is checking the wire, not a
mock of our own code.
"""

from __future__ import annotations

import ast
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests

# search_service reads config at import time, and config wants a real
# environment. Same defaults as the other test modules.
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
from vettarr.config.schema import Config
from vettarr.searcher import search_service, tracker_guard, unit3d_client
from vettarr.searcher.tracker_guard import (
    BASE_BACKOFF_SECONDS,
    ERROR_STATUS_LIMIT,
    MAX_BACKOFF_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    RATE_LIMIT_FAILURE,
    GuardedUnit3dClient,
    TrackerGuard,
    parse_retry_after,
)
from vettarr.searcher.unit3d_client import Unit3dClient

BASE_URL = "https://tracker.example"

# A dead session answers with the login form, and its password field is the
# logged-out marker the client looks for by default.
LOGGED_OUT_PAGE = (
    "<html><body><form action='/login'>"
    '<input name="password" type="password"></form></body></html>'
)
LOGIN_FORM = (
    '<html><head><meta name="csrf-token" content="tok123"></head>'
    '<body><form><input name="password" type="password"></form></body></html>'
)
RATE_LIMITED_PAGE = "<html><body><h1>Too Many Requests</h1></body></html>"
# Neither of these carries a login form, so the vendored "am I logged in?"
# text check reads both as a live session.
CHALLENGE_PAGE = "<html><body><h1>Checking your browser</h1></body></html>"
SERVER_ERROR_PAGE = "<html><body><h1>Service Unavailable</h1></body></html>"
TORRENT_LIST = """
<html><body><table><tbody><tr>
  <td><a class="view-torrent" href="/torrents/42">Example S01</a></td>
  <td class="torrent-listings-size">8.71 GiB</td>
  <td class="torrent-listings-seeders">25</td>
  <td class="torrent-listings-leechers">2</td>
  <td><a href="/torrents/download/42">Download</a></td>
</tr></tbody></table></body></html>
"""

# A Netscape cookie whose expiry is 0 is loaded from the file but never put on
# the wire — the jar drops it as expired on the way out. A test that asks "which
# cookies did the tracker actually see?" needs one that survives that, so this
# expiry is computed rather than pinned to a date that eventually goes stale.
COOKIE_EXPIRY = int(time.time()) + 10 * 365 * 24 * 3600


def cookie_file(value: str = "value") -> str:
    return (
        "# Netscape HTTP Cookie File\n"
        f"tracker.example\tFALSE\t/\tFALSE\t{COOKIE_EXPIRY}\tsession\t{value}\n"
    )


COOKIE_FILE = cookie_file()


class FakeAdapter(requests.adapters.BaseAdapter):
    """Counts every request and answers from a caller-supplied handler."""

    def __init__(self, handler) -> None:
        super().__init__()
        self.handler = handler
        self.sent: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        self.sent.append((request.method, request.path_url))
        status, body, headers = self.handler(request)
        response = requests.Response()
        response.status_code = status
        response._content = body.encode()
        response.headers.update(headers or {})
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p in self.sent if method is None or m == method]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_scratch: list[TemporaryDirectory] = []


def build_client(handler, cookie_path: str = "", clock=None) -> tuple[GuardedUnit3dClient, FakeAdapter]:
    if not cookie_path:
        # The vendored client persists cookies on every healthy response, so it
        # always needs a real path to write to.
        tmp = TemporaryDirectory()
        _scratch.append(tmp)
        cookie_path = str(Path(tmp.name) / "cookies.txt")
        Path(cookie_path).write_text(COOKIE_FILE)
    guard = TrackerGuard("test", cookie_path, clock=clock) if clock else None
    client = GuardedUnit3dClient(
        BASE_URL, cookie_path, name="test", username="u", password="p", guard=guard,
    )
    client._cookies_loaded = True  # skip disk cookie loading unless a test wants it
    adapter = FakeAdapter(handler)
    client.session.mount("https://", adapter)
    return client, adapter


def tearDownModule() -> None:
    for tmp in _scratch:
        tmp.cleanup()
    _scratch.clear()


def rate_limited(retry_after: str | None = None):
    headers = {"Retry-After": retry_after} if retry_after else {}

    def handler(_request):
        return 429, RATE_LIMITED_PAGE, headers

    return handler


def login_always_fails(_request):
    if _request.path_url.startswith("/login"):
        return 200, LOGIN_FORM, {}
    return 200, LOGGED_OUT_PAGE, {}


class RetryAfterParsingTests(unittest.TestCase):
    def test_seconds_form(self) -> None:
        self.assertEqual(parse_retry_after("120"), 120.0)

    def test_http_date_form(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=300)
        parsed = parse_retry_after(format_datetime(when, usegmt=True))
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 250)
        self.assertLess(parsed, 320)

    def test_garbage_and_missing(self) -> None:
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after(""))
        self.assertIsNone(parse_retry_after("soon"))

    def test_past_date_is_clamped_to_zero(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.assertEqual(parse_retry_after(format_datetime(when, usegmt=True)), 0.0)


class RateLimitTests(unittest.TestCase):
    def test_429_stops_every_further_request(self) -> None:
        client, adapter = build_client(rate_limited("60"))

        self.assertEqual(client.search("first"), [])
        self.assertEqual(len(adapter.sent), 1, adapter.sent)

        for _ in range(5):
            self.assertEqual(client.search("again"), [])
        self.assertEqual(len(adapter.sent), 1, adapter.sent)
        self.assertIn("429", client.last_search_error or "")

    def test_429_never_triggers_a_login(self) -> None:
        client, adapter = build_client(rate_limited())
        client.search("x")
        self.assertEqual(adapter.paths("POST"), [])
        self.assertNotIn("/login", adapter.paths())

    def test_retry_after_longer_than_backoff_wins(self) -> None:
        long_wait = MAX_BACKOFF_SECONDS / 2 + 600
        client, _ = build_client(rate_limited(str(int(long_wait))))
        client.search("x")
        self.assertGreaterEqual(client._guard.blocked_seconds(), long_wait - 5)

    def test_an_absurd_retry_after_is_capped(self) -> None:
        # A tracker (or something in front of it) asking for a week off must not
        # park us until the next restart.
        client, _ = build_client(rate_limited(str(7 * 24 * 3600)))
        client.search("x")
        self.assertLessEqual(client._guard.blocked_seconds(), MAX_RETRY_AFTER_SECONDS)
        self.assertGreaterEqual(client._guard.blocked_seconds(), MAX_RETRY_AFTER_SECONDS - 5)

    def test_a_longer_retry_after_extends_an_open_cooldown(self) -> None:
        clock = FakeClock()
        guard = TrackerGuard("test", clock=clock)
        guard.note_failure("first", retry_after=60.0, kind=RATE_LIMIT_FAILURE)
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS)

        guard.note_failure("second", retry_after=3600.0, kind=RATE_LIMIT_FAILURE)
        self.assertEqual(guard.blocked_seconds(), 3600.0)
        # Still one failure: the window was extended, not reopened.
        self.assertEqual(guard.failures, 1)

        # A shorter one afterwards never pulls the window in.
        guard.note_failure("third", retry_after=30.0, kind=RATE_LIMIT_FAILURE)
        self.assertEqual(guard.blocked_seconds(), 3600.0)

    def test_retry_after_shorter_than_backoff_keeps_backoff(self) -> None:
        client, _ = build_client(rate_limited("5"))
        client.search("x")
        self.assertGreaterEqual(client._guard.blocked_seconds(), BASE_BACKOFF_SECONDS - 5)

    def test_rate_limited_page_is_not_mistaken_for_a_live_session(self) -> None:
        client, _ = build_client(rate_limited())
        # The 429 body has no login form, so the vendored text check reads it
        # as logged in. check_session must still report the tracker as down.
        self.assertFalse(client.check_session())

    def test_download_and_thank_are_blocked_too(self) -> None:
        client, adapter = build_client(rate_limited("60"))
        client.search("x")
        before = len(adapter.sent)
        self.assertIsNone(client.download_torrent(42))
        self.assertFalse(client.thank_torrent(42))
        self.assertFalse(client.keep_alive())
        self.assertEqual(len(adapter.sent), before, adapter.sent)


class RefusedCallTests(unittest.TestCase):
    def test_a_refused_call_leaves_the_live_cookie_jar_alone(self) -> None:
        client, _ = build_client(rate_limited("60"))
        client.search("x")
        self.assertIsNotNone(client._guard.blocked_reason())

        # Cookies the tracker set during the session exist only in memory.
        client.session.cookies.set_cookie(
            requests.cookies.create_cookie(
                "live_session", "1", domain="tracker.example",
            )
        )
        client.search("x")
        self.assertIn(
            "live_session", requests.utils.dict_from_cookiejar(client.session.cookies),
            "a refused call must not reload the jar from disk and lose them",
        )

    def test_a_cookie_only_tracker_also_backs_off(self) -> None:
        # No credentials, so the vendored login bails out without a request.
        # Nothing but ensure_session is left to notice the dead session.
        client, adapter = build_client(lambda _r: (200, LOGGED_OUT_PAGE, {}))
        client._username = ""
        client._password = ""

        self.assertEqual(client.search("first"), [])
        first_round = len(adapter.sent)
        self.assertGreater(first_round, 0)

        for _ in range(5):
            client.search("again")
        self.assertEqual(len(adapter.sent), first_round, adapter.sent)
        self.assertEqual(adapter.paths("POST"), [])


class LoginStormTests(unittest.TestCase):
    def test_login_is_attempted_once_however_many_searches_run(self) -> None:
        client, adapter = build_client(login_always_fails)

        self.assertEqual(client.search("first"), [])
        first_round = list(adapter.sent)
        # session check, login form, login post — and nothing else
        self.assertEqual(len(first_round), 3, first_round)
        self.assertEqual(adapter.paths("POST"), ["/login"])

        for _ in range(20):
            self.assertEqual(client.search("again"), [])

        self.assertEqual(adapter.sent, first_round, adapter.sent)
        self.assertEqual(adapter.paths("POST").count("/login"), 1)

    def test_login_still_refused_after_cooldown_expires_without_new_failure(self) -> None:
        clock = FakeClock()
        client, adapter = build_client(login_always_fails, clock=clock)
        client.search("first")
        self.assertEqual(len(adapter.sent), 3)

        # A new cooldown window may retry the login once, then shut again.
        clock.advance(BASE_BACKOFF_SECONDS + 1)
        client.search("second")
        self.assertEqual(adapter.paths("POST").count("/login"), 2)
        for _ in range(10):
            client.search("more")
        self.assertEqual(adapter.paths("POST").count("/login"), 2)

    def test_successful_session_sends_no_login_and_returns_results(self) -> None:
        def handler(_request):
            return 200, TORRENT_LIST, {}

        client, adapter = build_client(handler)
        results = client.search("example")
        self.assertEqual(len(results), 1)
        self.assertEqual(adapter.paths("POST"), [])
        self.assertEqual(client._guard.blocked_seconds(), 0.0)


class BackoffTests(unittest.TestCase):
    def test_backoff_doubles_across_cooldown_windows(self) -> None:
        clock = FakeClock()
        guard = TrackerGuard("test", clock=clock)

        guard.note_failure("first")
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS)

        clock.advance(BASE_BACKOFF_SECONDS)
        guard.note_failure("second")
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS * 2)

        clock.advance(BASE_BACKOFF_SECONDS * 2)
        guard.note_failure("third")
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS * 4)

    def test_backoff_is_capped(self) -> None:
        clock = FakeClock()
        guard = TrackerGuard("test", clock=clock)
        for _ in range(20):
            guard.note_failure("nope")
            clock.advance(guard.blocked_seconds())
        self.assertEqual(guard.blocked_seconds(), 0.0)
        guard.note_failure("nope")
        self.assertEqual(guard.blocked_seconds(), MAX_BACKOFF_SECONDS)

    def test_one_failure_seen_twice_counts_once(self) -> None:
        clock = FakeClock()
        guard = TrackerGuard("test", clock=clock)
        guard.note_failure("hook saw the 429")
        guard.note_failure("caller saw the failure too")
        self.assertEqual(guard.failures, 1)
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS)

    def test_success_clears_the_failure_count(self) -> None:
        clock = FakeClock()
        guard = TrackerGuard("test", clock=clock)
        guard.note_failure("a")
        clock.advance(BASE_BACKOFF_SECONDS)
        guard.note_failure("b")
        guard.note_success()
        self.assertEqual(guard.failures, 0)
        self.assertIsNone(guard.blocked_reason())
        guard.note_failure("c")
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS)


class CookieRefreshTests(unittest.TestCase):
    def test_new_cookie_file_clears_a_session_cooldown_at_once(self) -> None:
        # The tracker answers the torrent list ONLY to the new cookie value, so
        # a client that clears the cooldown but keeps the stale jar in memory
        # cannot pass this: it gets the logged-out page and falls through to a
        # password login. That is the whole point of the test — clearing the
        # cooldown is not recovery, sending the new cookies is.
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            fresh_cookies = cookie_file("fresher-value")

            def handler(request):
                if request.path_url.startswith("/login"):
                    return 200, LOGIN_FORM, {}
                if "fresher-value" in (request.headers.get("Cookie") or ""):
                    return 200, TORRENT_LIST, {}
                return 200, LOGGED_OUT_PAGE, {}

            client, adapter = build_client(handler, cookie_path=str(cookie_path))
            # Read the real cookie file, the way the service does.
            client._cookies_loaded = False

            self.assertEqual(client.search("x"), [])
            # session check, login form, login post — then the cooldown
            self.assertEqual(
                adapter.sent,
                [("GET", "/torrents"), ("GET", "/login"), ("POST", "/login")],
            )
            self.assertIsNotNone(client._guard.blocked_reason())

            # Operator drops working cookies onto the box.
            cookie_path.write_text(fresh_cookies)
            before = len(adapter.sent)

            self.assertTrue(client.ensure_session())
            recovery = adapter.sent[before:]
            # One request: the session check, carrying the NEW cookies. No
            # second password login, which is what a stale jar would cost.
            self.assertEqual(len(recovery), 1, recovery)
            self.assertEqual(recovery[0], ("GET", "/torrents"))
            self.assertEqual([m for m, _ in recovery if m == "POST"], [], recovery)
            self.assertEqual(client._guard.failures, 0)

            # And the tracker is usable again, not merely unblocked.
            self.assertEqual(len(client.search("x")), 1)

    def test_a_cookie_file_rewritten_with_the_same_bytes_is_not_a_refresh(self) -> None:
        # The other tool on this box rewrites the shared cookie file once per
        # healthy cycle. An identical rewrite is not an operator recovering us.
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            client, adapter = build_client(login_always_fails, cookie_path=str(cookie_path))
            client.search("x")
            first_round = len(adapter.sent)
            self.assertIsNotNone(client._guard.blocked_reason())

            cookie_path.write_text(COOKIE_FILE)
            stamp = cookie_path.stat().st_mtime + 600
            os.utime(cookie_path, (stamp, stamp))

            self.assertIsNotNone(client._guard.blocked_reason())
            # Nor is the loaded cookie jar thrown away: re-reading the same
            # bytes would be pointless work on every healthy cycle.
            self.assertTrue(client._cookies_loaded)
            client.search("x")
            self.assertEqual(len(adapter.sent), first_round, adapter.sent)

    def test_a_cookie_refresh_never_shortens_a_rate_limit_hold(self) -> None:
        # A 429 is the tracker asking for a pause. New cookies are no answer to
        # that, and the other tool writing this file must not lift the hold.
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            client, adapter = build_client(rate_limited("60"), cookie_path=str(cookie_path))
            client.search("x")
            self.assertEqual(len(adapter.sent), 1)
            self.assertIsNotNone(client._guard.blocked_reason())

            cookie_path.write_text(COOKIE_FILE.replace("value", "brand-new-value"))

            self.assertIsNotNone(client._guard.blocked_reason())
            for _ in range(5):
                client.search("x")
            self.assertEqual(len(adapter.sent), 1, adapter.sent)

    def test_a_rate_limit_during_a_session_cooldown_upgrades_the_hold(self) -> None:
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            guard = TrackerGuard("test", str(cookie_path))
            guard.note_failure("login rejected")
            guard.note_failure("rate limited", retry_after=60.0, kind=RATE_LIMIT_FAILURE)

            cookie_path.write_text(COOKIE_FILE.replace("value", "brand-new-value"))
            self.assertIsNotNone(guard.blocked_reason())

    def test_cooldown_holds_while_the_cookie_file_is_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            client, adapter = build_client(rate_limited(), cookie_path=str(cookie_path))
            client.search("x")
            for _ in range(5):
                client.search("x")
            self.assertEqual(len(adapter.sent), 1, adapter.sent)


class ProwlarrCookieRefreshTests(unittest.TestCase):
    """A session Prowlarr holds has no cookie file whose contents could change."""

    @staticmethod
    def client(handler, clock=None) -> tuple[GuardedUnit3dClient, FakeAdapter]:
        guard = TrackerGuard("test", "", clock=clock) if clock else None
        client = GuardedUnit3dClient(BASE_URL, "", name="test", guard=guard)
        adapter = FakeAdapter(handler)
        client.session.mount("https://", adapter)
        return client, adapter

    @staticmethod
    def only_the_new_session(request):
        if "session=fresh" in (request.headers.get("Cookie") or ""):
            return 200, TORRENT_LIST, {}
        return 200, LOGGED_OUT_PAGE, {}

    def test_a_new_cookie_from_prowlarr_clears_a_session_cooldown_at_once(self) -> None:
        clock = FakeClock()
        client, adapter = self.client(self.only_the_new_session, clock)
        client.use_cookie_header("session=stale")
        self.assertFalse(client.ensure_session())
        self.assertEqual(client._guard.blocked_seconds(), BASE_BACKOFF_SECONDS)
        parked = len(adapter.sent)

        # The same cookie again, as Prowlarr hands it over before every use,
        # is not a recovery: nothing is sent.
        clock.advance(1)
        client.use_cookie_header("session=stale")
        self.assertFalse(client.ensure_session())
        self.assertEqual(len(adapter.sent), parked, adapter.sent)

        # Somebody logged in again in Prowlarr.
        client.use_cookie_header("session=fresh")
        self.assertTrue(client.ensure_session())
        self.assertEqual(adapter.sent[parked:], [("GET", "/torrents")])
        self.assertEqual(client._guard.failures, 0)
        self.assertEqual(len(client.search("x")), 1)

    def test_a_new_cookie_from_prowlarr_never_shortens_a_rate_limit_hold(self) -> None:
        client, adapter = self.client(rate_limited("60"))
        client.use_cookie_header("session=stale")
        self.assertFalse(client.ensure_session())
        self.assertEqual(len(adapter.sent), 1)

        client.use_cookie_header("session=fresh")
        self.assertIsNotNone(client._guard.blocked_reason())
        self.assertFalse(client.ensure_session())
        self.assertEqual(len(adapter.sent), 1, adapter.sent)

    def test_the_first_cookie_is_not_a_recovery(self) -> None:
        guard = TrackerGuard("test", "")
        guard.note_failure("rate limited", retry_after=60.0, kind=RATE_LIMIT_FAILURE)
        client = GuardedUnit3dClient(BASE_URL, "", name="test", guard=guard)
        client.use_cookie_header("session=first")
        self.assertIsNotNone(guard.blocked_reason())


class HookSurvivalTests(unittest.TestCase):
    def test_hook_is_reattached_if_the_session_is_replaced(self) -> None:
        # Guards the re-vendor risk: a future unit3d_client.py that builds a
        # fresh requests.Session would otherwise drop the 429 hook.
        client, _ = build_client(rate_limited("60"))
        client.session = requests.Session()
        adapter = FakeAdapter(rate_limited("60"))
        client.session.mount("https://", adapter)
        client._cookies_loaded = True

        client.search("x")
        self.assertIsNotNone(client._guard.blocked_reason())
        self.assertEqual(len(adapter.sent), 1, adapter.sent)


def answers(status: int, body: str):
    def handler(_request):
        return status, body, {}

    return handler


class ErrorStatusTests(unittest.TestCase):
    """403 challenge pages and 5xx must not read as a healthy session."""

    def test_a_403_challenge_is_not_a_live_session(self) -> None:
        client, _ = build_client(answers(403, CHALLENGE_PAGE))
        self.assertFalse(client.check_session())

    def test_a_503_is_not_a_live_session(self) -> None:
        client, _ = build_client(answers(503, SERVER_ERROR_PAGE))
        self.assertFalse(client.check_session())

    def test_403_on_everything_parks_the_tracker_and_stops(self) -> None:
        client, adapter = build_client(answers(403, CHALLENGE_PAGE))

        for _ in range(10):
            self.assertEqual(client.search("x"), [])

        self.assertEqual(len(adapter.sent), ERROR_STATUS_LIMIT, adapter.sent)
        self.assertIsNotNone(client._guard.blocked_reason())
        self.assertEqual(adapter.paths("POST"), [])

    def test_503_on_everything_parks_the_tracker_and_stops(self) -> None:
        client, adapter = build_client(answers(503, SERVER_ERROR_PAGE))

        for _ in range(10):
            self.assertEqual(client.search("x"), [])

        self.assertEqual(len(adapter.sent), ERROR_STATUS_LIMIT, adapter.sent)
        self.assertIsNotNone(client._guard.blocked_reason())

    def test_one_bad_search_does_not_park_a_healthy_tracker(self) -> None:
        for status in (404, 500):
            with self.subTest(status=status):
                state = {"fail_search": True}

                def handler(request, state=state, status=status):
                    if "name=" in request.path_url and state["fail_search"]:
                        state["fail_search"] = False
                        return status, SERVER_ERROR_PAGE, {}
                    return 200, TORRENT_LIST, {}

                client, adapter = build_client(handler)

                self.assertEqual(client.search("x"), [])
                self.assertIsNone(client._guard.blocked_reason())
                self.assertEqual(client._guard.error_run, 1)

                # The next item still gets its search.
                self.assertEqual(len(client.search("x")), 1)
                self.assertIsNone(client._guard.blocked_reason())
                self.assertEqual(client._guard.error_run, 0)
                self.assertEqual(len(adapter.sent), 4, adapter.sent)

    def test_a_good_response_ends_the_error_run(self) -> None:
        guard = TrackerGuard("test", clock=FakeClock())
        for _ in range(ERROR_STATUS_LIMIT - 1):
            guard.note_request_error(500)
        self.assertEqual(guard.error_run, ERROR_STATUS_LIMIT - 1)

        guard.note_request_ok()
        self.assertEqual(guard.error_run, 0)

        # The run starts again from zero, so the next lone error is harmless.
        self.assertFalse(guard.note_request_error(500))
        self.assertIsNone(guard.blocked_reason())

    def test_repeated_lone_search_errors_never_park_the_tracker(self) -> None:
        def handler(request):
            # Every search 500s; the session check in front of it is fine.
            if "name=" in request.path_url:
                return 500, SERVER_ERROR_PAGE, {}
            return 200, TORRENT_LIST, {}

        client, adapter = build_client(handler)
        for _ in range(5):
            self.assertEqual(client.search("x"), [])

        self.assertIsNone(client._guard.blocked_reason())
        self.assertEqual(len(adapter.sent), 10, adapter.sent)

    def test_the_run_is_what_parks_it_not_the_status(self) -> None:
        guard = TrackerGuard("test", clock=FakeClock())
        for _ in range(ERROR_STATUS_LIMIT - 1):
            self.assertFalse(guard.note_request_error(500))
            self.assertIsNone(guard.blocked_reason())
        self.assertTrue(guard.note_request_error(500))
        self.assertIsNotNone(guard.blocked_reason())
        # Same backoff as a failed login.
        self.assertEqual(guard.blocked_seconds(), BASE_BACKOFF_SECONDS)

    def test_a_transient_error_on_a_session_check_does_not_park_it(self) -> None:
        # A cookie-only tracker: nothing but the session check can notice a bad
        # response, and two of them are still short of the limit.
        sent = {"n": 0}

        def handler(_request):
            sent["n"] += 1
            if sent["n"] <= 2:
                return 500, SERVER_ERROR_PAGE, {}
            return 200, TORRENT_LIST, {}

        client, adapter = build_client(handler)
        client._username = ""
        client._password = ""

        self.assertEqual(client.search("first"), [])
        self.assertIsNone(client._guard.blocked_reason())

        # The next item still gets through.
        self.assertEqual(len(client.search("second")), 1)
        self.assertIsNone(client._guard.blocked_reason())
        self.assertEqual(adapter.paths("POST"), [])

    def test_a_transient_error_on_the_login_post_does_not_park_it(self) -> None:
        # The login POST 5xxs, then the cookies on disk turn out to be fine.
        # That is a recovered tracker, not a rejected login.
        state = {"login_post_failed": False}

        def handler(request):
            if request.path_url.startswith("/login"):
                if request.method == "POST" and not state["login_post_failed"]:
                    state["login_post_failed"] = True
                    return 503, SERVER_ERROR_PAGE, {}
                return 200, LOGIN_FORM, {}
            if state["login_post_failed"]:
                return 200, TORRENT_LIST, {}
            return 200, LOGGED_OUT_PAGE, {}

        client, _ = build_client(handler)

        self.assertEqual(len(client.search("x")), 1)
        self.assertIsNone(client._guard.blocked_reason())

    def test_a_cookie_refresh_clears_an_error_run_cooldown(self) -> None:
        # It is recorded as a session failure, so dropping in new cookies is
        # allowed to clear it — unlike a 429 hold.
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            client, _ = build_client(answers(403, CHALLENGE_PAGE), cookie_path=str(cookie_path))
            client.search("x")
            self.assertIsNotNone(client._guard.blocked_reason())

            cookie_path.write_text(COOKIE_FILE.replace("value", "brand-new-value"))
            self.assertIsNone(client._guard.blocked_reason())


class GuardMemberCollisionTests(unittest.TestCase):
    """The guard's own names must not clash with the vendored client's.

    The companion tool's client now sets `self._blocked = None` in `__init__`.
    A straight re-vendor of that file into a guard that still called its own
    `_blocked()` method would raise "TypeError: 'NoneType' object is not
    callable" on every guarded call, so this test watches the two name sets.
    """

    @staticmethod
    def _init_attributes(path: str, class_name: str) -> set[str]:
        """Names assigned to `self` in one class's `__init__`, read from source."""
        names: set[str] = set()
        source = Path(path).read_text(encoding="utf-8")
        for cls in ast.walk(ast.parse(source)):
            if not isinstance(cls, ast.ClassDef) or cls.name != class_name:
                continue
            for func in cls.body:
                if not isinstance(func, ast.FunctionDef) or func.name != "__init__":
                    continue
                for node in ast.walk(func):
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, ast.AnnAssign):
                        targets = [node.target]
                    else:
                        continue
                    for target in targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            names.add(target.attr)
        return names

    def _vendored_init_attributes(self) -> set[str]:
        return self._init_attributes(unit3d_client.__file__, "Unit3dClient")

    def _guard_member_names(self) -> set[str]:
        """Every name the guard puts on the client: class body and `__init__`.

        Read from the source rather than from `vars()` of an instance, because
        an instance diff goes blind precisely when the vendored client sets the
        same attribute — which is the collision being looked for.
        """
        names = {n for n in vars(GuardedUnit3dClient) if not n.startswith("__")}
        return names | self._init_attributes(
            tracker_guard.__file__, "GuardedUnit3dClient",
        )

    def test_the_attribute_parser_actually_finds_something(self) -> None:
        # A zero-match parse would make the collision test below pass forever.
        vendored = self._vendored_init_attributes()
        self.assertIn("_cookie_path", vendored)
        self.assertIn("session", vendored)
        self.assertIn("_guard", self._guard_member_names())

    def test_no_guard_member_collides_with_a_vendored_attribute(self) -> None:
        clash = self._guard_member_names() & self._vendored_init_attributes()
        self.assertEqual(
            clash, set(),
            "the vendored client sets an attribute that shadows a guard member; "
            "rename the guard member or the re-vendor is not a copy",
        )

    def test_every_name_the_guard_adds_is_prefixed(self) -> None:
        # Names already on the vendored client are deliberate overrides.
        added = self._guard_member_names() - set(dir(Unit3dClient))
        self.assertTrue(added, "the guard adds nothing? the introspection is broken")
        unprefixed = {n for n in added if not n.startswith("_guard")}
        self.assertEqual(
            unprefixed, set(),
            "guard members must start with _guard so a vendored attribute "
            "cannot shadow them",
        )


class VendoredContractTests(unittest.TestCase):
    """Methods this service calls by name that upstream's client does not have.

    `search_with_status()` and `keep_alive()` exist only in this copy —
    upstream's current client has neither — so a re-vendor is exactly when they
    get lost. Both call sites catch `Exception` and carry on: `search_service`
    logs it and moves to the next tracker, `scheduler` logs it and reschedules.
    So losing either one costs no error anyone would notice; direct tracker
    search would simply stop happening, or sessions would stop being refreshed.
    These tests are what turns that silence into a red build.
    """

    def test_search_with_status_is_what_search_service_calls(self) -> None:
        self.assertIn(
            "search_with_status",
            Path(search_service.__file__).read_text(encoding="utf-8"),
            "search_service no longer calls search_with_status — retarget this test",
        )
        client, _ = build_client(rate_limited("60"))
        # The AttributeError a missing method raises here is the point: it is
        # swallowed in production, so it has to be caught in a test.
        results, error = client.search_with_status("x")
        self.assertEqual(results, [])
        self.assertIn("429", error or "", "the reason a search failed is lost")

    def test_keep_alive_is_what_the_scheduler_calls(self) -> None:
        self.assertIn(
            "keep_alive",
            Path(scheduler.__file__).read_text(encoding="utf-8"),
            "the scheduler no longer calls keep_alive — retarget this test",
        )
        client, adapter = build_client(lambda _request: (200, TORRENT_LIST, {}))
        self.assertTrue(client.keep_alive())
        # The session check, then the keep-alive visit itself.
        self.assertEqual(adapter.paths(), ["/torrents", "/torrents"], adapter.sent)

    def test_keep_alive_goes_through_the_guard_too(self) -> None:
        # The timer fires on its own schedule, so an unguarded keep_alive would
        # keep touching a tracker that everything else is refusing to contact.
        client, adapter = build_client(lambda _request: (200, TORRENT_LIST, {}))
        client._guard.note_failure("parked")
        self.assertFalse(client.keep_alive())
        self.assertEqual(adapter.sent, [], adapter.sent)


class WiringTests(unittest.TestCase):
    """The service must build guarded clients, not bare vendored ones."""

    def setUp(self) -> None:
        self.addCleanup(self._reset_module_state)
        self._reset_module_state()

    @staticmethod
    def _reset_module_state() -> None:
        search_service._trackers.clear()
        search_service._trackers_initialized = False

    @staticmethod
    def _config(cookie_path: str, **provider) -> Config:
        return Config.model_validate({"providers": [{
            "key": "test", "base_url": BASE_URL, "search": "unit3d",
            "auth": {"from": "login", "username": "u", "password": "p", "cookie_file": cookie_path},
            **provider,
        }]})

    def test_the_provider_s_guard_block_sets_the_thresholds(self) -> None:
        with TemporaryDirectory() as tmp:
            cookie_path = str(Path(tmp) / "cookies.txt")
            Path(cookie_path).write_text(COOKIE_FILE)
            config = self._config(
                cookie_path,
                guard={"error_limit": 5, "backoff_minutes": 2, "max_backoff_minutes": 30},
            )
            with patch.object(search_service, "settings", SimpleNamespace(config=config)):
                search_service.init_trackers()
            [tracker] = search_service.get_trackers()
            guard = tracker._guard
            self.assertEqual(
                (guard.error_limit, guard.base_backoff, guard.max_backoff), (5, 120.0, 1800.0),
            )
            # And they are what the guard acts on: four errors do not park it,
            # the fifth does, for the configured two minutes.
            for status in (500, 500, 500, 500):
                self.assertFalse(guard.note_request_error(status))
            self.assertTrue(guard.note_request_error(500))
            self.assertAlmostEqual(guard.blocked_seconds(), 120.0, delta=2)
            # A Retry-After past the ceiling is cut to it.
            guard.note_failure("rate limited", retry_after=99999, kind=RATE_LIMIT_FAILURE)
            self.assertLessEqual(guard.blocked_seconds(), 1800.0)

    def test_the_defaults_are_the_module_constants(self) -> None:
        with TemporaryDirectory() as tmp:
            cookie_path = str(Path(tmp) / "cookies.txt")
            Path(cookie_path).write_text(COOKIE_FILE)
            with patch.object(
                search_service, "settings", SimpleNamespace(config=self._config(cookie_path)),
            ):
                search_service.init_trackers()
            [tracker] = search_service.get_trackers()
            guard = tracker._guard
            self.assertEqual(
                (guard.error_limit, guard.base_backoff, guard.max_backoff),
                (ERROR_STATUS_LIMIT, BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS),
            )

    def test_configured_trackers_are_guarded(self) -> None:
        with TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text(COOKIE_FILE)
            config = self._config(str(cookie_path))
            with patch.object(search_service, "settings", SimpleNamespace(config=config)):
                search_service.init_trackers()

            trackers = search_service.get_trackers()
            self.assertEqual(len(trackers), 1)
            tracker = trackers[0]
            self.assertIsInstance(tracker, GuardedUnit3dClient)

            # And it behaves like one: a 429 stops everything after it.
            adapter = FakeAdapter(rate_limited("60"))
            tracker.session.mount("https://", adapter)
            for _ in range(5):
                self.assertEqual(tracker.search("x"), [])
            self.assertEqual(len(adapter.sent), 1, adapter.sent)
            self.assertIn("429", tracker.last_search_error or "")


if __name__ == "__main__":
    unittest.main()
