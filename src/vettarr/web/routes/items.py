"""One item (``/item/<id>``): the whole of it, and the actions taken on it.

The same panel is a page of its own, for a link the bot sends, and the
Inbox's slide-over, which asks for it with htmx and gets the panel alone.
Every action answers with the panel as it is now and a toast, and announces
``item-<id>`` so the row or the card behind the slide-over reads itself again.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from vettarr import actions
from vettarr.web import cards
from vettarr.web.ui import ItemId, ItemRoute, gone, is_htmx, templates

router = APIRouter(route_class=ItemRoute)

# Where a link to an item came from (``?from=``), and the back link it gets.
# A bot link carries none, and goes back to the Inbox.
BACK = {
    "review": ("/review", "Review"),
    "activity": ("/activity", "Activity"),
    "policy": ("/policy", "Policy"),
}
INBOX = ("/", "Inbox")


@router.get("/item/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: ItemId):
    detail = await cards.load_detail(item_id)
    if detail is None:
        return gone(request)
    if is_htmx(request):
        return HTMLResponse(cards.render("partials/item_panel.html", d=detail))
    back = BACK.get(request.query_params.get("from", ""), INBOX)
    return templates.TemplateResponse(request, "item.html", {"d": detail, "back": back})


@router.get("/item/{item_id}/progress", response_class=HTMLResponse)
async def progress(item_id: ItemId):
    """The download block. It asks again every 5 seconds only while it says "downloading".

    Always a 200: an error would leave the block on the page, asking forever.
    The answer that ends the polling announces ``over-<id>`` and ``item-<id>``,
    so the panel around the block and the row behind it read the item again:
    the badge stops saying "Downloading" and Reject is no longer offered.
    """
    card = await cards.load_download(item_id)
    block = cards.render("partials/download.html", card=card)
    if card is None or card.downloading:
        return HTMLResponse(block)
    return HTMLResponse(block, headers={"HX-Trigger": json.dumps({f"over-{item_id}": True, f"item-{item_id}": True})})


async def _answer(request: Request, item_id: int, result: actions.ActionResult):
    refusal = cards.refusal(result)
    if refusal is not None:
        return refusal
    if not is_htmx(request):
        return RedirectResponse(f"/item/{item_id}", status_code=303)
    detail = await cards.load_detail(item_id)
    panel = cards.render("partials/item_panel.html", d=detail) if detail is not None else ""
    return HTMLResponse(panel, headers=cards.announce(result.message, cards.toast_kind(result), item_id))


@router.post("/item/{item_id}/approve")
async def approve_item(request: Request, item_id: ItemId):
    return await _answer(request, item_id, await actions.approve(item_id))


@router.post("/item/{item_id}/reject")
async def reject(request: Request, item_id: ItemId):
    return await _answer(request, item_id, await actions.reject(item_id, request.app.state.posts))


@router.post("/item/{item_id}/search")
async def search_torrents(request: Request, item_id: ItemId):
    return await _answer(request, item_id, await actions.search(item_id))


@router.post("/item/{item_id}/download/{search_id}/{index}")
async def download_torrent(request: Request, item_id: ItemId, search_id: ItemId, index: int):
    return await _answer(request, item_id, await actions.grab(item_id, search_id, index))


@router.post("/item/{item_id}/rerun")
async def rerun_policy(request: Request, item_id: ItemId):
    return await _answer(request, item_id, await actions.rerun(item_id))
