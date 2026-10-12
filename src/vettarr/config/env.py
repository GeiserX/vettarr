"""``settings``: the flat view of the config the app's modules read.

Every attribute resolves from the process-wide ``Config``
(``runtime.get_config()``), which comes from ``config.yaml`` or, with no file,
from the environment variables. Nothing is read when a module is imported: the
Config is resolved on the first attribute that needs it, so importing any
module never raises. ``POSTGRES_*`` and ``LOG_LEVEL`` stay environment
variables and are read from the environment directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from vettarr.config.runtime import get_config
from vettarr.config.schema import QBIT_DEFAULT_ACCOUNT, Config, Telegram
from vettarr.config.signals import MEDIA_TYPES

load_dotenv()

_DEFAULT_WAIT_HOURS = 24
_DEFAULT_WEB_PORT = 4850


_PRODUCT = "vettarr"


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _secret(value) -> str:
    return value.get_secret_value() if value is not None else ""


@dataclass(frozen=True)
class TrackerConfig:
    """Configuration for a single UNIT3D tracker."""

    name: str
    url: str
    cookie_path: str
    username: str
    password: str
    # Text only a logged-out page carries. Empty means the client's default,
    # the login form's `name="password"` field, which reads the same in every
    # language; a tracker whose pages differ sets its own.
    logged_out_marker: str = ""


class Settings:
    """A view over a ``Config``: ``Settings(cfg)`` reads ``cfg``, ``Settings()`` the process one."""

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    @property
    def config(self) -> Config:
        return self._config if self._config is not None else get_config()

    # --- telegram ---

    @property
    def _telegram(self) -> Telegram:
        return self.config.telegram or Telegram()

    @property
    def telegram_api_id(self) -> int:
        return self._telegram.api_id or 0

    @property
    def telegram_api_hash(self) -> str:
        return _secret(self._telegram.api_hash)

    @property
    def telegram_phone(self) -> str:
        return _secret(self._telegram.phone)

    @property
    def telegram_group_id(self) -> int | str:
        """The chat of the first userbot source; 0 when there is none."""
        return next((s.chat for s in self.config.sources if s.kind == "userbot"), 0)

    @property
    def telegram_bot_token(self) -> str:
        return _secret(self._telegram.bot_token)

    @property
    def telegram_owner_id(self) -> int:
        """The first reviewer; 0 when there is none."""
        reviewers = self._telegram.reviewers
        return reviewers[0] if reviewers else 0

    @property
    def telegram_session_path(self) -> Path:
        """The Telethon session file, without ``.session``."""
        return Path(self._telegram.session_path)

    @property
    def session_dir(self) -> Path:
        return self.telegram_session_path.parent

    # --- integrations ---

    def _on(self, name: str):
        integrations = self.config.integrations
        return getattr(integrations, name) if integrations.enabled(name) else None

    @property
    def tmdb_api_key(self) -> str:
        block = self._on("tmdb")
        return _secret(block.api_key) if block else ""

    @property
    def prowlarr_url(self) -> str:
        block = self._on("prowlarr")
        return (block.url or "") if block else ""

    @property
    def prowlarr_api_key(self) -> str:
        block = self._on("prowlarr")
        return _secret(block.api_key) if block else ""

    @property
    def qbittorrent_url(self) -> str:
        block = self._on("qbittorrent")
        return (block.url or "") if block else ""

    @property
    def qbittorrent_user(self) -> str:
        block = self._on("qbittorrent")
        return block.username if block else QBIT_DEFAULT_ACCOUNT

    @property
    def qbittorrent_pass(self) -> str:
        block = self._on("qbittorrent")
        return _secret(block.password) if block else ""

    @property
    def google_books_api_key(self) -> str:
        block = self._on("google_books")
        return _secret(block.api_key) if block else ""

    # --- providers ---

    @property
    def trackers(self) -> tuple[TrackerConfig, ...]:
        return tuple(
            TrackerConfig(
                name=p.key,
                url=p.base_url,
                cookie_path=p.auth.cookie_file or "",
                username=p.auth.username or "",
                password=_secret(p.auth.password),
                logged_out_marker=p.logged_out_marker or "",
            )
            for p in self.config.providers
        )

    # --- policy ---

    @property
    def auto_approve_enabled(self) -> bool:
        # Only `on` acts; `shadow` decides and records without acting.
        return self.config.policy.auto_approve == "on"

    @property
    def auto_approve_types(self) -> frozenset[str]:
        policy = self.config.policy
        return frozenset(t for t in MEDIA_TYPES if policy.type_policy(t).mode != "never")

    @property
    def min_guide_votes(self) -> int:
        policy = self.config.policy
        if policy.min_votes is not None:
            return policy.min_votes
        return self.config.integrations.imdb_guide.min_votes

    @property
    def auto_approve_max_wait_hours(self) -> int:
        """The wait of the first type decided by rules (import-env writes one wait for all)."""
        policy = self.config.policy
        return next(
            (tp.max_wait_hours for t in MEDIA_TYPES if (tp := policy.type_policy(t)).mode == "rules"),
            _DEFAULT_WAIT_HOURS,
        )

    # --- web ---

    @property
    def web_port(self) -> int:
        web = self.config.web
        return web.port if web is not None else _DEFAULT_WEB_PORT

    # --- environment only: never in the file ---
    # With no POSTGRES_USER, POSTGRES_PASSWORD or POSTGRES_DB, each is the
    # product's name: what a local Postgres for development is set up with.

    @property
    def postgres_host(self) -> str:
        return _optional("POSTGRES_HOST", "localhost")

    @property
    def postgres_port(self) -> int:
        return int(_optional("POSTGRES_PORT", "5432"))

    @property
    def postgres_user(self) -> str:
        return _optional("POSTGRES_USER", _PRODUCT)

    @property
    def postgres_password(self) -> str:
        return _optional("POSTGRES_PASSWORD", _PRODUCT)

    @property
    def postgres_db(self) -> str:
        return _optional("POSTGRES_DB", _PRODUCT)

    @property
    def log_level(self) -> str:
        return _optional("LOG_LEVEL", "INFO")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
