from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def review_keyboard(
    item_id: int,
    is_flagged: bool = False,
    url: str | None = None,
) -> InlineKeyboardMarkup:
    """The buttons under a review card. ``url`` is the item's page in the web UI."""
    rows = []
    if is_flagged:
        rows.append([
            InlineKeyboardButton("Override & Approve", callback_data=f"approve:{item_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{item_id}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("Approve", callback_data=f"approve:{item_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{item_id}"),
        ])
    rows.append([
        InlineKeyboardButton("Skip", callback_data=f"skip:{item_id}"),
        InlineKeyboardButton("Details", callback_data=f"details:{item_id}"),
        InlineKeyboardButton("Releases", callback_data=f"releases:{item_id}"),
    ])
    last = [InlineKeyboardButton("Rerun policy", callback_data=f"rerun:{item_id}")]
    if url:
        last.append(InlineKeyboardButton("Open in vettarr", url=url))
    rows.append(last)
    return InlineKeyboardMarkup(rows)


def torrent_keyboard(
    item_id: int,
    results: list[dict],
    search_id: int | None = None,
) -> InlineKeyboardMarkup:
    """One button per release. The search id pins the list the buttons were
    drawn from, so a search run in between cannot change what a tap grabs."""
    rows = []
    for i, r in enumerate(results[:5]):
        seeders = r.get("seeders", 0)
        size_gb = (r.get("size") or 0) / (1024 ** 3)
        label = f"{i+1}. S:{seeders} | {size_gb:.1f}GB | {r.get('indexer', '?')}"
        data = (
            f"torrent:{item_id}:{search_id}:{i}" if search_id is not None
            else f"torrent:{item_id}:{i}"
        )
        rows.append([InlineKeyboardButton(label, callback_data=data)])
    rows.append([
        InlineKeyboardButton("Search again", callback_data=f"research:{item_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"cancel_search:{item_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def confirm_reject_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, reject", callback_data=f"confirm_reject:{item_id}"),
            InlineKeyboardButton("No, keep it", callback_data=f"cancel_reject:{item_id}"),
        ]
    ])
