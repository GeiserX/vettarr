"""Setup (``/setup``, and ``/settings`` as it was): where posts come from, what
is searched, what is connected.

Loading the page sends nothing anywhere: a provider's state is read from the
``guard`` events and a cookie's from its file. A request leaves the app only
on a click: "Check now" asks one tracker, "Test connection" one integration.

Every block of the config file has a form here. A simple block (an
integration, ``deliver``, ``notify``, ``web``, ``telegram``) is described once
in ``BLOCKS`` and handled by one pair of routes; sources and formats have
their own. A save validates the whole file first and puts each error beside
its field (``web/configfile.py``). A secret is shown as set or not set and is
written only when a new one is typed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from ruamel.yaml.comments import CommentedSeq
from sqlalchemy import func, select
from starlette.datastructures import FormData

from vettarr import dryrun, events
from vettarr.config.loader import ConfigError
from vettarr.config.schema import Config, FormatProfile
from vettarr.config.signals import MEDIA_TYPES
from vettarr.config.writer import ConfigReadOnly
from vettarr.database import async_session
from vettarr.models import Event, MediaItem
from vettarr.parser.formats import Format, FormatCompileError, compile_format, formats_for
from vettarr.parser.regex_parser import classify_non_media, parse_message
from vettarr.searcher import providers as provider_rules
from vettarr.searcher import search_service
from vettarr.web import configfile, connections, shell
from vettarr.web.ui import is_htmx, templates

log = logging.getLogger(__name__)

router = APIRouter()

_WHOLE = re.compile(r"^-?\d+$")
_SLUG = re.compile(r"[^a-z0-9]+")
_INDEXED = re.compile(r"\[\d+\]$")


# --- the simple blocks -----------------------------------------------------------


@dataclass(frozen=True)
class F:
    """One setting of a block, as a form field."""

    name: str
    label: str
    #: text | url | int | ints | bool | secret | select
    kind: str = "text"
    default: Any = ""
    options: tuple[str, ...] = ()
    hint: str = ""
    #: A secret the file holds as a number (``telegram.api_id``).
    whole: bool = False


@dataclass(frozen=True)
class Block:
    path: str
    title: str
    what: str
    fields: tuple[F, ...]
    #: The page section it is shown in; where a save comes back to.
    section: str
    test: bool = False
    #: The block goes when its last setting does (``notify`` is its address).
    removable: bool = False

    @property
    def slug(self) -> str:
        return _SLUG.sub("-", self.path)


DELIVER = ("none", "qbittorrent", "radarr", "sonarr", "seerr")
_ON = F("enabled", "Enabled", "bool")
_ADDRESS = F("url", "Address", "url", hint="As this app reaches it, for example http://name:8080.")
_API_KEY = F("api_key", "API key", "secret")

BLOCKS: dict[str, Block] = {
    b.path: b for b in (
        Block(
            "telegram", "Telegram", "What a userbot source and the bot sign in with.",
            (
                F("api_id", "API id", "secret", whole=True, hint="From my.telegram.org. A userbot source needs it."),
                F("api_hash", "API hash", "secret"),
                F("phone", "Phone number", "secret", hint="Only asked for when the userbot session has to sign in again."),
                F("bot_token", "Bot token", "secret", hint="From @BotFather. Its presence starts the bot."),
                F("reviewers", "Reviewers", "ints", hint="Telegram user ids allowed to decide and to use the bot, separated by commas."),
            ),
            "sources",
        ),
        Block(
            "integrations.prowlarr", "Prowlarr", "Searches your indexers for a release, and lends a provider its session.",
            (_ON, _ADDRESS, _API_KEY), "integrations", test=True,
        ),
        Block(
            "integrations.qbittorrent", "qBittorrent", "The download client: adds the release, reports progress, knows what you already have.",
            (_ON, _ADDRESS, F("username", "Username", default="admin"), F("password", "Password", "secret")),
            "integrations", test=True,
        ),
        Block(
            "integrations.tmdb", "TMDB", "Names a title and reads its certification, genres and keywords.",
            (_ON, _API_KEY, F("language", "Language", default="en-US", hint="Titles and genre names come back in it.")),
            "integrations", test=True,
        ),
        Block(
            "integrations.imdb_guide", "IMDb parents guide",
            "How violent, how explicit: the imdb.* signals. Needs no key. It reads IMDb's undocumented website API, "
            "which IMDb's terms forbid without consent: for a private instance, at your own risk.",
            (_ON, F("min_votes", "Minimum votes", "int", default=20, hint="A severity voted by fewer people counts as missing.")),
            "integrations",
        ),
        Block(
            "integrations.google_books", "Google Books", "A second catalogue for books. Without a key the shared quota is usually spent.",
            (_ON, _API_KEY), "integrations", test=True,
        ),
        Block(
            "integrations.jellyfin", "Jellyfin", "Tells a title you already have from a new one (the in_library signal).",
            (_ON, _ADDRESS, _API_KEY), "integrations", test=True,
        ),
        Block(
            "integrations.radarr", "Radarr", "A delivery target for films.",
            (_ON, _ADDRESS, _API_KEY, F("root_folder", "Root folder"), F("quality_profile", "Quality profile", hint="Its name or its id.")),
            "integrations", test=True,
        ),
        Block(
            "integrations.sonarr", "Sonarr", "A delivery target for series.",
            (_ON, _ADDRESS, _API_KEY, F("root_folder", "Root folder"), F("quality_profile", "Quality profile", hint="Its name or its id.")),
            "integrations", test=True,
        ),
        Block(
            "integrations.seerr", "Seerr", "Requests in Seerr, as a source and as a delivery target.",
            (_ON, _ADDRESS, _API_KEY, F("user_id", "User id", "int", hint="The Seerr user requests are made as. Without it a request is approved as the admin.")),
            "integrations", test=True,
        ),
        Block(
            "deliver", "Deliver", "Where an approved item of each type goes.",
            tuple(F(t, t, "select", default="none", options=DELIVER) for t in MEDIA_TYPES),
            "deliver",
        ),
        Block(
            "notify", "Notify", "Each decision, each item that needs you and each tracker session change, sent to one address.",
            (F("url", "Address", "secret", hint="An Apprise API /notify endpoint or a plain http(s) webhook."),),
            "notify", removable=True,
        ),
        Block(
            "web", "Web", "Where this page listens, and the address people reach it at.",
            (
                F("bind", "Bind address", default="0.0.0.0"),
                F("port", "Port", "int", default=4850),
                # docs/CONFIG.md, "Not enforced yet": no bot message links to the web UI yet.
                F("base_url", "Public address", "url", hint="When it is https the session cookie is Secure. No bot message links to it yet."),
            ),
            "web",
        ),
    )
}
SECTIONS = (
    ("sources", "Sources"), ("providers", "Providers"), ("integrations", "Integrations"), ("formats", "Formats"),
    ("deliver", "Deliver"), ("notify", "Notify"), ("web", "Web"),
)


def _model(config: Config, path: str) -> Any:
    node: Any = config
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
        if node is None:
            return None
    return node


def _as_secret(state: configfile.State, block: Block, field: F) -> bool:
    """A secret, or an address written with a password or a token in it."""
    return field.kind == "secret" or (
        field.kind == "url" and configfile.carries_secret(state.raw(f"{block.path}.{field.name}"))
    )


def card(
    state: configfile.State, block: Block, posted: FormData | None = None, errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A block as its form: what is written now, or what was just posted, and each error by its field."""
    # An error inside a list (reviewers[1]) belongs to the list's field.
    errors = {_INDEXED.sub("", path): message for path, message in (errors or {}).items()}
    model = _model(state.config, block.path)
    fields = []
    for f in block.fields:
        raw = state.raw(f"{block.path}.{f.name}")
        item: dict[str, Any] = {
            "f": f, "kind": "secret" if _as_secret(state, block, f) else f.kind,
            "id": f"{block.slug}-{f.name}", "error": errors.get(f"{block.path}.{f.name}", ""),
        }
        if item["kind"] == "secret":
            item["state"] = configfile.secret_state(raw)
        elif f.kind != "bool" and configfile.reference(raw):
            # Written as ${VAR}: the environment decides it, and the page says so instead of a field.
            item["env"] = configfile.reference(raw)
        elif f.kind == "bool":
            item["value"] = f.name in posted if posted is not None else bool(model is not None and getattr(model, f.name, False))
        elif posted is not None:
            item["value"] = str(posted.get(f.name) or "")
        elif f.kind == "ints":
            item["value"] = ", ".join(str(v) for v in raw) if isinstance(raw, list) else ""
        elif f.kind == "select":
            item["value"] = str(raw if raw is not None else f.default)
        else:
            item["value"] = "" if raw is None else str(raw)
        fields.append(item)
    known = {f"{block.path}.{f.name}" for f in block.fields}
    name = block.path.rpartition(".")[2]
    status = None
    if block.section == "integrations":
        written = state.raw(block.path) is not None
        status = ("on", "On") if state.config.integrations.enabled(name) else ("off", "Off" if written else "Not set up")
    facts = []
    if block.path == "web":
        auth = state.config.web.auth if state.config.web is not None else None
        if auth is None:
            facts.append(("Sign-in", "none: anyone who reaches the port can approve and reject", "bad"))
        elif auth.password_hash is not None:
            facts.append(("Sign-in", "a password", "ok"))
        else:
            facts.append(("Sign-in", f"the reverse proxy's header {auth.trusted_header}", "ok"))
    return {
        "block": block, "fields": fields, "status": status, "facts": facts,
        # Nine integrations make a long page: each shows its name and state, and opens on a click.
        # One state for all, so the grid's columns line up; a save with errors stays open.
        "folded": status is not None and not errors,
        "problems": [m if path == block.path else f"{path}: {m}" for path, m in errors.items() if path not in known],
    }


