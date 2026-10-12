"""Keeping secrets out of the logs.

One filter, attached to every log handler, replaces two things with ``***``
in a record's message, its arguments, its traceback and its stack:

- every secret value the loaded config holds (its ``SecretStr`` fields), as
  written, as a URL carries it, and as JSON or a Python ``repr`` escapes it;
- anything shaped like a Telegram bot token, whatever the config says, so a
  token is masked even before the config is read or when it is not the one
  in the file.

Libraries put secrets in their own messages: python-telegram-bot names the
token it was refused with, and an HTTP client logs the URL it called. The
app cannot stop them writing it, so it masks what reaches a handler.

A secret shorter than four characters is not masked: it would blank out
ordinary words all over the log, and it protects nothing. A secret written
in any other encoding (base64, a hash) is not recognised either.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

from pydantic import BaseModel, SecretStr

MASK = "***"
_MIN_LENGTH = 4
# The bot's numeric id, a colon, and the 35 characters BotFather issues. A
# URL carries it glued to "bot", so only a digit may not come before it.
_BOT_TOKEN = r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}"
_FORMATTER = logging.Formatter()


def secret_values(node: Any) -> set[str]:
    """Every secret a config model holds, however deep."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, SecretStr):
            found.add(value.get_secret_value())
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                walk(getattr(value, name))
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                walk(item)

    walk(node)
    return found


class SecretFilter(logging.Filter):
    """Masks secrets in every record it sees. Never drops a record."""

    def __init__(self) -> None:
        super().__init__()
        self._pattern = re.compile(_BOT_TOKEN)

    def set_secrets(self, values: Iterable[str]) -> None:
        forms: set[str] = set()
        for value in values:
            if len(value) >= _MIN_LENGTH:
                forms.update((value, quote(value, safe=""), quote_plus(value)))
                # A quote, a backslash or a non-ASCII letter is written
                # differently inside a JSON body or an exception's repr.
                forms.update((json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1]))
        # Longest first, so a secret that contains another is masked whole.
        words = sorted(forms, key=len, reverse=True)
        self._pattern = re.compile("|".join([*map(re.escape, words), _BOT_TOKEN]))

    def mask(self, text: str) -> str:
        return self._pattern.sub(MASK, text)

    def _masked_arg(self, arg: Any) -> Any:
        # A path or an exception stays what it is unless its text holds a
        # secret; then the masked text takes its place. A number always stays
        # a number, or `%d` could no longer format it and the whole line
        # would be lost; the formatted message is masked below.
        if isinstance(arg, (int, float)):
            return arg
        text = arg if isinstance(arg, str) else str(arg)
        masked = self.mask(text)
        return masked if masked != text else arg

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self.mask(record.msg)
            if isinstance(record.args, Mapping):
                record.args = {key: self._masked_arg(value) for key, value in record.args.items()}
            elif record.args:
                record.args = tuple(self._masked_arg(arg) for arg in record.args)
            # A secret split between the message and an argument.
            message = record.getMessage()
            masked = self.mask(message)
            if masked != message:
                record.msg, record.args = masked, None
        except Exception:
            # A record that does not format is logging's to report, not ours.
            pass
        if record.exc_info and not record.exc_text:
            record.exc_text = _FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.mask(record.exc_text)
        if record.stack_info:
            record.stack_info = self.mask(record.stack_info)
        return True


_filter = SecretFilter()


def install(config: BaseModel | None = None) -> SecretFilter:
    """Put the filter on every handler that exists now; safe to call again.

    With a config, its secrets are the ones masked from here on. Call it
    again after anything that adds handlers of its own (uvicorn builds its
    handlers when its config is made).
    """
    if config is not None:
        _filter.set_secrets(secret_values(config))
    loggers = [logging.getLogger()]
    loggers += [lg for lg in logging.root.manager.loggerDict.values() if isinstance(lg, logging.Logger)]
    for logger in loggers:
        for handler in logger.handlers:
            if _filter not in handler.filters:
                handler.addFilter(_filter)
    return _filter
