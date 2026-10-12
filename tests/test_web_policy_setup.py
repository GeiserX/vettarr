"""The Policy and Setup pages: every route, what they write to the config file, and when they refuse.

The app runs through Starlette's test client on a real config file in a
temporary directory. A test posts what the page's own form holds, read out of
the rendered HTML, so a field the template names one way and the route reads
another fails here.
"""

import asyncio
import hashlib
import json
import os
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest import mock

import httpx
from sqlalchemy.dialects import postgresql
from test_web_foundation import HASH, WebCase

from vettarr import events, ingest
from vettarr.checker.policy import APPROVE, REVIEW, Impact
from vettarr.config import loader
from vettarr.config.schema import Config
from vettarr.searcher import search_service
from vettarr.web import configfile, connections, shell, ui
from vettarr.web.routes import policy as policy_routes
from vettarr.web.routes import setup as setup_routes

# Built from words, so no line here reads as a credential to a scanner.
TMDB_VALUE = "-".join(("tmdb", "value", "from", "the", "environment"))
PROWLARR_VALUE = "-".join(("written", "in", "the", "file"))
NEW_VALUE = "-".join(("typed", "into", "the", "form"))
COOKIE_VALUE = "-".join(("cookie", "value", "nobody", "sees"))
HTMX = {"HX-Request": "true"}

CONFIG = """\
# The operator's own notes stay.
telegram:
  api_id: 1
  api_hash: ${TG_HASH}
  bot_token: ${TG_TOKEN}
  reviewers: [7]
sources:
  - key: films                 # the main group
    kind: userbot
    chat: -100123
    formats: [card]
    allowed_senders: [7, 8]
    on_reject: delete
formats:
  - key: card
    kind: card
    type_hints:
      movie: [NEW MOVIE]
    category_words:
      movie: [Movie]
    fields:
      title: [Title]
      type: [Type]
    ignore_hosts: [instagram.com]
  - key: scene
    kind: scene
providers:
  - key: northwind
    base_url: https://northwind.example
    auth:
      from: cookie_file
      cookie_file: COOKIES/northwind.txt
    links:
      - pattern: 'torrents/(?P<id>\\d+)'
        download_url: '{base}/torrents/download/{id}'
        examples:
          - url: 'https://northwind.example/torrents/42'
            id: '42'
integrations:
  tmdb:
    api_key: ${TMDB_KEY}
  prowlarr:
    url: http://prowlarr:9696
    api_key: PROWLARR
  qbittorrent:
    url: http://qbittorrent:8080
policy:
  auto_approve: shadow         # trying it out
  lists:
    unwanted: [list-a-word]
  rules:
    - id: unwanted-keywords
      check: tmdb.keyword      # spelled by where it comes from
      matches_any: unwanted
      on_hit: reject
    - id: violence
      name: Violence is severe
      check: imdb.violence
      at_least: severe
      split_vote: 0.40
web:
  port: 4850
"""
ENV = {"TG_HASH": "telegram-hash-value", "TG_TOKEN": "telegram-token-value", "TMDB_KEY": TMDB_VALUE}

# The same file as an operator writes one: a comment on a line of its own above the first entry of a list.
OWN_LINE_COMMENTS = ("# the family group", "# how the group writes a post", "# what nobody here wants to see")
COMMENTED = (
    CONFIG.replace("sources:\n", f"sources:\n  {OWN_LINE_COMMENTS[0]}\n")
    .replace("formats:\n", f"formats:\n  {OWN_LINE_COMMENTS[1]}\n")
    .replace("  rules:\n", f"  rules:\n    {OWN_LINE_COMMENTS[2]}\n")
)
EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml"

LIST_WORD = "-".join(("word", "from", "the", "environment"))
# A policy whose values are written in ways a save must not flatten: references, a float, quoted digits.
AS_WRITTEN = CONFIG[:CONFIG.index("policy:\n")] + """\
policy:
  auto_approve: ${MODE}
  min_votes: ${VOTES}
  types:
    movie:
      max_wait_hours: ${HOURS}
    series:
      mode: ${SERIES_MODE}
  lists:
    unwanted: ["${LISTWORD}", plain]
  rules:
    - id: spies
      types: [series, movie]
      check: tmdb.keyword
      matches_any: ["007", "se7en", 1984]
      on_hit: reject
    - id: violence
      check: imdb.violence
      at_least: ${SEVERITY}
      split_vote: 0.333
      min_votes: ${VOTES}
    - id: long
      check: runtime_minutes
      between: [200, 400.50]
web:
  port: 4850
"""
AS_WRITTEN_ENV = {"MODE": "shadow", "VOTES": "25", "HOURS": "12", "SERIES_MODE": "never", "LISTWORD": LIST_WORD,
                  "SEVERITY": "severe"}
PASSKEY = "-".join(("passkey", "nobody", "sees"))
URL_PASSWORD = "-".join(("address", "password", "nobody", "sees"))


class Forms(HTMLParser):
    """Every form of a page as ``(action, [(name, value), ...])``: what a browser would send, buttons aside."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[str, list[tuple[str, str]]]] = []
        self._fields: list[tuple[str, str]] | None = None
        self._select: list | None = None
        self._textarea: list | None = None
        self._off = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._fields = []
            self.forms.append((a.get("action", ""), self._fields))
        if self._fields is None:
            return
        if tag == "fieldset" and (self._off or "disabled" in a):
            self._off += 1
        name = a.get("name")
        if tag == "input" and name and not self._off:
            if a.get("type") in ("checkbox", "radio") and "checked" not in a:
                return
            self._fields.append((name, a.get("value") or ""))
        elif tag == "select" and name:
            self._select = [name, None, None]
        elif tag == "option" and self._select is not None:
            if self._select[2] is None:
                self._select[2] = a.get("value") or ""
            if "selected" in a:
                self._select[1] = a.get("value") or ""
        elif tag == "textarea" and name:
            self._textarea = [name, ""]

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._fields = None
        elif tag == "fieldset" and self._off:
            self._off -= 1
        elif tag == "select" and self._select is not None:
            name, chosen, first = self._select
            if self._fields is not None and not self._off:
                self._fields.append((name, chosen if chosen is not None else first or ""))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            if self._fields is not None and not self._off:
                # A browser drops the newline right after the opening tag.
                self._fields.append((self._textarea[0], self._textarea[1].removeprefix("\n")))
            self._textarea = None


def forms(html: str, action: str) -> list[dict[str, list[str]]]:
    parser = Forms()
    parser.feed(html)
    out = []
    for found, fields in parser.forms:
        if found == action:
            data: dict[str, list[str]] = {}
            for name, value in fields:
                data.setdefault(name, []).append(value)
            out.append(data)
    return out


def text_of(html: str) -> str:
    """The page as a reader meets it: no tags, single spaces."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def tag(html: str, element_id: str) -> str:
    """The opening tag of the element with this id."""
    found = re.search(rf'<[a-z]+\b[^>]*\bid="{re.escape(element_id)}"[^>]*>', html, re.S)
    assert found, f"no element with id {element_id}"
    return found.group(0)


class ConfigCase(WebCase):
    """The app on a config file in a temporary directory. ``CONFIG = None`` is no file at all."""

    AUTH = None
    CONFIG: str | None = CONFIG
    READONLY = False

    def setUp(self) -> None:
        super().setUp()
        self.cookies = self.data / "cookies"
        self.path = self.data / "config.yaml"
        env = {"CONFIG_PATH": str(self.path), **ENV}
        if self.READONLY:
            env["CONFIG_READONLY"] = "1"
        patched = mock.patch.dict(os.environ, env)
        patched.start()
        self.addCleanup(patched.stop)
        if not self.READONLY:
            os.environ.pop("CONFIG_READONLY", None)
        self.running = Config()
        if self.CONFIG is not None:
            self.path.write_text(
                self.CONFIG.replace("COOKIES", str(self.cookies)).replace("PROWLARR", PROWLARR_VALUE)
            )
            try:
                self.running = loader.load(self.path)
            except loader.ConfigError:
                pass
        self.before = self.path.read_text() if self.path.exists() else None
        for target, name, value in (
            (configfile, "current", lambda: self.running),
            (setup_routes, "bot_chats", mock.AsyncMock(return_value=[])),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def file(self) -> Config:
        return loader.load(self.path)

    def saved(self, section: str) -> str:
        """Where a save that just worked sends the browser: the section, and the file as that save wrote it."""
        return f"/setup?saved={section}.{hashlib.sha256(self.path.read_bytes()).hexdigest()[:12]}#{section}"

    def unchanged(self) -> None:
        self.assertEqual(self.path.read_text() if self.path.exists() else None, self.before)

    def form(self, url: str, action: str, pick=lambda data: True) -> dict[str, list[str]]:
        found = [data for data in forms(self.get(url).text, action) if pick(data)]
        self.assertTrue(found, f"no form posts to {action} on {url}")
        return found[0]


# ---------------------------------------------------------------------------
# The file, read and changed
# ---------------------------------------------------------------------------


class ConfigFileTests(ConfigCase):
    def test_a_change_keeps_the_comments_and_every_reference(self) -> None:
        state = configfile.load()
        doc = state.candidate()
        configfile.put(doc["policy"], "auto_approve", "on")
        configfile.put(doc["web"], "port", 4851)
        text, config, errors = configfile.validate(doc)
        self.assertEqual((config.web.port, errors), (4851, {}))
        configfile.write(state, text)
        written = self.path.read_text()
        for kept in ("# The operator's own notes stay.", "# the main group", "# trying it out",
                     "api_key: ${TMDB_KEY}", "api_hash: ${TG_HASH}", "check: tmdb.keyword"):
            with self.subTest(kept=kept):
                self.assertIn(kept, written)
        self.assertIn("auto_approve: on", written)
        self.assertNotIn(TMDB_VALUE, written)
        self.assertEqual(self.path.with_name("config.yaml.bak").read_text(), self.before)

    def test_what_did_not_change_is_written_as_it_was(self) -> None:
        state = configfile.load()
        doc = state.candidate()
        configfile.put(doc, "policy", loader.plain(doc["policy"]))
        configfile.put(doc, "sources", loader.plain(doc["sources"]))
        self.assertEqual(loader.dump(doc), loader.dump(state.doc))

    def test_an_error_comes_back_under_the_path_of_its_setting(self) -> None:
        doc = configfile.load().candidate()
        doc["web"]["port"] = 0
        doc["policy"]["rules"][1]["at_least"] = "awful"
        _text, config, errors = configfile.validate(doc)
        self.assertIsNone(config)
        self.assertEqual(set(errors), {"web.port", "policy.rules[1]"})
        self.assertIn("severity is one of", errors["policy.rules[1]"])

    def test_a_secret_is_said_to_be_set_and_never_shown(self) -> None:
        state = configfile.load()
        self.assertEqual(configfile.secret_state(state.raw("integrations.tmdb.api_key")), "set, from ${TMDB_KEY}")
        self.assertEqual(configfile.secret_state(state.raw("integrations.prowlarr.api_key")), "set")
        self.assertEqual(configfile.secret_state(state.raw("integrations.qbittorrent.password")), "")
        self.assertEqual(configfile.secret_state(self.running.integrations.tmdb.api_key), "set")

    def test_an_address_with_a_password_or_a_token_counts_as_a_secret(self) -> None:
        for url in ("http://me:word@host:8080", "https://host/notify?token=abc", "https://host/#frag"):
            self.assertTrue(configfile.carries_secret(url), url)
        for url in ("http://prowlarr:9696", "${PROWLARR_URL}", None):
            self.assertFalse(configfile.carries_secret(url), url)

    def test_the_file_is_editable_only_when_it_loads_and_nothing_forbids_it(self) -> None:
        state = configfile.load()
        self.assertEqual((state.mode, state.writable, state.pending), (configfile.EDIT, True, False))
        with mock.patch.dict(os.environ, {"CONFIG_READONLY": "1"}):
            readonly = configfile.load()
            self.assertEqual((readonly.mode, readonly.writable), (configfile.FILE, False))
            with self.assertRaises(configfile.ConfigReadOnly):
                configfile.write(readonly, loader.dump(readonly.candidate()))
        self.path.write_text(self.before.replace("auto_approve: shadow", "auto_approve: maybe"))
        broken = configfile.load()
        self.assertEqual((broken.mode, broken.writable), (configfile.BROKEN, False))
        self.assertIn("policy.auto_approve", broken.errors[0])
        self.assertIs(broken.config, self.running)
        self.path.unlink()
        self.assertEqual(configfile.load().mode, configfile.ENVIRONMENT)

    def test_a_file_the_app_has_not_loaded_is_pending(self) -> None:
        self.path.write_text(self.before.replace("auto_approve: shadow", "auto_approve: on"))
        self.assertTrue(configfile.load().pending)

    def test_a_field_error_is_a_sentence_not_the_schema_s_pattern(self) -> None:
        doc = configfile.load().candidate()
        doc["integrations"]["prowlarr"]["url"] = "notaurl"
        doc["telegram"]["api_id"] = "abc"
        doc["sources"][0]["key"] = "Not A Key"
        _text, _config, errors = configfile.validate(doc)
        self.assertEqual(errors["integrations.prowlarr.url"], "An address starts with http:// or https:// and names a host.")
        self.assertEqual(errors["telegram.api_id"], "A whole number goes here.")
        self.assertEqual(errors["sources[0].key"], "Lowercase letters, digits, - and _ only, starting with a letter or a digit.")


class CommentedFileTests(ConfigCase):
    """A comment on its own line above a list's first entry: ruamel wrote a deep copy of such a file correctly
    once and as broken YAML the second time, and a save wrote it twice (once to check it, once to the file)."""

    CONFIG = COMMENTED

    def kept(self) -> None:
        written = self.path.read_text()
        for comment in (*OWN_LINE_COMMENTS, "# The operator's own notes stay.", "# the main group", "# trying it out"):
            with self.subTest(comment=comment):
                self.assertEqual(written.count(comment), 1)
        self.file()

    def test_the_document_to_change_is_written_the_same_however_often(self) -> None:
        doc = configfile.load().candidate()
        first = loader.dump(doc)
        self.assertEqual(loader.dump(doc), first)
        self.assertEqual(first, self.before)

    def test_what_is_checked_is_the_text_that_is_written(self) -> None:
        state = configfile.load()
        doc = state.candidate()
        configfile.put(doc["web"], "port", 4851)
        text, config, errors = configfile.validate(doc)
        self.assertEqual((config.web.port, errors), (4851, {}))
        with mock.patch.object(loader, "dump", side_effect=AssertionError("written from the document again")):
            configfile.write(state, text)
        self.assertEqual(self.path.read_text(), text)
        self.kept()

    def test_every_setup_form_saves_such_a_file(self) -> None:
        web = self.form("/setup", "/setup/block/web")
        resp = self.post("/setup/block/web", data={**web, "port": ["4851"]}, headers=HTMX)
        self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("web"), resp.text)
        self.assertEqual(self.file().web.port, 4851)
        self.kept()
        source = self.form("/setup", "/setup/sources", lambda d: d["original"] == ["films"])
        resp = self.post("/setup/sources", data={**source, "on_reject": ["react"]}, headers=HTMX)
        self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("sources"), resp.text)
        self.assertEqual(self.file().sources[0].on_reject, "react")
        self.kept()
        added = self.post("/setup/formats", data={"key": "plain", "kind": "scene"}, headers=HTMX)
        self.assertEqual(added.headers.get("HX-Redirect"), self.saved("formats"), added.text)
        self.assertEqual([f.key for f in self.file().formats], ["card", "scene", "plain"])
        self.kept()

    def test_the_policy_page_saves_such_a_file(self) -> None:
        data = self.form("/policy", "/policy")
        resp = self.post("/policy", data={**data, "auto_approve": ["on"], "rule-1-severity": ["moderate"]}, headers=HTMX)
        self.assertNotIn("Nothing was saved", resp.text)
        self.assertIn("Restart the container to apply it (docker compose restart app).", resp.headers["HX-Trigger"])
        policy = self.file().policy
        self.assertEqual((policy.auto_approve, policy.rules[1].at_least), ("on", "moderate"))
        self.kept()
        # And again, on the file that save wrote.
        again = self.post("/policy", data={**self.form("/policy", "/policy"), "auto_approve": ["off"]}, headers=HTMX)
        self.assertNotIn("Nothing was saved", again.text)
        self.assertEqual(self.file().policy.auto_approve, "off")
        self.kept()


    def test_the_last_entry_of_a_list_goes_with_the_comment_above_it(self) -> None:
        # ruamel would write that comment between the key and the empty [], which is not YAML.
        gone = self.post("/setup/sources/films/delete", headers=HTMX)
        self.assertEqual(gone.headers.get("HX-Redirect"), self.saved("sources"), gone.text)
        self.assertEqual(self.file().sources, [])
        self.assertIn("sources: []\n", self.path.read_text())
        data = self.form("/policy", "/policy")
        for name in [n for n in data if n.startswith(("rule-", "list-"))]:
            del data[name]
        resp = self.post("/policy", data=data, headers=HTMX)
        self.assertNotIn("Nothing was saved", resp.text)
        policy = self.file().policy
        self.assertEqual((policy.rules, policy.lists), ([], {}))
        written = self.path.read_text()
        self.assertNotIn(OWN_LINE_COMMENTS[0], written)
        self.assertNotIn(OWN_LINE_COMMENTS[2], written)
        for comment in (OWN_LINE_COMMENTS[1], "# The operator's own notes stay.", "# trying it out"):
            with self.subTest(comment=comment):
                self.assertEqual(written.count(comment), 1)