def apply_block(state: configfile.State, doc: Any, block: Block, data: FormData) -> None:
    """Write the posted fields of ``block`` into ``doc``. A block nobody filled in is not made."""
    node = configfile.block(doc, block.path)
    model = _model(state.config, block.path)
    values: dict[str, Any] = {}
    drop: list[str] = []
    # Ticked on a block that is off or not there: it is made, so what it still needs is said.
    wanted = False
    for f in block.fields:
        if f.name not in data and configfile.reference(state.raw(f"{block.path}.{f.name}")):
            # A ${VAR} the page showed as a line, not a field: left as it is written.
            continue
        text = str(data.get(f.name) or "").strip()
        had = node is not None and f.name in node
        if _as_secret(state, block, f):
            if data.get(f"{f.name}-clear"):
                drop.append(f.name)
            elif text:
                values[f.name] = int(text) if f.whole and _WHOLE.match(text) else text
        elif f.kind == "bool":
            # A block that exists is on unless it says otherwise, so only "off" has to be written.
            if f.name not in data:
                values[f.name] = False
            elif had:
                values[f.name] = True
            else:
                wanted = not getattr(model, f.name, False)
        elif not text:
            drop.append(f.name)
        elif f.kind == "int":
            values[f.name] = configfile.number(text)
        elif f.kind == "ints":
            values[f.name] = [int(w) if _WHOLE.match(w) else w for w in re.split(r"[\s,]+", text) if w]
        elif had or text != str(f.default):
            values[f.name] = text
    if node is None:
        if not wanted and not any(value is not False for value in values.values()):
            return
        node = configfile.block(doc, block.path, create=True)
    for name in drop:
        node.pop(name, None)
    for name, value in values.items():
        configfile.put(node, name, value)
    if block.removable and not node:
        parent, _, key = block.path.rpartition(".")
        del (configfile.block(doc, parent) if parent else doc)[key]


