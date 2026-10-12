"""The notifier: each event row, POSTed as JSON to ``notify.url``.

On only while config.yaml has a ``notify`` block. The body is the event row
(id, at, kind, item_id, payload) plus ``title``, ``body`` and ``type``, the
three fields an Apprise API server's ``/notify`` endpoint reads, so the same
URL can be a plain webhook or an Apprise API endpoint. A URL that is not
http(s) is not something this module can POST to; it says so once.

One attempt per event with a 5 s timeout. A failure is logged at most once an
hour, never with the URL (it usually carries a token), and is never retried:
the event row stays in the table either way.

A receiver that does not answer (a timeout, a refused connection) is left
alone for five minutes: the events of that time are not sent, so a dead URL
costs the event writer one timeout every five minutes and not one per event.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from vettarr import dryrun

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
FAILURE_LOG_SECONDS = 3600.0
# How long nothing is sent after the receiver failed to answer at all.
PAUSE_SECONDS = 300.0

_TITLES = {
    "decision": "Decision",
    "would_have": "Shadow decision",
    "needs_you": "Needs your decision",
    "guard": "Tracker session",
    "cookie_expired": "Tracker session expired",
    "bot_added": "Added through the bot",
    "delivered": "Delivered",
    "completed": "Completed",
    "delivery_failed": "Delivery failed",
    "declined": "Declined at the target",
    "webhook_test": "Seerr's webhook test arrived",
    "bot_chat": "The bot joined or left a chat",
}
_WARNINGS = frozenset({"needs_you", "cookie_expired", "guard", "delivery_failed", "declined"})

# Indirection so tests can drive the clock and the HTTP client.
_clock = time.monotonic
_client: httpx.AsyncClient | None = None
_last_failure_log = float("-inf")
_failures_since_log = 0
_paused_until = float("-inf")


def _url() -> str | None:
    from vettarr.config.runtime import get_config

    block = get_config().notify
    if block is None:
        return None
    return block.url.get_secret_value()


def _body(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    parts = [str(payload[key]) for key in ("title", "provider", "target", "outcome", "state") if payload.get(key)]
    reasons = payload.get("reasons")
    if isinstance(reasons, list) and reasons:
        parts.append("; ".join(str(r) for r in reasons[:3]))
    return " - ".join(parts) or event.get("kind", "")


def message(event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("kind", ""))
    return {
        **event,
        "title": f"vettarr: {_TITLES.get(kind, kind)}",
        "body": _body(event),
        "type": "warning" if kind in _WARNINGS else "info",
    }


def _note_failure(what: str) -> None:
    global _last_failure_log, _failures_since_log
    _failures_since_log += 1
    now = _clock()
    if now - _last_failure_log >= FAILURE_LOG_SECONDS:
        log.warning(
            "Notification failed (%s); %d failed since the last report, next report in an hour at most",
            what, _failures_since_log,
        )
        _last_failure_log = now
        _failures_since_log = 0


def _pause() -> None:
    global _paused_until
    _paused_until = _clock() + PAUSE_SECONDS


async def send(event: dict[str, Any]) -> bool:
    """POST one event. True when the receiver answered 2xx. Never raises."""
    global _client
    try:
        url = _url()
    except Exception:
        return False
    if not url:
        return False
    if not url.lower().startswith(("http://", "https://")):
        _note_failure("notify.url is not http(s); point it at an Apprise API /notify endpoint")
        return False
    if dryrun.active():
        # Never the URL: it usually carries a token. A dry run's own event is
        # not announced again, or each one would make the next.
        if not (event.get("kind") == "would_have" and dryrun.is_dry_run_event(event.get("payload"))):
            dryrun.would_have(
                "notify", "notify", method="POST", item_id=event.get("item_id"),
                event_id=event.get("id"), event_kind=event.get("kind"),
            )
        return False
    if _clock() < _paused_until:
        _note_failure("the receiver did not answer; nothing is sent for a few minutes")
        return False
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        resp = await _client.post(url, json=message(event))
    except httpx.TimeoutException:
        _pause()
        _note_failure("timeout")
        return False
    except httpx.TransportError as exc:
        _pause()
        _note_failure(type(exc).__name__)
        return False
    except Exception as exc:
        _note_failure(type(exc).__name__)
        return False
    if not resp.is_success:
        _note_failure(f"HTTP {resp.status_code}")
        return False
    return True
