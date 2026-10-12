"""Review (``/review``): one item at a time, oldest first, decided with a key or a tap.

The queue is the Inbox's "Needs you" tab in the same order. An action answers
with the card that comes next and a toast saying what was done; a search
answers with the same card, now with its releases. The keys are attributes on
the card's own buttons (``data-key``), so a key does exactly what its button
does, the confirm dialog included.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from vettarr import actions
from vettarr.web import cards
from vettarr.web.ui import ItemId, ItemIdQuery, ItemRoute, is_htmx, templates

router = APIRouter(route_class=ItemRoute)

# key, what it does. The legend on the page and the test read this.
KEYS = (
    ("a", "Approve"),
    ("r", "Reject"),
    ("s", "Search for releases"),
    ("g", "Grab the best release"),
    ("j", "Next item"),
    ("k", "Previous item"),
    ("o", "Open the item"),
    ("?", "Show or hide this list"),
)
ALONE = "This is the only item waiting for you."
NOTHING_STORED = "No release is stored for this item. Search first."


def _card(review: cards.Review, headers: dict[str, str] | None = None) -> HTMLResponse:
    return HTMLResponse(cards.render("partials/review_card.html", review=review), headers=headers)


@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request, item: ItemIdQuery = None, after: ItemIdQuery = None, before: ItemIdQuery = None,
):
    review = await cards.load_review(item=item, after=after, before=before)
    if not is_htmx(request):
        return templates.TemplateResponse(request, "review.html", {"review": review, "keys": KEYS})
    anchor = after if after is not None else before
    alone = anchor is not None and review.card is not None and review.card.id == anchor
    return _card(review, cards.announce(ALONE) if alone else None)


@router.post("/review/{item_id}/{action}", response_class=HTMLResponse)
async def act(request: Request, item_id: ItemId, action: str):
    if action == "grab":
        stored = await actions.latest_search(item_id)
        if stored is None or not stored.prowlarr_results:
            return HTMLResponse(NOTHING_STORED, status_code=422)
        result = await actions.grab(item_id, stored.id, 0)
    else:
        result = await cards.act(action, item_id, request.app.state.posts)
    if result is None:
        return HTMLResponse("Not found", status_code=404)
    refusal = cards.refusal(result)
    if refusal is not None:
        return refusal
    # A search, and anything that failed on the way out, leaves the item to
    # look at: the same card, as it is now. A decision moves on.
    if action == "search" or not result.ok:
        review = await cards.load_review(item=item_id)
    else:
        review = await cards.load_review(after=item_id)
    return _card(review, cards.announce(result.message, cards.toast_kind(result)))
