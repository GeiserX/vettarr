"""``dry_run``: nothing outside this database is changed, whoever clicks what.

Three kinds of evidence:

- the audit: every call in the source that writes to the outside world (an
  HTTP call that is not a GET, a Telegram call that sends, edits, deletes or
  reacts) is found by reading the code, and each must sit behind its
  client's one choke point. A fake client with an unguarded POST makes it
  fail, so the audit can go red;
- each write path, under ``dry_run``, against a fake server that records
  every call: a ``would_have`` event, no call that is not a GET, and the
  reads still made;
- the item still moves (approved, delivered by would-have, completed), the
  UI says "dry run", and ``DRY_RUN=1`` wins over a file that says false.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import os
import pathlib
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
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
import requests
from sqlalchemy.dialects import postgresql

import delivery_fakes as fakes
import test_seerr_source as seerr_case
from delivery_fakes import API_KEY
from test_actions import Store, make_item
from test_delivery import DeliveryCase

from vettarr import (
    actions,
    delivery,
    dryrun,
    events,
    ingest,
    notify,
    review,
    scheduler,
    search_jobs,
    telegram_writes,
)
from vettarr.checker import gather
from vettarr.checker.policy import check_rule
from vettarr.config import cli, runtime, settings
from vettarr.config.components import components
from vettarr.config.loader import load_text
from vettarr.config.schema import Config, Rule
from vettarr.models import Download, ItemStatus, MediaType, TorrentSearch
from vettarr.searcher import search_service
from vettarr.searcher.prowlarr_client import ProwlarrClient, SearchResult, TorrentResult
from vettarr.searcher.qbittorrent_client import QBittorrentClient
from vettarr.searcher.unit3d_client import Unit3dClient
from vettarr.web import cards, ui
from vettarr.web.routes import setup as setup_routes
from vettarr.web.routes import stats

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "vettarr"

# ---------------------------------------------------------------------------
# The audit: every outside write, found in the source
# ---------------------------------------------------------------------------

#: HTTP verbs that change something, as httpx and requests name their methods.
_HTTP_WRITES = {"post", "put", "patch", "delete"}
#: Telegram calls that change something: python-telegram-bot's and Telethon's.
_TELEGRAM_WRITES = {
    "send_message", "send_photo", "send_document", "send_media_group", "send_file",
    "edit_message_text", "edit_message_caption", "edit_message_reply_markup", "edit_message",
    "delete_message", "delete_messages", "set_message_reaction", "send_reaction",
    "reply_text", "reply_photo", "reply_document", "reply_html", "reply_markdown",
    "answer", "copy_message", "forward_message", "forward_messages", "pin_chat_message",
    "unpin_chat_message", "leave_chat", "ban_chat_member",
    "send_video", "send_animation", "send_chat_action", "edit_message_media", "set_my_commands",
    # Telethon's high-level writes on a client, an event or a message.
    "forward_to", "mark_read", "send_read_acknowledge", "pin_message",
}
#: Telethon's writes whose names are common words: a write when it is given
#: something to send (``event.reply("…")``), not ``result.reply()``.
_TELEGRAM_WRITES_WITH_ARGUMENTS = {"reply", "respond", "edit"}


@dataclass(frozen=True)
class Write:
    """One call that writes to the outside world."""

    file: str
    #: ``Class.method`` or ``function``: where the call is.
    where: str
    line: int
    call: str
    #: The lines of a ``dryrun`` call in the same function, before this one.
    guarded_before: bool


def _is_method_literal(node: ast.AST) -> str | None:
    return node.value.upper() if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _writes_by_http(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in _HTTP_WRITES:
            return True
        if func.attr == "request" and call.args:
            # .request("GET", ...) reads; any other verb, or one held in a variable, may write.
            return _is_method_literal(call.args[0]) != "GET"
    # urllib.request.Request(..., method="POST")
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name == "Request":
        for kw in call.keywords:
            if kw.arg == "method" and _is_method_literal(kw.value) not in (None, "GET", "HEAD"):
                return True
    return False


def _writes_by_telegram(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _TELEGRAM_WRITES:
        return True
    if isinstance(func, ast.Attribute) and func.attr in _TELEGRAM_WRITES_WITH_ARGUMENTS and (call.args or call.keywords):
        return True
    # Telethon's raw form: client(SomethingRequest(...)).
    return bool(
        call.args and isinstance(call.args[0], ast.Call)
        and (getattr(call.args[0].func, "id", "") or getattr(call.args[0].func, "attr", "")).endswith("Request")
        and not (getattr(call.args[0].func, "id", "") or getattr(call.args[0].func, "attr", "")).startswith("Get")
        and (getattr(call.func, "id", "") or getattr(call.func, "attr", "")) in ("client", "self", "_client")
    )


def _calls_dryrun(node: ast.AST) -> list[int]:
    """Lines where ``node`` calls ``dryrun.<something>``."""
    return [
        n.lineno for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "dryrun"
    ]


def find_writes(source: str, file: str) -> list[Write]:
    """Every call in ``source`` that writes to the outside world, and whether a dryrun check comes first."""
    tree = ast.parse(source)
    found: list[Write] = []
    decorators = {id(d) for n in ast.walk(tree) for d in getattr(n, "decorator_list", ())}

    def visit(node: ast.AST, owner: list[str], function: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, [*owner, child.name], None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, [*owner, child.name], child if function is None else function)
            else:
                if (
                    isinstance(child, ast.Call) and id(child) not in decorators
                    and (_writes_by_http(child) or _writes_by_telegram(child))
                ):
                    lines = _calls_dryrun(function) if function is not None else []
                    found.append(Write(
                        file, ".".join(owner) or "<module>", child.lineno,
                        ast.unparse(child.func), any(line <= child.lineno for line in lines),
                    ))
                visit(child, owner, function)

    visit(tree, [], None)
    return found


def all_writes() -> list[Write]:
    out: list[Write] = []
    for path in sorted(SRC.rglob("*.py")):
        out += find_writes(path.read_text(), str(path.relative_to(SRC)))
    return out


#: The function that holds the write checks ``dryrun`` itself, before the write.
SELF = "self"


@dataclass(frozen=True)
class Transport:
    """The writes travel through another object, whose one method is the choke point.

    ``via`` names that object as the calls reach it (``self._api``,
    ``self.session``): a write in scope made on anything else (a fresh
    ``httpx.AsyncClient()``, ``requests.post``) does not go through the
    choke point, and is red.
    """

    file: str
    function: str
    via: tuple[str, ...]

    def carries(self, call: str) -> bool:
        return any(call == root or call.startswith(root + ".") for root in self.via)


@dataclass(frozen=True)
class Read:
    """Shaped like a write, and is not one."""

    why: str


# The bot's own objects: its Bot, and the Update, Message and CallbackQuery
# python-telegram-bot hands a handler, which send through that Bot.
_BOT = Transport(
    "telegram_writes.py", "GuardedBotRequest.do_request",
    via=("_bot_app.bot", "self.bot", "query", "update.message", "message", "Application.builder()"),
)
_USERBOT = Transport("telegram_writes.py", "guard_userbot._call", via=("client",))
_TRACKER = Transport("searcher/unit3d_client.py", "_ProviderSession.send", via=("self.session",))
_ARR_API = Transport("delivery/http.py", "Api.request", via=("self._api",))
# _to_seerr's client is a SeerrClient, whose request goes through Api.request.
_SEERR_CLIENT = Transport("delivery/http.py", "Api.request", via=("client",))
_CONFIG_DOCUMENT = Read("configfile.put edits the YAML document in memory; the writer owns the file")

#: Where each write is, and what keeps it from leaving under dry_run. Keyed by
#: file and the class or function that holds the call; a key ending in ``*``
#: covers everything under that name.
GUARDS: dict[str, object] = {
    # Radarr, Sonarr and Seerr: every call goes through Api.request.
    "delivery/http.py::Api.request": SELF,
    "delivery/radarr.py::RadarrClient.*": _ARR_API,
    "delivery/sonarr.py::SonarrClient.*": _ARR_API,
    "delivery/seerr.py::SeerrClient.*": _ARR_API,
    # SeerrClient.request: a Seerr request, made through Api.request.
    "delivery/__init__.py::_to_seerr": _SEERR_CLIENT,
    # The download client: every call but the login goes through _request.
    "searcher/qbittorrent_client.py::QBittorrentClient._request": SELF,
    "searcher/qbittorrent_client.py::QBittorrentClient._login": Read("a login changes nothing on the client"),
    "notify.py::send": SELF,
    # The tracker: thank, login and the Livewire thank go out through the session.
    "searcher/unit3d_client.py::Unit3dClient.*": _TRACKER,
    # The bot's calls all go through its request object.
    "bot/handlers.py::*": _BOT,
    # A post's on_download and on_reject: the userbot or the bot.
    "ingest.py::PostActions._as_userbot": _USERBOT,
    "ingest.py::PostActions._as_bot": _BOT,
    "checker/imdb_client.py::_post_graphql": Read("a GraphQL query: a read sent as a POST"),
    "web/connections.py::_qbittorrent": Read("Test connection is the download client's login"),
    "web/routes/policy.py::build": _CONFIG_DOCUMENT,
    "web/routes/setup.py::apply_block": _CONFIG_DOCUMENT,
    "web/routes/setup.py::_delete": _CONFIG_DOCUMENT,
}


def _guard_for(write: Write, guards: dict[str, object]) -> object | None:
    key = f"{write.file}::{write.where}"
    if key in guards:
        return guards[key]
    for pattern, guard in guards.items():
        file, _, where = pattern.partition("::")
        if file == write.file and where.endswith("*") and (
            where == "*" or key.split("::", 1)[1].startswith(where[:-1])
        ):
            return guard
    return None


def _function_calls_dryrun(file: str, function: str) -> bool:
    """Whether ``Class.method`` (or ``outer.inner``) in ``file`` calls ``dryrun``."""
    tree = ast.parse((SRC / file).read_text())
    node: ast.AST = tree
    for part in function.split("."):
        node = next(
            (n for n in ast.walk(node)
             if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part),
            None,
        )
        if node is None:
            return False
    return bool(_calls_dryrun(node))


def unguarded(writes: list[Write], guards: dict[str, object]) -> list[str]:
    """The writes no choke point stands in front of, as ``file:line where call``."""
    out = []
    for write in writes:
        guard = _guard_for(write, guards)
        if guard == SELF:
            ok = write.guarded_before
        elif isinstance(guard, Transport):
            ok = guard.carries(write.call) and _function_calls_dryrun(guard.file, guard.function)
        else:
            ok = isinstance(guard, Read)
        if not ok:
            out.append(f"{write.file}:{write.line} {write.where} {write.call}")
    return out


class ChokePointAuditTests(unittest.TestCase):
    def test_every_outside_write_is_behind_its_clients_choke_point(self) -> None:
        writes = all_writes()
        self.assertGreater(len(writes), 15, "the audit found almost nothing: is it still reading the source?")
        self.assertEqual(unguarded(writes, GUARDS), [])

    def test_the_audit_finds_every_client_it_knows(self) -> None:
        """A guard whose writes are gone is stale: the table must follow the code both ways."""
        writes = all_writes()
        stale = [
            key for key, guard in GUARDS.items()
            if not any(_guard_for(w, {key: guard}) is guard for w in writes)
        ]
        self.assertEqual(stale, [])

    def test_a_client_with_an_unguarded_post_turns_it_red(self) -> None:
        """The positive control: a new client that POSTs with no dryrun check is caught."""
        fake = textwrap.dedent('''
            class FakeClient:
                async def add(self, url):
                    return await self._client.post(url, json={"a": 1})
        ''')
        writes = find_writes(fake, "fake.py")
        self.assertEqual([w.call for w in writes], ["self._client.post"])
        self.assertEqual(unguarded(writes, GUARDS), ["fake.py:4 FakeClient.add self._client.post"])
        # Listed as its own choke point, it is still red until it checks dryrun first.
        self.assertTrue(unguarded(writes, {"fake.py::FakeClient.add": SELF}))
        guarded = fake.replace("        return await", "        if dryrun.active():\n            return None\n        return await")
        self.assertEqual(unguarded(find_writes(guarded, "fake.py"), {"fake.py::FakeClient.add": SELF}), [])

    def test_a_raw_write_inside_a_client_with_a_transport_turns_it_red(self) -> None:
        """A class or file whose writes go through a transport is not a free pass for a raw client."""
        planted = {
            "delivery/radarr.py": textwrap.dedent('''
                class RadarrClient:
                    async def add(self, movie):
                        return await httpx.AsyncClient().post("http://radarr/api/v3/movie", json=movie)
            '''),
            "bot/handlers.py": textwrap.dedent('''
                async def _respond(update, text):
                    await httpx.AsyncClient().post("http://example.invalid/hook", json={"text": text})
            '''),
            "searcher/unit3d_client.py": textwrap.dedent('''
                class Unit3dClient:
                    def thank(self, url):
                        return requests.post(url, data={"a": 1})
            '''),
        }
        for file, source in planted.items():
            with self.subTest(file=file):
                writes = find_writes(source, file)
                self.assertEqual(len(writes), 1)
                self.assertEqual(len(unguarded(writes, GUARDS)), 1)
        # The same writes made on the transport's own object stay green.
        through = textwrap.dedent('''
            class RadarrClient:
                async def add(self, movie):
                    return await self._api.request("POST", "/api/v3/movie", json=movie)
        ''')
        self.assertEqual(unguarded(find_writes(through, "delivery/radarr.py"), GUARDS), [])

    def test_a_request_with_a_verb_in_a_variable_counts_as_a_write(self) -> None:
        source = "async def go(c, method):\n    await c.request(method, '/x')\n    await c.request('GET', '/y')\n"
        self.assertEqual([w.line for w in find_writes(source, "x.py")], [2])

    def test_telethon_s_high_level_writes_are_found(self) -> None:
        source = textwrap.dedent('''
            async def on_post(event, client, result):
                await event.reply("seen")
                await event.respond("seen")
                await event.message.edit("seen")
                await client.send_read_acknowledge(event.chat_id)
                await event.message.mark_read()
                text = result.reply()
        ''')
        self.assertEqual([w.line for w in find_writes(source, "x.py")], [3, 4, 5, 6, 7])

    def test_telegram_writes_are_found(self) -> None:
        source = textwrap.dedent('''
            async def handler(update, context, client):
                await update.message.reply_text("hi")
                await context.bot.send_message(chat_id=1, text="x")
                await client(SendReactionRequest(peer=1))
                await client(GetHistoryRequest(peer=1))
        ''')
        self.assertEqual([w.line for w in find_writes(source, "x.py")], [3, 4, 5])


# ---------------------------------------------------------------------------
# The switch: dry_run in the file, DRY_RUN in the environment
# ---------------------------------------------------------------------------

MAGNET_HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{MAGNET_HASH}&dn=Some.Film.1999.1080p&tr=https://tracker.example/announce/PASSKEY"


def run(coro):
    return asyncio.run(coro)


def drain() -> list[tuple[str, int | None, dict]]:
    out = []
    while events.pending():
        _at, kind, item_id, payload = events._pending.get_nowait()
        out.append((kind, item_id, payload))
    return out


def would_haves(drained) -> list[tuple[int | None, dict]]:
    return [(item_id, p) for kind, item_id, p in drained if kind == events.WOULD_HAVE and p.get("dry_run")]


class EnvCase(unittest.TestCase):
    """DRY_RUN taken out of the environment, so each test says what it wants."""

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(dryrun.ENV, None)
        drain()
        self.addCleanup(drain)

    def dry(self) -> None:
        os.environ[dryrun.ENV] = "1"


class SwitchTests(EnvCase):
    def test_it_is_off_unless_the_file_says_so(self) -> None:
        self.assertFalse(Config().dry_run)
        self.assertFalse(load_text("{}\n", "t", {}).dry_run)
        self.assertTrue(load_text("dry_run: true\n", "t", {}).dry_run)
        self.assertTrue(load_text("dry_run: on\n", "t", {}).dry_run)

    def test_dry_run_in_the_environment_wins_over_a_file_that_says_false(self) -> None:
        self.assertFalse(load_text("dry_run: false\n", "t", {}).dry_run)
        for word in ("1", "true", "yes", "on", "TRUE"):
            with self.subTest(word=word):
                self.assertTrue(load_text("dry_run: false\n", "t", {dryrun.ENV: word}).dry_run)

    def test_the_environment_cannot_turn_it_off(self) -> None:
        self.assertTrue(load_text("dry_run: true\n", "t", {dryrun.ENV: "0"}).dry_run)

    def test_an_environment_boot_takes_it_too(self) -> None:
        env = {name: os.environ[name] for name in runtime.REQUIRED_ENV}
        self.assertFalse(runtime.from_environment(env).dry_run)
        self.assertTrue(runtime.from_environment({**env, dryrun.ENV: "1"}).dry_run)

    def test_active_reads_the_running_config_and_the_environment(self) -> None:
        with mock.patch.object(settings, "_config", Config(dry_run=False)):
            self.assertFalse(dryrun.active())
            self.dry()
            self.assertTrue(dryrun.active())
        os.environ.pop(dryrun.ENV)
        with mock.patch.object(settings, "_config", Config(dry_run=True)):
            self.assertTrue(dryrun.active())

    def test_config_check_lists_it_first_among_what_would_start(self) -> None:
        cfg = load_text("dry_run: true\nweb: {}\n", "t", {})
        self.assertEqual(components(cfg)[0], ("dry run", "nothing outside this database is changed"))
        self.assertNotIn("dry run", [name for name, _ in components(load_text("web: {}\n", "t", {}))])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.yaml"
            path.write_text("dry_run: true\nweb: {}\n")
            with mock.patch("sys.stdout", new=io.StringIO()) as out:
                self.assertEqual(cli.main(["check", str(path)]), 0)
        lines = out.getvalue().splitlines()
        started = lines[lines.index("Would start:") + 1]
        self.assertEqual(started.split(), ["dry", "run", "nothing", "outside", "this", "database", "is", "changed"])

    def test_the_example_and_the_contract_carry_it(self) -> None:
        root = SRC.parents[1]
        example = (root / "config.example.yaml").read_text()
        self.assertIn("\ndry_run: false\n", example)
        self.assertFalse(load_text(example, "example", {}).dry_run)
        contract = (root / "docs" / "CONFIG.md").read_text()
        self.assertIn("### `dry_run`", contract)
        self.assertIn("| `DRY_RUN` |", contract)


# ---------------------------------------------------------------------------
# The download client
# ---------------------------------------------------------------------------


class FakeQbit:
    """qBittorrent and an indexer on one recording transport."""

    def __init__(self, listed: list[dict] | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.listed = listed or []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            wanted = request.url.params.get("hashes")
            return httpx.Response(200, json=[t for t in self.listed if not wanted or t["hash"] == wanted])
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, json={"success_count": 1, "added_torrent_ids": [MAGNET_HASH]})
        if request.url.path == "/api/v2/torrents/delete":
            return httpx.Response(200, text="Ok.")
        if request.url.host == "indexer.invalid":
            return httpx.Response(200, content=b"d4:infod4:name4:filee6:lengthi1eee")
        return httpx.Response(404)

    def client(self) -> QBittorrentClient:
        client = QBittorrentClient()
        client.__dict__.update(_base="http://qbittorrent:8080", _user="admin", _pass="pw")
        transport = httpx.MockTransport(self.handle)
        client._client = httpx.AsyncClient(transport=transport)
        client._indexer_client = httpx.AsyncClient(transport=transport)
        return client

    def writes(self) -> list[str]:
        """Every call that is not a GET, the login aside: the login changes nothing."""
        return [
            f"{c.method} {c.url.path}" for c in self.calls
            if c.method != "GET" and c.url.path != "/api/v2/auth/login"
        ]

    def reads(self) -> list[str]:
        return [f"{c.method} {c.url.path}" for c in self.calls if c.method == "GET"]

    def indexer(self) -> list[str]:
        return [str(c.url) for c in self.calls if c.url.host == "indexer.invalid"]


class DownloadClientTests(EnvCase):
    def setUp(self) -> None:
        super().setUp()
        cfg = Config.model_validate({"integrations": {"qbittorrent": {
            "url": "http://qbittorrent:8080", "categories": {"movie": "films"},
        }}})
        patcher = mock.patch.object(settings, "_config", cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.qbit = FakeQbit()
        self.client = self.qbit.client()

    def test_without_dry_run_the_add_is_sent(self) -> None:
        """The control: the same fake records the add when dry_run is off."""
        outcome = run(self.client.add_torrent(MediaType.MOVIE, magnet_url=MAGNET))
        self.assertEqual(outcome.hash, MAGNET_HASH)
        self.assertEqual(self.qbit.writes(), ["POST /api/v2/torrents/add"])
        self.assertEqual(would_haves(drain()), [])

    def test_a_magnet_is_not_added_and_says_what_would_have_been(self) -> None:
        self.dry()
        with dryrun.about(7):
            outcome = run(self.client.add_torrent(MediaType.MOVIE, magnet_url=MAGNET))
        self.assertEqual(self.qbit.writes(), [])
        # The read that says whether the client held it already is still made.
        self.assertIn("GET /api/v2/torrents/info", self.qbit.reads())
        self.assertEqual((outcome.hash, outcome.created), (f"dry-run:{MAGNET_HASH}", True))
        [(item_id, payload)] = would_haves(drain())
        self.assertEqual(item_id, 7)
        self.assertEqual(
            {k: payload[k] for k in ("target", "action", "method", "path", "hash", "category")},
            {"target": "download client", "action": "add magnet", "method": "POST",
             "path": "/api/v2/torrents/add", "hash": MAGNET_HASH, "category": "films"},
        )
        # The magnet's trackers carry passkeys: it is never written down.
        self.assertNotIn("PASSKEY", json.dumps(payload))

    def test_a_torrent_file_is_not_added(self) -> None:
        self.dry()
        data = b"d4:infod4:name4:filee6:lengthi1eee"
        outcome = run(self.client.add_torrent_file(data, MediaType.MOVIE))
        self.assertEqual(self.qbit.writes(), [])
        self.assertTrue(outcome.hash.startswith("dry-run:"))
        self.assertEqual([p["action"] for _, p in would_haves(drain())], ["add .torrent"])

    def test_a_link_is_neither_fetched_from_the_indexer_nor_added(self) -> None:
        self.dry()
        url = "http://indexer.invalid/dl/1?apikey=SECRET&file=x"
        outcome = run(self.client.add_torrent(MediaType.MOVIE, download_url=url, expected_title="Some Film"))
        self.assertEqual(self.qbit.indexer(), [], "the tracker behind the link counts a fetch as a download")
        self.assertEqual(self.qbit.writes(), [])
        self.assertEqual(outcome.hash, "dry-run")
        payloads = [p for _, p in would_haves(drain())]
        self.assertEqual([p["action"] for p in payloads], ["fetch .torrent", "add URL"])
        self.assertEqual(payloads[0]["reason"], dryrun.NOT_FETCHED)
        self.assertNotIn("SECRET", json.dumps(payloads))

    def test_a_delete_is_not_sent(self) -> None:
        self.dry()
        self.qbit.listed = [{"hash": "aa" * 20, "content_path": "/films/x", "save_path": "/films"}]
        self.assertTrue(run(self.client.remove_torrent("aa" * 20)))
        self.assertEqual(self.qbit.writes(), [])
        [(_, payload)] = would_haves(drain())
        self.assertEqual((payload["action"], payload["hash"]), ("delete", "aa" * 20))

    def test_a_torrent_the_dry_run_made_reads_back_as_complete_without_asking(self) -> None:
        made = f"dry-run:{MAGNET_HASH}"
        self.assertEqual(run(self.client.get_torrent_progress(made)), 1.0)
        self.assertTrue(run(self.client.confirm_torrent_healthy(made, attempts=1)))
        self.assertEqual([c for c in self.qbit.calls if c.url.path != "/api/v2/auth/login"], [])


# ---------------------------------------------------------------------------
# A tracker
# ---------------------------------------------------------------------------

LOGGED_IN = b'<html><head><meta name="csrf-token" content="tok"></head><body>torrents</body></html>'


class Wire(requests.adapters.BaseAdapter):
    """Answers every request with a signed-in page and keeps it."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(LOGGED_IN)
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass


