"""``dry_run``: nothing outside this database is changed.

A preview runs beside the live instance against the same download client,
the same Radarr, Sonarr and Seerr, the same trackers and the same Telegram
chats, on a copy of the live database. ``auto_approve: shadow`` only stops
the policy's own actions: a person clicking Approve still acts. Under
``dry_run`` nothing acts, whoever asks.

Each client that writes to the outside world checks ``active()`` at one
place of its own, and there sends nothing and calls ``would_have`` instead:

- the download client (``QBittorrentClient._request`` and its ``.torrent``
  fetch from an indexer link);
- Radarr, Sonarr and Seerr (``delivery.http.Api.request``);
- the trackers (``Unit3dClient``: thank, keep-alive, the ``.torrent`` fetch,
  the cookie file it saves, and ``_ProviderSession.send`` for anything with
  a body);
- Telegram, both the userbot (``telegram_writes.guard_userbot``) and the bot
  (``telegram_writes.GuardedBotRequest``);
- the notifier (``notify.send``) and the Setup page's cookie upload.

Reads stay real: searches, TMDB, the IMDb guide, the book catalogue,
Jellyfin, the GET routes of the arrs and of Seerr. A ``.torrent`` fetched
from a tracker is read-only HTTP, but a private tracker counts it as a
download, so it is a write here.

What the clients answer instead of the real thing lets the item move on as
if it had happened. The download client answers an add with a torrent whose
hash is marked (``mark``), and reads that torrent back as complete; a
delivery to Radarr, Sonarr or Seerr is recorded with a marked id, and the
download poll finds it done. So an approved item is delivered, by would-have,
and completed at the next check.

``DRY_RUN=1`` (or true, yes, on) in the environment turns it on whatever
the file says. It cannot turn it off.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from vettarr import events

log = logging.getLogger(__name__)

ENV = "DRY_RUN"
_ON = ("1", "true", "yes", "on")
#: What ``config check`` and the start say.
SUMMARY = "nothing outside this database is changed"
#: The reason an item gets when a ``.torrent`` was not fetched from a tracker.
NOT_FETCHED = "dry run: torrent not fetched"
#: What a hash or an id a dry run made up starts with.
SIMULATED = "dry-run"

F = TypeVar("F", bound=Callable[..., Any])

_item: ContextVar[int | None] = ContextVar("dry_run_item", default=None)


def env_on(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``DRY_RUN`` in the environment asks for a dry run."""
    env = os.environ if env is None else env
    return (env.get(ENV) or "").strip().lower() in _ON


def active() -> bool:
    """Whether this process changes nothing outside its own database. Never raises."""
    if env_on():
        return True
    try:
        from vettarr.config import settings

        return bool(settings.config.dry_run)
    except Exception:
        # No config at all: the app cannot start, and nothing is sent.
        return False


@contextmanager
def about(item_id: int | None) -> Iterator[None]:
    """Name the item the writes inside are for, so each ``would_have`` carries its id."""
    token = _item.set(item_id)
    try:
        yield
    finally:
        _item.reset(token)


def _id_of(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    for name in ("id", "item_id", "media_item_id"):
        found = getattr(value, name, None)
        if isinstance(found, int) and not isinstance(found, bool):
            return found
    return None


def about_item(parameter: str) -> Callable[[F], F]:
    """Decorate an async function: the writes it makes carry the id of the item ``parameter`` names.

    ``parameter`` holds the item's id, or an object with ``id``, ``item_id``
    or ``media_item_id``.
    """

    def decorate(func: F) -> F:
        signature = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                value = signature.bind_partial(*args, **kwargs).arguments.get(parameter)
            except TypeError:
                value = None
            with about(_id_of(value)):
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def would_have(target: str, action: str, *, method: str | None = None, path: str | None = None,
               item_id: int | None = None, **detail: Any) -> None:
    """Record what would have been sent: one ``would_have`` event, and a log line.

    ``path`` is a path or an address already cut down for a log
    (``providers.shown``). Nothing passed here may be a secret, a cookie or
    a message's text.
    """
    item = item_id if item_id is not None else _item.get()
    payload: dict[str, Any] = {"dry_run": True, "target": target, "action": action}
    if method:
        payload["method"] = method
    if path:
        payload["path"] = path
    payload.update({k: v for k, v in detail.items() if v is not None})
    events.emit(events.WOULD_HAVE, item, **payload)
    log.info("Dry run: would have %s (%s%s)", action, target, f", item {item}" if item is not None else "")


def is_dry_run_event(payload: Any) -> bool:
    """Whether a ``would_have`` payload is a dry run's, not shadow mode's decision."""
    return isinstance(payload, Mapping) and bool(payload.get("dry_run"))


def mark(value: Any = None) -> str:
    """An id or a hash a dry run made, or touched: ``dry-run`` or ``dry-run:<value>``."""
    if value is None or value == "":
        return SIMULATED
    text = str(value)
    if simulated(text):
        return text
    return f"{SIMULATED}:{text}"


def simulated(value: Any) -> bool:
    """Whether an id or a hash is one a dry run made."""
    return isinstance(value, str) and (value == SIMULATED or value.startswith(SIMULATED + ":"))


def unmark(value: Any) -> Any:
    """The real id inside a marked one, or None when the dry run made it up."""
    if not simulated(value):
        return value
    real = value[len(SIMULATED) + 1:]
    return real or None
