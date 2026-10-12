"""Booting from config.yaml or, with no file, from the environment variables."""

import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vettarr import __main__ as app
from vettarr.config import cli, runtime
from vettarr.config.components import components
from vettarr.config.env import Settings, TrackerConfig
from vettarr.config.import_env import secret_names
from vettarr.config.loader import load, load_text
from vettarr.config.schema import Config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

# Every variable the app reads from the environment, with fake values.
FULL_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "fake-hash-boot-0001",
    "TELEGRAM_PHONE": "+15550002222",
    "TELEGRAM_GROUP_ID": "-1001234567890",
    "TELEGRAM_BOT_TOKEN": "777:fake-bot-token-boot",
    "TELEGRAM_OWNER_ID": "4242",
    "TMDB_API_KEY": "fake-tmdb-key-boot",
    # A URL carrying a password is kept as a reference, never a value.
    "PROWLARR_URL": "http://operator:fake-url-pass@indexer:9696",
    "PROWLARR_API_KEY": "fake-prowlarr-key-boot",
    "QBITTORRENT_URL": "http://download-client:8080",
    "QBITTORRENT_USER": "operator",
    "QBITTORRENT_PASS": "fake-qbit-pass-boot",
    "TRACKER_1_NAME": "Tracker A",
    "TRACKER_1_URL": "https://tracker-a.example",
    "TRACKER_1_COOKIE_PATH": "/data/cookies/a.txt",
    "TRACKER_1_USERNAME": "someone",
    "TRACKER_1_PASSWORD": "fake-tracker-pass-boot",
    "TRACKER_1_LOGGED_OUT_MARKER": 'name="password"',
    "TRACKER_2_NAME": "Tracker B",
    "TRACKER_2_URL": "https://tracker-b.example",
    "TRACKER_2_COOKIE_PATH": "/data/cookies/b.txt",
    "GOOGLE_BOOKS_API_KEY": "fake-books-key-boot",
    "AUTO_APPROVE_ENABLED": "true",
    "AUTO_APPROVE_TYPES": "movie,series,magazine,ebook",
    "MIN_GUIDE_VOTES": "7",
    "AUTO_APPROVE_MAX_WAIT_HOURS": "12",
    "WEB_PORT": "4851",
    "SESSION_DIR": "/data/sessions",
}
# Values that must never reach a log: the secret variables, and the password in a URL.
SECRETS = [FULL_ENV[name] for name in secret_names(FULL_ENV)] + ["fake-url-pass"]


def found(text: str) -> list[str]:
    return [value for value in SECRETS if value in text]


