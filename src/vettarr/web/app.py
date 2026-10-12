"""The web UI's wiring: the gate, the static mounts and one router per page.

No route lives here. Each page owns a module in ``web/routes``; what they
share is in ``web/ui.py`` (templates, htmx answers), ``web/auth.py`` (who may
come in) and ``web/shell.py`` (the banner).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from vettarr.config.runtime import get_config
from vettarr.config.schema import Config, Web
from vettarr.web import shell
from vettarr.web.auth import NONE, Gate, GateMiddleware
from vettarr.web.routes import auth, hooks, inbox, items, policy, review, setup, stats
from vettarr.web.ui import WEB_DIR, is_htmx, templates

if TYPE_CHECKING:
    from vettarr.ingest import PostActions

log = logging.getLogger(__name__)

ROUTERS = (auth, hooks, inbox, review, items, policy, setup, stats)
NOT_FOUND = "Nothing here."


async def not_found(request: Request, exc: HTTPException):
    """An address no page has: the layout and a way back, as a mistyped or stale bot link gets.

    JSON only for a request that asks for it, and the files' own answer for
    a missing file under /static or /photos.
    """
    path = request.scope["path"]
    wants_json = "application/json" in request.headers.get("accept", "") and "text/html" not in request.headers.get("accept", "")
    if exc.status_code != 404 or wants_json or path.startswith(("/static/", "/photos/")):
        return await http_exception_handler(request, exc)
    if is_htmx(request):
        return HTMLResponse(NOT_FOUND, status_code=404)
    await shell.load(request)
    return templates.TemplateResponse(request, "missing.html", {}, status_code=404)


def create_web_app(ingester: PostActions | None, config: Config | None = None) -> FastAPI:
    """The app. ``ingester`` is what a rejection does to a post; ``config`` defaults to the process one."""
    web = (config or get_config()).web or Web()
    # Under /data/sessions, which the image declares a volume, so an upgrade keeps people signed in.
    gate = Gate(web, secret_file=Path("/data/sessions/web-session.key"))
    if gate.mode == NONE:
        log.warning("The web UI has no auth: anyone who reaches the port can approve and reject")
    static = WEB_DIR / "static"
    if not (static / "app.css").exists():
        log.warning("%s is missing: the pages have no styles until scripts/build-css.sh runs", static / "app.css")

    app = FastAPI(
        title="vettarr", docs_url=None, redoc_url=None,
        dependencies=[Depends(shell.load)],
    )
    app.add_exception_handler(HTTPException, not_found)
    app.state.posts = ingester
    app.state.gate = gate
    app.add_middleware(GateMiddleware, gate=gate)
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    photos_dir = Path("/data/photos")
    try:
        photos_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("%s cannot be created (%s): posts' photos are not served", photos_dir, exc.strerror or exc)
    else:
        app.mount("/photos", StaticFiles(directory=str(photos_dir)), name="photos")

    for module in ROUTERS:
        app.include_router(module.router)
    return app
