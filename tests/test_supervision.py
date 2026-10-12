"""A component that dies stops the app with exit 1, and no secret reaches a log.

Two defects of the old start-up path, each tested through the code that runs
in production:

- python-telegram-bot names the token it was refused with, and the app logged
  that message as it came.
- A bot, a userbot, a web server or a scheduler that failed left the others
  cancelled mid-flight and the process exiting 0, so no restart policy acted.

Nothing here touches the network or a database: Telegram's answer is a stubbed
HTTP 401, which is what it sends for a token it does not know.
"""

import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

from vettarr import __main__ as app
from vettarr.config import runtime
from vettarr.config.schema import Config

# Shaped like a real bot token (the bot's id, a colon, 35 characters), and fake.
FAKE_TOKEN = "1234567890:AAF_fake-token-for-the-tests-0123456789"
UNAUTHORIZED = (401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}')
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def mask_module():
    """`vettarr.log_mask`, imported late so a test names what is missing."""
    return importlib.import_module("vettarr.log_mask")


class CapturedLog:
    """Everything the process logs, through a handler set up as production's is.

    The handler is on the root logger, where the app's own handler sits, and
    `install()` is what puts the filter on it. Other handlers are set aside
    for the duration so the expected errors do not land in the test output.
    """

    def __enter__(self) -> "CapturedLog":
        root = logging.getLogger()
        self._disabled = root.manager.disable
        logging.disable(logging.NOTSET)
        self._level = root.level
        root.setLevel(logging.INFO)
        self._others = list(root.handlers)
        for handler in self._others:
            root.removeHandler(handler)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(self.handler)
        return self

    def __exit__(self, *exc_info) -> None:
        root = logging.getLogger()
        root.removeHandler(self.handler)
        for handler in self._others:
            root.addHandler(handler)
        root.setLevel(self._level)
        logging.disable(self._disabled)

    @property
    def text(self) -> str:
        return self.stream.getvalue()


def forget_secrets() -> None:
    """Leave the process-wide filter knowing no secret, as it was before the test."""
    try:
        module = mask_module()
    except ImportError:
        return
    module.install().set_secrets(())


# --- the filter ---------------------------------------------------------------


class SecretValuesTests(unittest.TestCase):
    # One invented value per secret field of the schema, named after its block.
    FIELDS = (
        "telegram-hash", "telegram-phone", "telegram-bot", "provider-login", "prowlarr", "qbittorrent",
        "radarr", "sonarr", "seerr", "tmdb", "google-books", "jellyfin", "web-auth",
    )

    def test_every_secret_field_of_the_config_is_found(self) -> None:
        fake = {name: f"invented-{name}-value" for name in self.FIELDS}
        notify_url = "https://notify.example/invented-notify-path"
        cfg = Config.model_validate({
            "telegram": {
                "api_id": 1, "api_hash": fake["telegram-hash"], "phone": fake["telegram-phone"],
                "bot_token": fake["telegram-bot"],
            },
            "providers": [
                {"key": "a", "base_url": "https://a.example"},
                {"key": "b", "base_url": "https://b.example", "search": "unit3d",
                 "auth": {"from": "login", "username": "me", "password": fake["provider-login"]}},
            ],
            "integrations": {
                "prowlarr": {"url": "http://prowlarr", "api_key": fake["prowlarr"]},
                "qbittorrent": {"url": "http://qbittorrent", "password": fake["qbittorrent"]},
                "radarr": {"url": "http://radarr", "api_key": fake["radarr"]},
                "sonarr": {"url": "http://sonarr", "api_key": fake["sonarr"]},
                "seerr": {"url": "http://seerr", "api_key": fake["seerr"]},
                "tmdb": {"api_key": fake["tmdb"]},
                "google_books": {"api_key": fake["google-books"]},
                "jellyfin": {"url": "http://jellyfin", "api_key": fake["jellyfin"]},
            },
            "notify": {"url": notify_url},
            "web": {"auth": {"password_hash": fake["web-auth"]}},
        })
        self.assertEqual(mask_module().secret_values(cfg), {*fake.values(), notify_url})
        self.assertEqual(len(fake), 13)

    def test_a_config_with_no_secret_has_none(self) -> None:
        self.assertEqual(mask_module().secret_values(Config()), set())


class SecretFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        # Other test modules switch logging off for the whole process.
        disabled = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, disabled)
        self.filter = mask_module().SecretFilter()
        self.stream = io.StringIO()
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(self.filter)
        self.log = logging.Logger("masking-test")
        self.log.addHandler(handler)

    def test_a_secret_is_masked_in_the_message_and_in_an_argument(self) -> None:
        self.filter.set_secrets(["fake-secret-value"])
        self.log.error("key fake-secret-value refused")
        self.log.error("Task %s failed: %s", "bot", RuntimeError("no: fake-secret-value"))
        self.log.error("%(what)s", {"what": "fake-secret-value"})
        out = self.stream.getvalue()
        self.assertNotIn("fake-secret-value", out)
        self.assertEqual(out.count("***"), 3)
        self.assertIn("Task bot failed: no: ***", out)

    def test_a_secret_is_masked_in_a_traceback(self) -> None:
        self.filter.set_secrets(["fake-secret-value"])
        try:
            raise RuntimeError("refused fake-secret-value")
        except RuntimeError:
            self.log.exception("it failed")
        out = self.stream.getvalue()
        self.assertIn("Traceback (most recent call last)", out)
        self.assertIn("RuntimeError: refused ***", out)
        self.assertNotIn("fake-secret-value", out)

    def test_a_secret_is_masked_as_a_url_carries_it(self) -> None:
        self.filter.set_secrets(["p@ss word/1"])
        self.log.error("GET https://tracker-a.example/?key=p%40ss%20word%2F1 and ?key=p%40ss+word%2F1")
        out = self.stream.getvalue()
        self.assertNotIn("p%40ss", out)
        self.assertEqual(out.count("***"), 2)

    def test_anything_shaped_like_a_bot_token_is_masked_with_no_config(self) -> None:
        self.log.error("POST https://api.telegram.org/bot%s/getMe", FAKE_TOKEN)
        self.log.error(f"The token `{FAKE_TOKEN}` was rejected by the server.")
        out = self.stream.getvalue()
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertIn("https://api.telegram.org/bot***/getMe", out)
        self.assertIn("The token `***` was rejected", out)

    def test_what_is_not_a_secret_is_left_alone(self) -> None:
        self.filter.set_secrets(["fake-secret-value", "abc"])
        self.log.error("at 12:30:45 item %d of chat -1001234567890: abc, id 777:short", 42)
        self.assertIn("at 12:30:45 item 42 of chat -1001234567890: abc, id 777:short", self.stream.getvalue())

    def test_the_arguments_keep_their_shape(self) -> None:
        """uvicorn's access formatter unpacks five arguments; a masked one is still one."""
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", f"/hook/{FAKE_TOKEN}", "1.1", 200), None,
        )
        self.assertTrue(self.filter.filter(record))
        self.assertEqual(record.args, ("127.0.0.1:1", "GET", "/hook/***", "1.1", 200))
        self.assertIs(type(record.args[4]), int)

    def test_a_secret_split_between_the_message_and_an_argument_is_masked(self) -> None:
        self.filter.set_secrets(["fake-secret-value"])
        self.log.error("fake-secret-%s", "value")
        self.assertNotIn("fake-secret-value", self.stream.getvalue())

    def test_a_number_holding_a_secret_s_digits_still_formats(self) -> None:
        """A masked number is text, `%d` cannot format text, and logging drops
        the whole line it cannot format. The number stays a number and the
        formatted line is masked."""
        self.filter.set_secrets(["20241"])
        with contextlib.redirect_stderr(io.StringIO()) as complaints:
            self.log.error("item %d of chat %d", 120241, 5)
            self.log.error("ratio %.1f", 20241.5)
        out = self.stream.getvalue()
        self.assertEqual(complaints.getvalue(), "", "logging could not format the line")
        self.assertIn("item 1*** of chat 5", out)
        self.assertIn("ratio ***.5", out)
        self.assertNotIn("20241", out)

    def test_a_secret_is_masked_as_json_and_a_repr_escape_it(self) -> None:
        """A quote, a backslash and a non-ASCII letter are each written
        differently in a JSON body and in the repr an exception prints."""
        value = "p@ss" + '"w' + "\\" + "y'ñ" + "!"
        self.filter.set_secrets([value])
        self.log.error("body %s", json.dumps({"field": value}))
        self.log.error("body %s", json.dumps({"field": value}, ensure_ascii=False))
        try:
            raise KeyError(value)
        except KeyError:
            self.log.exception("it failed")
        out = self.stream.getvalue()
        forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1]}
        self.assertEqual(len(forms), 4, "the value must be one each form writes differently")
        for form in forms:
            self.assertNotIn(form, out)
        self.assertNotIn("p@ss", out)
        self.assertEqual(out.count("***"), 3)

    def test_the_longer_secret_wins_over_one_it_contains(self) -> None:
        self.filter.set_secrets(["fake-secret", "fake-secret-value"])
        self.log.error("fake-secret-value")
        self.assertTrue(self.stream.getvalue().rstrip().endswith(": ***"))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(forget_secrets)

    def test_the_config_s_secrets_are_masked_on_the_root_handler(self) -> None:
        cfg = Config.model_validate({"integrations": {"tmdb": {"api_key": "fake-tmdb-key-install"}}})
        with CapturedLog() as log:
            mask_module().install(cfg)
            logging.getLogger("httpx").warning("GET https://api.example/3/movie?api_key=fake-tmdb-key-install")
        self.assertIn("api_key=***", log.text)
        self.assertNotIn("fake-tmdb-key-install", log.text)

    def test_uvicorn_s_own_handlers_carry_the_filter(self) -> None:
        cfg = Config.model_validate({"web": {"bind": "127.0.0.1", "port": 4850}})
        app.web_server(asgi_app, cfg)
        own = [
            handler for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
            for handler in logging.getLogger(name).handlers
        ]
        self.assertGreaterEqual(len(own), 2, "uvicorn builds handlers of its own; the test must see them")
        installed = mask_module().install()
        for handler in own:
            self.assertIn(installed, handler.filters)
        # And one of them, end to end.
        disabled = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, disabled)
        handler = logging.getLogger("uvicorn").handlers[0]
        stream = io.StringIO()
        old = handler.setStream(stream)
        self.addCleanup(handler.setStream, old)
        logging.getLogger("uvicorn.error").error("bad request to /bot%s/x", FAKE_TOKEN)
        self.assertIn("/bot***/x", stream.getvalue())
        self.assertNotIn(FAKE_TOKEN, stream.getvalue())