class ProcessConfig(unittest.TestCase):
    """Each test resolves the process Config afresh and leaves none behind."""

    def setUp(self) -> None:
        runtime.reset()
        self.addCleanup(runtime.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class CompatibilityTests(ProcessConfig):
    def test_the_environment_config_equals_the_imported_file(self) -> None:
        out = self.tmp / "config.yaml"
        with mock.patch.dict(os.environ, FULL_ENV, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["import-env", "--output", str(out)]), 0)
        from_file = load(out, FULL_ENV)
        from_env = runtime.from_environment(FULL_ENV)
        self.assertEqual(from_env, from_file)
        # Positive control: the comparison sees a single changed variable.
        changed = runtime.from_environment({**FULL_ENV, "MIN_GUIDE_VOTES": "8"})
        self.assertNotEqual(changed, from_file)

    def test_settings_read_what_the_environment_said(self) -> None:
        s = Settings(runtime.from_environment(FULL_ENV))
        self.assertEqual(s.telegram_api_id, 123456)
        self.assertEqual(s.telegram_api_hash, FULL_ENV["TELEGRAM_API_HASH"])
        self.assertEqual(s.telegram_phone, FULL_ENV["TELEGRAM_PHONE"])
        self.assertEqual(s.telegram_group_id, -1001234567890)
        self.assertEqual(s.telegram_bot_token, FULL_ENV["TELEGRAM_BOT_TOKEN"])
        self.assertEqual(s.telegram_owner_id, 4242)
        self.assertEqual(s.tmdb_api_key, FULL_ENV["TMDB_API_KEY"])
        self.assertEqual(s.prowlarr_url, FULL_ENV["PROWLARR_URL"])
        self.assertEqual(s.prowlarr_api_key, FULL_ENV["PROWLARR_API_KEY"])
        self.assertEqual(s.qbittorrent_url, FULL_ENV["QBITTORRENT_URL"])
        self.assertEqual(s.qbittorrent_user, "operator")
        self.assertEqual(s.qbittorrent_pass, FULL_ENV["QBITTORRENT_PASS"])
        self.assertEqual(s.google_books_api_key, FULL_ENV["GOOGLE_BOOKS_API_KEY"])
        self.assertTrue(s.auto_approve_enabled)
        self.assertEqual(s.auto_approve_types, frozenset({"movie", "series", "magazine", "ebook"}))
        self.assertEqual(s.min_guide_votes, 7)
        self.assertEqual(s.auto_approve_max_wait_hours, 12)
        self.assertEqual(s.web_port, 4851)
        self.assertEqual(s.session_dir, Path("/data/sessions"))
        self.assertEqual(s.telegram_session_path, Path("/data/sessions/media_triage"))
        self.assertEqual(
            s.trackers,
            (
                TrackerConfig("tracker-a", "https://tracker-a.example", "/data/cookies/a.txt",
                              "someone", FULL_ENV["TRACKER_1_PASSWORD"], 'name="password"'),
                TrackerConfig("tracker-b", "https://tracker-b.example", "/data/cookies/b.txt", "", ""),
            ),
        )
        # The name is now the provider key. For names of letters, digits and
        # spaces the key the app derives from it is unchanged; any other run of
        # characters becomes one "-" ("HD Torrents!" -> "hd-torrents").
        old_keys = [FULL_ENV[f"TRACKER_{n}_NAME"].lower().replace(" ", "-") for n in (1, 2)]
        self.assertEqual([t.name.lower().replace(" ", "-") for t in s.trackers], old_keys)

    def test_the_old_defaults_hold_with_only_the_required_variables(self) -> None:
        env = {name: FULL_ENV[name] for name in runtime.REQUIRED_ENV}
        s = Settings(runtime.from_environment(env))
        self.assertFalse(s.auto_approve_enabled)
        self.assertEqual(s.auto_approve_types, frozenset({"movie", "series", "magazine"}))
        self.assertEqual((s.min_guide_votes, s.auto_approve_max_wait_hours), (5, 24))
        self.assertEqual((s.qbittorrent_user, s.qbittorrent_pass), ("admin", ""))
        self.assertEqual((s.web_port, s.session_dir), (4850, Path("/data/sessions")))
        self.assertEqual((s.trackers, s.google_books_api_key), ((), ""))

    def test_an_empty_auto_approve_types_approves_no_type(self) -> None:
        # The old loader took the default only when the variable was unset.
        env = {**{name: FULL_ENV[name] for name in runtime.REQUIRED_ENV},
               "AUTO_APPROVE_ENABLED": "true", "AUTO_APPROVE_TYPES": ""}
        s = Settings(runtime.from_environment(env))
        self.assertTrue(s.auto_approve_enabled)
        self.assertEqual(s.auto_approve_types, frozenset())

    def test_an_empty_qbittorrent_user_stays_empty(self) -> None:
        env = {**{name: FULL_ENV[name] for name in runtime.REQUIRED_ENV}, "QBITTORRENT_USER": ""}
        self.assertEqual(Settings(runtime.from_environment(env)).qbittorrent_user, "")


