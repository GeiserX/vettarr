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

``POST /hooks/abreq/<source key>`` does the same for an AudioBookRequest
notification on a source of kind ``abreq``. AudioBookRequest runs its
notifications one after another and stops at the first that fails, so this
one answers at once and reads the book after the answer is sent.
"""

from __future__ import annotations

import hmac
import json
import logging

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from vettarr import abreq_source, seerr_source
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


async def _admit(request: Request, source_key: str, kind: str) -> tuple[Any, Any]:
    """(source, payload) of a call let in, or (None, the answer that refuses it)."""
    source = next(
        (s for s in settings.config.sources if s.key == source_key and s.kind == kind), None,
    )
    if source is None or source.webhook_secret is None:
        return None, PlainTextResponse("Not Found", status_code=404)
    if not authorized(request.headers.get("authorization"), source.webhook_secret.get_secret_value()):
        log.warning("Source %s: a webhook call without the right Authorization header was refused", source.key)
        return None, PlainTextResponse("Unauthorized", status_code=401)
    body = await _body(request)
    if body is None:
        return None, PlainTextResponse("Payload too large", status_code=413)
    try:
        return source, json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError: a body nested thousands deep is JSON nobody reads.
        return None, PlainTextResponse("The body is not JSON", status_code=400)


@router.post("/hooks/seerr/{source_key}")
async def seerr_hook(request: Request, source_key: str):
    source, payload = await _admit(request, source_key, "seerr")
    if source is None:
        return payload
    try:
        result = await seerr_source.hook(source, payload, request.app.state.posts)
    except Exception:
        # Seerr sends once and ignores the answer; the poll finds the request.
        log.exception("Source %s: a webhook call could not be handled", source.key)
        result = "deferred"
    return JSONResponse({"result": result})


@router.post("/hooks/abreq/{source_key}")
async def abreq_hook(request: Request, source_key: str, background: BackgroundTasks):
    source, payload = await _admit(request, source_key, "abreq")
    if source is None:
        return payload
    result, asin = abreq_source.triage(source, payload)
    if asin is not None:
        # After the answer: the poll finds the book if this fails.
        background.add_task(abreq_source.hook, source, asin)
    return JSONResponse({"result": result})
