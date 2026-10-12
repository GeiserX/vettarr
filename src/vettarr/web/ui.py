"""What every page shares: the templates, the layout's context, and the answers
routes give htmx.

A route module imports ``templates`` and the helpers from here and nothing
from another route module, so each page can change on its own.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Annotated, Any

from fastapi import Path as PathParam
from fastapi import Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from vettarr import actions, dryrun
from vettarr.web import words
from vettarr.web.auth import NONE, Identity

WEB_DIR = Path(__file__).parent

# (label, link, the first path segments that light it up)
NAV = (
    ("Inbox", "/", ("", "item", "bulk")),
    ("Review", "/review", ("review",)),
    ("Policy", "/policy", ("policy",)),
    ("Setup", "/setup", ("setup", "settings")),
    ("Stats", "/stats", ("stats", "activity")),
)


def _shell(request: Request) -> dict[str, Any]:
    """What base.html reads on every page: who is signed in, where we are, what is failing."""
    state = request.state
    section = request.scope["path"].strip("/").split("/", 1)[0]
    return {
        "auth": getattr(state, "auth", None) or Identity(NONE),
        "nav": [(label, href, section in segments) for label, href, segments in NAV],
        "failing": getattr(state, "failing", ()),
        # How many items wait for a person: the count beside Inbox.
        "needs_you": getattr(state, "needs_you", 0),
        # The nav's badge, and every toast's: nothing outside this database is changed.
        "dry_run": dryrun.active(),
    }


def size_fmt(size: int | None) -> str:
    if not size:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"), context_processors=[_shell])
templates.env.filters["size_fmt"] = size_fmt
templates.env.filters["signal_label"] = words.signal_label
templates.env.filters["reason_words"] = words.reason


def is_htmx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request"))


# The ids are int4 columns. A larger number in a link is a database error if
# it gets that far, so it is stopped here and answered as the missing item it is.
ID_MAX = 2**31 - 1
ItemId = Annotated[int, PathParam(ge=1, le=ID_MAX)]
ItemIdQuery = Annotated[int | None, Query(ge=1, le=ID_MAX)]


def gone(request: Request) -> HTMLResponse:
    """The answer for an item that does not exist: one line for htmx, the page otherwise."""
    if is_htmx(request):
        return HTMLResponse(escape(actions.NOT_FOUND_MESSAGE), status_code=404)
    return templates.TemplateResponse(request, "item.html", {"d": None}, status_code=404)


class ItemRoute(APIRoute):
    """A route addressed by an item's id.

    An address no item can have (``/item/abc``, an id past int4) answers what
    a missing item answers, where FastAPI would answer a JSON error.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def answer(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return gone(request)

        return answer


def toast(text: str, kind: str = "ok") -> dict[str, str]:
    """Headers that make the page show ``text`` as a toast after an htmx answer.

    ``kind`` is ``ok`` or ``error``. A 4xx or 5xx needs none of this: its body
    is the toast (static/app.js listens for htmx:responseError).
    """
    return {"HX-Trigger": json.dumps({"toast": {"text": text, "kind": kind}})}


def refused(result: actions.ActionResult) -> HTMLResponse | None:
    """The error answer for an action that did not run, or None if it ran.

    htmx swaps nothing on a 4xx, so the body is the one line the page shows
    as a toast.
    """
    if result.status is actions.Status.BUSY:
        return HTMLResponse(escape(result.message), status_code=409)
    if result.status is actions.Status.NOT_FOUND:
        return HTMLResponse(escape(result.message), status_code=404)
    return None