class BootSourceTests(ProcessConfig):
    def resolve_with(self, env) -> tuple[Config, list[str]]:
        with mock.patch.dict(os.environ, env, clear=True), \
                self.assertLogs("vettarr.config.runtime", level="INFO") as logs:
            cfg = runtime.get_config()
        return cfg, logs.output

    def setUp(self) -> None:
        super().setUp()
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)

    def test_boot_from_file(self) -> None:
        path = self.tmp / "config.yaml"
        path.write_text("web:\n  port: 8080\n")
        cfg, lines = self.resolve_with({"CONFIG_PATH": str(path)})
        self.assertEqual(cfg.web.port, 8080)
        self.assertEqual(lines, [f"INFO:vettarr.config.runtime:Config: {path}"])
        self.assertIs(runtime.get_config(), cfg)
        self.assertEqual(Settings().web_port, 8080)

    def test_boot_from_environment_writes_nothing(self) -> None:
        path = self.tmp / "config.yaml"
        cfg, lines = self.resolve_with({**FULL_ENV, "CONFIG_PATH": str(path)})
        self.assertEqual(lines, [
            f"WARNING:vettarr.config.runtime:No config file at {path}: booting from the environment variables",
            "INFO:vettarr.config.runtime:Config: environment, no config file",
            f"WARNING:vettarr.config.runtime:{runtime.ENVIRONMENT_ONLY}",
        ])
        self.assertEqual(cfg, runtime.from_environment(FULL_ENV))
        self.assertEqual(Settings().telegram_group_id, -1001234567890)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_an_environment_boot_says_what_it_is_missing(self) -> None:
        """A boot from the environment is not the setup a deployment had before
        config.yaml, and the log says so in as many words. A boot from a file
        logs no such line (test_boot_from_file)."""
        cfg, lines = self.resolve_with({**FULL_ENV, "CONFIG_PATH": str(self.tmp / "absent.yaml")})
        warnings = [line for line in lines if line.startswith("WARNING:") and "No config file at" not in line]
        self.assertEqual(len(warnings), 1)
        for phrase in (
            "scene release names only", "card format", "media words", "tracker link patterns",
            "categories and save paths", "keyword, genre and subject lists", "config.yaml",
            "every download is added with no category and no save path",
            "No tracker is thanked and no session is kept alive",
        ):
            self.assertIn(phrase, warnings[0])
        # And it is true of the config it describes: one scene profile, no words.
        self.assertEqual([f.kind for f in cfg.formats], ["scene"])
        self.assertEqual([(f.media_words, f.ignore_hosts, f.type_headers) for f in cfg.formats], [([], [], {})])
        # No category, no save path, and both trackers of this environment left alone.
        self.assertEqual((cfg.integrations.qbittorrent.categories, cfg.integrations.qbittorrent.save_paths), ({}, {}))
        self.assertEqual(len(cfg.providers), 2)
        self.assertEqual([(p.thank.enabled, p.keep_alive.enabled) for p in cfg.providers], [(False, False)] * 2)

    def test_the_doc_says_what_an_environment_boot_does(self) -> None:
        """The accepted cost of booting without a file is only accepted if the doc states it."""
        doc = (Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md").read_text()
        boot = doc.split("## The rules of the file")[0]
        self.assertNotIn("keeps working unchanged", doc)
        for phrase in (
            "An environment boot is not the setup a deployment had before",
            "Every download is added with no category and no save path",
            "`thank` and `keep_alive` off",
            "is never contacted",
        ):
            self.assertIn(phrase, boot)

    def test_words_that_only_compile_alone_stop_the_start_with_an_anchored_line(self) -> None:
        path = self.tmp / "config.yaml"
        path.write_text("formats:\n  - key: bad\n    type_hints:\n      series: ['/(?i)season/']\n")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**FULL_ENV, "CONFIG_PATH": str(path)}, clear=True), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            app.boot()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn(f"{path}:4: formats[0].type_hints.series[0]: pattern does not compile", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_missing_file_is_named_before_the_environment_boot_fails(self) -> None:
        """A quick start whose config.yaml is not where the app looks fails on
        a missing variable; the line before it says which file was missing."""
        path = self.tmp / "absent.yaml"
        with mock.patch.dict(os.environ, {"CONFIG_PATH": str(path)}, clear=True), \
                self.assertLogs("vettarr.config.runtime", level="WARNING") as logs, \
                self.assertRaises(RuntimeError):
            runtime.get_config()
        self.assertEqual(logs.output, [
            f"WARNING:vettarr.config.runtime:No config file at {path}: booting from the environment variables",
        ])

    def test_no_file_and_no_variables_is_the_old_error(self) -> None:
        env = {"CONFIG_PATH": str(self.tmp / "absent.yaml")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, r"^Missing required environment variable: TELEGRAM_API_ID$"):
                runtime.get_config()
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                app.boot()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("Missing required environment variable: TELEGRAM_API_ID", err.getvalue())

    def test_an_invalid_file_exits_1_with_anchored_errors(self) -> None:
        path = self.tmp / "config.yaml"
        path.write_text("web:\n  port: 99999\nbogus: 1\n")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**FULL_ENV, "CONFIG_PATH": str(path)}, clear=True), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            app.boot()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn(f"{path}:2: web.port:", err.getvalue())
        self.assertIn(f"{path}:3: bogus:", err.getvalue())

    def test_a_missing_example_exits_1_with_one_line(self) -> None:
        env = {**FULL_ENV, "CONFIG_PATH": str(self.tmp / "absent.yaml")}
        missing = FileNotFoundError(2, "No such file or directory", "config.example.yaml")
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(cli, "example_text", side_effect=missing), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            app.boot()
        self.assertEqual(stop.exception.code, 1)
        self.assertEqual(err.getvalue().splitlines(),
                         ["vettarr: [Errno 2] No such file or directory: 'config.example.yaml'"])

    def test_no_secret_reaches_the_boot_log(self) -> None:
        env = {**FULL_ENV, "CONFIG_PATH": str(self.tmp / "absent.yaml")}
        with mock.patch.dict(os.environ, env, clear=True), \
                self.assertLogs("vettarr", level="DEBUG") as logs:
            app.boot()
        text = "\n".join(logs.output)
        self.assertIn("Starting bot", text)
        self.assertIn("Config: environment, no config file", text)
        # Positive control: the grep finds every secret once they are in the text.
        self.assertEqual(found(text + "\n" + " ".join(SECRETS)), SECRETS)
        self.assertEqual(found(text), [])


