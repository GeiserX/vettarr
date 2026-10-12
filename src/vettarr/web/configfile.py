"""The config file as the Policy and Setup pages read and change it.

A page shows what the file says, not what the process loaded: the file is
what a save changes. The two differ from a save until the next start, because
the app reads its config once; ``State.pending`` says so and the pages show it.

A page can change the file in one case only (``EDIT``): the file exists,
loads, and ``CONFIG_READONLY`` is not set. Otherwise it shows the config with
every write control disabled and says why:

- ``FILE``: ``CONFIG_READONLY`` is set, the file is managed outside the app.
- ``ENVIRONMENT``: there is no file, the app booted from environment variables.
- ``BROKEN``: the file on disk does not load; the page shows the running config.

A change is made on a second reading of the round-trip document, so ``${VAR}``
stays ``${VAR}`` and comments stay where they were. The changed document is
turned into text once (``validate``); that text is validated the way the
loader validates the file, and each error comes back under the dotted path of
its setting (``policy.rules[2].at_least``), which is how a page puts it
beside the field. Only then is the same text written, through
``config.writer``: what was checked is what lands in the file.

A secret is never handed to a template: ``secret_state`` says whether one is
set and, when the file names a variable, which.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from vettarr.config import loader, runtime, writer
from vettarr.config.loader import ConfigError
from vettarr.config.schema import Config
from vettarr.config.writer import ConfigReadOnly

EDIT = "edit"
FILE = "file"
ENVIRONMENT = "environment"
BROKEN = "broken"

RESTART = "Restart the container to apply it (docker compose restart app)."
# What a POST answers when the page cannot write: one line, shown as a toast.
REFUSED = {
    FILE: "Managed by file: CONFIG_READONLY is set, so nothing was changed. Edit the config file and restart.",
    ENVIRONMENT: "There is no config file to change: this instance booted from environment variables.",
    BROKEN: "The config file on disk does not load, so nothing was changed. Fix the file first.",
}

_SOURCE = "config"
_ANCHORED = re.compile(rf"^{_SOURCE}:\d+: (?P<path>[^:]*): (?P<message>.*)$", re.S)
_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
TOP = "(top)"
# Where the schema answers with a regular expression or with parser talk, the sentence a field shows instead.
_SAID = (
    (re.compile(r"^String should match pattern '\^https\?:"), "An address starts with http:// or https:// and names a host."),
    (re.compile(r"^String should match pattern '\^\[a-z0-9\]\[a-z0-9_-\]\*\$'"),
     "Lowercase letters, digits, - and _ only, starting with a letter or a digit."),
    (re.compile(r"^Input should be a valid integer"), "A whole number goes here."),
)


def current() -> Config:
    """The config the process runs on."""
    return runtime.get_config()


@dataclass
class State:
    #: What the page shows: the file's config, or the running one when there is no file to read.
    config: Config
    #: The file as a round-trip document; None when it cannot be edited from.
    doc: CommentedMap | None
    path: Path
    mode: str
    #: The file holds changes the running app has not loaded.
    pending: bool = False
    #: Why the file does not load (``BROKEN``).
    errors: tuple[str, ...] = ()
    _view: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def writable(self) -> bool:
        return self.mode == EDIT

    @property
    def refusal(self) -> str:
        return REFUSED.get(self.mode, "")

    @property
    def view(self) -> dict[str, Any]:
        """The settings as written: the file's own text (``${VAR}`` included) when there is a file."""
        if self._view is None:
            if self.doc is not None:
                self._view = loader.plain(self.doc)
            else:
                self._view = self.config.model_dump(by_alias=True)
        return self._view

    def raw(self, path: str) -> Any:
        """The written value at a dotted path (``integrations.tmdb.api_key``); None when absent."""
        node: Any = self.view
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return node

    def candidate(self) -> CommentedMap:
        """A second copy of the document, to change. Only in ``EDIT`` and ``FILE``.

        Parsed again from its text, not deep-copied: ruamel writes a deep copy
        correctly once, and the next time without the comment above a list's
        first entry, as text that no longer parses.
        """
        if self.doc is None:
            raise ConfigReadOnly(self.refusal)
        return loader.parse(loader.dump(self.doc), str(self.path))


def load() -> State:
    """Read the file as it is now. Never raises: a file that does not load is ``BROKEN``."""
    path = runtime.config_path()
    running = current()
    if not path.exists():
        return State(running, None, path, ENVIRONMENT)
    try:
        doc = loader.read_document(path)
        config = loader.validate_document(doc, str(path))
    except ConfigError as exc:
        return State(running, None, path, BROKEN, errors=tuple(exc.errors))
    except OSError as exc:
        return State(running, None, path, BROKEN, errors=(f"{path}: cannot be read: {exc.strerror or exc}",))
    return State(config, doc, path, FILE if writer.readonly() else EDIT, pending=config != running)


