from __future__ import annotations

import ipaddress
import logging
from html import escape as _h
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from telegram import ChatMember, Message, MessageOriginChannel, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from vettarr import actions, delivery, ingest
from vettarr.bot.keyboards import (
    confirm_reject_keyboard,
    review_keyboard,
    torrent_keyboard,
)
from vettarr.config import settings
from vettarr.database import async_session
from vettarr.models import (
    OP_RESCAN,
    AutoDecision,
    ContentCheck,
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
    SeverityRating,
)
from vettarr.telegram_writes import GuardedBotRequest

if TYPE_CHECKING:
    from vettarr.ingest import PostActions

log = logging.getLogger(__name__)

_ingester: PostActions | None = None
_bot_app: Application | None = None


def _authorized(func_handler):
    # An update with no user (a channel post, an anonymous admin) is nobody's
    # decision to make, so it is turned away like a stranger's.
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id not in ingest.reviewers():
            return
        return await func_handler(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Messaging helpers
# ---------------------------------------------------------------------------

async def _respond(query, text: str, reply_markup=None) -> Message:
    """Edit or replace a callback query's message with text content.

    Handles both photo messages (delete + send new) and text messages (edit).
    Returns the resulting Message so callers can edit it further.
    """
    if query.message and query.message.photo:
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass
        return await _bot_app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
        )
    return await query.edit_message_text(
        text=text, parse_mode="HTML", reply_markup=reply_markup,
    )


def item_url(item_id: int) -> str | None:
    """The item's page in the web UI, or None when the config does not say where the UI is.

    ``web.base_url`` is the address a reviewer's phone reaches the UI at. The
    session cookie is SameSite=Lax, so a link tapped in Telegram lands on the
    item, not on the login page.
    """
    web = settings.config.web
    base = (web.base_url or "") if web is not None else ""
    if not base:
        return None
    # The item's path goes after the address's own path, before any query it carries.
    parts = urlsplit(base)
    return urlunsplit(parts._replace(path=f"{parts.path.rstrip('/')}/item/{item_id}", fragment=""))


def button_url(url: str | None) -> str | None:
    """``url`` when Telegram takes it as a button's link, else None.

    Telegram refuses a message whose button links to ``localhost`` or to a
    host without a dot, and a refused message is a card nobody gets. Such an
    address stays a link in the card's text, which Telegram never refuses.
    """
    if not url:
        return None
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return url
    except ValueError:
        pass
    return url if "." in host and not host.endswith(".") else None


_AUTO_LABELS = {
    AutoDecision.AUTO_APPROVE: "\u2705 <b>Auto-approved</b>",
    AutoDecision.WAIT: "\u23f3 <b>Waiting for content data</b>",
    AutoDecision.NEEDS_REVIEW: "\U0001f9d1 <b>Needs your decision</b>",
}


def _auto_decision_line(item: MediaItem) -> str | None:
    """One line saying what the policy decided about this item, and why."""
    if item.auto_decision is None:
        return None
    label = _AUTO_LABELS.get(item.auto_decision, item.auto_decision.value)
    reasons = "; ".join(item.auto_reasons or [])
    return f"\n{label}" + (f"\n{_h(reasons)}" if reasons else "")