# --- sources -----------------------------------------------------------------------

SOURCE_KINDS = (
    ("userbot", "A group or channel, read as you", "Reads the chat with your own Telegram account. The only kind that can read what was posted before."),
    ("bot", "The bot", "The bot receives what is sent to it: in a group, a channel, or in private."),
    ("seerr", "Seerr requests", "Pending requests in Seerr, decided by the policy."),
)
POST_ACTIONS = ("none", "react", "delete")


def _source_values(raw: dict[str, Any]) -> dict[str, Any]:
    senders = raw.get("allowed_senders")
    return {
        "original": str(raw.get("key") or ""), "key": str(raw.get("key") or ""), "kind": str(raw.get("kind") or ""),
        "chat": "" if raw.get("chat") is None else str(raw.get("chat")),
        "backfill": str(raw.get("backfill") or ""),
        "formats": [str(k) for k in raw.get("formats") or []],
        "allowed_senders": "listed" if isinstance(senders, list) else str(senders or ""),
        "direct_adds": str(raw.get("direct_adds") or ""),
        "on_download": str(raw.get("on_download") or "none"), "on_reject": str(raw.get("on_reject") or "none"),
        "notify_on_review": bool(raw.get("notify_on_review")),
        "poll_minutes": "" if raw.get("poll_minutes") is None else str(raw.get("poll_minutes")),
        # Never the secret itself: set or not set, and which ${VAR} it comes from.
        "webhook_secret": "", "webhook_secret_clear": False,
        "webhook_secret_state": configfile.secret_state(raw.get("webhook_secret")),
    }


def _blank_source(kind: str, **values: Any) -> dict[str, Any]:
    return {**_source_values({"kind": kind}), **values}


def _source_posted(data: FormData) -> dict[str, Any]:
    def text(name: str) -> str:
        return str(data.get(name) or "").strip()

    return {
        "original": text("original"), "key": text("key"), "kind": text("kind"), "chat": text("chat"),
        "backfill": text("backfill"), "formats": [str(k) for k in data.getlist("formats")],
        "allowed_senders": text("allowed_senders"), "direct_adds": text("direct_adds"),
        "on_download": text("on_download") or "none", "on_reject": text("on_reject") or "none",
        "notify_on_review": "notify_on_review" in data, "poll_minutes": text("poll_minutes"),
        "webhook_secret": text("webhook_secret"), "webhook_secret_clear": "webhook_secret-clear" in data,
        "webhook_secret_state": "",
    }