class ShippedExampleTests(ConfigCase):
    """config.example.yaml is what a new instance starts from, and it is full of comments on lines of their own."""

    CONFIG = None

    def setUp(self) -> None:
        super().setUp()
        self.path.write_text(EXAMPLE.read_text())
        self.running = loader.load(self.path)
        self.before = self.path.read_text()

    @staticmethod
    def comments(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]

    def test_the_example_is_saved_from_setup_and_from_policy_with_its_comments(self) -> None:
        web = self.form("/setup", "/setup/block/web")
        resp = self.post("/setup/block/web", data={**web, "port": ["4851"]}, headers=HTMX)
        self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("web"), resp.text)
        self.assertEqual(self.file().web.port, 4851)
        self.assertEqual(self.comments(self.path.read_text()), self.comments(self.before))
        resp = self.post("/policy", data={**self.form("/policy", "/policy"), "auto_approve": ["on"]}, headers=HTMX)
        self.assertNotIn("Nothing was saved", resp.text)
        self.assertEqual(self.file().policy.auto_approve, "on")
        self.assertEqual(self.comments(self.path.read_text()), self.comments(self.before))
        # Its two formats sit under a comment of several lines: both can go.
        for key in ("card", "scene"):
            gone = self.post(f"/setup/formats/{key}/delete", headers=HTMX)
            self.assertEqual(gone.headers.get("HX-Redirect"), self.saved("formats"), gone.text)
        self.assertEqual(self.file().formats, [])


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class PolicyCase(ConfigCase):
    def posted(self, **changes) -> dict[str, list[str]]:
        data = self.form("/policy", "/policy")
        for name, value in changes.items():
            data[name.replace("__", "-")] = value if isinstance(value, list) else [value]
        return data

    def send(self, url="/policy", headers=HTMX, **changes):
        return self.post(url, data=self.posted(**changes), headers=headers)


class PolicyPageTests(PolicyCase):
    def test_the_switch_has_three_positions_each_with_its_line(self) -> None:
        page = self.get("/policy").text
        read = text_of(page)
        for label, line in (
            ("Off", "Nothing is decided for you: every item waits in the inbox."),
            ("Shadow", "The policy decides and records what it would have done, and still leaves every item to you."),
            ("On", "The policy acts: it approves, holds for review or rejects by the rules below."),
        ):
            self.assertIn(f"{label} {line}", read)
        self.assertIn("checked", tag(page, "auto-shadow"))
        self.assertNotIn("checked", tag(page, "auto-on"))

    def test_the_type_table_has_a_row_of_three_settings_for_every_type(self) -> None:
        page = self.get("/policy").text
        data = self.posted()
        for media_type in ("movie", "series", "audiobook", "ebook", "magazine", "comic", "music"):
            with self.subTest(media_type=media_type):
                self.assertEqual(
                    [data[f"type-{media_type}-{name}"] for name in ("mode", "missing", "wait")],
                    [["rules"], ["wait"], ["24"]],
                )
                self.assertIn(f'aria-label="{media_type}: decided"', page)

    def test_a_rule_reads_as_a_sentence(self) -> None:
        page = self.get("/policy").text
        card = page[page.index('id="rule-1"'):page.index('name="op" value="add-rule"')]
        # Only what a reader sees: the chosen option of each select, the marker on the chosen chip.
        card = re.sub(r"<option(?![^>]*selected)[^>]*>[^<]*</option>|<legend.*?</legend>", "", card, flags=re.S)
        card = re.sub(
            r'(<input class="sr-only" type="radio"[^>]*?( checked)?>\s*<label[^>]*>)(<span class="sev-mark">[^<]*</span>)',
            lambda m: m.group(1) + (m.group(3) if m.group(2) else ""), card,
        )
        read = text_of(card[card.index('<div class="sentence'):])
        self.assertTrue(
            read.startswith("When Violence (IMDb) is at least None Mild Moderate hold from Severe "
                            "with at least votes, hold it for review Also when at least % of the votes say so"),
            read[:200],
        )
        self.assertIn('placeholder="20"', tag(card, "rule-1-min_votes"))
        self.assertIn('value="40"', tag(card, "rule-1-split"))

    def test_every_severity_chip_carries_its_word_and_the_chosen_one_the_marker(self) -> None:
        page = self.get("/policy").text
        chips = re.findall(
            r'<input class="sr-only" type="radio" id="rule-1-severity-(\w+)"[^>]*?( checked)?>\s*'
            r'<label class="pill pill-(\w+)" for="rule-1-severity-\w+"><span class="sev-mark">([^<]+)</span>(\w+)</label>',
            page,
        )
        self.assertEqual([(value, bool(on), colour, word) for value, on, colour, _, word in chips], [
            ("none", False, "none", "None"), ("mild", False, "mild", "Mild"),
            ("moderate", False, "moderate", "Moderate"), ("severe", True, "severe", "Severe"),
        ])
        self.assertEqual({mark for *_, mark, _ in chips}, {"hold from"})
        self.assertIn('data-op="at_least"', page)
        rejecting = self.send("/policy/edit", rule__1__on_hit="reject", rule__1__operator="at_most").text
        self.assertIn('<span class="sev-mark">reject up to</span>', rejecting)
        self.assertIn('data-op="at_most"', rejecting)

    def test_a_signal_offers_its_own_operators_and_its_own_kind_of_value(self) -> None:
        def operators(page: str) -> list[str]:
            select = page[page.index('id="rule-0-operator"'):]
            return re.findall(r'<option value="(\w+)"', select[:select.index("</select>")])

        page = self.get("/policy").text
        self.assertEqual(operators(page), ["any_of", "none_of", "matches_any"])
        self.assertIn('<option value="unwanted" selected>the list unwanted</option>', page)
        for check, expected, control in (
            ("year", ["at_least", "at_most", "between"], 'type="number" step="any" inputmode="decimal" id="rule-0-number"'),
            ("adult", ["is"], 'id="rule-0-flag"'),
            ("time_window", ["between"], 'type="time" id="rule-0-from"'),
            ("resolution", ["at_least", "at_most", "any_of", "none_of"], 'id="rule-0-resolution"'),
            ("certification", ["at_least", "any_of", "none_of"], 'id="rule-0-cert"'),
            ("imdb.nudity", ["at_least", "at_most"], 'id="rule-0-severity-moderate"'),
        ):
            with self.subTest(check=check):
                page = self.send("/policy/edit", rule__0__check=check, rule__0__operator="nonsense").text
                self.assertEqual(operators(page), expected)
                self.assertIn(control, page)
        self.unchanged()

    def test_the_signals_on_offer_are_the_catalogue_s(self) -> None:
        page = self.get("/policy").text
        select = page[page.index('id="rule-0-check"'):]
        offered = re.findall(r'<option value="([\w.]+)"', select[:select.index("</select>")])
        self.assertEqual(sorted(offered), sorted(policy_routes.SIGNALS))
        self.assertIn('<optgroup label="Parents guide (IMDb)">', page)

    def test_rules_and_lists_are_added_removed_and_moved_without_a_write(self) -> None:
        def ids(resp) -> list[str]:
            return [v[0] for k, v in forms(resp.text, "/policy")[0].items() if re.fullmatch(r"rule-\d+-id", k)]

        added = self.send("/policy/edit", op="add-rule")
        self.assertEqual(ids(added), ["unwanted-keywords", "violence", "rule-1"])
        self.assertEqual(ids(self.send("/policy/edit", op="up:1")), ["violence", "unwanted-keywords"])
        self.assertEqual(ids(self.send("/policy/edit", op="down:0")), ["violence", "unwanted-keywords"])
        self.assertEqual(ids(self.send("/policy/edit", op="down:1")), ["unwanted-keywords", "violence"])
        self.assertEqual(ids(self.send("/policy/edit", op="remove-rule:0")), ["violence"])
        lists = forms(self.send("/policy/edit", op="add-list").text, "/policy")[0]
        self.assertEqual((lists["list-0-name"], lists["list-1-name"]), (["unwanted"], ["list-1"]))
        self.assertNotIn("list-0-name", forms(self.send("/policy/edit", op="remove-list:0").text, "/policy")[0])
        self.unchanged()

    def test_enter_in_a_field_never_saves(self) -> None:
        # A form's first submit button is the one Enter presses.
        page = self.get("/policy").text
        first = re.search(r"<button[^>]*>", page[page.index('<form id="policy-form"'):]).group(0)
        self.assertIn('formaction="/policy/edit"', first)
        self.assertIn('hx-post="/policy/edit"', first)
        self.assertIn('value="refresh"', first)

    def test_the_page_as_it_stands_saves_back_to_the_same_policy(self) -> None:
        resp = self.send()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.file().policy, self.running.policy)
        written = self.path.read_text()
        for kept in ("# trying it out", "check: tmdb.keyword      # spelled by where it comes from", "split_vote: 0.40",
                     "lists:\n    unwanted: [list-a-word]"):
            with self.subTest(kept=kept):
                self.assertIn(kept, written)

    def test_a_warning_about_the_policy_is_shown(self) -> None:
        self.assertIn("Worth a look.", self.get("/policy").text)
        self.assertIn("no rule applies to audiobook", text_of(self.get("/policy").text))


