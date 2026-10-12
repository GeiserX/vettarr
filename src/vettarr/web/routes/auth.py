"""Signing in and out (``/login``, ``/logout``). Only the password mode has them."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from vettarr.web.auth import PASSWORD, Gate, address_key, safe_next
from vettarr.web.ui import templates

log = logging.getLogger(__name__)

router = APIRouter()


def _page(request: Request, target: str, *, error: str | None = None, locked: int = 0, status: int = 200):
    """The login page. ``locked`` is the seconds the address stays locked."""
    minutes = max(1, -(-locked // 60)) if locked else 0
    headers = {"Cache-Control": "no-store"}
    if locked:
        headers["Retry-After"] = str(locked)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": target, "error": error, "locked_minutes": minutes},
        status_code=status, headers=headers,
    )


@router.get("/login")
async def login_page(request: Request, next: str | None = None):
    gate: Gate = request.app.state.gate
    target = safe_next(next)
    if gate.mode != PASSWORD or gate.signed_in(request):
        return RedirectResponse(target, status_code=303)
    locked = gate.lockout.locked_for(address_key(request.client.host if request.client else None))
    return _page(request, target, locked=locked, status=429 if locked else 200)


@router.post("/login")
async def login(
    request: Request,
    password: str = Form(""),
    remember: str | None = Form(None),
    next: str | None = Form(None),
):
    gate: Gate = request.app.state.gate
    target = safe_next(next)
    if gate.mode != PASSWORD:
        return RedirectResponse(target, status_code=303)
    address = address_key(request.client.host if request.client else None)
    locked = gate.lockout.locked_for(address)
    if locked:
        # Not even the right password gets in while the address is locked.
        return _page(request, target, locked=locked, status=429)
    # Counted before it is checked. Logins sent together wait their turn at
    # the check below; counted afterwards, every one of them would get it.
    left = gate.lockout.attempt(address)
    if await gate.check_password(password):
        gate.lockout.clear(address)
        response = RedirectResponse(target, status_code=303)
        gate.sign_in(response, remember=bool(remember))
        return response
    # The address is not logged: a log line is no place for who tried.
    log.warning("Wrong web password (%d tries left before the address is locked)", left)
    locked = gate.lockout.locked_for(address)
    if locked:
        return _page(request, target, locked=locked, status=429)
    error = "That password is not right."
    if left:
        tries = "1 more try" if left == 1 else f"{left} more tries"
        minutes = round(gate.lockout.seconds / 60)
        error += f" {tries} before this address is locked for {minutes} minutes."
    return _page(request, target, status=401, error=error)


@router.post("/logout")
async def logout(request: Request):
    gate: Gate = request.app.state.gate
    response = RedirectResponse("/login" if gate.mode == PASSWORD else "/", status_code=303)
    if gate.mode == PASSWORD:
        gate.sign_out(response)
    return response