def validate(doc: CommentedMap) -> tuple[str, Config | None, dict[str, str]]:
    """``doc`` as the text to write, with the config it stands for or its errors by the dotted path of the setting.

    What is validated is that text, parsed again: it is what the next start
    reads, and a node added to ``doc`` has no line of its own for the loader
    to point an error at. ``doc`` is turned into text here and nowhere else;
    ``write`` takes the text.
    """
    text = loader.dump(doc)
    try:
        return text, loader.validate_document(loader.parse(text, _SOURCE), _SOURCE), {}
    except ConfigError as exc:
        errors: dict[str, str] = {}
        for line in exc.errors:
            match = _ANCHORED.match(line)
            path, message = (match["path"], match["message"]) if match else (TOP, line)
            message = next((sentence for pattern, sentence in _SAID if pattern.match(message)), message)
            errors[path] = f"{errors[path]}; {message}" if path in errors else message
        return text, None, errors


def write(state: State, text: str) -> None:
    """Write the text ``validate`` returned to the file. Raises ``ConfigReadOnly`` or ``ConfigError``; keeps a ``.bak``."""
    if not state.writable:
        raise ConfigReadOnly(state.refusal)
    writer.write_text(state.path, text)


# --- changing a document in place ---------------------------------------------


def _ident(node: Any) -> Any:
    if isinstance(node, Mapping):
        return node.get("id") or node.get("key")
    return None


def merged(old: Any, new: Any) -> Any:
    """``new`` written over ``old``, keeping every node of ``old`` that is still there.

    A mapping is changed key by key, a list of mappings is matched by ``id``
    or ``key`` and a list of words word by word, so the comments and the
    quoting of what did not change stay as the operator wrote them. A value
    equal to the old one is the old one.
    """
    if isinstance(new, Mapping):
        target = old if isinstance(old, CommentedMap) else CommentedMap()
        for key in [k for k in target if k not in new]:
            del target[key]
        for key, value in new.items():
            put(target, key, value)
        return target
    if isinstance(new, list):
        if isinstance(old, CommentedSeq) and loader.plain(old) == new:
            return old
        known = {_ident(o): o for o in old if _ident(o) is not None} if isinstance(old, list) else {}
        items = [merged(known.get(_ident(v)), v) for v in new]
        if isinstance(old, CommentedSeq) and not known:
            # A list of words: same node, so it keeps the style it was written in,
            # and a word that stays is the one written, quotes and all ("007", 0.50).
            written: dict[Any, Any] = {}
            for node in old:
                if not isinstance(node, (Mapping, list)):
                    value = loader.plain(node)
                    written.setdefault((type(value), value), node)
            old[:] = [v if isinstance(v, (Mapping, list)) else written.get((type(v), v), v) for v in items]
            return old
        fresh = CommentedSeq(items)
        if items and len(items) <= 8 and not any(isinstance(v, (Mapping, list)) for v in items):
            fresh.fa.set_flow_style()  # a short list of words reads best on one line
        return fresh
    if old is not None and not isinstance(old, (Mapping, list)) and loader.plain(old) == new:
        return old
    return new


def put(parent: CommentedMap, key: str, new: Any) -> None:
    """Set ``parent[key]`` to ``new`` the way ``merged`` does.

    When a list or a mapping is left with no entry, the comment that stood
    above its first entry goes with them: ruamel would write it between the
    key and the ``[]``, which is not YAML.
    """
    node = merged(parent.get(key), new)
    parent[key] = node
    above = parent.ca.items.get(key)
    if isinstance(node, (Mapping, list)) and not node and above and len(above) > 3:
        above[3] = None


def block(doc: CommentedMap, path: str, create: bool = False) -> CommentedMap | None:
    """The mapping at a dotted path; made on the way when ``create``."""
    node = doc
    for part in path.split("."):
        child = node.get(part)
        if not isinstance(child, CommentedMap):
            if not create:
                return None
            child = CommentedMap()
            node[part] = child
        node = child
    return node


# --- what a template may know about a secret ----------------------------------


def secret_state(raw: Any) -> str:
    """``""`` when nothing is set; otherwise words for the page, never the value."""
    if isinstance(raw, SecretStr):
        raw = raw.get_secret_value()
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    reference = _REFERENCE.match(str(raw).strip())
    return f"set, from ${{{reference[1]}}}" if reference else "set"


def reference(raw: Any) -> str | None:
    """The variable's name when the whole value is ``${VAR}``, else None."""
    if not isinstance(raw, str):
        return None
    found = _REFERENCE.match(raw.strip())
    return found[1] if found else None


def carries_secret(url: Any) -> bool:
    """True for a URL written with a password, a query or a fragment: any of those can hold a token."""
    if not isinstance(url, str) or "${" in url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    return bool(parts.password or parts.username or parts.query or parts.fragment)


def words(text: str) -> list[str]:
    """One word per line; blank lines dropped."""
    return [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]


def number(text: str) -> Any:
    """``text`` as an int or a float; as it is when it is neither, so validation names the field."""
    text = text.strip()
    if "${" in text:
        return text
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text