class PolicyFocusTests(PolicyCase):
    """An edit swaps the whole editor. The answer names the control the keyboard goes to next (htmx focuses
    what carries ``autofocus``), or focus would fall to the top of the page."""

    def focused(self, resp) -> str | None:
        # The editor only: the layout's confirm dialog has an autofocus of its own, on Cancel.
        editor = resp.text[resp.text.index('id="policy-editor"'):]
        found = re.findall(r"<[a-z]+\b[^>]*\sautofocus[^>]*>", editor[:editor.index("</form>")])
        self.assertLessEqual(len(found), 1, found)
        if not found:
            return None
        self.assertNotIn("disabled", found[0])
        return re.search(r'\bid="([^"]+)"', found[0])[1]

    def edit(self, page, op: str):
        return self.post("/policy/edit", data={**forms(page.text, "/policy")[0], "op": op}, headers=HTMX)

    def test_a_moved_rule_keeps_the_focus_on_its_own_button(self) -> None:
        three = self.send("/policy/edit", op="add-rule")
        for op, control in (
            ("down:0", "rule-1-down"),  # still a rule below it
            ("down:1", "rule-2-up"),    # now the last: Move down is disabled
            ("up:2", "rule-1-up"),
            ("up:1", "rule-0-down"),    # now the first: Move up is disabled
            ("up:0", None), ("down:2", None), ("down:9", None),
        ):
            with self.subTest(op=op):
                self.assertEqual(self.focused(self.edit(three, op)), control)

    def test_a_new_rule_or_list_gets_the_focus_on_its_name(self) -> None:
        self.assertEqual(self.focused(self.send("/policy/edit", op="add-rule")), "rule-2-name")
        self.assertEqual(self.focused(self.send("/policy/edit", op="add-list")), "list-1-name")

    def test_after_a_removal_the_focus_goes_to_what_took_its_place(self) -> None:
        self.assertEqual(self.focused(self.send("/policy/edit", op="remove-rule:0")), "rule-0-name")
        last = self.send("/policy/edit", op="remove-rule:1")
        self.assertEqual(self.focused(last), "rule-0-name")
        self.assertEqual(self.focused(self.edit(last, "remove-rule:0")), "add-rule")
        self.assertEqual(self.focused(self.send("/policy/edit", op="remove-list:0")), "add-list")
        two = self.send("/policy/edit", op="add-list")
        self.assertEqual(self.focused(self.edit(two, "remove-list:0")), "list-0-name")

    def test_nothing_takes_the_focus_when_no_rule_or_list_moved(self) -> None:
        self.assertIsNone(self.focused(self.get("/policy")))
        self.assertIsNone(self.focused(self.send("/policy/edit", op="refresh")))
        self.assertIsNone(self.focused(self.send("/policy/edit", rule__1__check="year")))
        self.assertIsNone(self.focused(self.send()))

    def test_every_button_that_changes_the_shape_has_an_id(self) -> None:
        page = self.get("/policy").text
        for control in ("rule-0-up", "rule-0-down", "rule-0-remove", "rule-1-up", "add-rule", "list-0-remove", "add-list"):
            with self.subTest(control=control):
                self.assertIn('formaction="/policy/edit"', tag(page, control))


class PolicyAsWrittenTests(PolicyCase):
    """A save changes what was changed. What came back as the page showed it is written as the file has it."""

    CONFIG = AS_WRITTEN

    def setUp(self) -> None:
        patched = mock.patch.dict(os.environ, AS_WRITTEN_ENV)
        patched.start()
        self.addCleanup(patched.stop)
        super().setUp()

    def rules(self) -> list[dict]:
        return loader.plain(loader.read_document(self.path))["policy"]["rules"]

    def test_the_fixture_loads_as_meant(self) -> None:
        policy = self.running.policy
        self.assertEqual((policy.auto_approve, policy.min_votes, policy.lists), ("shadow", 25, {"unwanted": [LIST_WORD, "plain"]}))
        self.assertEqual((policy.types["movie"].max_wait_hours, policy.types["series"].mode), (12, "never"))
        self.assertEqual([(r.id, r.value) for r in policy.rules],
                         [("spies", ["007", "se7en", 1984]), ("violence", "severe"), ("long", [200, 400.5])])

    def test_a_word_is_shown_as_the_file_writes_it_and_a_choice_as_it_loads(self) -> None:
        page = self.get("/policy").text
        data = forms(page, "/policy")[0]
        self.assertEqual(data["list-0-words"], ["${LISTWORD}\nplain"])
        self.assertNotIn(LIST_WORD, page)
        self.assertEqual(data["rule-0-words"], ["007\nse7en\n1984"])
        self.assertEqual((data["auto_approve"], data["min_votes"], data["type-movie-wait"], data["type-series-mode"]),
                         (["shadow"], ["25"], ["12"], ["never"]))
        self.assertEqual((data["rule-1-severity"], data["rule-1-split"], data["rule-1-min_votes"]), (["severe"], ["33.3"], ["25"]))
        self.assertEqual((data["rule-2-low"], data["rule-2-high"]), (["200"], ["400.5"]))

    def test_a_save_that_changes_nothing_leaves_the_file_as_it_was(self) -> None:
        resp = self.send()
        self.assertNotIn("Nothing was saved", resp.text)
        self.assertIn("HX-Trigger", resp.headers)
        self.unchanged()

    def test_a_change_elsewhere_keeps_every_reference_number_and_quote(self) -> None:
        resp = self.send(rule__1__on_hit="reject", list__0__words="${LISTWORD}\nplain\nadded",
                         rule__0__words="se7en\n007\n1984\n0451\n27", rule__2__low="210", type__movie__missing="review")
        self.assertNotIn("Nothing was saved", resp.text)
        written = self.path.read_text()
        for kept in ("auto_approve: ${MODE}", "\n  min_votes: ${VOTES}", "max_wait_hours: ${HOURS}", "mode: ${SERIES_MODE}",
                     'unwanted: ["${LISTWORD}", plain, added]', "types: [series, movie]",
                     "at_least: ${SEVERITY}", "split_vote: 0.333\n", "      min_votes: ${VOTES}",
                     "between: [210, 400.50]"):
            with self.subTest(kept=kept):
                self.assertIn(kept, written)
        self.assertNotIn(LIST_WORD, written)
        # A word of digits is a number only when it reads back as typed: 007 and 0451 are not 7 and 451.
        self.assertEqual(self.rules()[0]["matches_any"], ["se7en", "007", 1984, "0451", 27])
        self.assertIn('"007"', written)
        policy = self.file().policy
        self.assertEqual((policy.rules[1].on_hit, policy.types["movie"].when_data_missing), ("reject", "review"))
        self.assertEqual(policy.lists["unwanted"], [LIST_WORD, "plain", "added"])

    def test_a_number_that_is_changed_is_written_as_typed(self) -> None:
        self.send(rule__1__split="33.4", rule__1__min_votes="30", min_votes="15", rule__1__severity="moderate",
                  auto_approve="on", type__movie__wait="6", type__series__mode="always")
        rule = self.rules()[1]
        self.assertEqual((rule["split_vote"], rule["min_votes"], rule["at_least"]), (0.334, 30, "moderate"))
        written = self.path.read_text()
        for line in ("auto_approve: on", "\n  min_votes: 15", "max_wait_hours: 6", "mode: always", "split_vote: 0.334\n"):
            with self.subTest(line=line):
                self.assertIn(line, written)

    def test_a_rule_whose_signal_changes_takes_its_value_from_the_form(self) -> None:
        self.send(rule__1__check="imdb.nudity")
        self.assertEqual(self.rules()[1]["at_least"], "severe")
        self.assertEqual(self.rules()[1]["split_vote"], 0.333)


