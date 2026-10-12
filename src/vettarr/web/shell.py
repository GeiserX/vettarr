"""What the layout shows on every page: the banner and the nav's "needs you" count.

The banner says which providers the app is not talking to right now.

Read from the ``events`` table, never from the provider: the tracker guard
writes a ``guard`` event when it opens a cooldown (``state: cooldown``, with
the reason and how many seconds it holds) and when a request works again
(``state: ok``). A provider is failing while its latest event is a cooldown
whose seconds have not run out. Loading a page sends nothing to a provider.

The count beside Inbox is how many items wait for a person. ``waiting_items``
says which those are, once: the badge counts that query and Activity's "still
waiting for you" filter lists the events of the same items. They are the
Inbox's "Needs you" tab and Review's queue (``cards.NEEDS_YOU``): an item to
decide, a post nothing could read, and one that stopped on an error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import func, select

from vettarr import events
from vettarr.database import async_session
from vettarr.models import Event, MediaItem
from vettarr.web.cards import NEEDS_YOU

log = logging.getLogger(__name__)

_LOOK_BACK = timedelta(hours=24)
_CACHE_SECONDS = 20.0
_cache: tuple[float, list[Failing]] | None = None


@dataclass(frozen=True)
class Failing:
    name: str
    reason: str
    minutes: int


def failing_from(rows: list[Any], now: datetime) -> list[Failing]:
    """The providers in an open cooldown, from ``guard`` events newest first."""
    seen: set[str] = set()
    out: list[Failing] = []
    for row in rows:
        payload = row.payload or {}
        name = payload.get("provider")
        if not name or name in seen:
            continue
        seen.add(name)
        if payload.get("state") != "cooldown":
            continue
        at = row.at if row.at.tzinfo else row.at.replace(tzinfo=timezone.utc)
        left = (at + timedelta(seconds=int(payload.get("seconds") or 0)) - now).total_seconds()
        if left > 0:
            out.append(Failing(str(name), str(payload.get("reason") or "not answering"), max(1, round(left / 60))))
    return sorted(out, key=lambda f: f.name)


async def failing() -> list[Failing]:
    """What the banner shows. Cached for a few seconds; never raises."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as session:
            rows = (await session.execute(
                select(Event)
                .where(Event.kind == events.GUARD, Event.at > now - _LOOK_BACK)
                .order_by(Event.at.desc(), Event.id.desc())
                .limit(200)
            )).scalars().all()
        found = failing_from(list(rows), now)
    except Exception as exc:
        # The page's own queries say when the database is down; the banner
        # must not be what breaks a page.
        log.debug("The banner could not read the guard events: %s", type(exc).__name__)
        found = []
    _cache = (time.monotonic(), found)
    return found


def waiting_items():
    """The ids of the items waiting for a person's decision, as a query."""
    return select(MediaItem.id).where(MediaItem.status.in_(sorted(NEEDS_YOU, key=lambda s: s.value)))


async def needs_you() -> int:
    """How many items wait for a person. Not cached: a decision must show at once. Never raises."""
    try:
        async with async_session() as session:
            return (await session.execute(
                select(func.count()).select_from(waiting_items().subquery())
            )).scalar() or 0
    except Exception as exc:
        log.debug("The nav could not count the waiting items: %s", type(exc).__name__)
        return 0


async def load(request: Request) -> None:
    """Put the banner's rows and the nav's count on the request, for a full page with the layout only."""
    if request.method != "GET" or request.headers.get("HX-Request"):
        return
    if request.scope["path"] in ("/login", "/openapi.json"):
        return
    request.state.failing = await failing()
    request.state.needs_you = await needs_you()