async def _send_review(chat_id: int, item: MediaItem) -> None:
    """Build and send a review message with poster, content info, and buttons."""
    is_flagged = item.auto_recommendation == Recommendation.REJECT
    type_emoji = {
        "movie": "\U0001f3ac", "series": "\U0001f4fa", "audiobook": "\U0001f3a7",
        "ebook": "\U0001f4d6", "magazine": "\U0001f4f0", "comic": "\U0001f5ef",
        "music": "\U0001f3b5",
    }.get(item.media_type.value, "\U0001f4e6")

    parts = [f"<b>{_h(item.title or 'Unknown')}</b>"]

    meta = []
    if item.year:
        meta.append(str(item.year))
    meta.append(f"{type_emoji} {item.media_type.value.title()}")
    if item.season:
        meta.append(item.season)
    if meta:
        parts.append(" \u00b7 ".join(meta))

    if item.quality or item.source:
        parts.append(" ".join(filter(None, [item.quality, item.source])))
    if item.size_text:
        parts.append(f"\U0001f4be {_h(item.size_text)}")

    async with async_session() as session:
        result = await session.execute(
            select(ContentCheck).where(ContentCheck.media_item_id == item.id)
        )
        check = result.scalar_one_or_none()

    if check:
        if check.genres:
            parts.append(f"\n\U0001f3f7 {_h(', '.join(check.genres[:6]))}")

        ratings = []
        for label, rating in [
            ("Sex", check.sex_rating),
            ("Violence", check.violence_rating),
            ("Profanity", check.profanity_rating),
            ("Frightening", check.frightening_rating),
        ]:
            if rating and rating != SeverityRating.NONE:
                ratings.append(f"{label}: {rating.value}")
        if check.title_rule_hit:
            ratings.append("\u26a1 Title rule hit")
        if ratings:
            parts.append(" | ".join(ratings))

        certs = []
        if check.certifications:
            for country in ("ES", "US", "GB", "DE"):
                if country in check.certifications:
                    certs.append(f"{country}:{check.certifications[country]}")
        if certs:
            parts.append(f"\U0001f4cb {', '.join(certs)}")

        links = []
        if check.tmdb_id:
            kind = "tv" if item.media_type == MediaType.SERIES else "movie"
            links.append(f'<a href="https://www.themoviedb.org/{kind}/{check.tmdb_id}">TMDB</a>')
        else:
            links.append("TMDB: —")
        if check.imdb_id:
            links.append(f'<a href="https://www.imdb.com/title/{check.imdb_id}/parentalguide">IMDb</a>')
        else:
            links.append("IMDb: —")
        parts.append(" | ".join(links))

    auto_line = _auto_decision_line(item)
    if auto_line:
        parts.append(auto_line)

    if is_flagged and item.error_message:
        parts.append(f"\n\u26a0\ufe0f <b>Flagged:</b> {_h(item.error_message)}")

    # Where approving sends it, when that is not the download client.
    going = delivery.where(item)
    if going:
        parts.append(f"\n\U0001f4e6 <b>Delivery:</b> {_h(going)}")

    # Approving a direct add downloads what the post carried, not a search
    # result, and the title above is only the name its sender gave it. Only
    # the download client takes a torrent as it is.
    carried = (
        ingest.direct_add_of(item)
        if delivery.target_for(item.media_type) == delivery.QBITTORRENT else None
    )
    if carried:
        parts.append(
            f"\n\U0001f9f2 <b>Direct add:</b> approving downloads the {carried} this post "
            "carries, as it is. Nothing is searched, and the title is the name its sender gave it."
        )

    url = item_url(item.id)
    if url:
        parts.append(f'\n<a href="{_h(url)}">Open in vettarr</a>')

    text = "\n".join(parts)
    keyboard = review_keyboard(item.id, is_flagged, button_url(url))

    poster = check.poster_url if check else None
    if poster:
        try:
            await _bot_app.bot.send_photo(
                chat_id=chat_id, photo=poster,
                caption=text, parse_mode="HTML", reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    # Fall back to Telegram photo (ebooks, magazines, etc.)
    if item.telegram_photo_path:
        photo_path = ingest.PHOTOS_DIR / item.telegram_photo_path
        if photo_path.exists():
            try:
                with open(photo_path, "rb") as f:
                    await _bot_app.bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=text, parse_mode="HTML", reply_markup=keyboard,
                    )
                return
            except Exception:
                pass

    try:
        await _bot_app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard,
        )
    except BadRequest:
        if not button_url(url):
            raise
        # Telegram refused the card. The link button is the one part of it
        # that depends on the config, so the card goes again without it; the
        # link is still in the text.
        log.warning("Telegram refused a review card with a link button to web.base_url; sent without the button")
        await _bot_app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=review_keyboard(item.id, is_flagged),
        )