# --- one component fails ------------------------------------------------------


async def asgi_app(scope, receive, send) -> None:
    """The smallest app uvicorn serves: it answers the lifespan and nothing else."""
    if scope["type"] != "lifespan":
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


class Component:
    """A well-behaved component: it runs until the stop and then tears itself down."""

    def __init__(self) -> None:
        self.stopped_cleanly = False

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()
        await asyncio.sleep(0)
        self.stopped_cleanly = True


async def supervised(*components, grace: float = 5.0, stop_after: float | None = None):
    """Run `(name, coroutine function taking the stop)` pairs under `supervise`."""
    stop = asyncio.Event()
    tasks = [asyncio.create_task(run(stop), name=name) for name, run in components]
    if stop_after is not None:
        asyncio.get_running_loop().call_later(stop_after, stop.set)
    return await app.supervise(tasks, stop, grace=grace)


class SuperviseTests(unittest.TestCase):
    def test_a_stop_nobody_failed_at_is_clean(self) -> None:
        a, b = Component(), Component()
        with CapturedLog() as log:
            failed = asyncio.run(supervised(("bot", a.run), ("scheduler", b.run), stop_after=0.01))
        self.assertEqual(failed, [])
        self.assertTrue(a.stopped_cleanly and b.stopped_cleanly)
        self.assertEqual(log.text, "")

    def test_a_component_that_raises_is_named_and_the_others_stop_their_own_way(self) -> None:
        async def bot(stop):
            raise RuntimeError("cannot log in")

        other = Component()
        with CapturedLog() as log:
            failed = asyncio.run(supervised(("scheduler", other.run), ("bot", bot)))
        self.assertEqual(failed, ["bot"])
        self.assertTrue(other.stopped_cleanly, "the scheduler was cancelled instead of stopped")
        self.assertIn("bot failed: RuntimeError: cannot log in", log.text)
        self.assertNotIn("scheduler failed", log.text)
        self.assertNotIn("CancelledError", log.text)

    def test_a_component_that_ends_by_itself_has_failed(self) -> None:
        async def web(stop):
            return None

        other = Component()
        with CapturedLog() as log:
            failed = asyncio.run(supervised(("web", web), ("scheduler", other.run)))
        self.assertEqual(failed, ["web"])
        self.assertTrue(other.stopped_cleanly)
        self.assertIn("web failed: it stopped by itself", log.text)

    def test_a_second_failure_during_the_stop_is_named_too(self) -> None:
        async def bot(stop):
            raise RuntimeError("first")

        async def userbot(stop):
            await stop.wait()
            raise RuntimeError("second")

        with CapturedLog():
            failed = asyncio.run(supervised(("bot", bot), ("userbot", userbot)))
        self.assertEqual(failed, ["bot", "userbot"])

    def test_a_component_that_ignores_the_stop_is_cancelled_without_a_traceback(self) -> None:
        async def bot(stop):
            raise RuntimeError("cannot log in")

        async def deaf(stop):
            await asyncio.sleep(3600)

        with CapturedLog() as log:
            failed = asyncio.run(supervised(("bot", bot), ("userbot", deaf), grace=0.05))
        self.assertEqual(failed, ["bot"])
        self.assertIn("userbot did not stop within", log.text)
        self.assertNotIn("CancelledError", log.text)


class WebComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(forget_secrets)

    @staticmethod
    def server(port: int):
        return app.web_server(asgi_app, Config.model_validate({"web": {"bind": "127.0.0.1", "port": port}}))

    def test_a_port_already_taken_is_a_failed_component_not_a_process_exit(self) -> None:
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        other = Component()

        async def scenario():
            server = self.server(taken.getsockname()[1])
            return await supervised(("web", lambda stop: app.run_web(server, stop)), ("scheduler", other.run))

        # uvicorn says why on its own stderr handler; the app's log names the component.
        with CapturedLog() as log, contextlib.redirect_stderr(io.StringIO()) as uvicorn_says:
            failed = asyncio.run(scenario())
        self.assertIn("address already in use", uvicorn_says.getvalue().lower())
        self.assertEqual(failed, ["web"])
        self.assertTrue(other.stopped_cleanly)
        self.assertIn("web failed: RuntimeError: the web server could not start", log.text)

    def test_the_stop_reaches_the_web_server(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()

        async def scenario():
            server = self.server(free_port)
            stop = asyncio.Event()
            task = asyncio.create_task(app.run_web(server, stop), name="web")
            for _ in range(500):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started, "the web server never came up")
            stop.set()
            return await app.supervise([task], stop, grace=10.0)

        with CapturedLog() as log:
            failed = asyncio.run(scenario())
        self.assertEqual(failed, [])
        self.assertNotIn("did not stop within", log.text)


# --- the real failure: Telegram refuses the bot token ----------------------------