def _source_yaml(values: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    kind = values["kind"]
    out: dict[str, Any] = {"key": values["key"], "kind": kind}
    posts = kind != "seerr"
    if posts:
        chat = values["chat"]
        out["chat"] = int(chat) if _WHOLE.match(chat) else chat
    if kind == "userbot" and values["backfill"]:
        out["backfill"] = values["backfill"]
    if values["formats"]:
        out["formats"] = list(values["formats"])
    if values["allowed_senders"] == "listed":
        out["allowed_senders"] = old.get("allowed_senders")
    elif values["allowed_senders"]:
        out["allowed_senders"] = values["allowed_senders"]
    if posts and values["direct_adds"]:
        out["direct_adds"] = values["direct_adds"]
    for name in ("on_download", "on_reject"):
        if posts and (values[name] != "none" or name in old):
            out[name] = values[name]
    if values["notify_on_review"] or "notify_on_review" in old:
        out["notify_on_review"] = values["notify_on_review"]
    if kind == "seerr" and values["poll_minutes"]:
        out["poll_minutes"] = configfile.number(values["poll_minutes"])
    # A secret is written only when a new one is typed; left empty, the file keeps its own.
    if kind == "seerr" and values["webhook_secret"]:
        out["webhook_secret"] = values["webhook_secret"]
    elif kind == "seerr" and not values["webhook_secret_clear"] and "webhook_secret" in old:
        out["webhook_secret"] = old["webhook_secret"]
    # What the form has no field for stays as the file has it.
    for name, value in old.items():
        if name not in _SOURCE_FIELDS and name not in out:
            out[name] = value
    return out


def _entry_errors(errors: dict[str, str], prefix: str, fields: set[str]) -> tuple[dict[str, str], list[str]]:
    """Split a save's errors into those of one list entry's fields and everything else."""
    mine: dict[str, str] = {}
    other: list[str] = []
    for path, message in errors.items():
        name = path[len(prefix) + 1:] if path.startswith(prefix + ".") else None
        while name and _INDEXED.search(name):
            name = _INDEXED.sub("", name)
        if name in fields:
            mine[name] = f"{mine[name]}; {message}" if name in mine else message
        else:
            other.append(message if path in (prefix, configfile.TOP) else f"{path}: {message}")
    return mine, other


_SOURCE_FIELDS = {
    "key", "chat", "backfill", "formats", "allowed_senders", "direct_adds", "on_download", "on_reject",
    "notify_on_review", "poll_minutes", "webhook_secret",
}


def _sequence(doc: Any, key: str) -> CommentedSeq:
    node = doc.get(key)
    if not isinstance(node, CommentedSeq):
        node = CommentedSeq(node or [])
        doc[key] = node
    return node


def _index(sequence: list[Any], key: str) -> int | None:
    return next((n for n, entry in enumerate(sequence) if isinstance(entry, dict) and str(entry.get("key")) == key), None)


def _slug(text: str, taken: set[str]) -> str:
    stem = _SLUG.sub("-", text.lower()).strip("-")[:40] or "chat"
    return stem if stem not in taken else next(f"{stem}-{n}" for n in range(2, len(taken) + 3) if f"{stem}-{n}" not in taken)


# --- the bot was added to a chat ----------------------------------------------------


def chats_from(rows: list[Any]) -> list[dict[str, Any]]:
    """The chats the bot is in, from ``bot_chat`` events: each chat's latest event, when it is a join. Newest first."""
    seen: set[Any] = set()
    out = []
    for row in sorted(rows, key=lambda row: row.at, reverse=True):
        payload = row.payload or {}
        chat_id = payload.get("chat_id")
        if chat_id is None or chat_id in seen:
            continue
        seen.add(chat_id)
        if payload.get("state") == "joined":
            out.append({
                "chat_id": chat_id, "title": str(payload.get("title") or chat_id),
                "is_admin": bool(payload.get("is_admin")),
                "reads_all": bool(payload.get("is_admin") or payload.get("reads_all_messages")),
            })
    return out


def latest_bot_chats() -> Any:
    """The query: the latest ``bot_chat`` event of every chat.

    One row a chat: however often the bot left and came back somewhere, that
    never pushes another chat's join out of what is read.
    """
    chat = Event.payload["chat_id"].astext
    rank = func.row_number().over(partition_by=chat, order_by=(Event.at.desc(), Event.id.desc()))
    ranked = select(Event.id, rank.label("rank")).where(Event.kind == events.BOT_CHAT).subquery()
    return select(Event).where(Event.id.in_(select(ranked.c.id).where(ranked.c.rank == 1)))


async def bot_chats() -> list[dict[str, Any]]:
    """Read from the ``events`` table. Never raises: the page works without it."""
    try:
        async with async_session() as session:
            rows = (await session.execute(latest_bot_chats())).scalars().all()
    except Exception as exc:
        log.debug("Could not read the bot_chat events: %s", type(exc).__name__)
        return []
    return chats_from(list(rows))


def hints(chats: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    """The chats to offer: the bot is in them and no bot source reads them."""
    read = {str(s.chat) for s in config.sources if s.kind == "bot"}
    taken = {s.key for s in config.sources}
    return [{**chat, "key": _slug(chat["title"], taken)} for chat in chats if str(chat["chat_id"]) not in read]


# --- providers ----------------------------------------------------------------------


def _age(seconds: float) -> str:
    for size, word in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {word}{'' if n == 1 else 's'} ago"
    return "just now"


def _cookie(path: str | None) -> dict[str, Any] | None:
    """Whether the cookie file is there. Its content is never read."""
    if not path:
        return None
    try:
        info = Path(path).stat()
    except OSError:
        return {"path": path, "set": False, "age": ""}
    return {"path": path, "set": info.st_size > 0, "age": _age(max(0.0, time.time() - info.st_mtime))}


# Words in a cooldown's reason that say the session is the trouble: new cookies are the fix.
_SESSION_WORDS = ("session", "cookie", "login", "logged out", "signed out")


def provider_cards(config: Config, failing: Any) -> list[dict[str, Any]]:
    paused = {f.name: f for f in failing or ()}
    cards = []
    for p in config.providers:
        base = provider_rules.base(p)
        cookie = _cookie(p.auth.cookie_file)
        pause = paused.get(p.key)
        # No cookie, or a pause because the session was refused: pasting cookies is what helps.
        renew = cookie is not None and (
            not cookie["set"] or (pause is not None and any(w in pause.reason.lower() for w in _SESSION_WORDS))
        )
        # A download address can embed a passkey and a base address a password: neither is shown.
        cards.append({
            "key": p.key, "address": provider_rules.shown(p.base_url), "aliases": list(p.aliases),
            "auth": p.auth.from_, "password": p.auth.password is not None, "cookie": cookie,
            "search": p.search, "thank": p.thank.enabled,
            "keep_alive": p.keep_alive.every_minutes if p.keep_alive.enabled else None,
            "paused": pause, "renew": renew,
            "links": [
                {
                    "pattern": link.pattern,
                    "examples": [
                        {"url": provider_rules.shown(ex.url), "id": ex.id,
                         "download": provider_rules.shown(link.download_url.replace("{base}", base).replace("{id}", ex.id))}
                        for ex in link.examples
                    ],
                }
                for link in p.links
            ],
        })
    return cards


# --- formats, and reading a post -------------------------------------------------------

FORMAT_TYPED = (
    ("type_headers", "Type headers", "A line holding one of these decides the type outright."),
    ("type_hints", "Type hints", "Weaker: one of these anywhere in the post suggests the type."),
    ("category_words", "Category words", "Category names as a header or a Type line writes them. Card formats only."),
)
FORMAT_LISTS = (
    ("fields.title", "Title labels", "The label of the title line, as in Title: Some Film. Card formats only."),
    ("fields.year", "Year labels", ""),
    ("fields.type", "Type labels", "The label of a line shaped category - quality - source - size."),
    ("fields.link", "Link labels", ""),
    ("header_markers", "Header markers", "What opens a header line (marker, category, arrow, source). Card formats only."),
    ("ignore_line_prefixes", "Ignored line starts", "A line starting with one of these is never the title."),
    ("ignore_hosts", "Ignored hosts", "A post whose only link points at one of these is not media."),
    ("media_words", "Media words", "A message with no release in it that holds one of these is kept for review."),
)
_FORMAT_FIELDS = {name for name, *_ in FORMAT_LISTS} | {f"{group}.{t}" for group, *_ in FORMAT_TYPED for t in MEDIA_TYPES}


def _format_values(raw: dict[str, Any]) -> dict[str, Any]:
    lists: dict[str, str] = {}
    for group, *_ in FORMAT_TYPED:
        for media_type, words in (raw.get(group) or {}).items():
            lists[f"{group}.{media_type}"] = "\n".join(str(w) for w in words or [])
    for name, *_ in FORMAT_LISTS:
        head, _, tail = name.partition(".")
        words = (raw.get(head) or {}).get(tail) if tail else raw.get(head)
        lists[name] = "\n".join(str(w) for w in words or [])
    return {"key": str(raw.get("key") or ""), "kind": str(raw.get("kind") or "card"), "lists": lists, "sample": ""}


def _format_posted(data: FormData, key: str) -> dict[str, Any]:
    return {
        "key": key, "kind": str(data.get("kind") or "card"),
        "lists": {name: str(data.get(name) or "") for name in _FORMAT_FIELDS},
        "sample": str(data.get("sample") or ""),
    }


def _format_yaml(values: dict[str, Any]) -> dict[str, Any]:
    lists = values["lists"]
    out: dict[str, Any] = {"key": values["key"], "kind": values["kind"]}
    for group, *_ in FORMAT_TYPED:
        by_type = {t: w for t in MEDIA_TYPES if (w := configfile.words(lists.get(f"{group}.{t}", "")))}
        if by_type:
            out[group] = by_type
    fields = {n: w for n in ("title", "link", "year", "type") if (w := configfile.words(lists.get(f"fields.{n}", "")))}
    if fields:
        out["fields"] = fields
    for name in ("header_markers", "ignore_line_prefixes", "ignore_hosts", "media_words"):
        if found := configfile.words(lists.get(name, "")):
            out[name] = found
    return out


_LABELLED = re.compile(r"^\s*([A-Za-z][A-Za-z ]{0,20}):\s*\S", re.M)


def junk_title(text: str, title: str | None) -> str | None:
    """The label of a labelled line, when what was read as the title is the post's first line.

    A card post (``Title: ...`` under a header line) read by no card format
    falls to the release-name reader, which takes its first line, such as
    ``Film``, for the title. That reads as a success and is not one.
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    labels = _LABELLED.findall(text)
    if not title or len(labels) < 2 or title.strip().lower() not in first.lower():
        return None
    return next((label for label in labels if label.strip().lower() == "title"), labels[0]).strip()


def read_post(formats: tuple[Format, ...] | list[Format], text: str, config: Config) -> dict[str, Any]:
    """What the app reads in a pasted post with these formats, and what it would do with it."""
    item, read_by = None, None
    for fmt in formats:
        got = parse_message(text, [fmt])
        item = item or got
        if got.is_valid:
            item, read_by = got, fmt.key or "the release-name reader"
            break
    assert item is not None
    ignored = None if item.is_valid else classify_non_media(text, list(formats))
    linked = provider_rules.linked_torrent(config.providers, item.external_links)
    media_type = item.media_type.value
    return {
        "read_by": read_by, "ignored": ignored, "media_type": media_type,
        "label": junk_title(text, item.title) if read_by else None,
        "fields": [
            (label, value) for label, value in (
                ("Type", media_type), ("Title", item.title), ("Year", item.year), ("Season", item.season),
                ("Quality", item.quality), ("Source", item.source), ("Size", item.size_text),
                ("Group", item.release_group), ("Links", ", ".join(provider_rules.shown(u) for u in item.external_links)),
            ) if value
        ],
        "linked": {"provider": linked.provider.key, "id": linked.torrent_id} if linked else None,
    }


# --- the page ------------------------------------------------------------------------


def checklist(config: Config) -> list[dict[str, Any]]:
    """What a config needs before it does anything, each with whether it is there."""
    on = config.integrations.enabled
    return [
        {"done": done, "name": name, "missing": missing, "href": href}
        for done, name, missing, href in (
            (bool(config.sources), "A source",
             "No source yet, so nothing arrives. Add a Telegram group, the bot or Seerr.", "#sources"),
            (on("tmdb"), "A TMDB key",
             "No TMDB key, so no title is looked up and every rule on a title or on the IMDb guide sees missing data.",
             "#integrations"),
            (search_service.has_search(config), "Somewhere to search",
             "No Prowlarr and no provider with its own search, so an approved item finds no release.",
             "#integrations"),
            (on("qbittorrent"), "A download client",
             "No qBittorrent, so a release that is found cannot be downloaded.", "#integrations"),
            (bool(config.policy.rules), "A rule",
             "No rule, so every item of a type decided by the rules is held for you.", "/policy"),
            (config.web is not None and config.web.auth is not None, "A sign-in for these pages",
             "No web.auth, so anyone who reaches the port can approve and reject.", "#web"),
        )
    ]


def _formats_raw(state: configfile.State) -> list[dict[str, Any]]:
    return [f for f in state.view.get("formats") or [] if isinstance(f, dict)]


async def _post_to_try(request: Request) -> str:
    """The post ``?try=<item>`` names, for "Try a post": an Inbox item no format could read."""
    asked = request.query_params.get("try", "")
    if not (asked.isascii() and asked.isdigit() and 0 < len(asked) < 10):
        return ""
    try:
        async with async_session() as session:
            text = (await session.execute(select(MediaItem.raw_text).where(MediaItem.id == int(asked)))).scalar()
    except Exception as exc:
        log.debug("Setup could not read the post to try: %s", type(exc).__name__)
        return ""
    return str(text or "")


async def _context(request: Request, state: configfile.State, override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything setup.html reads. ``override`` replaces one form with what was posted and its errors."""
    override = override or {}
    config = state.config
    raw_sources = [s for s in state.view.get("sources") or [] if isinstance(s, dict)]
    sources = []
    for n, source in enumerate(config.sources):
        values = _source_values(raw_sources[n]) if n < len(raw_sources) else _blank_source(source.kind, key=source.key)
        sources.append({"source": source, "form": {"vals": values, "errors": {}, "problems": [], "new": False}})
    adding = {kind: {"vals": _blank_source(kind), "errors": {}, "problems": [], "new": True} for kind, *_ in SOURCE_KINDS}
    if posted := override.get("source"):
        target = next((s for s in sources if s["source"].key == posted["vals"]["original"]), None)
        if target is not None:
            target["form"] = posted
        elif posted["vals"]["kind"] in adding:
            adding[posted["vals"]["kind"]] = posted
    formats = [{"vals": _format_values(raw), "errors": {}, "problems": [], "parsed": None} for raw in _formats_raw(state)]
    if posted := override.get("format"):
        formats = [posted if f["vals"]["key"] == posted["vals"]["key"] else f for f in formats]
    cards = {
        path: override["card"] if override.get("card", {}).get("block") is block else card(state, block)
        for path, block in BLOCKS.items()
    }
    todo = checklist(config)
    return {
        "state": state, "sections": SECTIONS, "saved": _saved(request, state),
        "checklist": todo if not all(item["done"] for item in todo) else [],
        "sources": sources, "adding": adding, "source_kinds": SOURCE_KINDS, "post_actions": POST_ACTIONS,
        "format_keys": [f.key for f in config.formats],
        "hints": hints(await bot_chats(), config) if state.writable else [],
        "providers": provider_cards(config, getattr(request.state, "failing", ())),
        "cards": cards, "integrations": [cards[path] for path in BLOCKS if path.startswith("integrations.")],
        "formats": formats, "format_typed": FORMAT_TYPED, "format_lists": FORMAT_LISTS, "media_types": MEDIA_TYPES,
        "format_error": override.get("format_error", ""), "new_format": override.get("new_format", {"key": "", "kind": "card"}),
        "section_error": override.get("section_error", {}),
        "try_post": await _post_to_try(request),
    }


async def _page(request: Request, state: configfile.State, override: dict[str, Any] | None = None, status: int = 200):
    return templates.TemplateResponse(request, "setup.html", await _context(request, state, override), status_code=status)


def _refused(state: configfile.State) -> HTMLResponse:
    return HTMLResponse(escape(state.refusal), status_code=409)


def _stamp(path: Path) -> str:
    """The config file as it is now, in a word: the start of its hash."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _saved(request: Request, state: configfile.State) -> str:
    """The section whose "Saved" note to show: the one ``?saved=`` names, while the file is the one that save wrote."""
    section, _, stamp = request.query_params.get("saved", "").partition(".")
    return section if stamp and stamp == _stamp(state.path) else ""


def _done(request: Request, state: configfile.State, section: str):
    """A save worked: the whole page again, so the checklist and every other form show the new file.

    The address names the file this save wrote, so a save that changed the
    file has an address of its own. A browser sent to the address it is
    already at only scrolls to the section and keeps showing the old file; and
    an address opened later, when the file is another one, shows no "Saved"
    note.
    """
    url = f"/setup?saved={section}.{_stamp(state.path)}#{section}"
    if is_htmx(request):
        return HTMLResponse("", headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _commit(state: configfile.State, doc: Any) -> dict[str, str]:
    """Validate ``doc`` and write it. The errors by path; empty when it was written."""
    text, _, errors = configfile.validate(doc)
    if errors:
        return errors
    try:
        configfile.write(state, text)
    except ConfigError as exc:
        return {configfile.TOP: "; ".join(exc.errors)}
    return {}


def _line(message: str, ok: bool | None = None) -> HTMLResponse:
    """One line for a result slot: a mark, a word and the sentence, so it never rests on colour."""
    tone, mark, word = {True: ("ok", "&#10003;", "Works"), False: ("bad", "&#10007;", "Failed"), None: ("warn", "&#9888;", "Note")}[ok]
    return HTMLResponse(
        f'<span class="state state-{tone}"><span aria-hidden="true">{mark}</span>'
        f'<span class="sr-only">{word}: </span><span>{escape(message)}</span></span>'
    )


@router.get("/setup", response_class=HTMLResponse)
@router.get("/settings", response_class=HTMLResponse)
async def setup_page(request: Request):
    return await _page(request, configfile.load())


# --- a simple block ---


@router.post("/setup/block/{path}", response_class=HTMLResponse)
async def save_block(request: Request, path: str):
    block = BLOCKS.get(path)
    if block is None:
        return HTMLResponse("No such block.", status_code=404)
    state = configfile.load()
    if not state.writable:
        return _refused(state)
    data = await request.form()
    doc = state.candidate()
    apply_block(state, doc, block, data)
    try:
        errors = _commit(state, doc)
    except ConfigReadOnly as exc:
        return HTMLResponse(escape(str(exc)), status_code=409)
    if not errors:
        return _done(request, state, block.section)
    posted = card(state, block, data, errors)
    if is_htmx(request):
        return templates.TemplateResponse(request, "setup/_block.html", {"card": posted, "state": state})
    return await _page(request, state, {"card": posted}, 422)


@router.post("/setup/block/{path}/test", response_class=HTMLResponse)
async def test_block(path: str):
    """Test connection: one request to the integration, as the config file has it now."""
    block = BLOCKS.get(path)
    if block is None or not block.test:
        return HTMLResponse("Nothing to test there.", status_code=404)
    result = await connections.test(path.rpartition(".")[2], configfile.load().config)
    return _line(result.text, result.ok)


# --- sources ---


@router.post("/setup/sources", response_class=HTMLResponse)
async def save_source(request: Request):
    """Add a source, or change the one named by ``original``."""
    state = configfile.load()
    if not state.writable:
        return _refused(state)
    data = await request.form()
    values = _source_posted(data)
    if values["kind"] not in {kind for kind, *_ in SOURCE_KINDS}:
        return HTMLResponse("A source is a userbot, a bot or seerr.", status_code=400)
    doc = state.candidate()
    sources = _sequence(doc, "sources")
    at = _index(sources, values["original"]) if values["original"] else None
    if at is None:
        at = len(sources)
        sources.append(configfile.merged(None, _source_yaml(values, {})))
    else:
        values["webhook_secret_state"] = configfile.secret_state(sources[at].get("webhook_secret"))
        sources[at] = configfile.merged(sources[at], _source_yaml(values, dict(sources[at])))
    try:
        errors = _commit(state, doc)
    except ConfigReadOnly as exc:
        return HTMLResponse(escape(str(exc)), status_code=409)
    if not errors:
        return _done(request, state, "sources")
    mine, other = _entry_errors(errors, f"sources[{at}]", _SOURCE_FIELDS)
    if data.get("brief"):
        # The one-button offer of a chat the bot joined has no form to put errors in: one line, shown as a toast.
        return HTMLResponse(escape("Not added. " + "; ".join([*mine.values(), *other])), status_code=409)
    form = {"vals": values, "errors": mine, "problems": other, "new": not values["original"]}
    if is_htmx(request):
        return templates.TemplateResponse(request, "setup/_source_form.html", {
            "form": form, "state": state, "format_keys": [f.key for f in state.config.formats],
            "post_actions": POST_ACTIONS,
        })
    return await _page(request, state, {"source": form}, 422)


@router.post("/setup/sources/{key}/delete", response_class=HTMLResponse)
async def delete_source(request: Request, key: str):
    return await _delete(request, "sources", key)


async def _delete(request: Request, block: str, key: str):
    state = configfile.load()
    if not state.writable:
        return _refused(state)
    doc = state.candidate()
    entries = _sequence(doc, block)
    at = _index(entries, key)
    if at is None:
        return HTMLResponse(escape(f"There is no {key} under {block}."), status_code=404)
    del entries[at]
    if not entries:
        configfile.put(doc, block, [])
    try:
        errors = _commit(state, doc)
    except ConfigReadOnly as exc:
        return HTMLResponse(escape(str(exc)), status_code=409)
    if errors:
        # Something else in the file still names it. One line: htmx shows a 4xx body as a toast.
        return HTMLResponse(escape(f"{key} was not removed. " + "; ".join(f"{p}: {m}" for p, m in errors.items())), status_code=409)
    return _done(request, state, block)


@router.post("/setup/test-parse", response_class=HTMLResponse)
async def test_parse(request: Request):
    """Read a pasted post with a source's formats. Nothing is stored."""
    data = await request.form()
    config = configfile.load().config
    text = str(data.get("text") or "")
    if not text.strip():
        return _line("Paste a post first.")
    source = str(data.get("source") or "") or None
    parsed = read_post(formats_for(config, source), text, config)
    return templates.TemplateResponse(request, "setup/_parsed.html", {"parsed": parsed})


# --- providers ---


@router.post("/setup/providers/{key}/check", response_class=HTMLResponse)
async def check_provider(key: str):
    """Check now: one request to the tracker, and only because someone clicked."""
    paused = next((f for f in await shell.failing() if f.name == key), None)
    if paused is not None:
        return _line(f"Not asked: {paused.reason}, so nothing is sent there for another {paused.minutes} min.")
    live = await search_service.check_session(key)
    if live is None:
        return _line("The app has no session for it: no cookie file yet, or Prowlarr has no indexer signed in there.")
    if live:
        return _line("Signed in.", True)
    return _line("Not signed in: the tracker answered with its login page, an error, or not at all.", False)


@router.post("/setup/providers/link", response_class=HTMLResponse)
async def check_link(url: str = Form("")):
    """Which provider a pasted link belongs to and what its patterns read in it. No request is sent."""
    url = url.strip()
    if not url:
        return _line("Paste a link first.")
    config = configfile.load().config
    linked = provider_rules.linked_torrent(config.providers, [url])
    if linked is not None:
        fetched = provider_rules.shown(linked.download_url)
        return _line(f"{linked.provider.key} reads id {linked.torrent_id} and would fetch {fetched}", True)
    owner = provider_rules.provider_for_url(config.providers, url)
    if owner is None:
        return _line("It is on none of the providers' hosts. A subdomain is another host: add it under aliases.", False)
    return _line(f"It is on {owner.key}, and none of its link patterns reads an id in it.", False)


def _tracker_key_map() -> dict[str, tuple[str, str]]:
    return {
        p.key.lower().replace(" ", "-"): (p.key, p.auth.cookie_file or "")
        for p in configfile.current().providers
    }


_COOKIE_HEADER_LINES = 4


@router.post("/settings/cookies/{key}", response_class=HTMLResponse)
async def update_cookies(key: str, cookies_json: str = Form(...)):
    """Write a pasted browser cookie export to the provider's cookie file. Not the config file: it works under CONFIG_READONLY."""
    key_map = _tracker_key_map()
    if key not in key_map:
        return _line("The running app has no provider by that name. A provider added to the file needs a restart first.", False)
    name, cookie_path = key_map[key]
    if not cookie_path:
        return _line("This provider has no cookie file configured.", False)

    try:
        cookies = json.loads(cookies_json.strip())
    except json.JSONDecodeError as exc:
        return _line(f"That is not JSON: {exc.msg} at line {exc.lineno}.", False)
    if isinstance(cookies, dict) and "cookies" in cookies:
        cookies = cookies["cookies"]
    elif isinstance(cookies, dict):
        cookies = [cookies]
    if not isinstance(cookies, list) or not cookies:
        return _line("Expected a JSON list of cookies, as a browser's cookie export writes it.", False)

    lines = [  # _COOKIE_HEADER_LINES lines, then one per cookie
        "# Netscape HTTP Cookie File",
        "# https://curl.haxx.se/rfc/cookie_spec.html",
        "# This is a generated file!  Do not edit.",
        "",
    ]
    for c in cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        try:
            expiry = str(int(c.get("expirationDate") or 0))
        except (TypeError, ValueError):
            expiry = "0"  # a session cookie
        prefix = "#HttpOnly_" if c.get("httpOnly", False) else ""
        lines.append(f"{prefix}{domain}\t{flag}\t{c.get('path', '/')}\t{secure}\t{expiry}\t{c['name']}\t{c['value']}")

    if dryrun.active():
        # The cookie directory may be the live instance's: a dry run writes nothing there.
        held = len(lines) - _COOKIE_HEADER_LINES
        dryrun.would_have(name, "write cookie file", cookies=held)
        return _line(f"Dry run: the cookie file was not written. It would have held {held} cookies.")
    try:
        Path(cookie_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cookie_path).write_text("\n".join(lines) + "\n")
    except OSError as exc:
        return _line(f"The cookie file could not be written: {exc.strerror or type(exc).__name__}.", False)
    log.info("Cookies updated for %s (%d cookies written to %s)", name, len(cookies), cookie_path)

    # The client reads the file again on its next request.
    for tracker in search_service.get_trackers():
        if tracker.name == name:
            tracker._cookies_loaded = False
            try:
                live = await asyncio.to_thread(tracker.check_session)
            except Exception:
                live = False
            if live:
                return _line("Cookies saved, and the tracker accepts them.", True)
            return _line("Cookies saved, but the tracker does not accept them. They may have expired.")
    return _line("Cookies saved. The app has no client for this provider yet, so they were not tried.", True)


# --- formats ---


@router.post("/setup/formats", response_class=HTMLResponse)
async def add_format(request: Request, key: str = Form(""), kind: str = Form("card")):
    state = configfile.load()
    if not state.writable:
        return _refused(state)
    key = key.strip()
    doc = state.candidate()
    formats = _sequence(doc, "formats")
    formats.append(configfile.merged(None, {"key": key, "kind": kind}))
    try:
        errors = _commit(state, doc)
    except ConfigReadOnly as exc:
        return HTMLResponse(escape(str(exc)), status_code=409)
    if not errors:
        return _done(request, state, "formats")
    message = "; ".join(m for p, m in errors.items() if p.startswith(f"formats[{len(formats) - 1}]")) or "; ".join(
        f"{p}: {m}" for p, m in errors.items()
    )
    if is_htmx(request):
        return templates.TemplateResponse(request, "setup/_format_new.html", {
            "state": state, "format_error": message, "new_format": {"key": key, "kind": kind},
        })
    return await _page(request, state, {"format_error": message, "new_format": {"key": key, "kind": kind}}, 422)


@router.post("/setup/formats/{key}", response_class=HTMLResponse)
async def save_format(request: Request, key: str):
    """Save a format's word lists, or with ``op=test`` read the pasted post with them and save nothing."""
    state = configfile.load()
    data = await request.form()
    testing = data.get("op") == "test"
    if not state.writable and not testing:
        return _refused(state)
    values = _format_posted(data, key)
    form: dict[str, Any] = {"vals": values, "errors": {}, "problems": [], "parsed": None}
    saved = False
    if testing:
        try:
            compiled = compile_format(FormatProfile.model_validate(_format_yaml(values)))
        except ValidationError as exc:
            form["problems"] = [
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False)
            ]
        except FormatCompileError as exc:
            form["problems"] = [str(exc)]
        else:
            if values["sample"].strip():
                form["parsed"] = read_post([compiled], values["sample"], state.config)
            else:
                form["problems"] = ["Paste a post to read first."]
    else:
        doc = state.candidate()
        formats = _sequence(doc, "formats")
        at = _index(formats, key)
        if at is None:
            return HTMLResponse(escape(f"There is no format {key}."), status_code=404)
        formats[at] = configfile.merged(formats[at], _format_yaml(values))
        try:
            errors = _commit(state, doc)
        except ConfigReadOnly as exc:
            return HTMLResponse(escape(str(exc)), status_code=409)
        form["errors"], form["problems"] = _entry_errors(errors, f"formats[{at}]", _FORMAT_FIELDS)
        saved = not errors
    if saved:
        return _done(request, state, "formats")
    if is_htmx(request):
        return templates.TemplateResponse(request, "setup/_format_form.html", {
            "form": form, "state": state, "format_typed": FORMAT_TYPED, "format_lists": FORMAT_LISTS,
            "media_types": MEDIA_TYPES,
        })
    return await _page(request, state, {"format": form}, 422 if not testing else 200)


@router.post("/setup/formats/{key}/delete", response_class=HTMLResponse)
async def delete_format(request: Request, key: str):
    return await _delete(request, "formats", key)