async def _send_next(chat_id: int) -> None:
    """Send the next pending-review item, if any."""
    item = await actions.next_for_review()
    if item:
        await _send_review(chat_id, item)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@_authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "vettarr bot ready.\n\n"
        "/queue \u2014 pending items\n"
        "/next \u2014 review next item\n"
        "/stats \u2014 statistics\n"
        "/rescan \u2014 re-search no-match items\n"
        "/reparse \u2014 re-parse failed items\n"
        "/rerun \u2014 re-decide reviews the policy made without a flag"
    )


@_authorized
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        counts = {}
        for status in [
            ItemStatus.PENDING_REVIEW,
            ItemStatus.AWAITING_DATA,
            ItemStatus.NO_MATCH,
            ItemStatus.PARSE_FAILED,
            ItemStatus.IGNORED,
            ItemStatus.DOWNLOADING,
            ItemStatus.COMPLETED,
        ]:
            result = await session.execute(
                select(func.count()).select_from(MediaItem).where(MediaItem.status == status)
            )
            counts[status.value] = result.scalar()

    lines = [
        "<b>Queue status:</b>",
        f"  Pending review: {counts.get('pending_review', 0)}",
        f"  Waiting for content data: {counts.get('awaiting_data', 0)}",
        f"  No match: {counts.get('no_match', 0)}",
        f"  Parse failed: {counts.get('parse_failed', 0)}",
        f"  Ignored (not media): {counts.get('ignored', 0)}",
        f"  Downloading: {counts.get('downloading', 0)}",
        f"  Completed: {counts.get('completed', 0)}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    item = await actions.next_for_review()
    if not item:
        await update.message.reply_text("No items pending review.")
        return

    await _send_review(update.message.chat_id, item)


@_authorized
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(MediaItem))).scalar()
        by_type = (
            await session.execute(
                select(MediaItem.media_type, func.count())
                .group_by(MediaItem.media_type)
            )
        ).all()
        by_status = (
            await session.execute(
                select(MediaItem.status, func.count())
                .group_by(MediaItem.status)
            )
        ).all()

    lines = [f"<b>Total items: {total}</b>\n", "<b>By type:</b>"]
    for mt, c in sorted(by_type, key=lambda x: x[1], reverse=True):
        lines.append(f"  {mt.value}: {c}")
    lines.append("\n<b>By status:</b>")
    for st, c in sorted(by_status, key=lambda x: x[1], reverse=True):
        lines.append(f"  {st.value}: {c}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_rescan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(MediaItem.id)
            .where(MediaItem.status == ItemStatus.NO_MATCH)
        )
        item_ids = result.scalars().all()

    if not item_ids:
        await update.message.reply_text("No items with no-match status.")
        return

    await update.message.reply_text(f"Re-searching {len(item_ids)} items...")
    found = 0
    processed = 0
    for item_id in item_ids:
        result = await actions.search(
            item_id, from_statuses={ItemStatus.NO_MATCH}, op=OP_RESCAN,
        )
        if result.status in (actions.Status.BUSY, actions.Status.NOT_FOUND):
            continue
        processed += 1
        if result.releases:
            found += 1

    await update.message.reply_text(f"Re-scan done: {found}/{processed} now have matches.")


@_authorized
async def cmd_reparse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from vettarr.pipeline import reprocess_failed
    count = await reprocess_failed()
    await update.message.reply_text(f"Re-parsed {count} items.")


@_authorized
async def cmd_rerun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from vettarr.pipeline import rerun_pending_reviews
    await update.message.reply_text("Re-running policy reviews...")
    summary = await rerun_pending_reviews()
    await update.message.reply_text(summary.text())


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------

