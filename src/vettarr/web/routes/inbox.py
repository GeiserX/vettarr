"""Inbox (``/``): the list of items by tab, and the actions taken from a row or on several rows.

Every answer to an action is a fragment: the row as it is now, and the tab
counts beside it. An action that did not run answers 409, 404 or 422 with one
line, which the page shows as a toast.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from vettarr import actions
from vettarr.checker import policy
from vettarr.models import MediaType
from vettarr.web import cards, shell
from vettarr.web.ui import ID_MAX, ItemId, ItemRoute, templates

log = logging.getLogger(__name__)

router = APIRouter(route_class=ItemRoute)

# The most ids one bulk request acts on: a page of rows, with room to spare.
BULK_MAX = 100


def _page(value: str | None) -> int:
    """The page asked for. Anything that is not a page number is the first."""
    if value and value.isascii() and value.isdigit() and len(value) < 7:
        return max(1, int(value))
    return 1


def _per(value: str | None) -> int:
    """The page size the browser measured (the ``per`` cookie app.js sets): the
    whole lines of posters its screen holds. Anything else is the default."""
    if value and value.isascii() and value.isdigit() and len(value) < 4 and 1 <= int(value) <= cards.PER_MAX:
        return int(value)
    return cards.PER_PAGE


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, tab: str | None = None, page: str | None = None):
    current = cards.tab_of(tab)
    filters = cards.Filters.read(request.query_params)
    inbox = await cards.load_inbox(current, filters, _page(page), _per(request.cookies.get("per")))
    return templates.TemplateResponse(request, "inbox.html", {
        "tab": current,
        "tabs": cards.TABS,
        "filters": filters,
        "inbox": inbox,
        "media_types": [t.value for t in MediaType],
        "outcomes": policy.OUTCOMES,
    })


async def _rows(request: Request, ids: Sequence[int], *, oob: bool, headers: dict[str, str] | None = None):
    """The rows of ``ids`` as they are now, and the tab counts out of band.

    ``oob`` sends every row out of band too, for an answer to the bulk bar,
    which has no row of its own to replace.
    """
    tab, filters = cards.where(request)
    loaded, counts = await cards.load_rows(ids, filters)
    # With the tab, a row whose item has left it shrinks to one line.
    parts = [cards.render("partials/inbox_row.html", card=card, oob=oob, tab=tab) for card in loaded]
    parts.append(cards.render(
        "partials/inbox_tabs.html", tab=tab, tabs=cards.TABS, filters=filters, counts=counts, oob=True,
    ))
    return HTMLResponse("\n".join(parts), headers=headers)


@router.get("/inbox/count", response_class=HTMLResponse)
async def count(request: Request):
    """The nav's count of items waiting for a person, read again after an action."""
    # The layout reads it from the request, and a template's context processor wins over its context.
    request.state.needs_you = await shell.needs_you()
    return templates.TemplateResponse(request, "_nav_count.html", {})


@router.get("/inbox/row/{item_id}", response_class=HTMLResponse)
async def row(request: Request, item_id: ItemId):
    """One row, read again. An item that is gone answers nothing, and its row goes."""
    return await _rows(request, [item_id], oob=False)


@router.post("/inbox/{item_id}/{action}", response_class=HTMLResponse)
async def act(request: Request, item_id: ItemId, action: str):
    result = await cards.act(action, item_id, request.app.state.posts)
    if result is None:
        return HTMLResponse("Not found", status_code=404)
    refusal = cards.refusal(result)
    if refusal is not None:
        return refusal
    return await _rows(
        request, [item_id], oob=False,
        headers=cards.announce(result.message, cards.toast_kind(result)),
    )


async def _selected(request: Request) -> list[int]:
    form = await request.form()
    ids: list[int] = []
    for value in form.getlist("ids"):
        # ASCII digits only: "²" is a digit to str.isdigit and an error to int.
        # And an id a row can have: one past int4 would fail the whole batch.
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) < 11 \
                and 0 < int(value) <= ID_MAX and int(value) not in ids:
            ids.append(int(value))
    return ids[:BULK_MAX]


def _left_out(asked: int, done: int) -> str:
    left = asked - done
    if not left:
        return ""
    return f" {left} {'was' if left == 1 else 'were'} left as {'it is' if left == 1 else 'they are'}: already decided or being processed."


@router.post("/bulk/approve", response_class=HTMLResponse)
async def bulk_approve(request: Request):
    ids = await _selected(request)
    if not ids:
        return HTMLResponse("Select at least one item.", status_code=422)
    done = await actions.approve_many(ids)
    text = f"{actions.queued_word()} {len(done)} for download." + _left_out(len(ids), len(done))
    return await _rows(request, ids, oob=True, headers=cards.announce(text, "ok" if done else "error"))


@router.post("/bulk/reject", response_class=HTMLResponse)
async def bulk_reject(request: Request):
    ids = await _selected(request)
    if not ids:
        return HTMLResponse("Select at least one item.", status_code=422)
    posts = request.app.state.posts
    done = 0
    # What a rejection could not undo at Radarr, Sonarr or Seerr: that target
    # still has the title, so the toast says which and is an error.
    kept: list[str] = []
    for item_id in ids:
        try:
            result = await actions.reject(item_id, posts)
        except Exception:
            log.exception("Rejection failed for item %d", item_id)
            continue
        done += result.ok
        if result.ok and getattr(result, "remote_failed", False) and result.remote_note:
            kept.append(result.remote_note)
    text = " ".join([f"Rejected {done}." + _left_out(len(ids), done), *kept])
    return await _rows(request, ids, oob=True, headers=cards.announce(text, "ok" if done and not kept else "error"))
