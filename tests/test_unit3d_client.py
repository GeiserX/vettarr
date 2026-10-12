import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

from vettarr.searcher.unit3d_client import DEFAULT_LOGGED_OUT_MARKER, Unit3dClient


class ResultParserTests(unittest.TestCase):
    def test_parses_unit3d_rows_without_metric_css_classes(self) -> None:
        html = """
        <table><tbody><tr>
          <td class="poster"></td>
          <td>1080p BDRip</td>
          <td><a class="view-torrent" href="/torrents/39159">Example S01</a></td>
          <td></td><td></td><td></td><td>79%</td>
          <td>8.71 GiB</td><td>25</td><td>2</td><td>237</td><td>3 años</td>
          <td><a href="/torrents/download/39159">Download</a></td>
        </tr></tbody></table>
        """
        client = Unit3dClient.__new__(Unit3dClient)
        client.name = "test"
        client.base_url = "https://tracker.example"

        results = client._parse_results(html)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].size_str, "8.71 GiB")
        self.assertEqual(results[0].seeders, 25)
        self.assertEqual(results[0].leechers, 2)


ENGLISH_LOGIN = (
    '<html><body><h1>Login</h1><form method="POST" action="/login">'
    '<input name="username"><input name="password" type="password">'
    "<button>Login</button></form></body></html>"
)
SPANISH_LOGIN = ENGLISH_LOGIN.replace("Login", "Iniciar sesión")
TORRENT_LIST = (
    "<html><body><table><tbody><tr><td>"
    "<a class='view-torrent' href='/torrents/1'>Example</a></td></tr></tbody></table>"
    "</body></html>"
)


class LoggedOutMarkerTests(unittest.TestCase):
    """The check read one Spanish string, so any other UNIT3D read as logged in."""

    def test_the_default_marker_is_the_login_form_s_password_field(self) -> None:
        self.assertEqual(DEFAULT_LOGGED_OUT_MARKER, 'name="password"')

    def test_a_login_page_in_any_language_is_logged_out(self) -> None:
        client = Unit3dClient("https://tracker.example", "/nonexistent")
        for page in (ENGLISH_LOGIN, SPANISH_LOGIN):
            with self.subTest(page=page[:40]):
                self.assertFalse(client._is_logged_in(page))

    def test_the_torrent_list_is_logged_in(self) -> None:
        client = Unit3dClient("https://tracker.example", "/nonexistent")
        self.assertTrue(client._is_logged_in(TORRENT_LIST))

    def test_a_configured_marker_replaces_the_default(self) -> None:
        client = Unit3dClient(
            "https://tracker.example", "/nonexistent", logged_out_marker="Sign in to continue",
        )
        self.assertFalse(client._is_logged_in("<p>Sign in to continue</p>"))
        self.assertTrue(client._is_logged_in(ENGLISH_LOGIN))

    def test_an_empty_marker_falls_back_to_the_default(self) -> None:
        client = Unit3dClient("https://tracker.example", "/nonexistent", logged_out_marker="")
        self.assertFalse(client._is_logged_in(ENGLISH_LOGIN))


class MarkerWiringTests(unittest.TestCase):
    """The setting has to reach the client the service actually builds."""

    def test_the_env_var_reaches_the_tracker_config(self) -> None:
        from vettarr import config
        from vettarr.config.runtime import from_environment

        env = {
            "TRACKER_1_URL": "https://tracker.example",
            "TRACKER_1_LOGGED_OUT_MARKER": "Please log in",
        }
        with mock.patch.dict(os.environ, env):
            os.environ.pop("TRACKER_2_URL", None)
            [tracker] = config.Settings(from_environment(dict(os.environ))).trackers
        self.assertEqual(tracker.logged_out_marker, "Please log in")

    def test_no_env_var_means_the_client_default(self) -> None:
        from vettarr import config
        from vettarr.config.runtime import from_environment

        with mock.patch.dict(os.environ, {"TRACKER_1_URL": "https://tracker.example"}):
            os.environ.pop("TRACKER_1_LOGGED_OUT_MARKER", None)
            os.environ.pop("TRACKER_2_URL", None)
            [tracker] = config.Settings(from_environment(dict(os.environ))).trackers
        self.assertEqual(tracker.logged_out_marker, "")

    def test_init_trackers_hands_the_marker_to_the_client(self) -> None:
        from vettarr.config.schema import Config
        from vettarr.searcher import search_service

        def reset():
            search_service._trackers.clear()
            search_service._trackers_initialized = False

        reset()
        self.addCleanup(reset)
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n")
            cfg = Config.model_validate({"providers": [{
                "key": "tracker-a", "base_url": "https://tracker.example",
                "auth": {"from": "cookie_file", "cookie_file": str(cookie_path)},
                "logged_out_marker": "Please log in",
            }]})
            with mock.patch.object(search_service, "settings", SimpleNamespace(config=cfg)):
                search_service.init_trackers()
        [client] = search_service.get_trackers()
        self.assertEqual(client.logged_out_marker, "Please log in")


if __name__ == "__main__":
    unittest.main()
