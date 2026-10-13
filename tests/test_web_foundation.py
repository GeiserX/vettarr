"""The web UI's foundation: who gets in, the layout every page shares, and the wiring.

The app is driven through Starlette's test client with a real config, so a
route that loses the gate, a cookie that loses a flag or a POST that stops
being checked fails here.
"""

import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import pathlib
import re
import stat
import tempfile
import time
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
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from vettarr import actions
from vettarr.config.loader import ConfigError, load_text
from vettarr.config.schema import Config
from vettarr.models import ItemStatus, MediaItem, MediaType, Recommendation
from vettarr.web import app as web_app
from vettarr.web import auth, cards, cli, passwords, shell, ui

WEB_DIR = pathlib.Path(web_app.__file__).parent
REPO = WEB_DIR.parents[2]
# Built from words, so no line here reads as a credential to a scanner.
PASSWORD = " ".join(("correct", "horse", "battery"))
WRONG = " ".join(("not", "the", "one"))
NOT_A_HASH = "-".join(("plainly", "not", "a", "hash"))
# A cheap cost: the tests check the flow, not how long scrypt takes.
HASH = passwords.hash_password(PASSWORD, n=2**10, p=1)
HTMX_FILE = "htmx-2.0.11.min.js"
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class WebCase(unittest.TestCase):
    """An app on a given ``web`` block, with a photos directory and no database."""

    AUTH: dict | None = {"password_hash": HASH}
    BASE_URL: str | None = None

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = pathlib.Path(tmp.name)
        self.photos = self.data / "photos"
        self.photos.mkdir()
        (self.photos / "poster.jpg").write_bytes(b"jpeg")
        self.secret_file = self.data / "sessions" / "web-session.key"

        self.failing = mock.AsyncMock(return_value=[])
        banner = mock.patch.object(shell, "failing", self.failing)
        banner.start()
        self.addCleanup(banner.stop)
        # The nav's count of items waiting for a person is a database read too.
        self.needs_you = mock.AsyncMock(return_value=0)
        count = mock.patch.object(shell, "needs_you", self.needs_you)
        count.start()
        self.addCleanup(count.stop)

        # No database runs under these tests: what the three item pages read
        # is replaced, and a test sets what a page should find.
        nothing = {tab.key: 0 for tab in cards.TABS}
        self.load_inbox = self.replace("load_inbox", cards.Inbox([], nothing, 0, 1, 1, []))
        self.load_rows = self.replace("load_rows", ([], nothing))
        self.load_detail = self.replace("load_detail", None)
        self.load_download = self.replace("load_download", None)
        self.load_review = self.replace("load_review", cards.Review(None, None, 0))

        self.app = self.build()
        self.gate = self.app.state.gate
        self.clock = Clock()
        self.gate.clock = self.clock
        self.lock_clock = Clock(0.0)
        self.gate.lockout = auth.Lockout(clock=self.lock_clock)
        scheme = "https" if (self.BASE_URL or "").startswith("https") else "http"
        self.client = TestClient(self.app, base_url=f"{scheme}://testserver")

    def replace(self, loader: str, found):
        patcher = mock.patch.object(cards, loader, mock.AsyncMock(return_value=found))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def build(self):
        """The app, with ``/data`` in a temporary directory. Twice is a restart."""
        real_path = pathlib.Path

        def _path(*args):
            if args and str(args[0]).startswith("/data/"):
                return self.data / str(args[0])[len("/data/"):]
            return real_path(*args)

        web: dict = {"port": 4850}
        if self.AUTH:
            web["auth"] = self.AUTH
        if self.BASE_URL:
            web["base_url"] = self.BASE_URL
        with mock.patch.object(web_app, "Path", _path):
            return web_app.create_web_app(None, Config.model_validate({"web": web}))

    def get(self, url, **kwargs):
        return self.client.get(url, follow_redirects=False, **kwargs)

    def post(self, url, **kwargs):
        return self.client.post(url, follow_redirects=False, **kwargs)

    def sign_in(self, **data):
        return self.post("/login", data=dict(password=PASSWORD, **data))

    def wrong(self, **data):
        return self.post("/login", data=dict(password=WRONG, **data))

    def approve(self, **headers):
        """POST an approval; returns the answer and whether the action ran."""
        ok = actions.ActionResult(1, actions.Status.OK, "Queued.")
        with mock.patch.object(actions, "approve", mock.AsyncMock(return_value=ok)) as approve:
            resp = self.post("/item/1/approve", headers={"HX-Request": "true", **headers})
        return resp, approve.await_count == 1


# ---------------------------------------------------------------------------
# The password hash and its command
# ---------------------------------------------------------------------------