@_authorized
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data:
        return

    parts = data.split(":")
    action = parts[0]

    if action == "approve":
        await _handle_approve(query, int(parts[1]))
    elif action == "reject":
        await _handle_reject(query, int(parts[1]))
    elif action == "confirm_reject":
        await _handle_confirm_reject(query, int(parts[1]))
    elif action == "cancel_reject":
        await _handle_cancel_reject(query, int(parts[1]))
    elif action == "skip":
        await _handle_skip(query, int(parts[1]))
    elif action == "details":
        await _handle_details(query, int(parts[1]))
    elif action == "releases":
        await _handle_releases(query, int(parts[1]))
    elif action == "rerun":
        await _handle_rerun(query, int(parts[1]))
    elif action == "torrent":
        if len(parts) >= 4:
            await _handle_torrent_select(query, int(parts[1]), int(parts[3]), int(parts[2]))
        else:
            # A button drawn before the search id was part of its data.
            await _handle_torrent_select(query, int(parts[1]), int(parts[2]))
    elif action == "research":
        await _handle_research(query, int(parts[1]))
    elif action == "cancel_search":
        await _handle_cancel_search(query, int(parts[1]))


async def _handle_approve(query, item_id: int) -> None:
    result = await actions.approve(item_id)
    if not result.ok:
        await _respond(query, text=_h(result.message))
        return
    await _respond(query, text=_h(result.message))
    await _send_next(query.message.chat_id)


# What the reviewer is asked before a rejection, by what it will do to the post.
_REJECT_PROMPTS = {
    "delete": "Reject this item? Its post will be deleted.",
    "react": "Reject this item? Its post will get a reaction.",
    "none": "Reject this item? Its post stays where it is.",
}


async def _handle_reject(query, item_id: int) -> None:
    keyboard = confirm_reject_keyboard(item_id)
    effect = await ingest.reject_effect(item_id)
    await _respond(query, text=_REJECT_PROMPTS[effect], reply_markup=keyboard)


async def _handle_confirm_reject(query, item_id: int) -> None:
    result = await actions.reject(item_id, _ingester)
    await _respond(query, text=_h(result.message))
    if result.ok:
        await _send_next(query.message.chat_id)


async def _handle_cancel_reject(query, item_id: int) -> None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
    if not item:
        return

    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await _send_review(chat_id, item)


async def _handle_skip(query, item_id: int) -> None:
    chat_id = query.message.chat_id
    result = await actions.skip(item_id)
    await _respond(query, text=_h(result.message))
    if result.next_item is not None:
        await _send_review(chat_id, result.next_item)


async def _handle_rerun(query, item_id: int) -> None:
    """Decide the item again under the policy as it is now, and say what it said."""
    chat_id = query.message.chat_id
    msg = await _respond(query, text="Deciding again under the current policy...")
    result = await actions.rerun(item_id)
    await _edit_or_send(query, msg, _h(result.message))
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
    if item is not None and item.status == ItemStatus.PENDING_REVIEW:
        # Still this person's to decide: the card again, with its reasons as they are now.
        await _send_review(chat_id, item)
        return
    if result.ok:
        await _send_next(chat_id)


