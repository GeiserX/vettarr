"""Where other programs call in: ``/hooks/...``. Outside the login, each behind its own secret.

``POST /hooks/seerr/<source key>`` takes the notifications of Seerr's webhook
agent for a source of kind ``seerr``. No browser sends it, so there is no
session to ask for. What lets a call in is the source's ``webhook_secret``,
which Seerr sends as its ``Authorization`` header:

- a source with no ``webhook_secret``, a source of another kind and a key no
  source has all answer 404: a hook nobody gave a secret to is never open;
- a missing or wrong header answers 401, compared in constant time;
- anything let in answers 200 with what was done. Seerr does not read the
  answer and never sends a notification twice, so there is nothing to gain
  from an error status: what could not be handled now is found by the poll.
"""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from vettarr import seerr_source
from vettarr.config import settings

log = logging.getLogger(__name__)

router = APIRouter()

#: Seerr's notification with its default template is under 2 kB.
MAX_BODY_BYTES = 64 * 1024


def authorized(header: str | None, secret: str) -> bool:
    """Whether the ``Authorization`` header carries the secret, bare or as ``Bearer <secret>``."""
    sent = (header or "").strip()
    if sent[:7].lower() == "bearer ":
        sent = sent[7:].strip()
    return hmac.compare_digest(sent.encode("utf-8"), secret.encode("utf-8"))


async def _body(request: Request) -> bytes | None:
    """The body, or None once it is over the limit. Read in pieces, so a large one is never held whole."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@router.post("/hooks/seerr/{source_key}")
async def seerr_hook(request: Request, source_key: str):
    source = next(
        (s for s in settings.config.sources if s.key == source_key and s.kind == "seerr"), None,
    )
    if source is None or source.webhook_secret is None:
        return PlainTextResponse("Not Found", status_code=404)
    if not authorized(request.headers.get("authorization"), source.webhook_secret.get_secret_value()):
        log.warning("Source %s: a webhook call without the right Authorization header was refused", source.key)
        return PlainTextResponse("Unauthorized", status_code=401)
    body = await _body(request)
    if body is None:
        return PlainTextResponse("Payload too large", status_code=413)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError: a body nested thousands deep is JSON nobody reads.
        return PlainTextResponse("The body is not JSON", status_code=400)
    try:
        result = await seerr_source.hook(source, payload, request.app.state.posts)
    except Exception:
        # Seerr sends once and ignores the answer; the poll finds the request.
        log.exception("Source %s: a webhook call could not be handled", source.key)
        result = "deferred"
    return JSONResponse({"result": result})
