"""The one place each Telegram client sends from, and what ``dry_run`` does there.

The bot (python-telegram-bot) sends every Bot API call through its request
object, ``GuardedBotRequest``. The userbot (Telethon) sends every request
through ``TelegramClient._call``, which ``guard_userbot`` wraps. Under
``dry_run`` a call that changes something in Telegram (send, edit, delete,
react, answer a button) is not sent: it is a ``would_have`` event naming the
method and the chat, never the text. Reads (getting updates, a chat, a
member, a file, the history) go through as they are.
"""

from __future__ import annotations

import json
import time
from typing import Any

from telegram.request import HTTPXRequest

from vettarr import dryrun

#: What a Bot API method that only reads starts with.
_BOT_READS = ("get",)
#: What a Telethon request that only reads starts with.
_USERBOT_READS = ("Get", "Search", "Resolve", "Check")
#: Requests Telethon sends on its own while it reads media: exporting this
#: session's authorization to the data centre that holds a file, and asking
#: a CDN to hold a file again. Neither changes anything anybody sees.
_USERBOT_READ_NAMES = frozenset({"ExportAuthorizationRequest", "ReuploadCdnFileRequest"})


def _bot_method(url: str) -> str:
    # https://api.telegram.org/bot<token>/<method>: only the method leaves this function.
    return url.rstrip("/").rsplit("/", 1)[-1]


def bot_writes(method: str) -> bool:
    """Whether a Bot API method changes something in Telegram."""
    return not method.lower().startswith(_BOT_READS)


def _answer(method: str, parameters: dict[str, Any]) -> Any:
    """What the Bot API would have answered: a message for a send or an edit, True for the rest."""
    lowered = method.lower()
    chat = parameters.get("chat_id")
    if lowered.startswith(("send", "copy", "forward")) or (lowered.startswith("edit") and chat is not None):
        chat_id = chat if isinstance(chat, int) else 0
        return {"message_id": 0, "date": int(time.time()), "chat": {"id": chat_id, "type": "private"}}
    return True


class GuardedBotRequest(HTTPXRequest):
    """The bot's request object: under ``dry_run`` a write is a ``would_have`` event."""

    async def do_request(self, url: str, method: str, request_data=None, *args: Any, **kwargs: Any):
        name = _bot_method(url)
        if method.upper() == "POST" and bot_writes(name) and dryrun.active():
            parameters = dict(request_data.parameters) if request_data is not None else {}
            dryrun.would_have(
                "telegram bot", name, method="POST", path=name,
                chat_id=parameters.get("chat_id"), message_id=parameters.get("message_id"),
            )
            return 200, json.dumps({"ok": True, "result": _answer(name, parameters)}).encode()
        return await super().do_request(url, method, request_data, *args, **kwargs)


def userbot_writes(request: Any) -> bool:
    """Whether a Telethon request changes something in Telegram."""
    name = type(request).__name__
    return not (name.startswith(_USERBOT_READS) or name in _USERBOT_READ_NAMES)


def _message_ids(request: Any) -> list[int] | None:
    """The message ids a Telethon request acts on, when it names them as numbers."""
    for name in ("id", "msg_id"):
        value = getattr(request, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return [value]
        if isinstance(value, list) and value and all(isinstance(v, int) for v in value):
            return value
    return None


def guard_userbot(client: Any) -> Any:
    """Route every request of a Telethon client through the ``dry_run`` check. Returns the client."""
    send = client._call

    async def _call(sender, request, ordered=False, flood_sleep_threshold=None):
        asked = list(request) if isinstance(request, (list, tuple)) else [request]
        writes = [r for r in asked if userbot_writes(r)]
        if writes and dryrun.active():
            for r in writes:
                dryrun.would_have(
                    "telegram userbot", type(r).__name__, path=type(r).__name__, message_ids=_message_ids(r),
                )
            return [None] * len(asked) if isinstance(request, (list, tuple)) else None
        return await send(sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

    client._call = _call
    return client
