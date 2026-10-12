"""The one ``Config`` the running app reads.

``get_config()`` resolves it once per process, on first use:

- ``CONFIG_PATH`` (default ``config.yaml`` in the working directory) exists:
  the file is loaded and validated.
- It does not: the ``Config`` is built in memory from the environment
  variables, through the same mapping ``config import-env`` writes to a file
  (``import_env.document``). Nothing is written.

One log line says which, never a value. With no file a warning first names
the path that was not found, and after the boot a second warning
says what the environment cannot carry, so an environment boot is never read
as the setup the operator had before. An invalid file raises ``ConfigError``
with its anchored messages; with no file, a missing variable the app always
needed raises the old ``Missing required environment variable: NAME``.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from vettarr.config.loader import DEFAULT_PATH, ConfigError, load, validate_document
from vettarr.config.schema import Config

log = logging.getLogger(__name__)

# What the app could not start without before config.yaml existed. Booting
# from the environment still needs them, with the same error.
REQUIRED_ENV = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "TELEGRAM_GROUP_ID",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
    "TMDB_API_KEY",
    "PROWLARR_URL",
    "PROWLARR_API_KEY",
    "QBITTORRENT_URL",
)
FROM_ENVIRONMENT = "environment, no config file"
# What an environment boot is missing. These lived in code before config.yaml
# and live only in the file now.
ENVIRONMENT_ONLY = (
    "No config file: posts are read as scene release names only. The card format (labels, "
    "type headers, media words, ignored hosts), tracker link patterns, download-client "
    "categories and save paths, the release words, and the keyword, genre and subject lists are "
    "not in the environment. So every download is added with no category and no save path, and lands in "
    "the download client's default folder. No tracker is thanked and no session is kept alive: "
    "both are off until a file turns them on. "
    "Languages, subtitles and whole seasons are read with the English release words only: a post "
    "that names them in another language's words reads as naming none, so a rule on "
    "audio_language, subtitles or season_complete sees missing data. "
    "The policy is the public default, not the one this deployment decided with: no keyword, "
    "genre or subject rule and no certification rule for your country, so a title only those "
    "held for review is approved now, and a book type that had only subject rules has no rule "
    "left and is held for a person. "
    "Write them to a config.yaml (`config import-env` starts one) and boot from it."
)

_lock = threading.Lock()
_config: Config | None = None


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CONFIG_PATH") or DEFAULT_PATH)


def resolve(env: Mapping[str, str] | None = None) -> tuple[Config, str]:
    """The Config and where it came from: the file's path, or ``FROM_ENVIRONMENT``."""
    env = os.environ if env is None else env
    path = config_path(env)
    if path.exists():
        try:
            return load(path, env), str(path)
        except OSError as exc:
            raise ConfigError([f"{path}: cannot be read: {exc.strerror or exc}"]) from None
    # Before the environment is read: a missing variable stops the boot, and
    # the file that was not found is usually the real cause.
    log.warning("No config file at %s: booting from the environment variables", path)
    return from_environment(env), FROM_ENVIRONMENT


def from_environment(env: Mapping[str, str] | None = None) -> Config:
    """The Config today's environment variables stand for, built in memory."""
    from vettarr.config.import_env import document

    env = os.environ if env is None else env
    for name in REQUIRED_ENV:
        if not env.get(name):
            raise RuntimeError(f"Missing required environment variable: {name}")
    return validate_document(document(env), "<environment>", env)


def get_config() -> Config:
    """The process-wide Config, resolved on first call."""
    global _config
    if _config is None:
        with _lock:
            if _config is None:
                config, source = resolve()
                log.info("Config: %s", source)
                if source == FROM_ENVIRONMENT:
                    log.warning(ENVIRONMENT_ONLY)
                _config = config
    return _config


def reset() -> None:
    """Forget the resolved Config; the next ``get_config()`` resolves again. For tests."""
    global _config
    with _lock:
        _config = None