class PasswordHashTests(unittest.TestCase):
    def test_the_written_hash_verifies_and_carries_no_dollar(self) -> None:
        line = passwords.hash_password(PASSWORD)
        self.assertTrue(line.startswith("scrypt:32768:8:3:"))
        self.assertNotIn("$", line)
        self.assertTrue(passwords.verify(PASSWORD, line))
        self.assertFalse(passwords.verify(PASSWORD + "x", line))

    def test_two_hashes_of_one_password_differ(self) -> None:
        self.assertNotEqual(passwords.hash_password(PASSWORD, n=2**10), passwords.hash_password(PASSWORD, n=2**10))

    def test_what_is_not_a_hash_is_named_without_being_echoed(self) -> None:
        salt, key = "ab" * 16, "cd" * 32
        for line in (
            NOT_A_HASH,
            f"bcrypt:1024:8:1:{salt}:{key}",
            f"scrypt:1000:8:1:{salt}:{key}",        # n is not a power of two
            f"scrypt:{2**24}:8:1:{salt}:{key}",     # more memory than a login may take
            f"scrypt:1024:8:1:{salt}:nothex",
            f"scrypt:1024:8:1:{salt}",
        ):
            with self.subTest(line=line):
                problem = passwords.problem(line)
                self.assertEqual(problem, passwords.NOT_A_HASH)
                self.assertNotIn("plainly", problem)
                self.assertFalse(passwords.verify(PASSWORD, line))
        self.assertIsNone(passwords.problem(HASH))

    def test_a_config_with_a_bad_hash_does_not_load(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_text(f"web:\n  auth:\n    password_hash: {NOT_A_HASH}\n", "c.yaml", {})
        self.assertEqual(caught.exception.errors, [f"c.yaml:3: web.auth.password_hash: {passwords.NOT_A_HASH}"])
        cfg = load_text("web:\n  auth:\n    password_hash: ${H}\n", "c.yaml", {"H": HASH})
        self.assertEqual(cfg.web.auth.password_hash.get_secret_value(), HASH)

    def run_cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv, load_config=lambda: Config())
        return code, out.getvalue(), err.getvalue()

    def test_hash_password_reads_stdin_and_prints_the_line(self) -> None:
        code, out, _ = self.run_cli(["hash-password"], PASSWORD + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(passwords.verify(PASSWORD, out.strip()))
        self.assertNotIn(PASSWORD, out)

    def test_hash_password_refuses_a_short_password(self) -> None:
        code, out, err = self.run_cli(["hash-password"], "short\n")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("at least 8 characters", err)

    def test_serve_needs_a_web_block(self) -> None:
        code, _, err = self.run_cli(["serve"])
        self.assertEqual(code, 1)
        self.assertIn("no web block", err)


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


class LoginTests(WebCase):
    def test_every_page_sends_a_stranger_to_the_login_page(self) -> None:
        for url, where in (
            ("/", "/login"),
            ("/stats", "/login?next=/stats"),
            ("/item/5?tab=releases", "/login?next=/item/5%3Ftab%3Dreleases"),
            ("/settings", "/login?next=/settings"),
            ("/photos/poster.jpg", "/login?next=/photos/poster.jpg"),
            ("/openapi.json", "/login?next=/openapi.json"),
        ):
            with self.subTest(url=url):
                resp = self.get(url)
                self.assertEqual(resp.status_code, 303)
                self.assertEqual(resp.headers["location"], where)

    def test_the_login_page_has_the_mark_the_field_and_remember(self) -> None:
        resp = self.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('aria-label="vettarr"', resp.text)
        self.assertRegex(resp.text, r'<label class="label" for="password">Password</label>')
        self.assertIn('autocomplete="current-password"', resp.text)
        self.assertIn("Remember this device for 30 days", resp.text)
        self.assertNotIn("nav-links", resp.text)
        self.assertEqual(resp.headers["cache-control"], "no-store")

    def test_the_stylesheet_and_scripts_need_no_session(self) -> None:
        self.assertEqual(self.get("/static/app.js").status_code, 200)
        self.assertEqual(self.get(f"/static/vendor/{HTMX_FILE}").status_code, 200)

    def test_the_right_password_signs_in_and_goes_where_it_was_headed(self) -> None:
        resp = self.sign_in(next="/item/5?tab=releases")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/item/5?tab=releases")
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 200)
        self.assertEqual(self.get("/login").headers["location"], "/")

    def test_the_cookie_is_http_only_lax_and_ends_with_the_browser(self) -> None:
        cookie = self.sign_in().headers["set-cookie"]
        self.assertTrue(cookie.startswith(f"{auth.COOKIE}=v1."))
        flags = [part.strip().lower() for part in cookie.split(";")[1:]]
        self.assertIn("httponly", flags)
        self.assertIn("samesite=lax", flags)
        self.assertIn("path=/", flags)
        self.assertNotIn("secure", flags)
        self.assertFalse([f for f in flags if f.startswith(("max-age", "expires"))])

    def test_remember_this_device_keeps_the_cookie_30_days(self) -> None:
        cookie = self.sign_in(remember="1").headers["set-cookie"].lower()
        self.assertIn("max-age=2592000", cookie)
        self.clock.now += 29 * 86400
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 200)
        self.clock.now += 2 * 86400
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 303)

    def test_without_remember_the_session_ends_after_twelve_hours(self) -> None:
        self.sign_in()
        self.clock.now += 11 * 3600
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 200)
        self.clock.now += 2 * 3600
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 303)

    def test_a_wrong_password_says_so_and_sets_no_cookie(self) -> None:
        resp = self.wrong(next="/stats")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("That password is not right. 4 more tries", resp.text)
        self.assertIn('role="alert"', resp.text)
        self.assertIn('name="next" value="/stats"', resp.text)
        self.assertNotIn("set-cookie", resp.headers)
        self.assertEqual(self.get("/").status_code, 303)

    def test_five_wrong_passwords_lock_the_address_for_fifteen_minutes(self) -> None:
        for left in (4, 3, 2, 1):
            resp = self.wrong()
            self.assertEqual(resp.status_code, 401)
            self.assertIn(f"{left} more tr", resp.text)
        resp = self.wrong()
        self.assertEqual(resp.status_code, 429)
        self.assertIn("This address is locked", resp.text)
        self.assertIn("Try again in 15 minutes", resp.text)
        self.assertEqual(resp.headers["retry-after"], "900")
        self.assertNotIn('name="password"', resp.text)

        # Locked means locked: the right password is not even checked.
        with mock.patch.object(passwords, "verify") as verify:
            resp = self.sign_in()
        self.assertEqual(resp.status_code, 429)
        verify.assert_not_called()
        self.assertNotIn("set-cookie", resp.headers)
        self.assertEqual(self.get("/login").status_code, 429)

        self.lock_clock.now += 14 * 60
        self.assertEqual(self.sign_in().status_code, 429)
        self.lock_clock.now += 61
        self.assertEqual(self.sign_in().status_code, 303)

    def test_a_right_password_forgets_the_wrong_ones_before_it(self) -> None:
        for _ in range(4):
            self.wrong()
        self.assertEqual(self.sign_in().status_code, 303)
        self.client.cookies.clear()
        resp = self.wrong()
        self.assertIn("4 more tries", resp.text)

    def test_logins_sent_together_get_five_checks_between_them(self) -> None:
        # Counted after the check, all forty would be checked: they pass the
        # lock together and then wait their turn at scrypt.
        checked: list[str] = []

        def verify(password: str, line: str) -> bool:
            checked.append(password)
            time.sleep(0.01)
            return password == PASSWORD

        async def burst():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                wrong = [client.post("/login", data={"password": WRONG}) for _ in range(40)]
                answers = await asyncio.gather(*wrong, client.post("/login", data={"password": PASSWORD}))
                return answers, await client.get("/login")

        with mock.patch.object(passwords, "verify", verify):
            answers, after = asyncio.run(burst())
        self.assertEqual(checked, [WRONG] * auth.MAX_FAILURES)
        self.assertLessEqual({a.status_code for a in answers}, {401, 429})
        self.assertGreaterEqual([a.status_code for a in answers].count(429), 41 - auth.MAX_FAILURES)
        self.assertFalse([a for a in answers if "set-cookie" in a.headers])
        self.assertEqual(after.status_code, 429)

    def test_the_fifth_try_with_the_right_password_gets_in_and_leaves_no_lock(self) -> None:
        for _ in range(4):
            self.wrong()
        self.assertEqual(self.sign_in().status_code, 303)
        self.client.cookies.clear()
        self.assertEqual(self.get("/login").status_code, 200)

    def test_a_host_header_cannot_name_an_open_path(self) -> None:
        # Starlette before 1.7.0 built request.url from Host as it came, so
        # "x/static/" made every path look like a static file.
        forged = {"Host": "x/static/"}
        self.assertEqual(self.get("/", headers=forged).status_code, 303)
        self.assertEqual(self.get("/photos/poster.jpg", headers=forged).status_code, 303)
        with mock.patch.object(actions, "approve", mock.AsyncMock()) as approve:
            self.assertEqual(self.post("/item/1/approve", headers=forged).status_code, 401)
        approve.assert_not_awaited()

        # And on any Starlette: the gate reads the router's path, never the URL.
        class NoUrl(Request):
            @property
            def url(self):
                raise AssertionError("the gate read request.url, which Host can write")

        scope = {
            "type": "http", "method": "GET", "path": "/stats", "query_string": b"tab=1",
            "headers": [(b"host", b"x/static/")], "state": {},
        }
        refusal = self.gate.refusal(NoUrl(scope))
        self.assertEqual((refusal.status_code, refusal.headers["location"]), (303, "/login?next=/stats%3Ftab%3D1"))
        self.assertIsNone(self.gate.refusal(NoUrl({**scope, "path": "/static/app.js"})))
        floor = re.search(r'"starlette>=(\d+)\.(\d+)', (REPO / "pyproject.toml").read_text())
        self.assertGreaterEqual(tuple(map(int, floor.groups())), (1, 7))

    def test_a_cookie_nobody_signed_is_refused(self) -> None:
        self.sign_in()
        good = self.client.cookies.get(auth.COOKIE)
        version, expires, remember, signature = good.split(".")
        forged = (
            f"{version}.{int(expires) + 10**8}.{remember}.{signature}",   # a later expiry, the old signature
            f"{version}.{expires}.{remember}.{'0' * 64}",
            "v1.9999999999.1",
            "",
        )
        for value in forged:
            with self.subTest(value=value):
                self.client.cookies.clear()
                self.client.cookies.set(auth.COOKIE, value)
                self.assertEqual(self.get("/photos/poster.jpg").status_code, 303)
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE, good)
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 200)

    def test_a_new_password_signs_every_device_out(self) -> None:
        secret = b"s" * 32
        value = auth.sign_session(auth.session_key(HASH, secret), int(self.clock.now) + 60, False)
        self.assertTrue(auth.session_valid(auth.session_key(HASH, secret), value, self.clock.now))
        other = passwords.hash_password("another password", n=2**10, p=1)
        self.assertFalse(auth.session_valid(auth.session_key(other, secret), value, self.clock.now))

    def test_the_hash_line_alone_signs_no_cookie(self) -> None:
        # Whoever reads the hash (a leaked .env, a backup) must still not get in.
        body = f"v1.{int(self.clock.now) + 3600}.1"
        from_the_hash = (
            hmac.new(HASH.encode(), b"vettarr web session v1", hashlib.sha256).digest(),
            auth.session_key(HASH, b""),
            auth.session_key(HASH, b"\0" * 32),
        )
        for key in from_the_hash:
            self.client.cookies.set(auth.COOKIE, f"{body}.{hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}")
            self.assertEqual(self.get("/photos/poster.jpg").status_code, 303)
            with mock.patch.object(actions, "approve", mock.AsyncMock()) as approve:
                self.assertEqual(self.post("/item/1/approve").status_code, 401)
            approve.assert_not_awaited()

    def test_the_secret_is_made_once_and_only_its_owner_reads_it(self) -> None:
        kept = self.secret_file.read_bytes().strip()
        self.assertEqual(len(kept), 64)
        self.assertEqual(stat.S_IMODE(self.secret_file.stat().st_mode), 0o600)
        self.assertEqual(auth.session_secret(self.secret_file), kept)
        self.assertNotEqual(auth.session_secret(self.data / "other" / "web-session.key"), kept)
        self.assertNotIn(kept, self.photos.joinpath("poster.jpg").read_bytes())

        # A restart on the same file keeps people signed in; another server's cookie is nothing here.
        self.sign_in()
        cookie = self.client.cookies.get(auth.COOKIE)
        again = TestClient(self.build())
        again.cookies.set(auth.COOKIE, cookie)
        self.assertEqual(again.get("/photos/poster.jpg").status_code, 200)
        self.secret_file.unlink()
        elsewhere = TestClient(self.build())
        elsewhere.cookies.set(auth.COOKIE, cookie)
        self.assertEqual(elsewhere.get("/photos/poster.jpg", follow_redirects=False).status_code, 303)

    def test_a_short_or_unwritable_secret_file_never_gives_a_weak_key(self) -> None:
        self.secret_file.write_bytes(b"short\n")
        fresh = auth.session_secret(self.secret_file)
        self.assertEqual(len(fresh), 64)
        self.assertEqual(self.secret_file.read_bytes().strip(), fresh)

        blocked = self.data / "a-file"
        blocked.write_bytes(b"")
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("vettarr.web.auth", level="WARNING") as logs:
            in_memory = auth.session_secret(blocked / "web-session.key")
        self.assertEqual(len(in_memory), 64)
        self.assertIn("sessions end when the app restarts", logs.output[0])
        self.assertNotIn(in_memory.decode(), logs.output[0])

    def test_a_signed_in_page_is_not_kept_by_the_browser(self) -> None:
        # Otherwise Back after Sign out shows the page again.
        self.sign_in()
        self.assertEqual(self.get("/review").headers["cache-control"], "no-store")
        self.assertEqual(self.get("/photos/poster.jpg").headers["cache-control"], "private, no-cache")
        self.assertNotIn("cache-control", self.get("/static/app.js").headers)

    def test_a_websocket_is_closed_before_any_route_sees_it(self) -> None:
        seen = []

        async def socket(websocket):
            seen.append(websocket)
            await websocket.accept()
            await websocket.close()

        self.app.router.add_websocket_route("/live", socket)
        self.sign_in()
        with self.assertRaises(WebSocketDisconnect) as closed, self.client.websocket_connect("/live"):
            pass
        self.assertEqual(closed.exception.code, 1008)
        self.assertEqual(seen, [])

    def test_signing_in_never_leaves_the_site(self) -> None:
        for target in ("//evil.example/x", "https://evil.example", "/\\evil.example", "item/5", "/a\nb"):
            with self.subTest(target=target):
                self.client.cookies.clear()
                self.assertEqual(self.sign_in(next=target).headers["location"], "/")
        self.client.cookies.clear()
        self.assertEqual(self.get("/photos/x?next=//evil.example").headers["location"][:13], "/login?next=/")

    def test_logout_ends_the_session(self) -> None:
        self.sign_in()
        resp = self.post("/logout")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/login"))
        self.assertIn(f'{auth.COOKIE}="";', resp.headers["set-cookie"])
        self.assertEqual(self.get("/photos/poster.jpg").status_code, 303)
        self.assertEqual(self.get("/logout").status_code, 405)

    def test_an_htmx_request_without_a_session_sends_the_whole_page_to_login(self) -> None:
        resp, ran = self.approve(**{"HX-Current-URL": "http://testserver/item/1?tab=releases"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["HX-Redirect"], "/login?next=/item/1%3Ftab%3Dreleases")
        self.assertFalse(ran)

    def test_a_plain_post_without_a_session_is_refused(self) -> None:
        with mock.patch.object(actions, "approve", mock.AsyncMock()) as approve:
            resp = self.post("/item/1/approve")
        self.assertEqual((resp.status_code, resp.text), (401, auth.SIGN_IN_FIRST))
        approve.assert_not_awaited()

    def test_a_signed_in_page_offers_sign_out_and_no_unprotected_badge(self) -> None:
        self.sign_in()
        page = self.get("/review").text
        self.assertIn('action="/logout"', page)
        self.assertNotIn("unprotected", page)


class SecureCookieTests(WebCase):
    BASE_URL = "https://vettarr.example"

    def test_an_https_base_url_marks_the_cookie_secure(self) -> None:
        flags = [part.strip().lower() for part in self.sign_in().headers["set-cookie"].split(";")[1:]]
        self.assertIn("secure", flags)
        self.assertIn("httponly", flags)
        self.assertIn("samesite=lax", flags)


class LockoutTests(unittest.TestCase):
    def test_a_lock_is_one_address_s(self) -> None:
        clock = Clock(0.0)
        lockout = auth.Lockout(clock=clock)
        for _ in range(5):
            lockout.attempt("203.0.113.7")
        self.assertEqual(lockout.locked_for("203.0.113.7"), 900)
        self.assertEqual(lockout.locked_for("203.0.113.8"), 0)

    def test_wrong_passwords_spread_over_more_than_the_window_never_lock(self) -> None:
        clock = Clock(0.0)
        lockout = auth.Lockout(clock=clock)
        for _ in range(12):
            self.assertGreater(lockout.attempt("203.0.113.7"), 0)
            clock.now += 4 * 60
        self.assertEqual(lockout.locked_for("203.0.113.7"), 0)

    def test_an_ipv6_address_is_locked_by_its_64(self) -> None:
        self.assertEqual(auth.address_key("2001:db8:1:2:aaaa::1"), auth.address_key("2001:db8:1:2:bbbb::2"))
        self.assertNotEqual(auth.address_key("2001:db8:1:2::1"), auth.address_key("2001:db8:1:3::1"))
        self.assertEqual(auth.address_key("::ffff:203.0.113.7"), "203.0.113.7")
        self.assertEqual(auth.address_key("203.0.113.7"), "203.0.113.7")
        self.assertEqual(auth.address_key(None), "unknown")

    def test_the_table_of_addresses_is_bounded(self) -> None:
        lockout = auth.Lockout(clock=Clock(0.0))
        with mock.patch.object(auth, "_TRACKED_ADDRESSES", 100):
            for n in range(1000):
                lockout.attempt(f"address-{n}")
        self.assertLessEqual(len(lockout._failures), 100)


# ---------------------------------------------------------------------------
# Requests another site sent
# ---------------------------------------------------------------------------


class CrossOriginTests(WebCase):
    def setUp(self) -> None:
        super().setUp()
        self.sign_in()

    def test_a_post_from_this_site_runs(self) -> None:
        for headers in (
            {},                                                   # not a browser
            {"Sec-Fetch-Site": "same-origin"},
            {"Sec-Fetch-Site": "none"},
            {"Origin": "http://testserver"},
            {"Sec-Fetch-Site": "same-origin", "Origin": "https://elsewhere.example"},
        ):
            with self.subTest(headers=headers):
                resp, ran = self.approve(**headers)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(ran)

    def test_a_post_another_site_sent_is_refused_before_anything_runs(self) -> None:
        for headers in (
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
            {"Origin": "https://evil.example"},
            {"Origin": "http://testserver.evil.example"},
            {"Origin": "http://testserver:8080"},
            {"Origin": "null"},
        ):
            with self.subTest(headers=headers):
                resp, ran = self.approve(**headers)
                self.assertEqual((resp.status_code, resp.text), (403, auth.CROSS_ORIGIN))
                self.assertFalse(ran)

    def test_the_login_form_and_logout_are_checked_too(self) -> None:
        self.client.cookies.clear()
        resp = self.post("/login", data=dict(password=PASSWORD), headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("set-cookie", resp.headers)
        self.assertEqual(self.post("/logout", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_a_get_from_another_site_is_a_link_and_is_served(self) -> None:
        resp = self.get("/photos/poster.jpg", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 200)


class ProxiedOriginTests(WebCase):
    BASE_URL = "https://vettarr.example"

    def test_base_url_names_the_site_when_the_proxy_rewrites_host(self) -> None:
        self.sign_in()
        resp, ran = self.approve(Origin="https://vettarr.example")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(ran)
        resp, ran = self.approve(Origin="https://other.example")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ran)


# ---------------------------------------------------------------------------
# A link the bot sent, tapped in another app
# ---------------------------------------------------------------------------


class DeepLinkTests(WebCase):
    # What a browser sends when a link is tapped in another app.
    FROM_ANOTHER_APP = {
        "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1",
    }

    def setUp(self) -> None:
        super().setUp()
        item = MediaItem(
            id=7, telegram_message_id=1, telegram_chat_id=-1, raw_text="Title: The Lantern Keeper",
            title="The Lantern Keeper", year=2021, media_type=MediaType.MOVIE,
            status=ItemStatus.PENDING_REVIEW, auto_recommendation=Recommendation.NEUTRAL,
        )
        self.load_detail.return_value = cards.detail_of(item)

    def test_with_a_session_the_link_lands_on_the_item(self) -> None:
        self.sign_in(remember="1")
        resp = self.get("/item/7", headers=self.FROM_ANOTHER_APP)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("The Lantern Keeper", resp.text)
        self.assertIn('hx-post="/item/7/reject"', resp.text)

    def test_without_one_it_lands_there_after_signing_in(self) -> None:
        resp = self.get("/item/7", headers=self.FROM_ANOTHER_APP)
        self.assertEqual(resp.headers["location"], "/login?next=/item/7")
        page = self.get(resp.headers["location"], headers={"Sec-Fetch-Site": "same-origin"})
        self.assertIn('name="next" value="/item/7"', page.text)
        resp = self.sign_in(next="/item/7")
        self.assertEqual(resp.headers["location"], "/item/7")
        self.assertIn("The Lantern Keeper", self.get("/item/7").text)

    def test_the_reject_button_asks_first(self) -> None:
        self.sign_in()
        page = self.get("/item/7").text
        button = re.search(r'<button[^>]*hx-post="/item/7/reject"[^>]*>', page, re.S).group(0)
        self.assertIn('data-confirm-title="Reject The Lantern Keeper?"', button)
        self.assertIn("data-confirm=", button)


# ---------------------------------------------------------------------------
# The other two modes
# ---------------------------------------------------------------------------


class TrustedHeaderTests(WebCase):
    AUTH = {"trusted_header": "Remote-User"}

    def test_a_request_without_the_proxy_s_header_is_refused(self) -> None:
        for url in ("/", "/stats", "/photos/poster.jpg", "/login"):
            with self.subTest(url=url):
                resp = self.get(url)
                self.assertEqual(resp.status_code, 401)
                self.assertIn("Remote-User", resp.text)
        self.assertEqual(self.get("/", headers={"Remote-User": "  "}).status_code, 401)
        self.assertEqual(self.get("/static/app.js").status_code, 200)
        self.assertEqual(self.get("/", headers={"Host": "x/static/"}).status_code, 401)

    def test_an_empty_header_name_is_a_config_error_not_a_crash_at_start(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_text("web:\n  auth:\n    trusted_header: ''\n", "c.yaml", {})
        self.assertEqual(len(caught.exception.errors), 1)
        self.assertTrue(caught.exception.errors[0].startswith("c.yaml:3: web.auth.trusted_header:"))

    def test_with_the_header_the_page_is_served_and_names_the_user(self) -> None:
        resp = self.get("/review", headers={"Remote-User": "ada"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(">ada</span>", resp.text)
        self.assertNotIn('action="/logout"', resp.text)
        self.assertNotIn("unprotected", resp.text)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(self.get("/photos/poster.jpg", headers={"Remote-User": "ada"}).status_code, 200)

    def test_there_is_no_login_page_and_a_password_gets_nobody_in(self) -> None:
        self.assertEqual(self.get("/login", headers={"Remote-User": "ada"}).headers["location"], "/")
        resp = self.post("/login", data=dict(password=PASSWORD), headers={"Remote-User": "ada"})
        self.assertNotIn("set-cookie", resp.headers)
        self.assertEqual(self.get("/").status_code, 401)

    def test_another_site_s_post_is_refused_even_with_the_header(self) -> None:
        resp, ran = self.approve(**{"Remote-User": "ada", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ran)
        resp, ran = self.approve(**{"Remote-User": "ada", "Sec-Fetch-Site": "same-origin"})
        self.assertTrue(ran)


class UnprotectedTests(WebCase):
    AUTH = None

    def test_the_nav_says_unprotected(self) -> None:
        page = self.get("/review")
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.text, r'<span class="badge badge-warn"[^>]*>unprotected</span>')
        self.assertNotIn('action="/logout"', page.text)
        self.assertNotIn("cache-control", page.headers)
        self.assertEqual(self.get("/login").headers["location"], "/")

    def test_the_start_warns_in_one_line(self) -> None:
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("vettarr.web.app", level="WARNING") as logs, \
                mock.patch.object(web_app, "Path", lambda *a: self.photos if a == ("/data/photos",) else pathlib.Path(*a)):
            web_app.create_web_app(None, Config.model_validate({"web": {}}))
        self.assertEqual(len([line for line in logs.output if "no auth" in line]), 1)

    def test_another_site_s_post_is_refused_here_too(self) -> None:
        resp, ran = self.approve(Origin="https://evil.example")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ran)

    def test_a_bad_hash_stops_the_app_being_built(self) -> None:
        web = SimpleNamespace(
            base_url=None,
            auth=SimpleNamespace(trusted_header=None, password_hash=SimpleNamespace(get_secret_value=lambda: "junk")),
        )
        with self.assertRaises(ValueError):
            auth.Gate(web)


# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------


class LayoutTests(WebCase):
    AUTH = None

    def test_the_nav_has_the_five_pages_and_marks_the_current_one(self) -> None:
        page = self.get("/policy").text
        links = re.findall(r'<a class="nav-link" href="([^"]+)" (aria-current="page")?', page)
        self.assertEqual(
            links,
            [("/", ""), ("/review", ""), ("/policy", 'aria-current="page"'), ("/setup", ""), ("/stats", "")],
        )
        for label in ("Inbox", "Review", "Policy", "Setup", "Stats"):
            self.assertIn(f"<span>{label}</span>", page)

    def test_an_item_page_belongs_to_the_inbox_and_settings_to_setup(self) -> None:
        current = {
            section: [label for label, _, on in ui._shell(SimpleNamespace(
                state=SimpleNamespace(), scope={"path": section},
            ))["nav"] if on]
            for section in ("/", "/item/9", "/settings", "/setup", "/stats", "/review", "/policy", "/elsewhere")
        }
        self.assertEqual(current, {
            "/": ["Inbox"], "/item/9": ["Inbox"], "/settings": ["Setup"], "/setup": ["Setup"],
            "/stats": ["Stats"], "/review": ["Review"], "/policy": ["Policy"], "/elsewhere": [],
        })

    def test_nothing_is_loaded_from_another_host(self) -> None:
        for template in (WEB_DIR / "templates").glob("*.html"):
            text = template.read_text()
            with self.subTest(template=template.name):
                self.assertNotIn("cdn.tailwindcss.com", text)
                self.assertNotIn("unpkg.com", text)
                self.assertEqual(re.findall(r'<(?:script|link)[^>]+(?:src|href)="(?!/static/)[^"]*"', text), [])
        base = (WEB_DIR / "templates" / "base.html").read_text()
        self.assertIn('href="/static/app.css"', base)
        self.assertIn(f'src="/static/vendor/{HTMX_FILE}"', base)

    def test_the_vendored_htmx_is_the_pinned_file(self) -> None:
        data = (WEB_DIR / "static" / "vendor" / HTMX_FILE).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), HTMX_SHA256)
        self.assertIn(b'version:"2.0.11"', data)

    def test_the_page_has_a_toast_region_and_one_confirm_dialog(self) -> None:
        page = self.get("/review").text
        self.assertIn('<div id="toast" class="toasts" aria-live="polite">', page)
        self.assertEqual(page.count('<dialog id="confirm"'), 1)
        self.assertIn('<form method="dialog">', page)

    def test_the_script_shows_a_toast_for_an_error_answer_and_for_hx_trigger(self) -> None:
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("addEventListener('htmx:responseError'", script)
        self.assertIn("addEventListener('toast'", script)
        self.assertIn("addEventListener('htmx:confirm'", script)
        self.assertEqual(json.loads(ui.toast("Saved.")["HX-Trigger"]), {"toast": {"text": "Saved.", "kind": "ok"}})

    def test_the_script_stays_small(self) -> None:
        lines = (WEB_DIR / "static" / "app.js").read_text().splitlines()
        self.assertLessEqual(len(lines), 215)

    def test_every_reject_goes_through_the_confirm_dialog(self) -> None:
        # The item page, the Inbox row, the Review card and the bulk bar.
        found = 0
        for template in (WEB_DIR / "templates").rglob("*.html"):
            for button in re.findall(r"<button[^>]*/reject\"[^>]*>", template.read_text(), re.S):
                found += 1
                with self.subTest(template=template.name):
                    self.assertIn("data-confirm=", button)
                    # "Reject", or "Dismiss" for a post nothing could read (Card.reject_label).
                    self.assertRegex(button, r'data-confirm-action="(Reject|\{\{ card\.reject_label \}\})"')
        self.assertEqual(found, 4)

    def test_a_bulk_action_is_htmx_s_so_a_refusal_is_a_toast_and_a_lost_session_goes_to_login(self) -> None:
        # htmx sends it, so the layout's script shows an error answer as a
        # toast and the gate's HX-Redirect takes a lost session to the login
        # page. What the browser does with both is checked by scripts/checks.js.
        inbox = (WEB_DIR / "templates" / "inbox.html").read_text()
        for action in ("approve", "reject"):
            self.assertRegex(inbox, rf'<button[^>]*hx-post="/bulk/{action}"[^>]*hx-include="\[name=\'ids\'\]:checked"')
        self.assertNotIn("fetch(", inbox)
        self.assertNotIn("<script", inbox)
        # Every select sits in the label that names it.
        self.assertEqual(len(re.findall(r"<label>[^<]+<select ", inbox)), inbox.count("<select "))

    def test_the_browser_checks_are_in_the_repo_and_in_the_dev_guide(self) -> None:
        checks = (REPO / "scripts" / "checks.js").read_text()
        for name in ("Escape closes the dialog and sends nothing", "Cancel closes the dialog and sends nothing",
                     "Confirm sends the request once", "what has focus shows an outline",
                     "an error answer is a toast", "Back after signing out"):
            with self.subTest(check=name):
                self.assertIn(name, checks)
        self.assertIn("scripts/checks.js", (REPO / "docs" / "DEV.md").read_text())

    def test_a_failing_provider_is_a_banner_on_a_full_page_only(self) -> None:
        self.failing.return_value = [shell.Failing("northwind", "the session cookie was refused", 48)]
        page = self.get("/review").text
        self.assertIn("<strong>Not working:</strong> northwind. The session cookie was refused; "
                      "nothing is sent there for another 48 min.", page)
        # To the provider's own card, not the top of a long page.
        self.assertIn('<a href="/setup#provider-northwind">Open Setup<span class="sr-only"> for northwind</span></a>', page)
        self.failing.reset_mock()
        self.approve()
        self.get("/review", headers={"HX-Request": "true"})
        self.failing.assert_not_awaited()

    def test_no_banner_when_nothing_fails(self) -> None:
        self.assertNotIn("Not working", self.get("/review").text)
        self.failing.assert_awaited_once()

    def test_the_login_page_reads_no_events_for_a_stranger(self) -> None:
        self.failing.return_value = [shell.Failing("northwind", "the session cookie was refused", 48)]
        with mock.patch.object(self.gate, "mode", auth.PASSWORD), \
                mock.patch.object(self.gate, "_key", auth.session_key(HASH, b"s" * 32)):
            page = self.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("northwind", page.text)
        self.failing.assert_not_awaited()

    def test_dark_is_the_default_and_light_names_the_same_tokens(self) -> None:
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        dark = re.search(r"^:root \{(.*?)\}", css, re.S | re.M).group(1)
        light = re.search(r'^:root\[data-theme="light"\] \{(.*?)\}', css, re.S | re.M).group(1)
        self.assertIn("color-scheme: dark;", dark)
        self.assertIn("color-scheme: light;", light)
        names = lambda block: sorted(re.findall(r"(--[\w-]+):", block))  # noqa: E731
        self.assertEqual(names(dark), names(light))
        self.assertGreater(len(names(dark)), 20)
        # The system setting is never consulted: the toggle is the only way to light.
        self.assertNotIn("prefers-color-scheme", css)
        self.assertNotIn("prefers-color-scheme", (WEB_DIR / "static" / "app.js").read_text())

    def test_the_stylesheet_honours_reduced_motion_and_shows_focus(self) -> None:
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn(":focus-visible", css)

    def test_the_image_builds_the_stylesheet_without_node(self) -> None:
        dockerfile = (REPO / "Dockerfile").read_text()
        self.assertIn("sh scripts/build-css.sh", dockerfile)
        self.assertNotRegex(dockerfile.lower(), r"\b(node|npm|npx)\b")
        script = (REPO / "scripts" / "build-css.sh").read_text()
        self.assertRegex(script, r"(?m)^VERSION=\d+\.\d+\.\d+$")
        self.assertEqual(len(set(re.findall(r"SUM=([0-9a-f]{64})\b", script))), 4)


class BannerTests(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def guard(self, minutes_ago, provider, state, **payload):
        return SimpleNamespace(
            at=self.NOW - timedelta(minutes=minutes_ago),
            payload={"provider": provider, "state": state, **payload},
        )

    def failing(self, *rows):
        newest_first = sorted(rows, key=lambda r: r.at, reverse=True)
        return [(f.name, f.minutes) for f in shell.failing_from(newest_first, self.NOW)]

    def test_an_open_cooldown_is_failing_until_its_seconds_run_out(self) -> None:
        self.assertEqual(self.failing(self.guard(10, "a", "cooldown", seconds=3600, reason="HTTP 429")), [("a", 50)])
        self.assertEqual(self.failing(self.guard(61, "a", "cooldown", seconds=3600)), [])

    def test_only_a_provider_s_latest_event_counts(self) -> None:
        self.assertEqual(self.failing(
            self.guard(30, "a", "cooldown", seconds=3600),
            self.guard(5, "a", "ok"),
            self.guard(40, "b", "ok"),
            self.guard(20, "b", "cooldown", seconds=1800),
        ), [("b", 10)])

    def test_a_row_without_a_provider_or_a_payload_is_skipped(self) -> None:
        rows = [SimpleNamespace(at=self.NOW, payload=None), self.guard(1, None, "cooldown", seconds=60)]
        self.assertEqual(shell.failing_from(rows, self.NOW), [])

    def test_a_database_that_is_down_shows_no_banner_and_raises_nothing(self) -> None:
        import asyncio

        def broken():
            raise OSError("connection refused")

        with mock.patch.object(shell, "async_session", broken), mock.patch.object(shell, "_cache", None):
            self.assertEqual(asyncio.run(shell.failing()), [])


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------


class RouterTests(WebCase):
    AUTH = None

    def test_every_path_the_app_had_is_still_served(self) -> None:
        served = {
            (method.upper(), path)
            for path, methods in self.app.openapi()["paths"].items() for method in methods
        }
        before = {
            ("GET", "/"), ("GET", "/item/{item_id}"), ("POST", "/item/{item_id}/approve"),
            ("POST", "/item/{item_id}/reject"), ("POST", "/item/{item_id}/search"),
            ("POST", "/item/{item_id}/download/{search_id}/{index}"),
            ("POST", "/bulk/approve"), ("POST", "/bulk/reject"), ("GET", "/stats"),
            ("GET", "/settings"), ("POST", "/settings/cookies/{key}"),
        }
        added = {
            ("GET", "/login"), ("POST", "/login"), ("POST", "/logout"),
            ("GET", "/review"), ("GET", "/policy"), ("GET", "/setup"),
            # The Inbox, Review and the item page.
            ("GET", "/inbox/row/{item_id}"), ("POST", "/inbox/{item_id}/{action}"),
            ("POST", "/review/{item_id}/{action}"),
            ("GET", "/item/{item_id}/progress"), ("POST", "/item/{item_id}/rerun"),
            # The hook Seerr calls for a new request.
            ("POST", "/hooks/seerr/{source_key}"),
            # What the policy and the providers did.
            ("GET", "/activity"),
        }
        # A page adds its own routes and lists them in its own tests; none of these may go.
        self.assertLessEqual(before | added, served)
        # The two mounts, and the schema FastAPI always served.
        for url in ("/static/app.js", "/photos/poster.jpg", "/openapi.json"):
            self.assertEqual(self.get(url).status_code, 200, url)

    def test_app_py_only_wires(self) -> None:
        source = (WEB_DIR / "app.py").read_text()
        self.assertNotRegex(source, r"@(app|router)\.")
        for name in ("auth", "hooks", "inbox", "review", "items", "policy", "setup", "stats"):
            with self.subTest(module=name):
                self.assertTrue((WEB_DIR / "routes" / f"{name}.py").exists())
                self.assertIn(name, [m.__name__.rsplit(".", 1)[1] for m in web_app.ROUTERS])

    def test_a_rejection_reaches_the_post_actions_the_app_was_built_with(self) -> None:
        self.app.state.posts = posts = object()
        done = actions.RejectResult(1, actions.Status.OK, "Rejected.")
        with mock.patch.object(actions, "reject", mock.AsyncMock(return_value=done)) as reject:
            resp = self.post("/item/1/reject", headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        reject.assert_awaited_once_with(1, posts)

    def test_an_unknown_address_is_a_page_with_a_way_back(self) -> None:
        """/nope answered FastAPI's {"detail":"Not Found"}: no layout, no way back."""
        resp = self.get("/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("Nothing here.", resp.text)
        self.assertIn('<nav aria-label="Main">', resp.text)
        self.assertIn('href="/">Open the Inbox</a>', resp.text)
        self.assertIn("<title>Not found · vettarr</title>", resp.text)
        # JSON for a client that asks for it, one line for htmx, and a missing file stays a plain 404.
        self.assertEqual(self.get("/nope", headers={"Accept": "application/json"}).json(), {"detail": "Not Found"})
        self.assertEqual(self.get("/nope", headers={"HX-Request": "true"}).text, "Nothing here.")
        self.assertNotIn("<html", self.get("/static/nope.js").text)
        # Not every refusal is a missing page.
        self.assertEqual(self.client.put("/").status_code, 405)

    def test_without_a_photos_directory_the_app_still_starts(self) -> None:
        def _path(*args):
            if args == ("/data/photos",):
                return mock.Mock(mkdir=mock.Mock(side_effect=PermissionError(13, "Permission denied")))
            return pathlib.Path(*args)

        with mock.patch.object(web_app, "Path", _path):
            client = TestClient(web_app.create_web_app(None, Config.model_validate({"web": {}})))
        self.assertEqual(client.get("/photos/poster.jpg").status_code, 404)
        self.assertEqual(client.get("/static/app.js").status_code, 200)


if __name__ == "__main__":
    unittest.main()