async def _handle_details(query, item_id: int) -> None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if not item:
            return
        result = await session.execute(
            select(ContentCheck).where(ContentCheck.media_item_id == item_id)
        )
        check = result.scalar_one_or_none()

    lines = [f"<b>{_h(item.title or 'Unknown')}</b>\n"]
    if check:
        if check.plot:
            lines.append(f"{_h(check.plot[:500])}\n")
        if check.genres:
            lines.append(f"<b>Genres:</b> {_h(', '.join(check.genres))}")
        lines.append(f"<b>Sex:</b> {check.sex_rating.value if check.sex_rating else 'N/A'}")
        lines.append(f"<b>Violence:</b> {check.violence_rating.value if check.violence_rating else 'N/A'}")
        lines.append(f"<b>Profanity:</b> {check.profanity_rating.value if check.profanity_rating else 'N/A'}")
        lines.append(f"<b>Frightening:</b> {check.frightening_rating.value if check.frightening_rating else 'N/A'}")
        lines.append(f"<b>Title rule hit:</b> {'Yes' if check.title_rule_hit else 'No'}")
        if check.certifications:
            certs = ", ".join(f"{k}:{v}" for k, v in check.certifications.items())
            lines.append(f"<b>Certifications:</b> {_h(certs)}")

        links = []
        if check.tmdb_id:
            kind = "tv" if item.media_type == MediaType.SERIES else "movie"
            links.append(f'<a href="https://www.themoviedb.org/{kind}/{check.tmdb_id}">TMDB</a>')
        else:
            links.append("TMDB: —")
        if check.imdb_id:
            links.append(f'<a href="https://www.imdb.com/title/{check.imdb_id}/parentalguide">IMDb</a>')
        else:
            links.append("IMDb: —")
        lines.append(" | ".join(links))

    auto_line = _auto_decision_line(item)
    if auto_line:
        lines.append(auto_line)

    lines.append(f"\n<b>Raw message:</b>\n<pre>{_h(item.raw_text[:800])}</pre>")

    keyboard = review_keyboard(
        item_id,
        item.auto_recommendation == Recommendation.REJECT,
        button_url(item_url(item_id)),
    )
    await _respond(query, text="\n".join(lines), reply_markup=keyboard)