class PolicySaveTests(PolicyCase):
    def test_save_writes_the_form_to_the_file_and_says_to_restart(self) -> None:
        resp = self.send(
            auto_approve="on", min_votes="15", type__magazine__mode="always", type__movie__wait="12",
            rule__1__severity="moderate", rule__1__on_hit="reject", rule__1__types=["movie"], rule__1__min_votes="30",
            list__0__words="list-a-word\nlist-a-phrase\n",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Restart the container to apply it (docker compose restart app).", resp.headers["HX-Trigger"])
        policy = self.file().policy
        self.assertEqual((policy.auto_approve, policy.min_votes), ("on", 15))
        self.assertEqual(policy.types["magazine"].mode, "always")
        self.assertEqual(policy.types["movie"].max_wait_hours, 12)
        self.assertNotIn("series", policy.types)
        self.assertEqual(policy.lists, {"unwanted": ["list-a-word", "list-a-phrase"]})
        rule = policy.rules[1]
        self.assertEqual(
            (rule.id, rule.at_least, rule.on_hit, rule.types, rule.min_votes, rule.split_vote),
            ("violence", "moderate", "reject", ["movie"], 30, 0.4),
        )
        self.assertEqual(self.path.with_name("config.yaml.bak").read_text(), self.before)
        self.assertIn("# The operator's own notes stay.", self.path.read_text())
        # The app reads its config once: the page says the file is ahead of it.
        self.assertIn("Restart to apply.", resp.text)
        self.assertIn("Restart to apply.", self.get("/policy").text)

    def test_a_rule_of_every_kind_of_value_is_written_back_as_the_file_had_it(self) -> None:
        page = self.send("/policy/edit", op="add-rule")
        for _ in range(4):
            page = self.post("/policy/edit", data={**forms(page.text, "/policy")[0], "op": "add-rule"}, headers=HTMX)
        data = forms(page.text, "/policy")[0]
        rules = (
            {"id": "old", "check": "year", "operator": "at_most", "number": "1950", "on_hit": "review"},
            {"id": "long", "check": "runtime_minutes", "operator": "between", "low": "200", "high": "400.5", "on_hit": "notify"},
            {"id": "grown-up", "check": "adult", "operator": "is", "flag": "true", "on_hit": "reject"},
            {"id": "night", "check": "time_window", "operator": "between", "from": "23:00", "to": "06:30", "on_hit": "approve"},
            {"id": "genres", "check": "genre", "operator": "any_of", "list": "", "words": "27\nHorror\n", "on_hit": "review"},
        )
        for n, rule in enumerate(rules, start=2):
            for name, value in rule.items():
                data[f"rule-{n}-{name}"] = [value]
        resp = self.post("/policy", data=data, headers=HTMX)
        self.assertNotIn("Nothing was saved", resp.text)
        written = loader.plain(loader.read_document(self.path))["policy"]["rules"][2:]
        self.assertEqual(written, [
            {"id": "old", "check": "year", "at_most": 1950},
            {"id": "long", "check": "runtime_minutes", "between": [200, 400.5], "on_hit": "notify"},
            {"id": "grown-up", "check": "adult", "is": True, "on_hit": "reject"},
            {"id": "night", "check": "time_window", "between": ["23:00", "06:30"], "on_hit": "approve"},
            {"id": "genres", "check": "genre", "any_of": [27, "Horror"]},
        ])

    def test_an_error_lands_beside_its_field_and_nothing_is_written(self) -> None:
        resp = self.send(rule__1__id="unwanted-keywords")
        self.assertEqual(resp.status_code, 200)
        field = tag(resp.text, "rule-1-id")
        self.assertIn('aria-invalid="true"', field)
        self.assertIn('aria-describedby="err-rule-1-id"', field)
        self.assertRegex(resp.text, r'<p class="field-error" id="err-rule-1-id">.*?duplicate rule id')
        self.assertNotIn("aria-invalid", tag(resp.text, "rule-0-id"))
        self.assertIn("Nothing was saved.", resp.text)
        self.assertIn('href="#rule-1"', resp.text)
        self.unchanged()

    def test_a_refused_save_says_so_in_a_toast_and_focuses_the_field(self) -> None:
        """The notice under the form was scrolled under the action bar: on a phone
        nothing said the save had failed."""
        resp = self.send(rule__1__id="unwanted-keywords")
        trigger = json.loads(resp.headers["HX-Trigger"])["toast"]
        self.assertEqual(trigger["kind"], "error")
        self.assertTrue(trigger["text"].startswith("Nothing saved. Fix rule 2: "), trigger)
        self.assertIn("duplicate rule id", trigger["text"])
        # The page goes to the field, not to the notice under the form.
        self.assertEqual(resp.headers["HX-Reswap"], "outerHTML")
        self.assertIn("autofocus", tag(resp.text, "rule-1-id"))
        self.assertEqual(resp.text.count(" autofocus"), 1 + resp.text.count('value="no" autofocus'))

    def test_a_value_the_signal_cannot_take_is_named_on_its_rule(self) -> None:
        for changes, control, message in (
            ({"rule__1__check": "year", "rule__1__operator": "at_least", "rule__1__number": ""}, "rule-1-number", "expected a number"),
            ({"rule__1__check": "certification", "rule__1__operator": "at_least", "rule__1__cert": "18"}, "rule-1-cert",
             "a certification is COUNTRY:rating"),
            ({"rule__0__list": "", "rule__0__words": ""}, "rule-0-words", "matches_any needs at least one value"),
            ({"rule__1__split": "140"}, "rule-1-split", "less than or equal to 1"),
            ({"rule__0__id": "Not A Key"}, "rule-0-id", "Lowercase letters, digits, - and _ only"),
        ):
            with self.subTest(control=control):
                resp = self.send(**changes)
                self.assertIn('aria-invalid="true"', tag(resp.text, control))
                self.assertIn(message, text_of(resp.text))
                self.unchanged()

    def test_a_list_a_rule_points_at_cannot_go_and_a_list_needs_a_name(self) -> None:
        data = self.posted()
        del data["list-0-name"], data["list-0-words"]
        resp = self.post("/policy", data=data, headers=HTMX)
        self.assertIn("no list 'unwanted' under policy.lists", text_of(resp.text))
        self.assertIn('aria-invalid="true"', tag(resp.text, "rule-0-list"))
        self.assertIn("the list unwanted (missing)", resp.text)
        resp = self.send(list__0__name="Bad Name")
        self.assertIn('aria-invalid="true"', tag(resp.text, "list-0-name"))
        self.assertIn("A list needs a name", resp.text)
        self.unchanged()

    def test_a_type_s_wait_must_be_a_number(self) -> None:
        resp = self.send(type__movie__wait="soon")
        self.assertIn('aria-invalid="true"', tag(resp.text, "type-movie-wait"))
        self.assertIn('id="err-type-movie"', resp.text)
        self.unchanged()

    def test_without_htmx_a_save_redirects_and_a_refused_one_is_a_422_page(self) -> None:
        refused = self.send(headers={}, rule__1__id="unwanted-keywords")
        self.assertEqual(refused.status_code, 422)
        self.assertIn("<title>Policy · vettarr</title>", refused.text)
        self.assertIn("duplicate rule id", refused.text)
        self.unchanged()
        saved = self.send(headers={}, auto_approve="off")
        self.assertEqual((saved.status_code, saved.headers["location"]), (303, "/policy"))
        self.assertEqual(self.file().policy.auto_approve, "off")


class PolicyPreviewTests(PolicyCase):
    def test_preview_runs_the_form_s_policy_over_the_last_200_and_writes_nothing(self) -> None:
        impact = Impact(
            counts={"approve": 41, "review": 9, "reject": 3, "wait": 2}, compared=55, not_comparable=5,
            changed=[{"item_id": 12, "title": "The Lantern Keeper", "before": REVIEW, "after": APPROVE,
                      "reasons": ["no rule hit (2 checked)"]}],
        )
        with mock.patch.object(policy_routes, "preview_impact", mock.AsyncMock(return_value=impact)) as preview:
            resp = self.send("/policy/preview", auto_approve="on", rule__1__severity="mild")
        candidate, limit = preview.await_args.args
        self.assertEqual((candidate.auto_approve, candidate.rules[1].at_least, limit), ("on", "mild", 200))
        read = text_of(resp.text)
        self.assertIn("Preview: this policy over the last 55 decided items", read)
        for count, label in ((41, "approved"), (9, "held for review"), (3, "rejected"), (2, "waiting for data")):
            self.assertIn(f"{count} {label}", read)
        self.assertIn("1 would be decided differently", read)
        self.assertIn("The Lantern Keeper held for review → becomes approved no rule hit (2 checked)", read)
        self.assertIn('href="/item/12?from=policy"', resp.text)
        self.assertIn("5 of the last 200 stored items have nothing stored to decide from", read)
        self.assertIn('name="rule-1-severity" value="mild" checked', resp.text)
        self.unchanged()

    def test_the_real_engine_decides_the_stored_rows_again(self) -> None:
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        guide = {"severity": "moderate", "votes": 80, "breakdown": {"None": 5, "Mild": 15, "Moderate": 50, "Severe": 10}}
        rows = [
            SimpleNamespace(id=n, title=title, media_type="movie", created_at=now - timedelta(hours=1),
                            auto_decided_at=now, signals=signals, policy_outcome=before)
            for n, (title, signals, before) in enumerate((
                ("Quiet Harbour", {"keyword": ["sea"], "imdb.violence": guide}, APPROVE),
                ("Old Row", None, None),
            ), start=1)
        ]

        class _Session:
            async def execute(self, _statement):
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        with mock.patch("vettarr.database.async_session", lambda: _Session()), \
                mock.patch("vettarr.config.runtime.get_config", lambda: self.running):
            same = text_of(self.send("/policy/preview").text)
            stricter = text_of(self.send("/policy/preview", rule__1__severity="moderate").text)
        self.assertIn("No stored item would be decided differently.", same)
        self.assertIn("1 of the last 200 stored item has nothing stored", same)
        self.assertIn("Quiet Harbour approved → becomes held for review imdb.violence: moderate", stricter)
        self.unchanged()

    def test_a_form_that_is_not_valid_is_not_previewed(self) -> None:
        with mock.patch.object(policy_routes, "preview_impact", mock.AsyncMock()) as preview:
            resp = self.send("/policy/preview", rule__1__id="unwanted-keywords")
        preview.assert_not_awaited()
        self.assertIn("duplicate rule id", resp.text)
        self.assertNotIn("Preview: this policy", resp.text)
        # Nobody asked for a save.
        self.assertIn("Nothing to preview yet.", resp.text)
        self.assertNotIn("Nothing was saved", resp.text)


class PolicyManagedTests(PolicyCase):
    READONLY = True

    def test_the_page_says_managed_by_file_and_every_control_is_off(self) -> None:
        page = self.get("/policy").text
        read = text_of(page)
        self.assertIn("Managed by file. CONFIG_READONLY is set, so this page shows the config and changes nothing.", read)
        self.assertIn(f"edit {self.path} where the file is kept, deploy it and restart vettarr", read)
        self.assertIn('<fieldset class="plain-fieldset space-y-6" disabled>', page)
        # Preview impact writes nothing, so it stays: the rest of the controls are gone.
        self.assertEqual(re.findall(r"<button[^>]*>", page[page.index('id="policy-editor"'):page.index('id="toast"')]), [
            '<button class="sr-only" tabindex="-1" aria-hidden="true" name="op" value="refresh" '
            'formaction="/policy/edit" hx-post="/policy/edit" hx-swap="outerHTML">',
            '<button class="btn btn-secondary btn-lg" name="op" value="preview" formaction="/policy/preview" '
            'hx-post="/policy/preview">',
        ])
        self.assertNotIn("before you save it", read)
        self.assertIn("Preview impact shows what the policy as the file has it decides", read)
        self.assertEqual(forms(page, "/policy"), [{}])
        # What the policy is can still be read.
        self.assertIn("Violence is severe", page)
        self.assertIn('<span class="sev-mark">hold from</span>Severe', page)

    def test_every_write_is_refused_and_the_file_is_as_it_was(self) -> None:
        for url in ("/policy", "/policy/edit"):
            with self.subTest(url=url):
                resp = self.post(url, data={"auto_approve": "on"}, headers=HTMX)
                self.assertEqual((resp.status_code, resp.text), (409, configfile.REFUSED[configfile.FILE]))
        self.unchanged()

    def test_preview_is_of_the_policy_the_file_holds(self) -> None:
        """Preview impact went with Save, though a preview writes nothing."""
        impact = Impact(compared=3, counts={"approve": 1, "review": 2, "reject": 0, "wait": 0}, changed=[])
        with mock.patch.object(policy_routes, "preview_impact", mock.AsyncMock(return_value=impact)) as preview:
            # What the disabled form posts is nothing; whatever arrives, the file's policy is previewed.
            resp = self.post("/policy/preview", data={"auto_approve": "off"}, headers=HTMX)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(preview.await_args.args[0], configfile.load().config.policy)
        self.assertIn("No stored item would be decided differently.", resp.text)
        self.unchanged()


class PolicyWithoutAFileTests(PolicyCase):
    CONFIG = None

    def test_an_environment_boot_shows_the_policy_and_changes_nothing(self) -> None:
        page = self.get("/policy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No config file.", page.text)
        self.assertIn(f"vettarr config import-env -o {self.path}", page.text)
        resp = self.post("/policy", data={"auto_approve": "on"}, headers=HTMX)
        self.assertEqual((resp.status_code, resp.text), (409, configfile.REFUSED[configfile.ENVIRONMENT]))
        self.assertFalse(self.path.exists())


class PolicyBrokenFileTests(PolicyCase):
    CONFIG = CONFIG.replace("auto_approve: shadow", "auto_approve: maybe")

    def test_a_file_that_does_not_load_is_said_so_with_its_error(self) -> None:
        page = self.get("/policy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("The config file does not load.", page.text)
        self.assertIn("policy.auto_approve", page.text)
        self.assertEqual(self.post("/policy", data={}, headers=HTMX).status_code, 409)
        self.unchanged()


class RouteTests(ConfigCase):
    def test_the_two_pages_serve_exactly_these_routes(self) -> None:
        served = {
            (method.upper(), path)
            for path, methods in self.app.openapi()["paths"].items() for method in methods
            if path.split("/")[1] in ("policy", "setup", "settings")
        }
        self.assertEqual(served, {
            ("GET", "/policy"), ("POST", "/policy"), ("POST", "/policy/edit"), ("POST", "/policy/preview"),
            ("GET", "/setup"), ("GET", "/settings"), ("POST", "/settings/cookies/{key}"),
            ("POST", "/setup/block/{path}"), ("POST", "/setup/block/{path}/test"),
            ("POST", "/setup/sources"), ("POST", "/setup/sources/{key}/delete"), ("POST", "/setup/test-parse"),
            ("POST", "/setup/providers/{key}/check"), ("POST", "/setup/providers/link"),
            ("POST", "/setup/formats"), ("POST", "/setup/formats/{key}"), ("POST", "/setup/formats/{key}/delete"),
        })

    def test_a_get_changes_nothing(self) -> None:
        for url in ("/policy", "/setup", "/settings", "/setup?saved=sources"):
            self.assertEqual(self.get(url).status_code, 200)
        self.unchanged()
        self.assertFalse(self.path.with_name("config.yaml.bak").exists())


class GateTests(ConfigCase):
    AUTH = {"password_hash": HASH}

    def test_a_stranger_reaches_neither_page_and_changes_nothing(self) -> None:
        for url in ("/policy", "/setup", "/settings"):
            self.assertEqual(self.get(url).status_code, 303, url)
        for url in ("/policy", "/policy/edit", "/policy/preview", "/setup/block/web", "/setup/sources",
                    "/setup/sources/films/delete", "/setup/test-parse", "/setup/providers/northwind/check",
                    "/setup/providers/link", "/setup/formats", "/setup/formats/card", "/setup/formats/card/delete",
                    "/setup/block/integrations.tmdb/test", "/settings/cookies/northwind"):
            with self.subTest(url=url):
                self.assertEqual(self.post(url, data={"auto_approve": "on"}).status_code, 401)
        self.unchanged()

    def test_another_site_cannot_save(self) -> None:
        self.sign_in()
        resp = self.post("/policy", data={"auto_approve": "on"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(resp.status_code, 403)
        self.unchanged()


# ---------------------------------------------------------------------------
# Setup: the page
# ---------------------------------------------------------------------------


class SetupPageTests(ConfigCase):
    def test_the_page_is_served_at_both_addresses_with_every_section(self) -> None:
        for url in ("/setup", "/settings"):
            page = self.get(url)
            self.assertEqual(page.status_code, 200)
            for anchor, label in setup_routes.SECTIONS:
                self.assertIn(f'<section class="setup-section" id="{anchor}"', page.text)
                self.assertIn(f'<a href="#{anchor}">{label}</a>', page.text)

    def test_opening_the_page_asks_no_tracker_and_no_service(self) -> None:
        with mock.patch.object(search_service, "check_session", mock.AsyncMock()) as check, \
                mock.patch.object(connections, "test", mock.AsyncMock()) as test, \
                mock.patch.object(search_service, "get_trackers", side_effect=AssertionError("asked")), \
                mock.patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("sent")):
            self.assertEqual(self.get("/setup").status_code, 200)
        check.assert_not_awaited()
        test.assert_not_awaited()

    def test_no_secret_is_on_the_page(self) -> None:
        page = self.get("/setup").text
        for value in (TMDB_VALUE, PROWLARR_VALUE, *ENV.values()):
            self.assertNotIn(value, page)
        read = text_of(page)
        self.assertIn("API key ✓ set, from ${TMDB_KEY}", read)
        self.assertIn("Password ○ not set", read)
        self.assertEqual(re.findall(r'<input class="input" type="password"[^>]*value=', page), [])

    def test_the_checklist_goes_once_nothing_is_missing(self) -> None:
        read = text_of(self.get("/setup").text)
        self.assertIn("Before it does anything ✓ Done A source ✓ Done A TMDB key ✓ Done Somewhere to search "
                      "✓ Done A download client ✓ Done A rule ✗ Missing A sign-in for these pages . No web.auth", read)
        self.path.write_text(self.before.replace("  port: 4850\n", "  port: 4850\n  auth:\n    trusted_header: Remote-User\n"))
        self.assertNotIn("Before it does anything", self.get("/setup").text)

    def test_a_source_is_listed_with_what_it_does(self) -> None:
        read = text_of(self.get("/setup").text)
        self.assertIn("films userbot Chat -100123 Posts from before since the last one read Read with card "
                      "Messages from 2 listed user ids Own torrents from the reviewers After a download the post is "
                      "left alone After a reject the post is deleted", read)


class EmptyConfigTests(ConfigCase):
    CONFIG = "web:\n  port: 4850\n"

    def test_the_checklist_names_what_is_missing(self) -> None:
        read = text_of(self.get("/setup").text)
        self.assertIn("Before it does anything", read)
        for line in (
            "✗ Missing A source . No source yet, so nothing arrives.",
            "✗ Missing A TMDB key . No TMDB key, so no title is looked up",
            "✗ Missing Somewhere to search . No Prowlarr and no provider",
            "✗ Missing A download client . No qBittorrent",
            "✗ Missing A rule . No rule, so every item of a type decided by the rules is held for you.",
            "✗ Missing A sign-in for these pages . No web.auth",
        ):
            with self.subTest(line=line):
                self.assertIn(line, read)
        self.assertIn("No source yet.", read)
        self.assertIn("No provider.", read)

    def test_what_is_there_is_ticked(self) -> None:
        checks = {item["name"]: item["done"] for item in setup_routes.checklist(Config.model_validate({
            "integrations": {"tmdb": {"api_key": "k"}, "prowlarr": {"enabled": False}},
            "providers": [{"key": "a", "base_url": "https://a.example", "auth": {"from": "cookie_file", "cookie_file": "/c"}}],
        }))}
        self.assertEqual(checks, {
            "A source": False, "A TMDB key": True, "Somewhere to search": False, "A download client": False,
            "A rule": False, "A sign-in for these pages": False,
        })

    def test_somewhere_to_search_is_what_the_search_itself_needs(self) -> None:
        """A provider whose own search is none was ticked as somewhere to search, and Search then failed."""
        def ticked(provider: dict, prowlarr: bool = False) -> bool:
            config = Config.model_validate({
                "integrations": {"prowlarr": {"url": "http://p", "api_key": "k"}} if prowlarr else {},
                "providers": [{"key": "a", "base_url": "https://a.example",
                               "auth": {"from": "cookie_file", "cookie_file": "/c"}, **provider}],
            })
            return {i["name"]: i["done"] for i in setup_routes.checklist(config)}["Somewhere to search"]

        self.assertFalse(ticked({"search": "none"}))
        self.assertTrue(ticked({"search": "unit3d"}))
        self.assertTrue(ticked({"search": "none"}, prowlarr=True))

    def test_every_integration_is_folded_with_its_state_on_its_line(self) -> None:
        """Folded and open cards mixed in one grid left its columns ragged."""
        state = configfile.load()
        self.assertTrue(setup_routes.card(state, setup_routes.BLOCKS["integrations.jellyfin"])["folded"])
        # On without a block: the IMDb guide needs no key, and its line says it is on.
        guide = setup_routes.card(state, setup_routes.BLOCKS["integrations.imdb_guide"])
        self.assertEqual((guide["folded"], guide["status"]), (True, ("on", "On")))


# ---------------------------------------------------------------------------
# Setup: sources
# ---------------------------------------------------------------------------


class SourceTests(ConfigCase):
    def adding(self, kind: str) -> dict[str, list[str]]:
        return self.form("/setup", "/setup/sources", lambda d: d["kind"] == [kind] and d["original"] == [""] and "brief" not in d)

    def test_a_userbot_source_is_added_to_the_file(self) -> None:
        data = {**self.adding("userbot"), "key": "series", "chat": "@some_channel", "backfill": "full", "formats": ["scene"],
                "on_download": "react"}
        resp = self.post("/setup/sources", data=data, headers=HTMX)
        self.assertEqual((resp.status_code, resp.headers["HX-Redirect"]), (200, self.saved("sources")))
        added = self.file().sources[1]
        self.assertEqual(
            (added.key, added.kind, added.chat, added.backfill, added.formats, added.on_download, added.on_reject),
            ("series", "userbot", "@some_channel", "full", ["scene"], "react", "none"),
        )
        written = loader.plain(loader.read_document(self.path))["sources"][1]
        self.assertEqual(written, {"key": "series", "kind": "userbot", "chat": "@some_channel", "backfill": "full",
                                   "formats": ["scene"], "on_download": "react"})
        self.assertIn("# the main group", self.path.read_text())
        page = self.get(resp.headers["HX-Redirect"]).text
        self.assertIn("Saved to the config file.", page)
        self.assertIn("Restart to apply.", page)

    def test_a_bot_source_takes_private_and_a_numeric_chat_is_written_as_a_number(self) -> None:
        self.post("/setup/sources", data={**self.adding("bot"), "key": "inbox", "chat": "private"})
        self.post("/setup/sources", data={**self.adding("bot"), "key": "club", "chat": "-100555", "notify_on_review": "1"})
        written = loader.plain(loader.read_document(self.path))["sources"][1:]
        self.assertEqual(written, [
            {"key": "inbox", "kind": "bot", "chat": "private"},
            {"key": "club", "kind": "bot", "chat": -100555, "notify_on_review": True},
        ])

    def test_an_error_lands_beside_its_field(self) -> None:
        resp = self.post("/setup/sources", data={**self.adding("userbot"), "key": "Bad Key", "chat": "-1"}, headers=HTMX)
        self.assertEqual(resp.status_code, 200)
        field = tag(resp.text, "source-new-userbot-key")
        self.assertIn('aria-invalid="true"', field)
        self.assertIn('aria-describedby="source-new-userbot-key-error"', field)
        self.assertRegex(resp.text, r'id="source-new-userbot-key-error">.*?Lowercase letters, digits, - and _ only')
        self.assertIn('value="Bad Key"', field)
        self.assertNotIn("HX-Redirect", resp.headers)
        self.unchanged()

    def test_what_a_source_needs_from_another_block_is_said_in_its_form(self) -> None:
        for kind, values, message in (
            ("seerr", {"key": "requests"}, "sources[1].kind: a seerr source needs integrations.seerr"),
            ("userbot", {"key": "x", "chat": ""}, "a userbot source needs a chat"),
            ("userbot", {"key": "x", "chat": "private"}, "chat: private is for bot sources"),
            ("userbot", {"key": "films", "chat": "-5"}, "duplicate key 'films'"),
        ):
            with self.subTest(values=values):
                resp = self.post("/setup/sources", data={**self.adding(kind), **values}, headers=HTMX)
                self.assertNotIn("HX-Redirect", resp.headers)
                self.assertIn(message, text_of(resp.text))
                self.unchanged()

    def test_without_htmx_an_error_is_the_whole_page_with_the_form_open(self) -> None:
        resp = self.post("/setup/sources", data={**self.adding("bot"), "key": "Bad Key", "chat": "private"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("<title>Setup · vettarr</title>", resp.text)
        self.assertIn('aria-invalid="true"', tag(resp.text, "source-new-bot-key"))
        self.assertRegex(resp.text, r'<details class="fold card px-4 pb-1" open>\s*<summary>The bot</summary>')
        ok = self.post("/setup/sources", data={**self.adding("bot"), "key": "inbox", "chat": "private"})
        self.assertEqual((ok.status_code, ok.headers["location"]), (303, self.saved("sources")))

    def test_changing_a_source_keeps_what_the_form_does_not_show(self) -> None:
        data = self.form("/setup", "/setup/sources", lambda d: d["original"] == ["films"])
        self.assertEqual((data["key"], data["allowed_senders"], data["on_reject"]), (["films"], ["listed"], ["delete"]))
        resp = self.post("/setup/sources", data={**data, "on_reject": "react", "formats": ["card", "scene"]}, headers=HTMX)
        self.assertIn("HX-Redirect", resp.headers)
        source = self.file().sources[0]
        self.assertEqual((source.on_reject, source.formats, source.allowed_senders), ("react", ["card", "scene"], [7, 8]))
        self.assertEqual(len(self.file().sources), 1)
        self.assertIn("- key: films                 # the main group", self.path.read_text())

    def test_changing_a_seerr_source_with_the_secret_left_empty_keeps_the_one_written(self) -> None:
        secret = "-".join(("hook", "secret", "nobody", "sees"))
        text = self.before.replace(
            "formats:\n", "  - key: requests\n    kind: seerr\n    webhook_secret: ${HOOK_SECRET}\n    poll_minutes: 5\nformats:\n", 1,
        ).replace("integrations:\n", "integrations:\n  seerr:\n    url: http://seerr:5055\n    api_key: ${TMDB_KEY}\n", 1)
        with mock.patch.dict(os.environ, {"HOOK_SECRET": secret}):
            self.path.write_text(text)
            self.running = self.file()
            self.assertEqual(self.running.sources[1].webhook_secret.get_secret_value(), secret)
            data = self.form("/setup", "/setup/sources", lambda d: d["original"] == ["requests"])
            resp = self.post("/setup/sources", data={**data, "poll_minutes": ["9"]}, headers=HTMX)
            self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("sources"), resp.text)
            self.assertEqual(loader.plain(loader.read_document(self.path))["sources"][1], {
                "key": "requests", "kind": "seerr", "webhook_secret": "${HOOK_SECRET}", "poll_minutes": 9,
            })
            self.assertNotIn(secret, self.get("/setup").text)

    def test_a_seerr_webhook_secret_is_shown_as_set_never_as_its_value_and_written_only_when_typed(self) -> None:
        secret = "-".join(("hook", "secret", "nobody", "sees"))
        typed = "-".join(("a", "new", "secret", "typed", "here"))
        text = self.before.replace(
            "formats:\n", "  - key: requests\n    kind: seerr\n    webhook_secret: ${HOOK_SECRET}\nformats:\n", 1,
        ).replace("integrations:\n", "integrations:\n  seerr:\n    url: http://seerr:5055\n    api_key: ${TMDB_KEY}\n", 1)
        with mock.patch.dict(os.environ, {"HOOK_SECRET": secret}):
            self.path.write_text(text)
            self.running = self.file()
            page = self.get("/setup").text
            self.assertNotIn(secret, page)
            read = text_of(page)
            self.assertIn("secret set, from ${HOOK_SECRET} /hooks/seerr/requests", read)
            self.assertIn('type="password" id="source-requests-webhook_secret"', page)

            def written() -> dict:
                return loader.plain(loader.read_document(self.path))["sources"][1]

            data = self.form("/setup", "/setup/sources", lambda d: d["original"] == ["requests"])
            self.assertEqual(data.get("webhook_secret", [""]), [""])
            resp = self.post("/setup/sources", data={**data, "webhook_secret": ["short"]}, headers=HTMX)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("at least 16 characters", text_of(resp.text))
            self.assertNotIn("short", re.findall(r'<input[^>]*webhook_secret[^>]*>', resp.text)[0])
            self.assertEqual(written()["webhook_secret"], "${HOOK_SECRET}")

            resp = self.post("/setup/sources", data={**data, "webhook_secret": [typed]}, headers=HTMX)
            self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("sources"), resp.text)
            self.assertEqual(written()["webhook_secret"], typed)
            self.running = self.file()
            self.assertNotIn(typed, self.get("/setup").text)

            data = self.form("/setup", "/setup/sources", lambda d: d["original"] == ["requests"])
            resp = self.post("/setup/sources", data={**data, "webhook_secret-clear": ["1"]}, headers=HTMX)
            self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("sources"), resp.text)
            self.assertNotIn("webhook_secret", written())
            self.running = self.file()
            self.assertIn("off: no secret set, the poll alone reads Seerr", text_of(self.get("/setup").text))

    def test_a_source_is_removed_after_a_question_and_not_while_a_rule_names_it(self) -> None:
        page = self.get("/setup").text
        remove = re.search(r'<form[^>]*action="/setup/sources/films/delete"[^>]*>', page).group(0)
        self.assertIn('data-confirm-title="Remove the source films?"', remove)
        self.assertIn("data-confirm=", remove)
        self.path.write_text(self.before.replace(
            "  rules:\n", "  rules:\n    - id: from-films\n      check: source\n      any_of: [films]\n"))
        refused = self.post("/setup/sources/films/delete", headers=HTMX)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("films was not removed.", refused.text)
        self.assertEqual(len(self.file().sources), 1)
        self.path.write_text(self.before)
        gone = self.post("/setup/sources/films/delete", headers=HTMX)
        self.assertEqual(gone.headers["HX-Redirect"], self.saved("sources"))
        self.assertEqual(self.file().sources, [])
        self.assertEqual(self.post("/setup/sources/films/delete", headers=HTMX).status_code, 404)


class BotChatHintTests(ConfigCase):
    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def event(self, minutes_ago, chat_id, state, **payload):
        return SimpleNamespace(at=self.NOW - timedelta(minutes=minutes_ago),
                               payload={"chat_id": chat_id, "state": state, **payload})

    def test_only_a_chat_s_latest_event_counts_and_only_a_join(self) -> None:
        rows = sorted((
            self.event(50, -1, "joined", title="Film club", is_admin=False, reads_all_messages=False),
            self.event(40, -2, "joined", title="Left since", is_admin=True),
            self.event(10, -2, "left"),
            self.event(30, -3, "left"),
            self.event(5, -3, "joined", title="Back again", is_admin=True),
            SimpleNamespace(at=self.NOW, payload=None),
        ), key=lambda row: row.at, reverse=True)
        self.assertEqual(setup_routes.chats_from(rows), [
            {"chat_id": -3, "title": "Back again", "is_admin": True, "reads_all": True},
            {"chat_id": -1, "title": "Film club", "is_admin": False, "reads_all": False},
        ])

    def test_a_chat_that_is_already_a_source_is_not_offered(self) -> None:
        chats = [{"chat_id": -100123, "title": "Films", "is_admin": True, "reads_all": True},
                 {"chat_id": -9, "title": "Film club!", "is_admin": False, "reads_all": False}]
        config = Config.model_validate({
            "telegram": {"bot_token": "t"},
            "sources": [{"key": "film-club", "kind": "bot", "chat": -100123}],
        })
        self.assertEqual([(h["chat_id"], h["key"]) for h in setup_routes.hints(chats, config)], [(-9, "film-club-2")])

    def test_the_events_table_is_what_is_read_and_a_database_that_is_down_shows_no_offer(self) -> None:
        rows = [self.event(5, -7, "joined", title="Film club")]

        class _Session:
            async def execute(self, statement):
                assert "events.kind" in str(statement.compile(dialect=postgresql.dialect()))
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        # BOT_CHATS is the route module's own function; the case replaces the name the page calls.
        with mock.patch.object(setup_routes, "async_session", lambda: _Session()):
            self.assertEqual([c["chat_id"] for c in asyncio.run(BOT_CHATS())], [-7])

        def broken():
            raise OSError("connection refused")

        with mock.patch.object(setup_routes, "async_session", broken):
            self.assertEqual(asyncio.run(BOT_CHATS()), [])

    def test_the_page_offers_the_chat_and_says_what_the_bot_can_see_there(self) -> None:
        setup_routes.bot_chats.return_value = [
            {"chat_id": -100777, "title": "Film club", "is_admin": False, "reads_all": False},
        ]
        page = self.get("/setup").text
        read = text_of(page)
        self.assertIn("The bot was added to Film club. Use it as a source?", read)
        self.assertIn("Privacy mode is on and it is not an admin, so it only sees commands and replies to it.", read)
        offer = self.form("/setup", "/setup/sources", lambda d: "brief" in d)
        self.assertEqual((offer["kind"], offer["chat"], offer["key"]), (["bot"], ["-100777"], ["film-club"]))
        resp = self.post("/setup/sources", data=offer, headers=HTMX)
        self.assertEqual(resp.headers["HX-Redirect"], self.saved("sources"))
        added = self.file().sources[1]
        self.assertEqual((added.key, added.kind, added.chat), ("film-club", "bot", -100777))
        # Once it is a source it is no longer on offer.
        self.running = self.file()
        self.assertNotIn("Use it as a source?", self.get("/setup").text)

    def test_an_admin_and_a_bot_with_privacy_mode_off_are_told_apart(self) -> None:
        setup_routes.bot_chats.return_value = [
            {"chat_id": -1, "title": "A", "is_admin": True, "reads_all": True},
            {"chat_id": -2, "title": "B", "is_admin": False, "reads_all": True},
        ]
        read = text_of(self.get("/setup").text)
        self.assertIn("It is an admin there, so it sees every message and can delete or react to a post.", read)
        self.assertIn("Privacy mode is off, so it sees every message. It is not an admin, so it cannot delete a post.", read)

    def test_an_offer_that_cannot_be_added_says_why_in_one_line(self) -> None:
        resp = self.post("/setup/sources", data={"brief": "1", "original": "", "kind": "bot", "chat": "-5", "key": "films"},
                         headers=HTMX)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("Not added. duplicate key &#x27;films&#x27;", resp.text)
        self.unchanged()

    def test_the_bot_being_added_and_leaving_are_events(self) -> None:
        while events.pending():
            events._pending.get_nowait()
        self.addCleanup(ingest._bot_chats.clear)
        ingest.bot_added(-100321, "Film club", is_admin=False, reads_all_messages=True)
        asyncio.run(ingest.bot_removed(-100321, self.running))
        queued = []
        while events.pending():
            _at, kind, item_id, payload = events._pending.get_nowait()
            queued.append((kind, item_id, payload))
        self.assertEqual(queued, [
            (events.BOT_CHAT, None, {"chat_id": -100321, "title": "Film club", "state": "joined", "is_admin": False,
                                     "reads_all_messages": True}),
            (events.BOT_CHAT, None, {"chat_id": -100321, "state": "left"}),
        ])


    def queued(self) -> list:
        out = []
        while events.pending():
            _at, kind, _item_id, payload = events._pending.get_nowait()
            out.append((kind, payload))
        return out

    def test_a_chat_nobody_here_knew_leaves_without_an_event(self) -> None:
        # Anyone can add the bot to a chat of theirs and take it out again: that is no row and no notification.
        self.queued()
        with mock.patch.object(ingest, "_join_stored", mock.AsyncMock(return_value=False)) as stored:
            for _ in range(3):
                asyncio.run(ingest.bot_removed(-555, self.running))
        self.assertEqual(stored.await_count, 3)
        self.assertEqual(self.queued(), [])

    def test_leaving_a_chat_an_earlier_start_joined_or_a_bot_source_reads_is_an_event(self) -> None:
        self.queued()
        with mock.patch.object(ingest, "_join_stored", mock.AsyncMock(return_value=True)):
            asyncio.run(ingest.bot_removed(-556, self.running))
        self.assertEqual(self.queued(), [(events.BOT_CHAT, {"chat_id": -556, "state": "left"})])
        reads = Config.model_validate({
            "telegram": {"bot_token": "t"}, "sources": [{"key": "club", "kind": "bot", "chat": -100777}],
        })
        with mock.patch.object(ingest, "_join_stored", mock.AsyncMock(return_value=False)) as stored:
            asyncio.run(ingest.bot_removed(-100777, reads))
            asyncio.run(ingest.bot_removed(-100778, reads))
        stored.assert_awaited_once_with(-100778)
        self.assertEqual(self.queued(), [(events.BOT_CHAT, {"chat_id": -100777, "state": "left"})])

    def test_a_stored_join_is_the_chat_s_last_event_and_a_database_that_is_down_is_no_join(self) -> None:
        asked = []

        def session(payload):
            class _Session:
                async def execute(self, statement):
                    asked.append(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
                    return SimpleNamespace(scalar_one_or_none=lambda: payload)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _Session

        for payload, joined in (({"chat_id": -9, "state": "joined"}, True), ({"chat_id": -9, "state": "left"}, False), (None, False)):
            with self.subTest(payload=payload), mock.patch.object(ingest, "async_session", session(payload)):
                self.assertIs(asyncio.run(ingest._join_stored(-9)), joined)
        self.assertIn("(events.payload ->> 'chat_id') = '-9'", asked[0])
        self.assertIn("ORDER BY events.at DESC, events.id DESC", asked[0])
        self.assertIn("LIMIT 1", asked[0])

        def broken():
            raise OSError("connection refused")

        with mock.patch.object(ingest, "async_session", broken):
            self.assertIs(asyncio.run(ingest._join_stored(-9)), False)

    def test_one_row_a_chat_is_read_so_no_run_of_events_pushes_a_join_out(self) -> None:
        sql = str(setup_routes.latest_bot_chats().compile(dialect=postgresql.dialect()))
        self.assertRegex(sql, r"row_number\(\) OVER \(PARTITION BY \(events\.payload ->> \S+\) "
                              r"ORDER BY events\.at DESC, events\.id DESC\) AS rank")
        self.assertRegex(sql, r"WHERE \w+\.rank = ")
        self.assertNotIn("LIMIT", sql)
        # They come back in the order of the chats; the page lists the newest join first.
        rows = [self.event(50, -1, "joined", title="Older"), self.event(5, -2, "joined", title="Newer"),
                self.event(1, -3, "left")]
        self.assertEqual([c["title"] for c in setup_routes.chats_from(rows)], ["Newer", "Older"])


BOT_CHATS = setup_routes.bot_chats


class ManagedHintTests(ConfigCase):
    READONLY = True

    def test_a_file_the_page_cannot_write_gets_no_offer(self) -> None:
        setup_routes.bot_chats.return_value = [{"chat_id": -1, "title": "Film club", "is_admin": True, "reads_all": True}]
        self.assertNotIn("Use it as a source?", self.get("/setup").text)


# ---------------------------------------------------------------------------
# Setup: reading a post
# ---------------------------------------------------------------------------

CARD_POST = "NEW MOVIE\nTitle: The Lantern Keeper\nType: Movie - 1080p - WEB-DL - 4.2 GB\nhttps://northwind.example/torrents/42"
SCENE_POST = "Quiet.Harbour.2024.1080p.WEB-DL.x264-GROUP"


class EnvironmentValueTests(ConfigCase):
    """Web's Port showed the literal ${WEB_PORT} in an editable field."""

    CONFIG = "web:\n  port: ${WEB_PORT}\n  bind: 127.0.0.1\n"

    def setUp(self) -> None:
        patched = mock.patch.dict(os.environ, {"WEB_PORT": "4850"})
        patched.start()
        self.addCleanup(patched.stop)
        super().setUp()

    def test_a_value_from_the_environment_is_a_line_not_a_field(self) -> None:
        page = self.get("/setup").text
        card = page[page.index('id="card-web"'):]
        card = card[:card.index("</form>")]
        self.assertIn("From the environment: <code>WEB_PORT</code>", card)
        self.assertNotIn('name="port"', card)
        self.assertNotIn("${WEB_PORT}", card)

    def test_a_save_of_its_block_leaves_it_as_written(self) -> None:
        data = self.form("/setup", "/setup/block/web")
        self.assertNotIn("port", data)
        resp = self.post("/setup/block/web", data={**data, "bind": ["0.0.0.0"]}, headers=HTMX)
        self.assertEqual(resp.headers.get("HX-Redirect"), self.saved("web"), resp.text)
        self.assertIn("port: ${WEB_PORT}", self.path.read_text())
        self.assertIn("bind: 0.0.0.0", self.path.read_text())


class TryAPostFromTheInboxTests(ConfigCase):
    def test_teach_a_format_opens_try_a_post_with_the_post_in_it(self) -> None:
        with mock.patch.object(setup_routes, "_post_to_try", mock.AsyncMock(return_value="thanks, that one worked")):
            page = self.get("/setup?try=18").text
        self.assertIn('name="text" spellcheck="false">thanks, that one worked</textarea>', page)
        self.assertIn("The post from the Inbox is below.", page)
        self.assertIn('id="try-a-post"', page)

    def test_only_an_item_number_is_read(self) -> None:
        for asked in ("", "abc", "-1", "1e3", "12345678901", "²"):
            with self.subTest(asked=asked):
                request = SimpleNamespace(query_params={"try": asked})
                self.assertEqual(asyncio.run(setup_routes._post_to_try(request)), "")

    def test_a_card_post_read_by_its_first_line_is_a_warning(self) -> None:
        """A demo post read as "🎬 Film" by the release-name reader showed a green tick."""
        post = "\U0001f3ac Film\nTitle: Night Ferry (2025)\nQuality: 720p WEB-DL\nSize: 7.1 GB"
        self.assertEqual(setup_routes.junk_title(post, "\U0001f3ac Film"), "Title")
        self.assertIsNone(setup_routes.junk_title(post, "Night Ferry"))
        self.assertIsNone(setup_routes.junk_title("Night.Ferry.2025.720p.WEB-DL", "Night Ferry"))
        parsed = {"read_by": "the release-name reader", "label": "Title", "media_type": "movie",
                  "ignored": None, "fields": [], "linked": None}
        shown = text_of(ui.templates.get_template("setup/_parsed.html").render(parsed=parsed))
        self.assertIn("⚠ Read by the release-name reader , but its title is the post's first line", shown)
        self.assertNotIn("✓", shown)


class TestParseTests(ConfigCase):
    def parse(self, text: str, source: str = "films") -> str:
        resp = self.post("/setup/test-parse", data={"source": source, "text": text}, headers=HTMX)
        self.assertEqual(resp.status_code, 200)
        return text_of(resp.text)

    def test_a_post_is_read_with_its_source_s_formats_and_the_fields_are_shown(self) -> None:
        read = self.parse(CARD_POST)
        self.assertIn("✓ Read by card : it would arrive as a movie.", read)
        self.assertIn("Type movie Title The Lantern Keeper Quality 1080P Source WEB-DL Size 4.2 GB", read)
        self.assertIn("Links https://northwind.example/torrents/42", read)
        self.assertIn("Its link is torrent 42 on northwind : that exact torrent is fetched, with no search.", read)
        self.unchanged()

    def test_a_source_reads_with_its_own_formats(self) -> None:
        with mock.patch.object(setup_routes, "formats_for", wraps=setup_routes.formats_for) as formats_for:
            self.assertIn("it would arrive as a movie.", self.parse(SCENE_POST))
            self.parse(SCENE_POST, source="")
        self.assertEqual([call.args[1] for call in formats_for.call_args_list], ["films", None])
        # films lists the card format alone; with no source every format of the file is tried.
        config = self.file()
        self.assertEqual([f.key for f in setup_routes.formats_for(config, "films")], ["card"])
        self.assertEqual([f.key for f in setup_routes.formats_for(config, None)], ["card"])
        self.assertEqual([f.key for f in setup_routes.formats_for(config, "no-such-source")], ["card", "scene"])

    def test_what_no_format_reads_is_said_to_land_in_parse_failed(self) -> None:
        self.assertIn("✗ No format reads both a title and a type in it. It would land in Parse failed",
                      self.parse("somebody remind me on friday"))

    def test_what_is_not_media_is_said_to_be_ignored(self) -> None:
        self.assertIn("⚠ Not media: instagram.com link, not a media post. It would be ignored",
                      self.parse("https://instagram.com/p/abc"))
        self.assertIn("Paste a post first.", self.parse("   "))

    def test_the_page_has_the_box_with_the_sources_to_choose_from(self) -> None:
        page = self.get("/setup").text
        self.assertIn('<label class="label" for="parse-text">The post</label>', page)
        self.assertRegex(page, r'<select class="input" id="parse-source" name="source">\s*<option value="films">films</option>')


# ---------------------------------------------------------------------------
# Setup: providers
# ---------------------------------------------------------------------------


class ProviderTests(ConfigCase):
    def test_a_provider_shows_its_cookie_its_guard_and_what_its_patterns_read(self) -> None:
        page = self.get("/setup").text
        read = text_of(page)
        # No cookie is no session: a warning, not a green tick, and pasting cookies comes first, open.
        self.assertIn("northwind ⚠ No session: its cookie is not set Address https://northwind.example "
                      "Session a cookie file Cookie ✗ not set", read)
        card = page[page.index('id="provider-northwind"'):]
        self.assertLess(card.index("<summary>Paste new cookies</summary>"), card.index(">Check now</button>"))
        self.assertIn('<details class="fold mt-1" open>', card[:card.index("<summary>Paste new cookies")])
        self.assertIn("✓ reads id 42 in https://northwind.example/torrents/42 , fetches "
                      "https://northwind.example/torrents/download/42", read)
        self.cookies.mkdir()
        (self.cookies / "northwind.txt").write_text(f"# Netscape\n.northwind.example\tTRUE\t/\tTRUE\t0\tsid\t{COOKIE_VALUE}\n")
        self.failing.return_value = [shell.Failing("northwind", "the session cookie was refused", 48)]
        page = self.get("/setup").text
        read = text_of(page)
        self.assertIn("Cookie ✓ set , written just now", read)
        self.assertIn("✗ Paused: the session cookie was refused. Nothing is sent for another 48 min.", read)
        self.assertNotIn(COOKIE_VALUE, page)
        # Refused: the fix is new cookies, so they come first and open, as when none were set.
        card = page[page.index('id="provider-northwind"'):]
        self.assertLess(card.index("<summary>Paste new cookies</summary>"), card.index(">Check now</button>"))
        # Set and not refused: Check now first, the paste folded.
        self.failing.return_value = []
        card = self.get("/setup").text
        card = card[card.index('id="provider-northwind"'):]
        self.assertLess(card.index(">Check now</button>"), card.index("<summary>Paste new cookies</summary>"))
        self.assertIn("✓ No cooldown open", text_of(card[:card.index("Address")]))

    def test_check_now_asks_the_tracker_once_and_only_on_the_click(self) -> None:
        for live, line in (
            (True, "Works: Signed in."),
            (False, "Failed: Not signed in: the tracker answered with its login page, an error, or not at all."),
            (None, "Note: The app has no session for it"),
        ):
            with self.subTest(live=live), \
                    mock.patch.object(search_service, "check_session", mock.AsyncMock(return_value=live)) as check:
                resp = self.post("/setup/providers/northwind/check", headers=HTMX)
                check.assert_awaited_once_with("northwind")
                self.assertIn(line, text_of(resp.text))

    def test_a_provider_in_a_cooldown_is_not_asked(self) -> None:
        self.failing.return_value = [shell.Failing("northwind", "HTTP 429", 12)]
        with mock.patch.object(search_service, "check_session", mock.AsyncMock()) as check:
            resp = self.post("/setup/providers/northwind/check", headers=HTMX)
        check.assert_not_awaited()
        self.assertIn("Not asked: HTTP 429, so nothing is sent there for another 12 min.", resp.text)

    def test_the_session_check_goes_through_the_provider_s_own_client(self) -> None:
        provider = self.running.providers[0]
        client = mock.Mock(check_session=mock.Mock(return_value=True))
        with mock.patch.object(search_service, "_provider", return_value=provider), \
                mock.patch.object(search_service, "_with_session", mock.AsyncMock(return_value=client)) as session:
            self.assertIs(asyncio.run(search_service.check_session("northwind")), True)
            session.assert_awaited_once_with(provider)
            session.return_value = None
            self.assertIsNone(asyncio.run(search_service.check_session("northwind")))
            session.return_value = mock.Mock(check_session=mock.Mock(side_effect=OSError("down")))
            self.assertIs(asyncio.run(search_service.check_session("northwind")), False)
        with mock.patch.object(search_service, "_provider", return_value=None):
            self.assertIsNone(asyncio.run(search_service.check_session("nobody")))

    def test_a_pasted_link_is_read_with_the_providers_patterns(self) -> None:
        for url, line in (
            ("https://northwind.example/torrents/977", "Works: northwind reads id 977 and would fetch "
                                                       "https://northwind.example/torrents/download/977"),
            ("https://northwind.example/forum/3", "Failed: It is on northwind, and none of its link patterns reads an id in it."),
            ("https://www.northwind.example/torrents/9", "Failed: It is on none of the providers' hosts."),
            ("", "Note: Paste a link first."),
        ):
            with self.subTest(url=url):
                resp = self.post("/setup/providers/link", data={"url": url}, headers=HTMX)
                self.assertIn(line, text_of(resp.text))

    def test_a_passkey_in_a_download_address_and_a_password_in_the_base_address_are_not_shown(self) -> None:
        self.path.write_text(
            self.before.replace("base_url: https://northwind.example", f"base_url: https://member:{URL_PASSWORD}@northwind.example")
            .replace("{base}/torrents/download/{id}", "{base}/download.php?id={id}&passkey=" + PASSKEY)
        )
        self.running = self.file()
        self.assertIn(PASSKEY, self.running.providers[0].links[0].download_url)
        page = self.get("/setup").text
        self.assertIn("✓ reads id 42 in https://northwind.example/torrents/42 , fetches "
                      "https://northwind.example/download.php?id=42&passkey=...", text_of(page))
        self.assertIn("Address https://northwind.example Session", text_of(page))
        resp = self.post("/setup/providers/link", data={"url": "https://northwind.example/torrents/977"}, headers=HTMX)
        self.assertIn("Works: northwind reads id 977 and would fetch https://northwind.example/download.php?id=977&passkey=...",
                      text_of(resp.text))
        for shown in (page, resp.text):
            self.assertNotIn(PASSKEY, shown)
            self.assertNotIn(URL_PASSWORD, shown)

    def test_pasted_cookies_are_written_to_the_cookie_file_and_never_shown_back(self) -> None:
        export = (
            '{"cookies": [{"domain": ".northwind.example", "name": "sid", "value": "%s", "path": "/", '
            '"secure": true, "httpOnly": true, "expirationDate": 1900000000.5}, {"oops": 1}, '
            '{"domain": "northwind.example", "name": "theme", "value": "dark", "expirationDate": "session"}]}' % COOKIE_VALUE
        )
        with mock.patch.object(search_service, "get_trackers", return_value=[]):
            resp = self.post("/settings/cookies/northwind", data={"cookies_json": export}, headers=HTMX)
        self.assertIn("Cookies saved.", resp.text)
        self.assertNotIn(COOKIE_VALUE, resp.text)
        written = (self.cookies / "northwind.txt").read_text()
        self.assertIn(f"#HttpOnly_.northwind.example\tTRUE\t/\tTRUE\t1900000000\tsid\t{COOKIE_VALUE}\n", written)
        # A cookie with no date a number can hold is a session cookie, not an error.
        self.assertIn("northwind.example\tFALSE\t/\tFALSE\t0\ttheme\tdark\n", written)
        self.unchanged()

    def test_saved_cookies_are_tried_once_against_the_tracker(self) -> None:
        for live, line in ((True, "Works: Cookies saved, and the tracker accepts them."),
                           (False, "Note: Cookies saved, but the tracker does not accept them.")):
            tracker = mock.Mock(check_session=mock.Mock(return_value=live))
            tracker.name = "northwind"
            with self.subTest(live=live), mock.patch.object(search_service, "get_trackers", return_value=[tracker]):
                resp = self.post("/settings/cookies/northwind", data={"cookies_json": '[{"name": "a", "value": "b"}]'})
                self.assertIn(line, text_of(resp.text))
                tracker.check_session.assert_called_once_with()
                self.assertIs(tracker._cookies_loaded, False)

    def test_what_is_not_a_cookie_export_is_refused_in_words(self) -> None:
        for body, line in (("not json", "That is not JSON"), ("[]", "Expected a JSON list of cookies"),
                           ('"text"', "Expected a JSON list of cookies")):
            resp = self.post("/settings/cookies/northwind", data={"cookies_json": body})
            self.assertIn(line, resp.text)
        self.assertIn("no provider by that name", self.post("/settings/cookies/nobody", data={"cookies_json": "[]"}).text)
        self.assertFalse(self.cookies.exists())


# ---------------------------------------------------------------------------
# Setup: integrations and the other simple blocks
# ---------------------------------------------------------------------------


class BlockTests(ConfigCase):
    def block(self, path: str) -> dict[str, list[str]]:
        return self.form("/setup", f"/setup/block/{path}")

    def save(self, path: str, headers=HTMX, **changes):
        data = self.block(path)
        for name, value in changes.items():
            if value is None:
                data.pop(name, None)
            else:
                data[name] = [value]
        return self.post(f"/setup/block/{path}", data=data, headers=headers)

    def written(self, path: str):
        node = loader.plain(loader.read_document(self.path))
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def test_every_block_of_the_file_has_a_form(self) -> None:
        page = self.get("/setup").text
        for path, block in setup_routes.BLOCKS.items():
            with self.subTest(path=path):
                self.assertIn(f'action="/setup/block/{path}"', page)
                self.assertIn(f'id="card-{block.slug}"', page)
        self.assertEqual(
            {path.split(".")[1] for path in setup_routes.BLOCKS if path.startswith("integrations.")},
            set(Config.model_fields["integrations"].annotation.model_fields),
        )

    def test_an_integration_is_saved_and_an_empty_secret_field_keeps_the_secret(self) -> None:
        resp = self.save("integrations.prowlarr", url="http://prowlarr.lan:9696")
        self.assertEqual(resp.headers["HX-Redirect"], self.saved("integrations"))
        self.assertEqual(self.written("integrations.prowlarr"), {"url": "http://prowlarr.lan:9696", "api_key": PROWLARR_VALUE})
        self.save("integrations.prowlarr", api_key=NEW_VALUE)
        self.assertEqual(self.written("integrations.prowlarr")["api_key"], NEW_VALUE)
        self.assertNotIn(NEW_VALUE, self.get("/setup").text)
        # A reference stays a reference.
        self.save("integrations.tmdb", language="es-ES")
        self.assertEqual(self.written("integrations.tmdb"), {"api_key": "${TMDB_KEY}", "language": "es-ES"})

    def test_unticking_enabled_is_written_and_ticking_it_again_too(self) -> None:
        self.save("integrations.qbittorrent", enabled=None)
        self.assertEqual(self.written("integrations.qbittorrent"), {"url": "http://qbittorrent:8080", "enabled": False})
        self.assertFalse(self.file().integrations.enabled("qbittorrent"))
        self.save("integrations.qbittorrent", enabled="1")
        self.assertTrue(self.file().integrations.enabled("qbittorrent"))
        self.save("integrations.imdb_guide", enabled=None, min_votes="35")
        self.assertEqual(self.written("integrations.imdb_guide"), {"enabled": False, "min_votes": 35})

    def test_a_block_nobody_filled_in_is_not_made(self) -> None:
        resp = self.save("integrations.jellyfin")
        self.assertIn("HX-Redirect", resp.headers)
        self.assertIsNone(self.written("integrations.jellyfin"))
        self.save("integrations.jellyfin", url="http://jellyfin:8096", api_key=NEW_VALUE, enabled="1")
        self.assertEqual(self.written("integrations.jellyfin"), {"url": "http://jellyfin:8096", "api_key": NEW_VALUE})

    def test_an_error_lands_beside_its_field_or_on_the_card(self) -> None:
        resp = self.save("integrations.prowlarr", url="prowlarr:9696")
        self.assertEqual(resp.status_code, 200)
        field = tag(resp.text, "integrations-prowlarr-url")
        self.assertIn('aria-invalid="true"', field)
        self.assertIn('aria-describedby="integrations-prowlarr-url-error"', field)
        self.assertIn('value="prowlarr:9696"', field)
        self.assertRegex(resp.text, r'id="integrations-prowlarr-url-error">.*?An address starts with http:// or https://')
        self.assertNotIn("should match pattern", resp.text)
        self.assertTrue(resp.text.lstrip().startswith("<div"))
        self.assertIn('id="card-integrations-prowlarr"', resp.text)
        whole = self.save("integrations.jellyfin", enabled="1")
        self.assertIn("Not saved.", whole.text)
        self.assertIn("enabled, so it needs url, api_key", whole.text)
        self.unchanged()

    def test_without_htmx_an_error_is_the_whole_page(self) -> None:
        resp = self.save("web", headers={}, port="99999")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("<title>Setup · vettarr</title>", resp.text)
        self.assertIn('aria-invalid="true"', tag(resp.text, "web-port"))
        self.unchanged()
        ok = self.save("web", headers={}, base_url="https://vettarr.example")
        self.assertEqual((ok.status_code, ok.headers["location"]), (303, self.saved("web")))
        self.assertEqual(self.written("web"), {"port": 4850, "base_url": "https://vettarr.example"})

    def test_each_save_has_its_own_address_so_a_second_one_loads_the_page_again(self) -> None:
        # A browser sent to the address it is already at only scrolls: the old page would stay.
        first = self.save("web", port="4851").headers["HX-Redirect"]
        self.assertEqual(first, self.saved("web"))
        self.assertIn("Saved to the config file.", self.get(first).text)
        second = self.save("web", port="4852").headers["HX-Redirect"]
        self.assertEqual(second, self.saved("web"))
        self.assertNotEqual(second, first)
        self.assertIn("Saved to the config file.", self.get(second).text)
        # The first address names a file that is no longer the one on disk.
        self.assertNotIn("Saved to the config file.", self.get(first).text)

    def test_an_address_that_names_no_save_shows_no_saved_note(self) -> None:
        for url in ("/setup?saved=web", "/setup?saved=web.", "/setup?saved=web.0123456789ab", "/setup?saved=.", "/setup"):
            with self.subTest(url=url):
                self.assertNotIn("Saved to the config file.", self.get(url).text)
        page = self.get(self.saved("notify")).text
        self.assertEqual(page.count("Saved to the config file."), 1)

    def test_deliver_is_one_choice_a_type_and_its_target_must_be_on(self) -> None:
        self.save("deliver", movie="qbittorrent", ebook="qbittorrent")
        self.assertEqual(self.written("deliver"), {"movie": "qbittorrent", "ebook": "qbittorrent"})
        before = self.path.read_text()
        resp = self.save("deliver", series="sonarr")
        self.assertIn('aria-invalid="true"', tag(resp.text, "deliver-series"))
        self.assertIn("deliver to sonarr needs integrations.sonarr enabled", resp.text)
        self.assertEqual(self.path.read_text(), before)

    def test_nothing_that_is_used_is_said_to_be_unused(self) -> None:
        # deliver, Radarr, Sonarr and Seerr, as a target and as a source, are all acted on.
        read = text_of(self.get("/setup").text)
        for stale in ("not used yet", "Kept in the file", "not read yet"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, read)
        self.assertIn("Pending requests in Seerr, decided by the policy.", read)

    def test_notify_is_an_address_nobody_sees_and_removing_it_removes_the_block(self) -> None:
        address = "https://apprise.example/notify/" + NEW_VALUE
        self.save("notify", url=address)
        self.assertEqual(self.written("notify"), {"url": address})
        page = self.get("/setup").text
        self.assertNotIn(NEW_VALUE, page)
        self.assertIn('name="url-clear"', page)
        self.save("notify", **{"url-clear": "1"})
        self.assertIsNone(self.written("notify"))
        self.assertIsNone(self.file().notify)

    def test_the_web_block_says_how_people_sign_in(self) -> None:
        read = text_of(self.get("/setup").text)
        self.assertIn("Sign-in ✗ none: anyone who reaches the port can approve and reject", read)
        self.assertIn("vettarr web hash-password prints the line for web.auth.password_hash", read)

    def test_telegram_takes_reviewers_as_numbers_and_a_numeric_api_id(self) -> None:
        self.save("telegram", reviewers="7, 8 9", api_id="12345")
        written = self.written("telegram")
        self.assertEqual((written["reviewers"], written["api_id"], written["api_hash"]), ([7, 8, 9], 12345, "${TG_HASH}"))
        resp = self.save("telegram", reviewers="7, someone")
        self.assertIn('aria-invalid="true"', tag(resp.text, "telegram-reviewers"))

    def test_an_address_with_a_password_in_it_is_treated_as_a_secret(self) -> None:
        self.path.write_text(self.before.replace("http://qbittorrent:8080", "http://me:" + NEW_VALUE + "@qbittorrent:8080"))
        page = self.get("/setup").text
        self.assertNotIn(NEW_VALUE, page)
        self.assertIn('type="password"', tag(page, "integrations-qbittorrent-url"))
        self.save("integrations.qbittorrent", username="someone")
        self.assertEqual(self.written("integrations.qbittorrent")["url"], "http://me:" + NEW_VALUE + "@qbittorrent:8080")

    def test_an_unknown_block_is_a_404(self) -> None:
        self.assertEqual(self.post("/setup/block/policy", data={}).status_code, 404)
        self.assertEqual(self.post("/setup/block/integrations.imdb_guide/test").status_code, 404)


class ConnectionTests(ConfigCase):
    def run_test(self, name: str, handler) -> connections.Result:
        return asyncio.run(connections.test(name, self.running, httpx.MockTransport(handler)))

    def test_each_integration_is_asked_once_with_its_own_credential(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.headers.get("x-api-key")))
            if request.url.host == "qbittorrent":
                return httpx.Response(200, text="Ok.")
            return httpx.Response(200, json={"version": "1.2.3"})

        self.assertEqual(self.run_test("prowlarr", handler), connections.Result(True, "Connected: Prowlarr 1.2.3 accepted the API key."))
        self.assertEqual(self.run_test("qbittorrent", handler), connections.Result(True, "Connected: qBittorrent accepted the login."))
        self.assertTrue(self.run_test("tmdb", handler).ok)
        self.assertEqual(seen, [
            ("GET", "http://prowlarr:9696/api/v1/system/status", PROWLARR_VALUE),
            ("POST", "http://qbittorrent:8080/api/v2/auth/login", None),
            ("GET", f"https://api.themoviedb.org/3/configuration?api_key={TMDB_VALUE}", None),
        ])

    def test_a_refusal_a_stranger_and_silence_are_told_apart_without_the_address_or_the_key(self) -> None:
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        def refused(request):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        for handler, line in (
            (lambda r: httpx.Response(401), "It answered, and refused the API key (HTTP 401)."),
            (lambda r: httpx.Response(404), "Something answered at that address with HTTP 404; it may not be Prowlarr."),
            (timeout, "No answer within 8 seconds."),
            (refused, "Could not connect (ConnectError). Is the address right, and reachable from this container?"),
        ):
            result = self.run_test("prowlarr", handler)
            self.assertEqual((result.ok, result.text), (False, line))
            self.assertNotIn("prowlarr:9696", result.text)
            self.assertNotIn(PROWLARR_VALUE, result.text)
        wrong = self.run_test("qbittorrent", lambda r: httpx.Response(200, text="Fails."))
        self.assertEqual(wrong.text, "It answered, and refused the username or the password.")

    def test_a_delivery_target_is_asked_by_the_delivery_code_and_answers_in_words(self) -> None:
        from vettarr import delivery
        from vettarr.delivery.http import DeliveryError

        self.path.write_text(self.path.read_text().replace("integrations:\n", (
            "integrations:\n"
            "  radarr: {url: http://radarr:7878, api_key: radarr-key-value}\n"
            "  sonarr: {url: http://sonarr:8989, api_key: sonarr-key-value}\n"
            "  seerr: {url: http://seerr:5055, api_key: seerr-key-value}\n"
        ), 1))
        config = self.file()

        def transport(request):
            raise AssertionError("a delivery target is not asked through the probes")

        answers = {
            "radarr": "5.2.0",
            "sonarr": DeliveryError("Sonarr refused the API key (HTTP 401)", 401),
            "seerr": "unknown",
        }

        async def answer(target, cfg):
            self.assertIs(cfg, config)
            if isinstance(answers[target], Exception):
                raise answers[target]
            return answers[target]

        with mock.patch.object(delivery, "test_connection", mock.AsyncMock(side_effect=answer)) as called:
            got = {name: asyncio.run(connections.test(name, config, httpx.MockTransport(transport))) for name in answers}
        self.assertEqual([c.args[0] for c in called.await_args_list], ["radarr", "sonarr", "seerr"])
        self.assertEqual(got, {
            "radarr": connections.Result(True, "Connected: Radarr 5.2.0 accepted the API key."),
            "sonarr": connections.Result(False, "Sonarr refused the API key (HTTP 401)."),
            "seerr": connections.Result(True, "Connected: Seerr accepted the API key."),
        })
        page = self.get("/setup").text
        for name in answers:
            with self.subTest(name=name):
                self.assertIn(f'hx-post="/setup/block/integrations.{name}/test"', page)

    def test_what_is_not_enabled_is_not_asked(self) -> None:
        def handler(request):
            raise AssertionError("asked")

        self.assertEqual(self.run_test("jellyfin", handler).text, connections.NOT_ENABLED)
        self.assertFalse(self.run_test("imdb_guide", handler).ok)
        from vettarr import delivery

        with mock.patch.object(delivery, "test_connection", mock.AsyncMock(side_effect=AssertionError("asked"))):
            self.assertEqual(self.run_test("radarr", handler).text, connections.NOT_ENABLED)

    def test_the_button_tests_what_the_file_holds_and_answers_in_words(self) -> None:
        result = connections.Result(False, "It answered, and refused the API key (HTTP 401).")
        with mock.patch.object(connections, "test", mock.AsyncMock(return_value=result)) as test:
            resp = self.post("/setup/block/integrations.tmdb/test", headers=HTMX)
        name, config = test.await_args.args
        self.assertEqual((name, config.integrations.tmdb.api_key.get_secret_value()), ("tmdb", TMDB_VALUE))
        self.assertIn("Failed: It answered, and refused the API key (HTTP 401).", text_of(resp.text))
        self.assertIn('class="state state-bad"', resp.text)
        page = self.get("/setup").text
        self.assertIn('hx-post="/setup/block/integrations.tmdb/test"', page)
        self.assertNotIn('hx-post="/setup/block/integrations.imdb_guide/test"', page)


# ---------------------------------------------------------------------------
# Setup: formats
# ---------------------------------------------------------------------------


class FormatTests(ConfigCase):
    def card_format(self) -> dict[str, list[str]]:
        return self.form("/setup", "/setup/formats/card")

    def test_the_word_lists_are_shown_one_word_a_line(self) -> None:
        data = self.card_format()
        self.assertEqual((data["kind"], data["type_hints.movie"], data["fields.title"], data["ignore_hosts"]),
                         (["card"], ["NEW MOVIE"], ["Title"], ["instagram.com"]))
        self.assertEqual(set(data) - {"kind", "sample"}, setup_routes._FORMAT_FIELDS)

    def test_saving_writes_the_lists_and_leaves_out_the_empty_ones(self) -> None:
        data = {**self.card_format(), "type_headers.ebook": "NEW EBOOK\nNEW BOOK\n", "media_words": "film\n\nmovie",
                "ignore_hosts": "", "op": "save"}
        resp = self.post("/setup/formats/card", data=data, headers=HTMX)
        self.assertEqual(resp.headers["HX-Redirect"], self.saved("formats"))
        written = loader.plain(loader.read_document(self.path))["formats"][0]
        self.assertEqual(written, {
            "key": "card", "kind": "card", "type_hints": {"movie": ["NEW MOVIE"]}, "category_words": {"movie": ["Movie"]},
            "fields": {"title": ["Title"], "type": ["Type"]}, "type_headers": {"ebook": ["NEW EBOOK", "NEW BOOK"]},
            "media_words": ["film", "movie"],
        })

    def test_a_word_that_does_not_compile_is_named_on_its_list(self) -> None:
        data = {**self.card_format(), "fields.title": "Title\n/(/", "op": "save"}
        resp = self.post("/setup/formats/card", data=data, headers=HTMX)
        field = tag(resp.text, "format-card-fields-title")
        self.assertIn('aria-invalid="true"', field)
        self.assertRegex(resp.text, r'id="format-card-fields-title-error">.*?pattern does not compile')
        self.assertIn("/(/", resp.text)
        self.unchanged()

    def test_a_post_is_tried_against_the_words_in_the_form_before_they_are_saved(self) -> None:
        data = {**self.card_format(), "fields.title": "Name", "sample": CARD_POST.replace("Title:", "Name:"), "op": "test"}
        resp = self.post("/setup/formats/card", data=data, headers=HTMX)
        read = text_of(resp.text)
        self.assertIn("✓ Read by card : it would arrive as a movie.", read)
        self.assertIn("Title The Lantern Keeper", read)
        self.assertIn(">Name</textarea>", resp.text)
        self.assertIn("Paste a post to read first.",
                      self.post("/setup/formats/card", data={**data, "sample": ""}, headers=HTMX).text)
        self.assertIn("pattern does not compile",
                      self.post("/setup/formats/card", data={**data, "media_words": "/(/"}, headers=HTMX).text)
        self.unchanged()

    def test_a_format_is_added_and_its_key_must_be_new(self) -> None:
        resp = self.post("/setup/formats", data={"key": "my-cards", "kind": "card"}, headers=HTMX)
        self.assertEqual(resp.headers["HX-Redirect"], self.saved("formats"))
        self.assertEqual([f.key for f in self.file().formats], ["card", "scene", "my-cards"])
        before = self.path.read_text()
        again = self.post("/setup/formats", data={"key": "my-cards", "kind": "card"}, headers=HTMX)
        self.assertIn("duplicate key", again.text)
        self.assertIn('aria-invalid="true"', tag(again.text, "format-new-key"))
        self.assertEqual(self.post("/setup/formats", data={"key": "Bad Key", "kind": "card"}).status_code, 422)
        self.assertEqual(self.path.read_text(), before)

    def test_a_format_a_source_names_cannot_be_removed(self) -> None:
        refused = self.post("/setup/formats/card/delete", headers=HTMX)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("card was not removed.", refused.text)
        self.assertIn("no format", refused.text)
        self.unchanged()
        self.assertIn("HX-Redirect", self.post("/setup/formats/scene/delete", headers=HTMX).headers)
        self.assertEqual([f.key for f in self.file().formats], ["card"])
        self.assertEqual(self.post("/setup/formats/scene", data={"op": "save"}, headers=HTMX).status_code, 404)


# ---------------------------------------------------------------------------
# Setup: managed by file
# ---------------------------------------------------------------------------


class SetupManagedTests(ConfigCase):
    READONLY = True

    def test_the_page_says_managed_by_file_and_offers_no_write(self) -> None:
        page = self.get("/setup").text
        self.assertIn("Managed by file.", page)
        body = page[page.index('id="main"'):page.index('id="toast"')]
        self.assertEqual(
            sorted(set(re.findall(r"<button[^>]*>([^<]+)</button>", body))),
            ["Check now", "Read it", "Save the cookies", "Test connection"],
        )
        self.assertNotIn("Add a source", body)
        self.assertNotIn("/delete", body)
        for path in setup_routes.BLOCKS:
            self.assertEqual(forms(page, f"/setup/block/{path}"), [{}], path)
        self.assertRegex(body, r'<fieldset class="plain-fieldset grid gap-3 sm:grid-cols-2" disabled>')
        # What is configured can still be read.
        self.assertIn("-100123", body)
        self.assertIn('value="http://prowlarr:9696"', body)

    def test_every_write_is_refused_and_the_file_is_as_it_was(self) -> None:
        for url, data in (
            ("/setup/block/web", {"port": "1"}),
            ("/setup/block/integrations.tmdb", {"language": "es-ES"}),
            ("/setup/sources", {"original": "", "kind": "bot", "key": "inbox", "chat": "private"}),
            ("/setup/sources/films/delete", {}),
            ("/setup/formats", {"key": "more", "kind": "card"}),
            ("/setup/formats/card", {"op": "save"}),
            ("/setup/formats/scene/delete", {}),
        ):
            with self.subTest(url=url):
                resp = self.post(url, data=data, headers=HTMX)
                self.assertEqual((resp.status_code, resp.text), (409, configfile.REFUSED[configfile.FILE]))
        self.unchanged()

    def test_what_writes_nothing_to_the_config_still_works(self) -> None:
        parsed = self.post("/setup/test-parse", data={"source": "films", "text": CARD_POST}, headers=HTMX)
        self.assertIn("Read by", parsed.text)
        self.assertIn("Works:", self.post("/setup/providers/link", data={"url": "https://northwind.example/torrents/1"}).text)
        with mock.patch.object(search_service, "check_session", mock.AsyncMock(return_value=True)):
            self.assertIn("Signed in.", self.post("/setup/providers/northwind/check").text)
        with mock.patch.object(connections, "test", mock.AsyncMock(return_value=connections.Result(True, "Connected."))):
            self.assertIn("Connected.", self.post("/setup/block/integrations.tmdb/test").text)
        with mock.patch.object(search_service, "get_trackers", return_value=[]):
            saved = self.post("/settings/cookies/northwind", data={"cookies_json": '[{"name": "a", "value": "b"}]'})
        self.assertIn("Cookies saved.", saved.text)
        self.unchanged()


class SetupWithoutAFileTests(ConfigCase):
    CONFIG = None

    def test_an_environment_boot_shows_the_running_config_and_changes_nothing(self) -> None:
        self.running = Config.model_validate({
            "integrations": {"tmdb": {"api_key": TMDB_VALUE}, "prowlarr": {"url": "http://prowlarr:9696", "api_key": PROWLARR_VALUE}},
        })
        page = self.get("/setup")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No config file.", page.text)
        self.assertNotIn(TMDB_VALUE, page.text)
        self.assertNotIn(PROWLARR_VALUE, page.text)
        self.assertIn("API key ✓ set", text_of(page.text))
        resp = self.post("/setup/block/web", data={"port": "1"}, headers=HTMX)
        self.assertEqual((resp.status_code, resp.text), (409, configfile.REFUSED[configfile.ENVIRONMENT]))
        self.assertFalse(self.path.exists())


class StylesheetTests(unittest.TestCase):
    SRC = pathlib.Path(configfile.__file__).parent / "static" / "src"

    def test_the_pages_styles_are_part_of_the_stylesheet(self) -> None:
        self.assertIn('@import "./forms.css";', (self.SRC / "app.css").read_text())
        css = (self.SRC / "forms.css").read_text()
        for name in (".sev-chips", ".field-inline", ".action-bar", ".stack-table", ".option-card", ".field-error"):
            self.assertIn(name, css)
        # A chip is told from its neighbours by more than its colour, and focus shows on it.
        self.assertIn(".sev-chips input:focus-visible + label", css)
        self.assertIn("border-style: dashed", css)
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")

    def test_the_browser_checks_cover_the_two_pages(self) -> None:
        checks = (pathlib.Path(configfile.__file__).parents[3] / "scripts" / "checks.js").read_text()
        for name in ("Enter in a rule field saves nothing", "an arrow key moves the severity and sends nothing",
                     "Remove this source opens the dialog and sends nothing", "Cancel keeps the source",
                     "the Policy page does not scroll sideways on a phone",
                     "the Setup page does not scroll sideways on a phone",
                     "a moved rule keeps the focus on its own button",
                     "a new rule gets the focus on its name",
                     "a second save in a section loads the page again"):
            with self.subTest(check=name):
                self.assertIn(name, checks)

    def test_neither_page_brought_a_script_of_its_own(self) -> None:
        templates = pathlib.Path(configfile.__file__).parent / "templates"
        for name in ("policy.html", "policy/_editor.html", "setup.html", *(f"setup/{p.name}" for p in (templates / "setup").iterdir())):
            with self.subTest(template=name):
                text = (templates / name).read_text()
                self.assertNotIn("<script", text)
                self.assertNotRegex(text, r'\son[a-z]+="')


if __name__ == "__main__":
    unittest.main()
