"""Read config.yaml into a validated ``Config``.

Every error names the file, the line and the dotted path of the setting, so
``config check`` output points at what to fix. ``${VAR}`` in any string is
replaced from the environment at load; a variable that is not set is an
error that names the variable, never a value. ``$${`` writes a literal ``${``.
A block with ``enabled: false`` drops every setting that holds a ``${VAR}``
before validation, so a disabled block may reference variables that are not
set yet. ``enabled`` itself may be a ``${VAR}``; it is read first.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError

from vettarr import dryrun
from vettarr.config.schema import Config, Loc, cross_check
from vettarr.config.schema import warnings as schema_warnings

DEFAULT_PATH = "config.yaml"
_VAR = re.compile(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_OFF_WORDS = {"false", "off", "no", "0"}


class ConfigError(ValueError):
    """The file is not a valid config. ``errors`` holds one line per problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def default_path() -> Path:
    return Path(os.environ.get("CONFIG_PATH", DEFAULT_PATH))


def yaml() -> YAML:
    """The round-trip parser: keeps comments, order and line numbers."""
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def parse(text: str, source: str = "<config>") -> CommentedMap:
    """Parse YAML text into a round-trip document. Raises ConfigError."""
    try:
        doc = yaml().load(text)
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark else 1
        raise ConfigError([f"{source}:{line}: not valid YAML: {exc.problem or exc}"]) from None
    if doc is None:
        return CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ConfigError([f"{source}:1: the file must be a mapping of blocks (sources:, policy:, ...)"])
    return doc


def read_document(path: str | os.PathLike[str]) -> CommentedMap:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError([f"{path}: no such file"]) from None
    return parse(text, str(path))


def load(path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Read, expand and validate the config file at ``path``."""
    path = Path(path) if path is not None else default_path()
    config, _ = _load_document(read_document(path), str(path), env)
    return config


def load_text(text: str, source: str = "<config>", env: Mapping[str, str] | None = None) -> Config:
    config, _ = _load_document(parse(text, source), source, env)
    return config


def check(path: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> tuple[Config, list[str]]:
    """Like ``load``, and also return the warnings, line-anchored."""
    path = Path(path)
    return _load_document(read_document(path), str(path), env)


def validate_document(doc: CommentedMap, source: str, env: Mapping[str, str] | None = None) -> Config:
    config, _ = _load_document(doc, source, env)
    return config


def _load_document(
    doc: CommentedMap, source: str, env: Mapping[str, str] | None
) -> tuple[Config, list[str]]:
    env = os.environ if env is None else env
    missing: list[tuple[Loc, str]] = []
    data = _expand(doc, (), env, missing)
    if missing:
        raise ConfigError([_anchor(doc, source, loc, msg) for loc, msg in missing])
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            [
                _anchor(doc, source, tuple(e["loc"]), _message(e), missing_field=e["type"] == "missing")
                for e in exc.errors(include_input=False)
            ]
        ) from None
    errors = cross_check(config)
    if errors:
        raise ConfigError([_anchor(doc, source, loc, msg) for loc, msg in errors])
    if not config.dry_run and dryrun.env_on(env):
        # DRY_RUN can only turn it on: a preview that forgot the line in its file is still safe.
        config = config.model_copy(update={"dry_run": True})
    return config,[_anchor(doc, source, loc, msg) for loc, msg in schema_warnings(config)]


def _message(error: Mapping[str, Any]) -> str:
    msg = str(error["msg"])
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            return msg[len(prefix):]
    return msg


def _expand(node: Any, loc: Loc, env: Mapping[str, str], missing: list[tuple[Loc, str]]) -> Any:
    """Plain Python copy of the document with ${VAR} replaced."""
    if isinstance(node, Mapping):
        enabled = node.get("enabled")
        if isinstance(enabled, str):
            enabled = _expand(enabled, (*loc, "enabled"), env, missing)
        disabled = _is_off(enabled)
        out = {}
        for key, value in node.items():
            if key == "enabled":
                out["enabled"] = plain(enabled)
            elif disabled and _has_reference(value):
                # The block is off: a placeholder for a variable not set yet is inert.
                continue
            else:
                out[str(key)] = _expand(value, (*loc, key), env, missing)
        return out
    if isinstance(node, list):
        return [_expand(v, (*loc, n), env, missing) for n, v in enumerate(node)]
    if isinstance(node, str):
        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name is None:
                return "${"
            value = env.get(name)
            if not value:
                missing.append((loc, f"environment variable {name} is not set or is empty"))
                return ""
            return value

        return _VAR.sub(sub, str(node))
    return plain(node)


def _has_reference(node: Any) -> bool:
    if isinstance(node, Mapping):
        return any(_has_reference(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_reference(v) for v in node)
    if isinstance(node, str):
        return any(m.group(1) is not None for m in _VAR.finditer(node))
    return False


def _is_off(value: Any) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() in _OFF_WORDS)


def plain(node: Any) -> Any:
    if isinstance(node, Mapping):
        return {str(k): plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [plain(v) for v in node]
    if isinstance(node, bool):
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    if node is None:
        return None
    return str(node)


def _anchor(doc: CommentedMap, source: str, loc: Loc, msg: str, missing_field: bool = True) -> str:
    """``source:line: dotted.path: message`` for a pydantic-style location.

    A last part that is not in the document is shown as a missing field only
    when ``missing_field``; otherwise it is a pydantic union member tag
    (``int``, ``str``) and is skipped.
    """
    node: Any = doc
    line = 1
    shown: list[str] = []
    for n, part in enumerate(loc):
        if isinstance(node, CommentedMap) and part in node:
            pos = node.lc.key(part)
            if pos:
                line = pos[0] + 1
            node = node[part]
            shown.append(str(part))
        elif isinstance(node, CommentedSeq) and isinstance(part, int) and 0 <= part < len(node):
            pos = node.lc.item(part)
            if pos:
                line = pos[0] + 1
            node = node[part]
            shown.append(f"[{part}]")
        elif missing_field and n == len(loc) - 1 and isinstance(part, str):
            # A missing field: point at its parent, name the field.
            shown.append(part)
        # Anything else is a union member tag from pydantic; skip it.
    path = ".".join(shown).replace(".[", "[") or "(top)"
    return f"{source}:{line}: {path}: {msg}"


def dump(doc: CommentedMap) -> str:
    buffer = io.StringIO()
    yaml().dump(doc, buffer)
    return buffer.getvalue()