async def _edit_or_send(query, msg, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text=text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        await _bot_app.bot.send_message(
            chat_id=query.message.chat_id, text=text, parse_mode="HTML",
            reply_markup=reply_markup,
        )


async def _handle_releases(query, item_id: int) -> None:
    """The releases the last search found; a fresh search when there are none."""
    ts = await actions.latest_search(item_id)
    if ts is not None and ts.prowlarr_results:
        async with async_session() as session:
            item = await session.get(MediaItem, item_id)
        title = item.title if item is not None else None
        await _respond(
            query,
            text=f"Select torrent for <b>{_h(title or 'this item')}</b>:",
            reply_markup=torrent_keyboard(item_id, ts.prowlarr_results, ts.id),
        )
        return
    await _handle_research(query, item_id)


async def _handle_torrent_select(
    query, item_id: int, index: int, search_id: int | None = None,
) -> None:
    if search_id is None:
        ts = await actions.latest_search(item_id)
        if ts is None:
            await _respond(query, text="That release is no longer listed. Search again.")
            return
        search_id = ts.id

    result = await actions.grab(item_id, search_id, index)
    if result.status in (actions.Status.BUSY, actions.Status.NOT_FOUND):
        await _respond(query, text=_h(result.message))
        return
    await _respond(query, text=_h(result.message))
    await _send_next(query.message.chat_id)


async def _handle_research(query, item_id: int) -> None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
    title = item.title if item is not None else None
    msg = await _respond(query, text=f"Searching for <b>{_h(title or 'this item')}</b>...")
    result = await actions.search(item_id)
    if result.search_id is None:
        # The search never ran: the item is busy, gone, or has no title.
        await _edit_or_send(query, msg, _h(result.message))
        return
    if not result.releases:
        await _edit_or_send(
            query, msg, _h(result.message),
            torrent_keyboard(item_id, [], result.search_id),
        )
        return
    await _edit_or_send(
        query, msg,
        f"Select torrent for <b>{_h(result.title or 'this item')}</b>:",
        torrent_keyboard(item_id, result.releases, result.search_id),
    )


async def _handle_cancel_search(query, item_id: int) -> None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
    if not item:
        return

    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await _send_review(chat_id, item)


# ---------------------------------------------------------------------------
# Intake: messages sent to the bot in a bot source's chat
# ---------------------------------------------------------------------------

# Text, captions, .torrent attachments and forwards; never a command, never an edit.
# filters.COMMAND only reads a message's text, so a command written as the
# caption of a photo or a file is turned away by its leading slash.
INTAKE_FILTER = (
    (filters.TEXT | filters.CAPTION | filters.Document.FileExtension("torrent") | filters.FORWARDED)
    & ~filters.COMMAND
    & ~filters.CaptionRegex(r"^/")
    & ~filters.UpdateType.EDITED
)


def incoming_from_bot(message: Message, sender_id: int | None) -> ingest.Incoming:
    """A python-telegram-bot message as ``ingest`` reads it."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or ()
    links = [e.url for e in entities if e.url and e.url not in text]
    if links:
        text = "\n".join([text, *links]) if text else "\n".join(links)
    origin = message.forward_origin
    forward_from = (
        (origin.chat.id, origin.message_id) if isinstance(origin, MessageOriginChannel) else None
    )
    document = message.document
    fetch = None
    if document is not None:
        async def fetch() -> bytes:
            file = await document.get_file()
            return bytes(await file.download_as_bytearray())

    return ingest.Incoming(
        chat_id=message.chat_id,
        message_id=message.message_id,
        text=text,
        sender_id=sender_id,
        forward_from=forward_from,
        document_name=document.file_name if document is not None else None,
        fetch_document=fetch,
        document_size=getattr(document, "file_size", None),
        posted_at=getattr(message, "date", None),
    )


async def _keep_photo(message: Message, item_id: int) -> None:
    """Keep the post's photo, as the userbot does for its sources."""
    photo = message.photo[-1]  # the sizes come smallest first

    async def download(dest) -> None:
        file = await photo.get_file()
        await file.download_to_drive(custom_path=dest)

    await ingest.keep_photo(item_id, message.chat_id, message.message_id, download)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A message in a chat a bot source reads: one more item, through ``ingest``."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    source = ingest.bot_source_for(chat.id, chat.type, chat.username)
    if source is None:
        return
    user = update.effective_user
    result = await ingest.ingest(source, incoming_from_bot(message, user.id if user else None))
    if result.outcome == "added" and result.item_id is not None and getattr(message, "photo", None):
        await _keep_photo(message, result.item_id)
    reply = result.reply()
    if chat.type == ChatType.PRIVATE and reply:
        await message.reply_text(reply)


_JOINED = {ChatMember.MEMBER, ChatMember.ADMINISTRATOR}
_GONE = {ChatMember.LEFT, ChatMember.BANNED}


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A reviewer added the bot to a chat: note it, with whether it can read and delete there.

    A chat anyone else adds the bot to is not noted, and one the bot has left
    is forgotten.
    """
    change = update.my_chat_member
    if change is None:
        return
    old, new = change.old_chat_member.status, change.new_chat_member.status
    chat = change.chat
    if new in _GONE:
        await ingest.bot_removed(chat.id)
        return
    if new not in _JOINED or old in _JOINED:
        return
    by = getattr(change, "from_user", None)
    if by is None or by.id not in ingest.reviewers():
        return
    ingest.bot_added(
        chat.id,
        chat.title or (f"@{chat.username}" if chat.username else str(chat.id)),
        is_admin=new == ChatMember.ADMINISTRATOR,
        reads_all_messages=bool(getattr(context.bot, "can_read_all_group_messages", False)),
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_bot_app(ingester: PostActions | None) -> Application:
    global _ingester, _bot_app
    _ingester = ingester

    # Every Bot API call goes through GuardedBotRequest, where dry_run stops the writes.
    app = (
        Application.builder().token(settings.telegram_bot_token)
        .request(GuardedBotRequest(connection_pool_size=256)).build()
    )
    _bot_app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("rescan", cmd_rescan))
    app.add_handler(CommandHandler("reparse", cmd_reparse))
    app.add_handler(CommandHandler("rerun", cmd_rerun))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(INTAKE_FILTER, on_message))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    ingest.set_review_pusher(_send_review)
    return app