class TrackerTests(EnvCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cookies = pathlib.Path(tmp.name) / "tracker-a.txt"
        self.cookies.write_text("# Netscape HTTP Cookie File\ntracker-a.example\tFALSE\t/\tTRUE\t0\tsid\tabc\n")
        self.before = self.cookies.read_text()
        self.client = Unit3dClient(
            "https://tracker-a.example", str(self.cookies), name="tracker-a",
            username="me", password="pw", software="unit3d",
        )
        self.wire = Wire()
        self.client.session.mount("https://", self.wire)

    def methods(self) -> list[str]:
        return [f"{r.method} {r.path_url}" for r in self.wire.requests]

    def test_without_dry_run_a_thank_is_sent(self) -> None:
        self.assertTrue(self.client.thank("42", "/torrents/{id}/thank", "POST"))
        self.assertIn("POST /torrents/42/thank", self.methods())

    def test_a_thank_is_not_sent(self) -> None:
        self.dry()
        self.assertTrue(self.client.thank("42", "/torrents/{id}/thank", "POST"))
        self.assertTrue(self.client.thank_torrent("42"))
        self.assertEqual(self.methods(), [])
        payloads = [p for _, p in would_haves(drain())]
        self.assertEqual([p["action"] for p in payloads], ["thank", "thank"])
        self.assertEqual(payloads[0]["path"], "https://tracker-a.example/torrents/42/thank")

    def test_a_keep_alive_is_not_sent(self) -> None:
        self.dry()
        self.assertTrue(self.client.keep_alive("/torrents"))
        self.assertEqual(self.methods(), [])
        self.assertEqual([p["action"] for _, p in would_haves(drain())], ["keep-alive"])

    def test_a_torrent_is_not_fetched_because_the_tracker_counts_it(self) -> None:
        self.dry()
        with dryrun.about(3):
            self.assertIsNone(self.client.download_torrent("42"))
            self.assertIsNone(self.client.fetch_torrent("https://tracker-a.example/download.php?id=42&passkey=SECRET"))
        self.assertEqual(self.methods(), [])
        payloads = would_haves(drain())
        self.assertEqual([(i, p["reason"]) for i, p in payloads], [(3, dryrun.NOT_FETCHED)] * 2)
        self.assertNotIn("SECRET", json.dumps(payloads))

    def test_a_login_is_not_posted_and_the_session_check_still_reads(self) -> None:
        self.dry()
        self.assertFalse(self.client.login())
        self.assertEqual(self.methods(), ["GET /login"])
        self.assertTrue(self.client.check_session())
        self.assertEqual(self.methods(), ["GET /login", "GET /torrents"])
        [(_, payload)] = would_haves(drain())
        self.assertEqual(
            (payload["target"], payload["method"], payload["action"]), ("tracker-a", "POST", "POST /login"),
        )
        self.assertNotIn('"pw"', json.dumps(payload))

    def test_the_cookie_file_is_left_as_it_was(self) -> None:
        """It may be the live instance's too."""
        self.dry()
        self.assertTrue(self.client.check_session())
        self.assertEqual(self.cookies.read_text(), self.before)
        os.environ.pop(dryrun.ENV)
        self.assertTrue(self.client.check_session())
        self.assertNotEqual(self.cookies.read_text(), self.before, "the control: without dry_run it is saved")


#: A provider whose link pattern reads a post's details link.
TRACKER_LINK = "https://tracker-a.example/details.php?id=1"
TRACKER_A = [{
    "key": "tracker-a", "base_url": "https://tracker-a.example",
    "auth": {"from": "cookie_file", "cookie_file": "/nonexistent/cookies.txt"},
    "links": [{
        "pattern": r"details\.php\?id=(?P<id>\d+)",
        "download_url": "{base}/download.php?id={id}",
        "examples": [{"url": TRACKER_LINK, "id": "1"}],
    }],
}]


class TorrentSignalTests(EnvCase):
    """A linked .torrent read for the file signals is a download on the tracker too."""

    def item(self):
        return SimpleNamespace(id=5, signals=None, external_links=[TRACKER_LINK])

    def signals(self, *, providers: list | None = None):
        # tracker-a's pattern reads the item's link; with no provider nothing would be fetched.
        cfg = Config.model_validate({"providers": TRACKER_A if providers is None else providers})
        with mock.patch.object(ingest, "direct_add_of", return_value=None), \
                mock.patch.object(settings, "_config", cfg):
            return run(gather._torrent_signals(self.item(), cfg))

    def test_the_file_signals_say_the_torrent_was_not_fetched(self) -> None:
        self.dry()
        found = self.signals()
        self.assertEqual(set(found), {"file_count", "file_extensions"})
        self.assertEqual(
            [(p["target"], p["reason"]) for _, p in would_haves(drain())], [("tracker-a", dryrun.NOT_FETCHED)],
        )
        rule = Rule.model_validate({"id": "many-files", "check": "file_count", "at_most": 50})
        self.assertEqual(
            check_rule(rule, found["file_count"], {}, 0), (None, "file_count: dry run: torrent not fetched"),
        )

    def test_without_dry_run_an_unreadable_torrent_is_missing_data(self) -> None:
        with mock.patch.object(search_service, "fetch_linked_torrent", mock.AsyncMock(return_value=None)):
            self.assertEqual(self.signals()["file_count"], {"missing": "the linked torrent could not be read"})

    def test_a_link_no_provider_reads_is_missing_data_in_a_dry_run_too(self) -> None:
        """Nothing would have been fetched live either: the dry run is not the reason."""
        self.dry()
        self.assertEqual(
            self.signals(providers=[])["file_count"], {"missing": "the linked torrent could not be read"},
        )
        self.assertEqual(would_haves(drain()), [])

    def test_an_approved_post_s_linked_torrent_is_not_swapped_for_a_search(self) -> None:
        """Live adds the linked file; a search would add a different release."""
        self.dry()
        cfg = Config.model_validate({"providers": TRACKER_A})
        item = search_service.ItemRef(
            id=7, title="Some Film", year=2024, quality=None, season=None,
            media_type=MediaType.MOVIE, telegram_message_id=1, external_links=[TRACKER_LINK],
        )
        search_for = mock.AsyncMock(return_value=SearchResult(results=[TorrentResult(
            guid="g", title="Other.Release", size=1, seeders=9, indexer="x", magnet_url=MAGNET,
        )]))
        record = mock.AsyncMock()

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        with mock.patch.object(settings, "_config", cfg), \
                mock.patch.object(search_service, "init_trackers", lambda: None), \
                mock.patch.object(search_service, "_with_session", mock.AsyncMock(return_value=None)), \
                mock.patch.object(search_service, "search_for", search_for), \
                mock.patch.object(search_service, "record_download", record), \
                mock.patch.object(search_service, "async_session", Session), \
                dryrun.about(item.id):
            self.assertEqual(run(search_service.search_and_download(item)), "error")
        search_for.assert_not_awaited()
        refused = record.await_args.args[2]
        self.assertFalse(refused)
        self.assertEqual(refused.reason, "Dry run: torrent not fetched: tracker-a counts a fetch as a download.")
        self.assertEqual(record.await_args.kwargs["download_url"], "https://tracker-a.example/download.php?id=1")
        self.assertEqual(
            [(i, p["target"], p["reason"]) for i, p in would_haves(drain())],
            [(item.id, "tracker-a", dryrun.NOT_FETCHED)],
        )

    def test_a_post_that_only_links_a_torrent_is_read_as_a_tracker_link(self) -> None:
        """Live names it from the linked file; a dry run does not fetch it, and the item says so."""
        self.dry()
        cfg = Config.model_validate({"providers": TRACKER_A})
        fetch = mock.AsyncMock()
        with mock.patch.object(settings, "_config", cfg), \
                mock.patch.object(search_service, "_with_session", fetch):
            kind, raw, resolved = run(ingest.read(TRACKER_LINK, None))
        self.assertEqual((kind, raw, resolved), ("tracker_link", TRACKER_LINK, None))
        fetch.assert_not_awaited()
        # Nothing is recorded before the item exists: ingest() names it.
        self.assertEqual(would_haves(drain()), [])

    def test_a_release_only_the_tracker_has_is_not_added(self) -> None:
        self.dry()
        provider = SimpleNamespace(key="tracker-a")
        tracker = SimpleNamespace(fetch_torrent=lambda url: None)
        qbit = SimpleNamespace(add_torrent_file=mock.AsyncMock(), add_torrent=mock.AsyncMock())
        release = TorrentResult(title="Some Film", download_url="https://tracker-a.example/dl/1", provider="tracker-a")
        with mock.patch.object(search_service, "_provider", return_value=provider), \
                mock.patch.object(search_service, "_with_session", mock.AsyncMock(return_value=tracker)), \
                mock.patch.object(search_service, "qbittorrent", qbit):
            refused = run(search_service.add_release(release, MediaType.MOVIE))
        self.assertFalse(refused)
        self.assertEqual(refused.reason, "Dry run: torrent not fetched: tracker-a counts a fetch as a download.")
        qbit.add_torrent_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# Radarr, Sonarr and Seerr, and the whole flow of an item
# ---------------------------------------------------------------------------


class DryDeliveryCase(DeliveryCase):
    """The delivery fixture with dry_run on in the running config."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(dryrun.ENV, None)
        self.cfg = self.cfg.model_copy(update={"dry_run": True})
        patcher = mock.patch.object(settings, "_config", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_nothing_sent(self) -> None:
        for server in (self.radarr, self.sonarr, self.seerr):
            self.assertEqual([f"{c.method} {c.path}" for c in server.writes()], [])


class ArrTests(DryDeliveryCase):
    def test_a_movie_is_not_added_and_the_item_completes_by_would_have(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        self.assert_nothing_sent()
        # The reads are real: what Radarr holds, the profile, the lookup.
        self.assertEqual(
            [c.path for c in self.radarr.sent("GET")],
            ["/api/v3/movie", "/api/v3/qualityprofile", "/api/v3/movie/lookup"],
        )
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(self.download().remote_id, "dry-run")
        drained = drain()
        [(item_id, payload)] = would_haves(drained)
        self.assertEqual(
            (item_id, payload["target"], payload["method"], payload["path"]),
            (item.id, "Radarr", "POST", "/api/v3/movie"),
        )
        self.assertNotIn(API_KEY, json.dumps(payload))
        self.assertIn(events.DELIVERED, [kind for kind, _, _ in drained])
        # Completed at the next check, with nothing asked of Radarr.
        before = len(self.radarr.calls)
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertEqual(len(self.radarr.calls), before)
        self.posts.after.assert_awaited_once_with("download", item.id)

    def test_monitoring_a_movie_radarr_holds_is_not_sent(self) -> None:
        movie = self.radarr.hold(has_file=False, monitored=False)
        item, ref = self.claimed()
        self.work(ref)
        self.assert_nothing_sent()
        self.assertFalse(self.radarr.movies[movie["id"]]["monitored"])
        self.assertEqual(self.download().remote_id, f"dry-run:{movie['id']}")
        self.assertEqual(
            [(p["method"], p["path"]) for _, p in would_haves(drain())],
            [("PUT", f"/api/v3/movie/{movie['id']}"), ("POST", "/api/v3/command")],
        )
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_reject_takes_back_nothing_but_says_what_it_would_have(self) -> None:
        movie = self.radarr.hold(has_file=False, monitored=False)
        item, ref = self.claimed()
        self.work(ref)
        drain()
        result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assert_nothing_sent()
        # The real id inside the marked one is read, and the unmonitor is a would-have.
        self.assertIn(f"/api/v3/movie/{movie['id']}", [c.path for c in self.radarr.sent("GET")])
        self.assertEqual([(i, p["method"]) for i, p in would_haves(drain())], [(item.id, "PUT")])
        self.assertIn("Dry run: Radarr was not told.", result.message)

    def test_a_reject_once_the_dry_run_is_off_takes_back_nothing_it_never_made(self) -> None:
        self.radarr.hold(has_file=False, monitored=False)
        item, ref = self.claimed()
        self.work(ref)
        drain()
        reads = len(self.radarr.calls)
        with mock.patch.object(settings, "_config", self.cfg.model_copy(update={"dry_run": False})):
            result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.ok)
        self.assert_nothing_sent()
        self.assertEqual(len(self.radarr.calls), reads)

    def test_a_season_sonarr_does_not_monitor_is_not_turned_on(self) -> None:
        self.sonarr.hold(monitored=True, monitored_seasons=(1,))
        item, ref = self.series("S02")
        self.work(ref)
        self.assert_nothing_sent()
        self.assertEqual([p["method"] for _, p in would_haves(drain())], ["PUT", "POST"])
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)


class SeerrDeliveryTests(DryDeliveryCase):
    DELIVER = {"movie": "seerr", "series": "seerr"}

    def test_a_request_is_not_made(self) -> None:
        item, ref = self.claimed()
        self.work(ref)
        self.assert_nothing_sent()
        self.assertEqual(self.seerr.requests, {})
        [(item_id, payload)] = would_haves(drain())
        self.assertEqual((item_id, payload["path"]), (item.id, "/api/v1/request"))
        self.poll()
        self.assertEqual(item.status, ItemStatus.COMPLETED)


class SeerrSourceTests(seerr_case.SourceCase):
    """A Seerr request the policy decides: neither approved nor declined in Seerr."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(dryrun.ENV, None)
        patcher = mock.patch.object(settings, "_config", self.cfg.model_copy(update={"dry_run": True}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_approval_is_not_sent_and_the_item_completes(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.work()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertIn(
            (item.id, f"/api/v1/request/{request_id}/approve"),
            [(i, p.get("path")) for i, p in would_haves(drain())],
        )
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_reviewer_s_reject_does_not_decline(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.ok)
        self.assertEqual(self.decisions(), [])
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_PENDING))

    def test_a_reject_is_a_would_have_once_and_the_poll_does_not_send_it_again(self) -> None:
        """Seerr was not told, so it keeps listing the request as pending."""
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        drain()
        result = run(actions.reject(item.id, self.posts))
        self.assertIn("would have been declined", result.message)
        self.assertNotIn(delivery._DECLINED, result.message)
        self.assertEqual(
            [(i, p["path"]) for i, p in would_haves(drain())],
            [(item.id, f"/api/v1/request/{request_id}/decline")],
        )
        for _ in range(3):
            self.assertEqual(self.poll().declined, 0)
        self.assertEqual(would_haves(drain()), [])
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_PENDING))


