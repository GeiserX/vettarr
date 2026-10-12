"""The policy signal catalogue.

A rule checks one signal with one operator against one value. The catalogue
is fixed in code because every signal is backed by a fetch or a field the app
already reads; the config only chooses thresholds and lists.

``kind`` says what a rule's value looks like:

- ``severity``: none, mild, moderate, severe (the parents-guide scale)
- ``number``: an int or float; ``between`` takes ``[low, high]``
- ``bool``: true or false, used with ``is``
- ``terms``: a list of words or ids, or the name of a list under ``policy.lists``
- ``certification``: ``CC:rating`` with a two-letter country, e.g. ``ES:18``
- ``resolution``: one of ``RESOLUTIONS``
- ``time``: ``HH:MM``; ``between`` takes ``[from, to]`` and may cross midnight
"""

from __future__ import annotations

from dataclasses import dataclass

MEDIA_TYPES: tuple[str, ...] = (
    "movie", "series", "audiobook", "ebook", "magazine", "comic", "music",
)
VIDEO = ("movie", "series")
BOOKS = ("audiobook", "ebook", "magazine", "comic")

SEVERITIES: tuple[str, ...] = ("none", "mild", "moderate", "severe")
RESOLUTIONS: tuple[str, ...] = ("480p", "576p", "720p", "1080p", "2160p")

OPERATORS: tuple[str, ...] = (
    "at_least", "at_most", "between", "is", "any_of", "none_of", "matches_any",
)


@dataclass(frozen=True)
class Signal:
    name: str
    group: str
    kind: str
    operators: tuple[str, ...]
    types: tuple[str, ...] = MEDIA_TYPES
    voted: bool = False


_NUM = ("at_least", "at_most", "between")
_SET = ("any_of", "none_of")
_LIST = ("any_of", "none_of", "matches_any")


def _catalogue() -> dict[str, Signal]:
    rows = [
        # Title metadata (TMDB).
        Signal("certification", "title", "certification", ("at_least", "any_of", "none_of"), VIDEO),
        Signal("genre", "title", "terms", _SET, VIDEO),
        Signal("keyword", "title", "terms", _LIST, VIDEO),
        Signal("original_language", "title", "terms", _SET, VIDEO),
        Signal("country", "title", "terms", _SET, VIDEO),
        Signal("year", "title", "number", _NUM),
        Signal("runtime_minutes", "title", "number", _NUM, VIDEO),
        Signal("rating", "title", "number", ("at_least", "at_most"), VIDEO),
        Signal("vote_count", "title", "number", ("at_least", "at_most"), VIDEO),
        Signal("adult", "title", "bool", ("is",), VIDEO),
        Signal("collection", "title", "bool", ("is",), VIDEO),
        Signal("cast", "title", "terms", _LIST, VIDEO),
        Signal("crew", "title", "terms", _LIST, VIDEO),
        Signal("studio", "title", "terms", _LIST, VIDEO),
        # Parents guide (IMDb). Voted: split_vote and min_votes apply.
        *(
            Signal(f"imdb.{c}", "parents_guide", "severity", ("at_least", "at_most"), VIDEO, voted=True)
            for c in ("violence", "nudity", "profanity", "frightening", "alcohol")
        ),
        # The release itself, from the post or the search result.
        Signal("size_gb", "release", "number", _NUM),
        Signal("resolution", "release", "resolution", ("at_least", "at_most", "any_of", "none_of"), VIDEO),
        Signal("release_source", "release", "terms", _SET, VIDEO),
        Signal("codec", "release", "terms", _SET, VIDEO),
        Signal("hdr", "release", "bool", ("is",), VIDEO),
        Signal("audio_language", "release", "terms", _SET, VIDEO),
        Signal("subtitles", "release", "terms", _SET, VIDEO),
        Signal("release_group", "release", "terms", _LIST),
        Signal("seeders", "release", "number", ("at_least", "at_most")),
        Signal("freeleech", "release", "bool", ("is",)),
        Signal("file_extensions", "release", "terms", _SET),
        Signal("file_count", "release", "number", ("at_least", "at_most")),
        # Library and state.
        # Looked up by the TMDB id of the matched record, which only a film
        # or a series has.
        Signal("in_library", "library", "bool", ("is",), VIDEO),
        Signal("already_requested", "library", "bool", ("is",)),
        Signal("in_download_client", "library", "bool", ("is",)),
        Signal("disk_free_gb", "library", "number", ("at_least", "at_most")),
        # Context.
        Signal("source", "context", "terms", _SET),
        Signal("sender", "context", "terms", _LIST),
        Signal("per_sender_per_week", "context", "number", ("at_least", "at_most")),
        Signal("time_window", "context", "time", ("between",)),
        Signal("max_active_downloads", "context", "number", ("at_least", "at_most")),
        Signal("post_age_hours", "context", "number", ("at_least", "at_most")),
        # Books and magazines.
        Signal("subject", "books", "terms", _LIST, BOOKS),
        Signal("language", "books", "terms", _SET, BOOKS),
        Signal("publisher", "books", "terms", _LIST, BOOKS),
        Signal("format", "books", "terms", _SET, BOOKS),
        Signal("pages", "books", "number", _NUM, BOOKS),
        Signal("issue_newer_than_owned", "books", "bool", ("is",), ("magazine",)),
        # Series.
        Signal("season_complete", "series", "bool", ("is",), ("series",)),
        Signal("status", "series", "terms", _SET, ("series",)),
        Signal("episode_count", "series", "number", ("at_least", "at_most"), ("series",)),
    ]
    return {s.name: s for s in rows}


SIGNALS: dict[str, Signal] = _catalogue()

# Prefixed spellings that name a signal by where its data comes from:
# ``tmdb.keyword`` is ``keyword``, ``book.subject`` is ``subject``.
_PREFIXES: dict[str, str] = {"tmdb.": "title", "book.": "books"}


def canonical(name: str) -> str | None:
    """The catalogue name for ``name``, accepting the prefixed aliases; None if unknown."""
    if name in SIGNALS:
        return name
    for prefix, group in _PREFIXES.items():
        if name.startswith(prefix):
            signal = SIGNALS.get(name[len(prefix):])
            if signal is not None and signal.group == group:
                return signal.name
    return None

# Which integration feeds each group. A rule whose integration is not enabled
# sees missing data, and the type's when_data_missing applies.
GROUP_NEEDS: dict[str, str] = {
    "title": "tmdb",
    "parents_guide": "imdb_guide",
}
