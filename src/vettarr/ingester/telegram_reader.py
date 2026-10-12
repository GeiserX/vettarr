from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from telethon import TelegramClient, events, utils
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from vettarr import ingest as intake
from vettarr.config import settings
from vettarr.database import async_session
from vettarr.models import MediaItem
from vettarr.telegram_writes import guard_userbot

if TYPE_CHECKING:
    from vettarr.config.schema import Source

log = logging.getLogger(__name__)

_PROGRESS_EVERY = 500

#: How often one message is tried before it is skipped, and the pause between tries.
_ATTEMPTS = 3
_RETRY_SECONDS = 60


class TelegramIngester:
    """Reads every ``userbot`` source's chat as the operator's account.

    ``since_last`` continues after the newest message of a chat that became an
    item, so nothing newer than a message still unread may become one: that
    message would never be read. Two rules keep it so. New messages are taken
    from the moment the app starts, but each waits until its own chat's
    history is read. And a chat's messages are ingested one at a time, in
    order; one that fails is tried again before the next is touched.
    """

    def __init__(self, sources: list[Source] | None = None) -> None:
        # Every request it sends passes the dry_run check (telegram_writes).
        self.client = guard_userbot(TelegramClient(
            str(settings.telegram_session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        ))
        configured = settings.config.sources if sources is None else sources
        self._sources = [s for s in configured if s.kind == "userbot"]
        #: Each source by its chat's id as Telethon marks it (-100... for a channel).
        self._chats: dict[int, Source] = {}
        #: New messages waiting their turn, by chat, oldest first.
        self._held: dict[int, deque] = {}
        #: Chats whose history is read (or needs no reading): their new messages may be ingested.
        self._open: set[int] = set()
        #: Chats whose waiting messages are being ingested right now.
        self._draining: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.client.start(phone=settings.telegram_phone)
        me = await self.client.get_me()
        log.info("Telethon connected as %s (id=%d)", me.first_name, me.id)
        await self.resolve_chats()

    async def resolve_chats(self) -> None:
        """Turn each source's chat (an id or an @username) into the id messages carry."""
        self._chats = {}
        for source in self._sources:
            self._chats[await self.client.get_peer_id(source.chat)] = source
        intake.set_resolved_chats({chat_id: source.key for chat_id, source in self._chats.items()})

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.client.disconnect()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Connect, take new messages, read the histories, then wait for the stop."""
        await self.start()
        self.subscribe()
        await self.backfill()
        await self.listen(stop_event)

    def subscribe(self) -> None:
        """Start taking new messages. Called before the histories are read.

        A post that arrives while any history is being read is kept, and
        ingested once its own chat's history is done.
        """
        self._open = {chat_id for chat_id, source in self._chats.items() if source.backfill == "off"}
        self.client.add_event_handler(self._on_new, events.NewMessage(chats=list(self._chats)))

    async def backfill(self) -> None:
        """Read each source's history as its ``backfill`` says.

        off: nothing. since_last: the messages after the last one ingested
        from that chat (all of them on a first run). full: from message 1.
        A message that cannot be read is logged and skipped; it never ends
        the read.
        """
        for chat_id, source in self._chats.items():
            if source.backfill != "off":
                min_id = 0
                if source.backfill == "since_last":
                    min_id = await self.get_last_ingested_id(chat_id) or 0
                log.info("Backfill of source %s after message %d...", source.key, min_id)
                count = 0
                async for message in self.client.iter_messages(chat_id, reverse=True, min_id=min_id):
                    await self._handle_or_skip(source, message)
                    count += 1
                    if count % _PROGRESS_EVERY == 0:
                        log.info("Backfill of source %s: %d messages read", source.key, count)
                log.info("Backfill of source %s complete: %d messages read", source.key, count)
            # Its history is read: what arrived meanwhile goes in now, and
            # new messages are ingested as they come.
            self._open.add(chat_id)
            self._drain_soon(chat_id, source)

    async def listen(self, stop_event: asyncio.Event) -> None:
        log.info("Listening for new messages in %d userbot source(s)", len(self._chats))
        await stop_event.wait()

    async def _on_new(self, event: events.NewMessage.Event) -> None:
        source = self._chats.get(event.chat_id)
        if source is None:
            return
        self._held.setdefault(event.chat_id, deque()).append(event.message)
        if event.chat_id in self._open:
            self._drain_soon(event.chat_id, source)

    def _drain_soon(self, chat_id: int, source: Source) -> None:
        if chat_id in self._draining or not self._held.get(chat_id):
            return
        self._draining.add(chat_id)
        task = asyncio.create_task(self._drain(chat_id, source))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, chat_id: int, source: Source) -> None:
        """Ingest a chat's waiting messages, oldest first, one at a time."""
        try:
            held = self._held[chat_id]
            while held:
                await self._handle_or_skip(source, held[0])
                held.popleft()
        finally:
            self._draining.discard(chat_id)

    async def _handle_or_skip(self, source: Source, message) -> None:
        """Ingest one message; never raises.

        A failure is tried again after a pause (the database or a lookup may
        be down for a moment). A message that keeps failing is logged and
        skipped, so one bad post cannot stop a history read or the app.
        """
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                await self._handle(source, message)
                return
            except Exception as exc:
                message_id = getattr(message, "id", None)
                if attempt == _ATTEMPTS:
                    log.exception(
                        "Source %s: message %s could not be read after %d tries; skipped",
                        source.key, message_id, _ATTEMPTS,
                    )
                    return
                log.warning(
                    "Source %s: message %s could not be read (%s); trying again in %d s",
                    source.key, message_id, type(exc).__name__, _RETRY_SECONDS,
                )
                await asyncio.sleep(_RETRY_SECONDS)

    async def _handle(self, source: Source, message) -> int | None:
        result = await intake.ingest(source, incoming(message, self.client))
        if result.outcome != "added" or result.item_id is None:
            return None
        await self._save_photo(message, result.item_id)
        return result.item_id

    async def _save_photo(self, message, item_id: int) -> None:
        if not message.photo:
            return

        async def download(dest: Path) -> None:
            await self.client.download_media(message.photo, file=str(dest))

        await intake.keep_photo(item_id, message.chat_id, message.id, download)

    async def get_last_ingested_id(self, chat_id: int) -> int | None:
        async with async_session() as session:
            result = await session.execute(
                select(MediaItem.telegram_message_id)
                .where(MediaItem.telegram_chat_id == chat_id)
                .order_by(MediaItem.telegram_message_id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()


def incoming(message, client) -> intake.Incoming:
    """A Telethon message as ``ingest`` reads it."""
    document_name = None
    document_size = None
    fetch = None
    if getattr(message, "document", None) is not None:
        file = getattr(message, "file", None)
        document_name = getattr(file, "name", None)
        document_size = getattr(file, "size", None)

        async def fetch() -> bytes:
            return await client.download_media(message, file=bytes)

    forward_from = None
    fwd = getattr(message, "fwd_from", None)
    if fwd is not None and fwd.channel_post and fwd.from_id is not None:
        forward_from = (utils.get_peer_id(fwd.from_id), fwd.channel_post)
    # Telethon gives a channel post its channel's id and an anonymous admin
    # the group's: only a user id (positive) is a person.
    sender_id = message.sender_id if (message.sender_id or 0) > 0 else None
    return intake.Incoming(
        chat_id=message.chat_id,
        message_id=message.id,
        text=_build_full_text(message) if message.text else "",
        sender_id=sender_id,
        forward_from=forward_from,
        document_name=document_name,
        fetch_document=fetch,
        document_size=document_size if isinstance(document_size, int) else None,
        posted_at=getattr(message, "date", None),
    )


def _build_full_text(message) -> str:
    """Build full text including entity URLs that aren't visible in plain text."""
    text = message.text or ""
    entity_urls = []
    for entity in message.entities or []:
        if isinstance(entity, MessageEntityTextUrl):
            entity_urls.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            url = text[entity.offset:entity.offset + entity.length]
            entity_urls.append(url)

    extra = [u for u in entity_urls if u not in text]
    if extra:
        text += "\n" + "\n".join(extra)
    return text