class DownloadClientFlowTests(EnvCase):
    """An item a person approves, delivered to the download client, end to end."""

    def setUp(self) -> None:
        super().setUp()
        from telegram import Bot
        from telegram.request import HTTPXRequest

        self.store = Store()
        self.cfg = Config.model_validate({
            "dry_run": True,
            "telegram": {"bot_token": "123456:fake", "reviewers": [1]},
            "sources": [{"key": "inbox", "kind": "bot", "chat": -100, "on_download": "react"}],
            "integrations": {
                "qbittorrent": {"url": "http://qbittorrent:8080"},
                "prowlarr": {"url": "http://prowlarr", "api_key": "k"},
            },
            "deliver": {"movie": "qbittorrent"},
        })
        self.qbit = FakeQbit()
        client = self.qbit.client()
        self.prowlarr = SimpleNamespace(
            build_query=mock.Mock(side_effect=ProwlarrClient.build_query),
            search=mock.AsyncMock(return_value=SearchResult(results=[TorrentResult(
                guid="g1", title="Some.Film.1999.1080p", size=1, seeders=5, indexer="x", magnet_url=MAGNET,
            )])),
        )
        self.bot_calls: list[str] = []

        async def telegram(_self, url, method, request_data=None, *args, **kwargs):
            self.bot_calls.append(url.rsplit("/", 1)[-1])
            return 200, b'{"ok": true, "result": true}'

        self.posts = ingest.PostActions(config=self.cfg)
        self.posts.bot = Bot("123456:fake", request=telegram_writes.GuardedBotRequest())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def recorded_hash(item_id):
            return next(d.qbittorrent_hash for d in self.store.all(Download) if d.media_item_id == item_id)

        for patcher in (
            *(mock.patch.object(module, "async_session", self.store.session)
              for module in (delivery, search_jobs, scheduler, review, actions, ingest, search_service)),
            mock.patch.object(settings, "_config", self.cfg),
            mock.patch.object(HTTPXRequest, "do_request", telegram),
            mock.patch.object(search_service, "qbittorrent", client),
            mock.patch.object(search_service, "prowlarr", self.prowlarr),
            mock.patch.object(search_jobs, "qbittorrent", client),
            mock.patch.object(scheduler, "qbt", client),
            mock.patch.object(search_jobs, "_recorded_torrent_hash", recorded_hash),
            mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp.name)),
            mock.patch.object(search_service, "_trackers_initialized", True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_approve_deliver_complete_with_nothing_sent(self) -> None:
        item = make_item(
            self.store, 1, media_type=MediaType.MOVIE, title="Some Film", year=1999, season=None,
            raw_text="Some Film 1999 1080p", source_key="inbox", telegram_chat_id=-100,
        )
        approved = run(actions.approve(item.id))
        self.assertEqual(approved.message, "Would have queued Some Film for download.")
        self.assertEqual(item.status, ItemStatus.APPROVED)
        [ref] = run(search_jobs._claim_approved_items(1))
        run(search_jobs._process_item(ref, self.posts))
        # Delivered by would-have: the search ran, nothing reached qBittorrent or Telegram.
        self.prowlarr.search.assert_awaited()
        self.assertEqual(self.qbit.writes(), [])
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        run(scheduler._check_downloads(self.posts))
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        drained = drain()
        self.assertEqual([(i, p["target"], p["action"]) for i, p in would_haves(drained)], [
            (item.id, "download client", "add magnet"),
            (item.id, "telegram bot", "setMessageReaction"),
        ])
        self.assertEqual(
            [kind for kind, i, _ in drained if i == item.id and kind in (events.DELIVERED, events.COMPLETED)],
            [events.DELIVERED, events.COMPLETED],
        )
        self.assertEqual(self.qbit.writes(), [])

    def test_a_picked_release_says_it_would_have_downloaded(self) -> None:
        item = make_item(
            self.store, 1, media_type=MediaType.MOVIE, title="Some Film", year=1999, season=None,
            raw_text="Some Film 1999 1080p", source_key="inbox", telegram_chat_id=-100,
        )
        self.store.put(TorrentSearch(
            id=50, media_item_id=item.id,
            prowlarr_results=[{"title": "Some.Film.1999.1080p", "magnet_url": MAGNET, "indexer": "x"}],
        ))
        result = run(actions.grab(item.id, 50, 0))
        self.assertEqual(result.message, "Would have downloaded Some.Film.1999.1080p.")
        self.assertEqual(self.qbit.writes(), [])
        self.assertEqual(
            [(i, p["action"]) for i, p in would_haves(drain())], [(item.id, "add magnet")],
        )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class BotTests(EnvCase):
    def setUp(self) -> None:
        super().setUp()
        from telegram import Bot
        from telegram.request import HTTPXRequest

        self.sent: list[str] = []

        async def telegram(_self, url, method, request_data=None, *args, **kwargs):
            name = url.rsplit("/", 1)[-1]
            self.sent.append(name)
            if name == "getMe":
                return 200, json.dumps({"ok": True, "result": {
                    "id": 123456, "is_bot": True, "first_name": "b", "username": "b_bot"}}).encode()
            return 200, json.dumps({"ok": True, "result": {
                "message_id": 9, "date": 0, "chat": {"id": 5, "type": "private"}}}).encode()

        patcher = mock.patch.object(HTTPXRequest, "do_request", telegram)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = Bot("123456:fake", request=telegram_writes.GuardedBotRequest())

    def test_sends_edits_deletes_and_reactions_are_not_sent_and_reads_are(self) -> None:
        self.dry()

        async def everything():
            await self.bot.initialize()
            message = await self.bot.send_message(5, "card text")
            await self.bot.edit_message_text("new", chat_id=5, message_id=9)
            await self.bot.delete_message(5, 9)
            await self.bot.set_message_reaction(5, 9, reaction="👍")
            await self.bot.answer_callback_query("q1")
            return message

        message = run(everything())
        self.assertEqual(self.sent, ["getMe"])
        self.assertEqual(message.chat.id, 5)
        payloads = [p for _, p in would_haves(drain())]
        self.assertEqual(
            [p["action"] for p in payloads],
            ["sendMessage", "editMessageText", "deleteMessage", "setMessageReaction", "answerCallbackQuery"],
        )
        text = json.dumps(payloads)
        self.assertNotIn("card text", text)
        self.assertNotIn("123456:fake", text)

    def test_without_dry_run_they_are_sent(self) -> None:
        async def send():
            await self.bot.initialize()
            await self.bot.send_message(5, "x")

        run(send())
        self.assertEqual(self.sent, ["getMe", "sendMessage"])
        self.assertEqual(would_haves(drain()), [])

    def test_the_bot_app_is_built_on_the_guarded_request(self) -> None:
        from vettarr.bot import handlers

        app = handlers.create_bot_app(None)
        self.addCleanup(ingest.set_review_pusher, None)
        self.assertIsInstance(app.bot.request, telegram_writes.GuardedBotRequest)


class UserbotTests(EnvCase):
    def setUp(self) -> None:
        super().setUp()
        self.sent: list = []

        async def call(sender, request, ordered=False, flood_sleep_threshold=None):
            self.sent.append(request)
            return "answered"

        self.client = telegram_writes.guard_userbot(SimpleNamespace(_call=call))

    def test_deletes_and_reactions_are_not_sent_and_reads_are(self) -> None:
        from telethon.tl.functions.messages import DeleteMessagesRequest, GetHistoryRequest, SendReactionRequest
        from telethon.tl.types import InputPeerEmpty, ReactionEmoji

        self.dry()
        history = GetHistoryRequest(InputPeerEmpty(), 0, None, 0, 10, 0, 0, 0)
        self.assertEqual(run(self.client._call(None, history)), "answered")
        react = SendReactionRequest(peer=InputPeerEmpty(), msg_id=4, reaction=[ReactionEmoji(emoticon="👍")])
        self.assertIsNone(run(self.client._call(None, react)))
        self.assertEqual(run(self.client._call(None, [DeleteMessagesRequest(id=[4, 5])])), [None])
        self.assertEqual(self.sent, [history])
        self.assertEqual(
            [(p["action"], p["message_ids"]) for _, p in would_haves(drain())],
            [("SendReactionRequest", [4]), ("DeleteMessagesRequest", [4, 5])],
        )

    def test_a_media_read_on_another_data_centre_still_exports_the_authorization(self) -> None:
        """Telethon exports this session's authorization to read a file on another DC: a read."""
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.functions.auth import ExportAuthorizationRequest
        from telethon.tl.functions.upload import ReuploadCdnFileRequest

        client = TelegramClient(StringSession(), 1, "x")
        exported = SimpleNamespace(id=7, bytes=b"k")
        telethon_call = mock.AsyncMock(return_value=exported)
        client._call = telethon_call
        telegram_writes.guard_userbot(client)
        self.dry()
        # Through TelegramClient.__call__, as _create_exported_sender asks it.
        self.assertIs(run(client(ExportAuthorizationRequest(dc_id=2))), exported)
        telethon_call.assert_awaited_once()
        self.assertFalse(telegram_writes.userbot_writes(ReuploadCdnFileRequest(file_token=b"t", request_token=b"r")))
        self.assertEqual(would_haves(drain()), [])

    def test_without_dry_run_they_are_sent(self) -> None:
        from telethon.tl.functions.messages import DeleteMessagesRequest

        run(self.client._call(None, DeleteMessagesRequest(id=[4])))
        self.assertEqual(len(self.sent), 1)

    def test_the_userbot_s_client_is_guarded(self) -> None:
        from telethon.tl.functions.messages import DeleteMessagesRequest

        from vettarr.ingester import telegram_reader

        telethon_call = mock.AsyncMock()
        made = SimpleNamespace(_call=telethon_call)
        with mock.patch.object(telegram_reader, "TelegramClient", return_value=made):
            ingester = telegram_reader.TelegramIngester([])
        self.assertIs(ingester.client, made)
        self.dry()
        run(ingester.client._call(None, DeleteMessagesRequest(id=[1])))
        telethon_call.assert_not_awaited()
        self.assertEqual([p["action"] for _, p in would_haves(drain())], ["DeleteMessagesRequest"])


# ---------------------------------------------------------------------------
# The notifier and the Setup page's cookie upload
# ---------------------------------------------------------------------------


class NotifyTests(EnvCase):
    def setUp(self) -> None:
        super().setUp()
        self.posted: list = []

        def handle(request):
            self.posted.append(request)
            return httpx.Response(200)

        cfg = Config.model_validate({"notify": {"url": "https://hooks.example/notify/TOKEN"}})
        for patcher in (
            mock.patch.object(runtime, "_config", cfg),
            mock.patch.object(settings, "_config", cfg),
            mock.patch.object(notify, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handle))),
            mock.patch.object(notify, "_paused_until", float("-inf")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_event_is_not_posted_and_the_would_have_names_no_url(self) -> None:
        self.dry()
        event = {"id": 4, "at": "x", "kind": "decision", "item_id": 2, "payload": {}}
        self.assertFalse(run(notify.send(event)))
        self.assertEqual(self.posted, [])
        [(item_id, payload)] = would_haves(drain())
        self.assertEqual((item_id, payload["target"], payload["event_kind"]), (2, "notify", "decision"))
        self.assertNotIn("TOKEN", json.dumps(payload))

    def test_a_dry_run_s_own_event_is_not_announced_again(self) -> None:
        self.dry()
        own = {"id": 5, "kind": "would_have", "item_id": 2, "payload": {"dry_run": True}}
        self.assertFalse(run(notify.send(own)))
        self.assertEqual(would_haves(drain()), [])

    def test_without_dry_run_it_is_posted(self) -> None:
        self.assertTrue(run(notify.send({"id": 4, "kind": "decision", "payload": {}})))
        self.assertEqual(len(self.posted), 1)


class CookieUploadTests(EnvCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "cookies" / "tracker-a.txt"
        patcher = mock.patch.object(
            setup_routes, "_tracker_key_map", return_value={"tracker-a": ("tracker-a", str(self.path))},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.export = json.dumps([{"name": "sid", "value": "SECRETCOOKIE", "domain": "tracker-a.example"}])

    def upload(self):
        with mock.patch.object(search_service, "get_trackers", return_value=[]):
            return run(setup_routes.update_cookies("tracker-a", cookies_json=self.export))

    def test_the_cookie_file_is_not_written(self) -> None:
        self.dry()
        answer = self.upload()
        self.assertFalse(self.path.exists())
        self.assertIn("Dry run: the cookie file was not written. It would have held 1 cookies.", answer.body.decode())
        [(_, payload)] = would_haves(drain())
        self.assertEqual(
            (payload["target"], payload["action"], payload["cookies"]), ("tracker-a", "write cookie file", 1),
        )
        self.assertNotIn("SECRETCOOKIE", json.dumps(payload))

    def test_without_dry_run_it_is_written(self) -> None:
        self.upload()
        self.assertIn("SECRETCOOKIE", self.path.read_text())


# ---------------------------------------------------------------------------
# What a person sees
# ---------------------------------------------------------------------------


class PageTests(EnvCase):
    def page(self) -> str:
        request = SimpleNamespace(state=SimpleNamespace(), scope={"path": "/"})
        return ui.templates.get_template("base.html").render(**ui._shell(request))

    def test_the_nav_and_every_toast_say_dry_run(self) -> None:
        with mock.patch.object(settings, "_config", Config(dry_run=True)):
            page = self.page()
        self.assertIn('<html lang="en" data-dry-run>', page)
        self.assertIn('class="badge badge-warn"', page)
        self.assertIn(">dry run</span>", page)
        css = (SRC / "web" / "static" / "src" / "app.css").read_text()
        self.assertIn('[data-dry-run] .toast::after {\n    content: "dry run";', css)

    def test_without_dry_run_neither_shows(self) -> None:
        with mock.patch.object(settings, "_config", Config(dry_run=False)):
            page = self.page()
        self.assertNotIn("data-dry-run", page)
        self.assertNotIn(">dry run</span>", page)

    def test_an_approval_says_what_it_would_have_done(self) -> None:
        cfg = Config.model_validate({
            "integrations": {"qbittorrent": {"url": "http://q"}},
            "deliver": {"movie": "qbittorrent"},
        })
        with mock.patch.object(settings, "_config", cfg):
            self.assertEqual(actions._approved_message("X", MediaType.MOVIE), "Queued X for download.")
        with mock.patch.object(settings, "_config", cfg.model_copy(update={"dry_run": True})):
            self.assertEqual(actions._approved_message("X", MediaType.MOVIE), "Would have queued X for download.")

    def test_a_rejection_says_what_it_would_have_done_to_the_post(self) -> None:
        outcome = SimpleNamespace(rejected=True, seerr_request=None, remote_note=None, remote_failed=False)

        def reject(done: str) -> str:
            with mock.patch.object(actions, "reject_item", mock.AsyncMock(return_value=outcome)), \
                    mock.patch.object(actions, "finish_rejection", mock.AsyncMock(return_value=done)):
                return run(actions.reject(1, None)).message

        self.assertEqual(reject("deleted"), "Rejected, and its post was deleted.")
        self.dry()
        self.assertEqual(reject("deleted"), "Rejected. Dry run: its post would have been deleted, and is still there.")
        self.assertEqual(reject("reacted"), "Rejected. Dry run: its post would have got a reaction, and has none.")
        self.assertEqual(reject("none"), "Rejected. Its post was left in place.")

    def test_a_dry_run_s_would_have_reads_as_one_line_and_is_no_shadow_decision(self) -> None:
        sent = {"dry_run": True, "target": "Radarr", "action": "POST /api/v3/movie"}
        fetched = {"dry_run": True, "target": "tracker-a", "action": "fetch .torrent", "reason": dryrun.NOT_FETCHED}
        self.assertEqual(cards.describe(events.WOULD_HAVE, sent), "Dry run: Radarr: POST /api/v3/movie, not sent")
        self.assertEqual(cards.describe(events.WOULD_HAVE, fetched), "Dry run: torrent not fetched from tracker-a")
        self.assertEqual(
            stats._sentence(events.WOULD_HAVE, sent), "Dry run: Radarr: POST /api/v3/movie, not sent.",
        )
        row = stats.log_row(SimpleNamespace(kind=events.WOULD_HAVE, payload=sent, at=None, item_id=3))
        self.assertEqual((row.label, row.badge), ("Dry run", "badge-warn"))
        # Shadow agreement reads the policy's would-haves only.
        shadow = SimpleNamespace(
            item_id=3, payload={"outcome": "approve"}, status=ItemStatus.APPROVED, reviewed_at=None, auto_decision=None,
        )
        dry = SimpleNamespace(item_id=3, payload=sent, status=ItemStatus.APPROVED, reviewed_at=None, auto_decision=None)
        self.assertEqual(stats.agreement_of([shadow, dry]), stats.agreement_of([shadow]))
        self.assertIn(
            "(events.payload ->> 'dry_run') IS NULL",
            str(stats.repeats().compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})),
        )