class RejectedBotTokenTests(unittest.TestCase):
    """`main()` with a config whose bot token Telegram answers 401 to."""

    def setUp(self) -> None:
        from vettarr import ingest, search_jobs
        from vettarr.bot import handlers
        from vettarr.searcher import search_service

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text("telegram:\n  bot_token: ${BOT_TOKEN_UNDER_TEST}\n  reviewers: [1]\n")
        runtime.reset()
        self.addCleanup(runtime.reset)
        self.addCleanup(forget_secrets)

        self.jobs_stopped_cleanly = False

        async def download_jobs(posts, stop) -> None:
            await stop.wait()
            await asyncio.sleep(0)
            self.jobs_stopped_cleanly = True

        self.do_request = mock.AsyncMock(return_value=UNAUTHORIZED)
        for patch in (
            mock.patch.dict(os.environ, {"CONFIG_PATH": str(path), "BOT_TOKEN_UNDER_TEST": FAKE_TOKEN}),
            mock.patch.object(app, "init_db", mock.AsyncMock()),
            mock.patch.object(app, "_last_flush", mock.AsyncMock()),
            mock.patch.object(search_jobs, "run_search_jobs", download_jobs),
            mock.patch.object(search_service, "init_trackers", lambda: None),
            mock.patch("telegram.request.HTTPXRequest.do_request", self.do_request),
            # What building the bot leaves behind in the modules other tests use.
            mock.patch.object(handlers, "_ingester", handlers._ingester),
            mock.patch.object(handlers, "_bot_app", handlers._bot_app),
            mock.patch.object(ingest, "_queue", ingest._queue),
            mock.patch.object(ingest, "_review_pusher", ingest._review_pusher),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self) -> tuple[str, int]:
        """The log of one start, and the exit code (0 when `main` just returns)."""
        code = 0
        with CapturedLog() as log:
            try:
                app.main([])
            except SystemExit as stop:
                code = stop.code
        return log.text, code

    def test_the_token_is_never_logged(self) -> None:
        text, _ = self.run_main()
        self.do_request.assert_awaited()
        self.assertIn("Starting bot", text)
        self.assertIn("was rejected by the server", text, "the failure was not logged at all")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("The token `***` was rejected by the server", text)

    def test_the_token_would_be_in_the_log_without_the_filter(self) -> None:
        """Positive control: the same start with the filter doing nothing."""
        with mock.patch.object(mask_module().SecretFilter, "filter", lambda self, record: True):
            text, _ = self.run_main()
        self.assertIn(FAKE_TOKEN, text)

    def test_the_log_names_the_bot_and_the_process_exits_1(self) -> None:
        text, code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("bot failed: InvalidToken", text)
        self.assertIn("Exiting with status 1: bot failed", text)

    def test_the_other_components_stop_their_own_way(self) -> None:
        text, _ = self.run_main()
        self.assertTrue(self.jobs_stopped_cleanly, "the download worker was cancelled, not stopped")
        self.assertNotIn("did not stop within", text)
        self.assertNotIn("CancelledError", text)


class ExitCodeTests(unittest.TestCase):
    def run_main(self, failed: list[str]) -> int:
        with (
            mock.patch.object(app, "boot", return_value=(Config(), [("scheduler", "always")])),
            mock.patch.object(app, "_run", mock.AsyncMock(return_value=failed)),
            CapturedLog(),
        ):
            try:
                app.main([])
            except SystemExit as stop:
                return stop.code
        return 0

    def test_a_clean_stop_exits_0(self) -> None:
        self.assertEqual(self.run_main([]), 0)

    def test_a_failed_component_exits_1(self) -> None:
        self.assertEqual(self.run_main(["web"]), 1)


class ComposeFileTests(unittest.TestCase):
    """The shipped compose file lets the stop finish, and restarts what exits 1."""

    COMPOSE = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()

    def test_the_stop_timeout_outlasts_the_grace_the_components_get(self) -> None:
        """Docker kills at 10 seconds unless told otherwise, in the middle of the stop."""
        import re

        found = re.search(r"^    stop_grace_period: (\d+)s$", self.COMPOSE, re.MULTILINE)
        self.assertIsNotNone(found, "the app service sets no stop_grace_period")
        self.assertGreater(int(found.group(1)), app._STOP_GRACE_SECONDS)

    def test_a_restart_policy_acts_on_the_exit_code(self) -> None:
        self.assertRegex(self.COMPOSE, r"(?m)^    restart: unless-stopped$")

    def test_the_web_ui_is_published_on_this_machine_only(self) -> None:
        """The example config has no web.auth: until the reader sets it, the UI
        must not answer anyone else on the network."""
        import re

        ports = re.findall(r'(?m)^      - "([^"]*):4850"$', self.COMPOSE)
        self.assertEqual(ports, ["127.0.0.1:4850"])


if __name__ == "__main__":
    unittest.main()