class GatingTests(unittest.TestCase):
    @staticmethod
    def names(cfg: Config) -> list[str]:
        return [name for name, _ in app.plan(cfg)]

    def test_an_empty_config_starts_nothing(self) -> None:
        cfg = load_text("", "c.yaml", {})
        self.assertEqual(components(cfg), [])
        self.assertEqual(app.plan(cfg), [("scheduler", "always")])

    def test_a_file_with_only_web_starts_web_only(self) -> None:
        cfg = load_text("web:\n  port: 8080\n", "c.yaml", {})
        self.assertEqual(self.names(cfg), ["web", "scheduler"])

    def test_a_bot_token_starts_the_bot(self) -> None:
        cfg = load_text("telegram:\n  bot_token: ${T}\n", "c.yaml", {"T": "x"})
        self.assertEqual(self.names(cfg), ["bot", "scheduler"])

    def test_a_bot_with_no_reviewers_says_it_answers_no_one(self) -> None:
        from vettarr.config.schema import warnings

        cfg = load_text("telegram:\n  bot_token: ${T}\n", "c.yaml", {"T": "x"})
        self.assertIn(("bot", "reviewers only (telegram.reviewers is empty: it answers no one)"), app.plan(cfg))
        self.assertIn(("telegram",), [loc for loc, _ in warnings(cfg)])
        # With a reviewer there is neither the note nor the warning.
        cfg = load_text("telegram:\n  bot_token: ${T}\n  reviewers: [1]\n", "c.yaml", {"T": "x"})
        self.assertIn(("bot", "reviewers only"), app.plan(cfg))
        self.assertNotIn(("telegram",), [loc for loc, _ in warnings(cfg)])

    def test_the_environment_starts_what_it_always_did(self) -> None:
        self.assertEqual(
            self.names(runtime.from_environment(FULL_ENV)), ["userbot", "bot", "web", "scheduler"],
        )


class CleanEnvironmentTests(unittest.TestCase):
    """No variable set and no config file: imports and the config commands still work."""

    def run_python(self, *args: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp,
                   "CONFIG_PATH": str(Path(tmp) / "absent.yaml")}
            return subprocess.run(
                [sys.executable, *args], cwd=tmp, env=env, capture_output=True, text=True, timeout=120,
            )

    IMPORTS = (
        "import vettarr.__main__, vettarr.pipeline, vettarr.web.app, "
        "vettarr.bot.handlers; from vettarr.config import settings"
    )

    def test_importing_the_app_does_not_raise(self) -> None:
        done = self.run_python("-c", self.IMPORTS + "; print('imported')")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "imported")

    def test_the_first_setting_read_is_what_raises(self) -> None:
        # Positive control: the environment really is empty, and resolution waits for first use.
        done = self.run_python("-c", self.IMPORTS + "; settings.telegram_api_id")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Missing required environment variable: TELEGRAM_API_ID", done.stderr)

    def test_config_example_and_check_need_no_variable(self) -> None:
        example = self.run_python("-m", "vettarr", "config", "example")
        self.assertEqual(example.returncode, 0, example.stderr)
        self.assertEqual(example.stdout, EXAMPLE.read_text())
        check = self.run_python("-m", "vettarr", "config", "check", str(EXAMPLE))
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("Would start:", check.stdout)


class NameTests(unittest.TestCase):
    """The product is vettarr. The old command name still starts the same
    program, so a compose file written for it keeps working."""

    PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

    def project(self) -> dict:
        import tomllib
        return tomllib.loads(self.PYPROJECT.read_text())["project"]

    def test_the_distribution_command_and_licence_are_vettarr_s(self) -> None:
        project = self.project()
        self.assertEqual(project["name"], "vettarr")
        self.assertEqual(project["license"], "AGPL-3.0-or-later")
        self.assertEqual(project["scripts"]["vettarr"], "vettarr.__main__:main")
        self.assertEqual(project["scripts"]["media-triage"], "vettarr.__main__:legacy_main")
        licence = (self.PYPROJECT.parent / "LICENSE").read_text()
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", licence.splitlines()[0])

    def test_the_old_command_says_so_once_and_runs_the_same_main(self) -> None:
        err = io.StringIO()
        with mock.patch.object(app, "main") as main, contextlib.redirect_stderr(err):
            app.legacy_main(["config", "check"])
        main.assert_called_once_with(["config", "check"])
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("now `vettarr`", lines[0])

    def test_the_database_defaults_name_vettarr(self) -> None:
        """Defaults only: a deployment's own POSTGRES_* values still win."""
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings()
            self.assertEqual((s.postgres_user, s.postgres_password, s.postgres_db), ("vettarr",) * 3)
        with mock.patch.dict(os.environ, {"POSTGRES_USER": "media_triage", "POSTGRES_DB": "media_triage"}, clear=True):
            s = Settings()
            self.assertEqual((s.postgres_user, s.postgres_db), ("media_triage", "media_triage"))

    def test_the_main_logger_is_vettarr(self) -> None:
        self.assertEqual(app.log.name, "vettarr")


if __name__ == "__main__":
    unittest.main()
