"""Events: what happened, one row each, and the notifier fed from them.

``emit()`` is synchronous and never blocks or raises, so any code path can
call it, including the tracker guard's worker threads while they hold their
lock. It only queues the event. ``flush()`` (the scheduler runs it every few
seconds) writes the queued events to the ``events`` table and then hands
each written row to ``vettarr.notify``. A batch the database refused is
kept and written by the next flush. ``flush_all()`` empties the queue before
the process stops, and for a command that runs without the scheduler.

Kinds:

- ``decision``: the policy decided an item (payload: outcome, what was done,
  reasons, policy_version)
- ``would_have``: ``auto_approve: shadow`` decided an item and did nothing;
  or, with ``dry_run: true`` in the payload, a write to the outside world that
  ``dry_run`` did not send (payload: target, action, method, path)
- ``needs_you``: an item went to the review queue
- ``guard``: a tracker's session guard opened or closed its cooldown
- ``cookie_expired``: a tracker session could not be established
- ``bot_added``: an item arrived through a bot source
- ``delivered``: an approved item was handed to its ``deliver`` target
  (payload: target, and for Radarr, Sonarr and Seerr the id there)
- ``completed``: the target has the files, already had them, or the type is
  delivered nowhere (target ``none``)
- ``delivery_failed``: the target would not take the item, or lost it
- ``declined``: a Seerr request was declined or removed there
- ``webhook_test``: Seerr's test notification reached a ``seerr`` source's
  webhook (payload: source)
- ``bot_chat``: a reviewer added the bot to a chat, or the bot left a chat a
  reviewer added it to or a bot source reads
  (payload: chat_id, title, state ``joined`` or ``left``, is_admin,
  reads_all_messages); the Setup page offers a joined chat as a source

A payload never carries a secret, a cookie or a message's text.
"""

from __future__ import annotations

import logging
import queue
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

DECISION = "decision"
WOULD_HAVE = "would_have"
NEEDS_YOU = "needs_you"
GUARD = "guard"
COOKIE_EXPIRED = "cookie_expired"
BOT_ADDED = "bot_added"
DELIVERED = "delivered"
COMPLETED = "completed"
DELIVERY_FAILED = "delivery_failed"
DECLINED = "declined"
WEBHOOK_TEST = "webhook_test"
BOT_CHAT = "bot_chat"
KINDS = (
    DECISION, WOULD_HAVE, NEEDS_YOU, GUARD, COOKIE_EXPIRED, BOT_ADDED,
    DELIVERED, COMPLETED, DELIVERY_FAILED, DECLINED, WEBHOOK_TEST, BOT_CHAT,
)

# Bounded: a database that is down for a day must not grow this without end.
_MAX_PENDING = 10_000
_pending: queue.Queue[tuple[datetime, str, int | None, dict[str, Any]]] = queue.Queue(_MAX_PENDING)


def emit(kind: str, item_id: int | None = None, **payload: Any) -> None:
    """Queue one event. Never raises; a full queue drops the event."""
    try:
        _pending.put_nowait((datetime.now(timezone.utc), kind, item_id, payload))
    except queue.Full:
        pass


def pending() -> int:
    return _pending.qsize()


def _drain(limit: int) -> list[tuple[datetime, str, int | None, dict[str, Any]]]:
    taken = []
    while len(taken) < limit:
        try:
            taken.append(_pending.get_nowait())
        except queue.Empty:
            break
    return taken


def row_json(row: Any) -> dict[str, Any]:
    """An event row as the JSON the notifier sends."""
    at = row.at
    return {
        "id": row.id,
        "at": at.isoformat() if isinstance(at, datetime) else at,
        "kind": row.kind,
        "item_id": row.item_id,
        "payload": row.payload or {},
    }


async def flush(limit: int = 500) -> int:
    """Write queued events, then notify each. Returns how many were written."""
    taken = _drain(limit)
    if not taken:
        return 0
    from vettarr.database import async_session
    from vettarr.models import Event
    from vettarr.notify import send

    rows = [Event(at=at, kind=kind, item_id=item_id, payload=payload) for at, kind, item_id, payload in taken]
    try:
        async with async_session() as session:
            session.add_all(rows)
            await session.commit()
    except Exception as exc:
        # Back on the queue, not dropped: it is bounded to outlast an outage.
        # Each event keeps its own time, so the order it is written in later
        # does not matter.
        for event in taken:
            try:
                _pending.put_nowait(event)
            except queue.Full:
                break
        log.warning(
            "Could not write %d events (%s); they are kept for the next flush",
            len(rows), type(exc).__name__,
        )
        return 0
    for row in rows:
        await send(row_json(row))
    return len(rows)


async def flush_all(limit: int = 500) -> int:
    """Flush until nothing is queued, or until a write fails. Returns how many were written."""
    total = 0
    while pending():
        written = await flush(limit)
        if not written:
            break
        total += written
    return total
