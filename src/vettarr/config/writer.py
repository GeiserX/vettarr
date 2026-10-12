"""Write config.yaml back without losing the operator's comments.

The writer works on the round-trip document (``loader.read_document``), never
on a loaded ``Config``: the document still holds ``${VAR}`` where the file
had it, so a write never turns a reference into the secret it points at.

Order of a write: refuse under ``CONFIG_READONLY``, validate the new text,
copy the current file to ``<path>.bak``, then replace the file atomically.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from vettarr.config.loader import dump, parse, validate_document

_TRUE = {"1", "true", "yes", "on"}


class ConfigReadOnly(PermissionError):
    """CONFIG_READONLY is set: the file is managed outside the app."""


def readonly(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("CONFIG_READONLY", "").strip().lower() in _TRUE


def write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    validate: bool = True,
) -> None:
    path = Path(path)
    if readonly(env):
        raise ConfigReadOnly(f"CONFIG_READONLY is set: {path} is managed outside the app, not writing it")
    if validate:
        validate_document(parse(text, str(path)), str(path), env)
    mode = 0o600
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_document(
    path: str | os.PathLike[str],
    doc: CommentedMap,
    *,
    env: Mapping[str, str] | None = None,
    validate: bool = True,
) -> None:
    """Write a round-trip document (from ``read_document``, edited in place)."""
    write_text(path, dump(doc), env=env, validate=validate)
