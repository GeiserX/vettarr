"""``web hash-password | serve``.

Exit codes: 0 fine, 1 refused (no password, no ``web`` block), 2 bad usage.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable

from vettarr.config.runtime import get_config
from vettarr.config.schema import Config
from vettarr.web import passwords


def _read_password() -> str | None:
    """The password: asked twice without echo at a terminal, otherwise the first line of stdin."""
    if sys.stdin.isatty():
        first = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != first:
            print("hash-password: the two entries differ", file=sys.stderr)
            return None
        return first
    return sys.stdin.readline().rstrip("\r\n")


def _hash_password() -> int:
    password = _read_password()
    if password is None:
        return 1
    if len(password) < passwords.MIN_LENGTH:
        print(f"hash-password: the password needs at least {passwords.MIN_LENGTH} characters", file=sys.stderr)
        return 1
    print(passwords.hash_password(password))
    return 0


async def _serve_web(cfg: Config) -> None:
    import uvicorn

    from vettarr import events
    from vettarr.database import engine, init_db
    from vettarr.ingest import PostActions
    from vettarr.web.app import create_web_app

    await init_db()
    # No userbot and no bot run here, so a rejection leaves the post alone.
    app = create_web_app(PostActions(config=cfg), cfg)
    try:
        await uvicorn.Server(
            uvicorn.Config(app, host=cfg.web.bind, port=cfg.web.port, log_level="warning")
        ).serve()
    finally:
        # No scheduler runs here to write what was queued.
        try:
            await events.flush_all()
        finally:
            await engine.dispose()


def _serve(load_config: Callable[[], Config]) -> int:
    cfg = load_config()
    if cfg.web is None:
        print("web serve: the config has no web block", file=sys.stderr)
        return 1
    try:
        asyncio.run(_serve_web(cfg))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None, load_config: Callable[[], Config] = get_config) -> int:
    """``load_config`` is the entry point's loader, which prints a config's errors and exits 1."""
    parser = argparse.ArgumentParser(prog="vettarr web", description="The web UI's own commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "hash-password",
        help="read a password (stdin, or asked twice at a terminal) and print the line for web.auth.password_hash",
    )
    commands.add_parser(
        "serve",
        help="run the web UI alone: no source is read and nothing is searched or downloaded. For UI work",
    )
    args = parser.parse_args(argv)
    return _hash_password() if args.command == "hash-password" else _serve(load_config)
