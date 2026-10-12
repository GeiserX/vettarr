"""The policy's signal names and reasons, in the words a person reads.

The engine writes ``imdb.violence: severe`` and ``keyword``; the pages say
``Violence (IMDb): severe`` and ``Keyword``. The stored reasons and the
config keep the engine's names: this only changes what is shown.
"""

from __future__ import annotations

import re

from vettarr.config.signals import SIGNALS

# A name that reads wrong when its underscores become spaces.
_OWN = {
    "hdr": "HDR",
    "size_gb": "Size (GB)",
    "disk_free_gb": "Free disk (GB)",
    "runtime_minutes": "Runtime (minutes)",
    "rating": "Rating (TMDB)",
    "vote_count": "Votes (TMDB)",
    "per_sender_per_week": "Posts per sender per week",
    "post_age_hours": "Post age (hours)",
    "max_active_downloads": "Active downloads",
    "in_download_client": "Already in the download client",
    "issue_newer_than_owned": "Issue newer than the one owned",
}


def signal_label(name: str) -> str:
    """``imdb.violence`` is ``Violence (IMDb)``, ``original_language`` is ``Original language``."""
    if name in _OWN:
        return _OWN[name]
    if name.startswith("imdb."):
        return f"{name[5:].replace('_', ' ').capitalize()} (IMDb)"
    return name.replace("_", " ").capitalize()


_LEADS = re.compile(r"^([a-z][a-z0-9_]*(?:\.[a-z_]+)?): (.*)$", re.S)
_NOT_FETCHED = re.compile(r"^parents guide not fetched(.*)$", re.S)


def reason(text: str) -> str:
    """A stored reason as a sentence: the signal named in words, a wait said as one.

    ``imdb.violence: parents guide not fetched (not listed yet)`` is
    ``Waiting for IMDb's parents guide (not listed yet)``; ``imdb.nudity:
    severe`` is ``Nudity (IMDb): severe``. Text that names no signal is as it was.
    """
    found = _LEADS.match(text or "")
    if found is None or found[1] not in SIGNALS:
        return text
    name, rest = found[1], found[2]
    waiting = _NOT_FETCHED.match(rest)
    if waiting is not None:
        return f"Waiting for IMDb's parents guide{waiting[1]}"
    return f"{signal_label(name)}: {rest}"
