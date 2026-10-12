"""Format profiles from config.yaml, compiled once into the patterns the parser runs.

A profile is word lists (docs/CONFIG.md, "formats"). Every word is literal,
matched case-insensitively as a whole word, with a space matching any run of
whitespace. A word written between slashes, ``/Films?\\s*WEB/``, is a regular expression,
for the shapes a list of words cannot write. A pattern that does not compile
is a ``FormatCompileError`` whose ``loc`` points at the word, so the loader
can anchor it to its line.

What a group writes lives here; how a release name is read (quality, source,
size, group, SxxEyy, year, ``[PACK]``, links) stays in ``regex_parser``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vettarr.models import MediaType

if TYPE_CHECKING:
    from vettarr.config.schema import Config, FormatProfile

Pattern = re.Pattern[str]

_FLAGS = re.IGNORECASE
# Punctuation that joins a category to what follows it: "Movies -> WEB-DL".
_ARROWS = r"[-–>→]"
_WORD_CHAR = re.compile(r"\w")
# What may stand between the start of a line and the word that opens it:
# symbols, emoji, markup. Never a letter or a digit.
_SYMBOLS = r"(?:[^\w\n]|_)*"
_SIZE = r"\d+(?:[.,]\d+)?\s*(?:GiB|GB|MiB|MB|TiB|TB)"
# Group names the app's own patterns use; a word may not take one.
RESERVED_GROUPS = frozenset({"cat", "rest", "quality", "source", "size", "value"})

# Fixed precedence, so the answer never depends on the order keys sit in a file.
# Written headers: the non-video types first, as a release file outranks them.
HEADER_ORDER = (
    MediaType.EBOOK,
    MediaType.AUDIOBOOK,
    MediaType.MAGAZINE,
    MediaType.COMIC,
    MediaType.MUSIC,
    MediaType.MOVIE,
    MediaType.SERIES,
)
VIDEO_TYPES = (MediaType.MOVIE, MediaType.SERIES)
# Hints: a season is a stronger claim than a film, so series is asked first.
HINT_ORDER = (
    MediaType.SERIES,
    MediaType.MOVIE,
    MediaType.EBOOK,
    MediaType.AUDIOBOOK,
    MediaType.MAGAZINE,
    MediaType.COMIC,
    MediaType.MUSIC,
)


class FormatCompileError(ValueError):
    """A word that does not compile. ``loc`` is its path inside the profile."""

    def __init__(self, loc: tuple[Any, ...], message: str):
        self.loc = loc
        self.message = message
        super().__init__(f"{'.'.join(str(p) for p in loc)}: {message}")


def word_pattern(word: str) -> str:
    """The regex source for one word: ``/regex/`` as written, anything else literal.

    A literal word has flexible spaces and never matches inside a longer
    word: ``Title`` does not match ``Subtitle``.
    """
    if len(word) > 2 and word.startswith("/") and word.endswith("/"):
        return word[1:-1]
    source = r"\s+".join(re.escape(part) for part in word.split())
    if _WORD_CHAR.match(word[0]):
        source = r"(?<!\w)" + source
    if _WORD_CHAR.match(word[-1]):
        source += r"(?!\w)"
    return source


def _alternation(words: Sequence[str], loc: tuple[Any, ...]) -> str | None:
    """``(?:w1|w2|...)``, each word checked on its own so an error names it.

    A word is checked inside a group, the way it is used: one that only
    compiles when it stands alone (a global flag such as ``(?i)``) fails here,
    under its own name.
    """
    if not words:
        return None
    sources = []
    for n, word in enumerate(words):
        source = word_pattern(word)
        try:
            compiled = re.compile(f"(?:{source})", _FLAGS)
        except re.error as exc:
            raise FormatCompileError((*loc, n), f"pattern does not compile: {exc}") from None
        taken = sorted(RESERVED_GROUPS.intersection(compiled.groupindex))
        if taken:
            raise FormatCompileError(
                (*loc, n), f"group name {taken[0]!r} is one the app uses itself; give the group another name"
            )
        sources.append(source)
    return "(?:" + "|".join(sources) + ")"


def _joined(source: str, loc: tuple[Any, ...]) -> Pattern:
    """Compile words joined into one pattern; ``loc`` is the setting they come from.

    Every word compiled on its own already. What can still fail is the join,
    when two words write the same group name. That is the setting's error,
    anchored like any other.
    """
    try:
        return re.compile(source, _FLAGS)
    except re.error as exc:
        raise FormatCompileError(loc, f"the words do not compile together: {exc}") from None


def _compile(source: str | None, loc: tuple[Any, ...]) -> Pattern | None:
    return _joined(source, loc) if source else None


@dataclass(frozen=True)
class Format:
    """One profile, compiled. Every pattern is None when its words are empty."""

    key: str
    kind: str = "scene"
    # (type, pattern) in HEADER_ORDER.
    headers: tuple[tuple[MediaType, Pattern], ...] = ()
    # (type, pattern) in HINT_ORDER: type_hints words anywhere, and a category
    # word that opens a line and is followed by an arrow.
    hints: tuple[tuple[MediaType, Pattern], ...] = ()
    # (type, fullmatch pattern) for telling which category a header named.
    categories: tuple[tuple[MediaType, Pattern], ...] = ()
    # "<marker> <category> -> <source>": groups cat, rest. The release name
    # is on a line of its own.
    header_line: Pattern | None = None
    # "<label>: <title>": group value. Named, so a group inside a label word
    # never shifts it.
    title_field: Pattern | None = None
    # "<label>: <text>": group value.
    link_field: Pattern | None = None
    # "<label>: <year>": group value.
    year_field: Pattern | None = None
    # "<label> <category> - <quality> - <source> - <size>": groups cat, quality, source, size.
    type_line: Pattern | None = None
    # What opens a metadata line: ignore_line_prefixes and the header markers.
    ignore_prefix: Pattern | None = None
    # Removed from a line before it is tried as a fallback title.
    strip: Pattern = re.compile("\ufe0f+")
    # A line that is only a label, or a category header, is never a title.
    label_line: Pattern | None = None
    media_words: Pattern | None = None
    ignore_hosts: tuple[str, ...] = field(default=())

    def category_type(self, text: str) -> MediaType:
        for media_type, pattern in self.categories:
            if pattern.fullmatch(text.strip()):
                return media_type
        return MediaType.UNKNOWN


# A post read with no profile at all: the scene parser and nothing else.
BARE = Format(key="")


def _by_type(
    words: Mapping[str, Sequence[str]], order: Sequence[MediaType], loc: tuple[Any, ...]
) -> list[tuple[MediaType, str]]:
    out = []
    for media_type in order:
        source = _alternation(list(words.get(media_type.value, ())), (*loc, media_type.value))
        if source:
            out.append((media_type, source))
    return out


def compile_format(profile: FormatProfile) -> Format:
    """Compile one profile. Raises FormatCompileError naming the word or the setting."""
    card = profile.kind == "card"
    headers = tuple(
        (t, _joined(s, ("type_headers", t.value)))
        for t, s in _by_type(profile.type_headers, HEADER_ORDER, ("type_headers",))
    )
    hint_words = dict(_by_type(profile.type_hints, HINT_ORDER, ("type_hints",)))
    categories = dict(_by_type(profile.category_words, HINT_ORDER, ("category_words",))) if card else {}

    any_category = "(?:" + "|".join(categories.values()) + ")" if categories else None
    # Checked here, under its own name, before anything is built on it.
    _compile(any_category, ("category_words",))
    fields = profile.fields
    title = _alternation(fields.title, ("fields", "title")) if card else None
    link = _alternation(fields.link, ("fields", "link")) if card else None
    year = _alternation(fields.year, ("fields", "year")) if card else None
    type_ = _alternation(fields.type, ("fields", "type")) if card else None
    markers = _alternation(profile.header_markers, ("header_markers",)) if card else None
    prefixes = _alternation(profile.ignore_line_prefixes, ("ignore_line_prefixes",))
    media_words = _alternation(profile.media_words, ("media_words",))

    # A category hints its type where a category is written: opening a line,
    # after nothing but symbols, a header marker or a type label. Anywhere
    # else it is a word of a title ("Some Show - 1998"), which says nothing.
    openers = [p for p in (markers, type_) if p]
    opener = rf"(?:(?:{'|'.join(openers)})\s*:?{_SYMBOLS})?" if openers else ""
    hints = []
    for media_type in HINT_ORDER:
        parts = []
        if media_type in hint_words:
            parts.append(hint_words[media_type])
        if media_type in categories:
            parts.append(rf"(?m:^{_SYMBOLS}{opener}{categories[media_type]}\s*{_ARROWS})")
        if parts:
            loc = ("type_hints" if media_type in hint_words else "category_words", media_type.value)
            hints.append((media_type, _joined("|".join(parts), loc)))

    header_line = None
    if markers and any_category:
        header_line = rf"{markers}\s*(?P<cat>{any_category})\s*{_ARROWS}+\s*(?P<rest>.+)"
    type_line = None
    if type_ and any_category:
        type_line = (
            rf"{type_}:?\s*(?P<cat>{any_category})\s*-\s*(?P<quality>\d+p)\s*-\s*"
            rf"(?P<source>.+?)\s*-\s*(?P<size>{_SIZE})"
        )

    # A header line is metadata like any other marked line, so its marker
    # need not be repeated under ignore_line_prefixes.
    opens_metadata = [p for p in (prefixes, markers) if p]
    strip_parts = [r"️"] + opens_metadata
    labels = [p for p in (title, type_, year, link) if p]
    label_line_parts = list(labels)
    if any_category:
        label_line_parts.append(rf"{any_category}\s*{_ARROWS}.*")
    label_line = (
        r"^\s*(?:" + "|".join(label_line_parts) + r")\s*$" if label_line_parts else None
    )

    return Format(
        key=profile.key,
        kind=profile.kind,
        headers=headers,
        hints=tuple(hints),
        categories=tuple(
            (t, _joined(s, ("category_words", t.value))) for t, s in categories.items()
        ),
        header_line=_compile(header_line, ("header_markers",)),
        title_field=_compile(
            rf"{title}\s*:\s*(?P<value>.+?)(?:\s[-–]\s*\d{{4}}(?!\d)|\n|$)" if title else None,
            ("fields", "title"),
        ),
        link_field=_compile(rf"{link}\s*:\s*(?P<value>.*)" if link else None, ("fields", "link")),
        year_field=_compile(rf"{year}\s*:\s*(?P<value>\d{{4}})" if year else None, ("fields", "year")),
        type_line=_compile(type_line, ("fields", "type")),
        ignore_prefix=_compile("|".join(opens_metadata), ("ignore_line_prefixes",)),
        strip=_joined("(?:" + "|".join(strip_parts) + ")+", ("ignore_line_prefixes",)),
        label_line=_compile(label_line, ("fields",)),
        # No word character on either side, whatever the word starts or ends
        # with: a \b would never match beside an emoji or a "!".
        media_words=_compile(rf"(?<!\w){media_words}(?!\w)" if media_words else None, ("media_words",)),
        ignore_hosts=tuple(h.lower() for h in profile.ignore_hosts),
    )


def compile_formats(profiles: Iterable[FormatProfile]) -> tuple[Format, ...]:
    return tuple(compile_format(p) for p in profiles)


def compile_errors(profiles: Sequence[FormatProfile]) -> list[tuple[tuple[Any, ...], str]]:
    """Every word that does not compile, as (loc from the top of the file, message)."""
    errors = []
    for n, profile in enumerate(profiles):
        try:
            compile_format(profile)
        except FormatCompileError as exc:
            errors.append((("formats", n, *exc.loc), exc.message))
    return errors


# The Config the formats were compiled for, and its formats by source key.
_cache: tuple[Config, dict[str | None, tuple[Format, ...]]] | None = None


def formats_for(config: Config, source: str | None = None) -> tuple[Format, ...]:
    """The compiled formats a source's posts are tried against, in order.

    ``source`` is a source key; None means the first userbot source, the one
    group the app read before sources existed. A source that lists no formats
    uses every profile, in file order, and so does a key no source has. With
    no profile at all, posts are read by the scene parser alone.
    """
    global _cache
    if _cache is None or _cache[0] is not config:
        _cache = (config, {})
    by_source = _cache[1]
    if source not in by_source:
        chosen = None
        for s in config.sources:
            if (source is None and s.kind == "userbot") or s.key == source:
                chosen = s
                break
        profiles = list(config.formats)
        if chosen is not None and chosen.formats:
            by_key = {p.key: p for p in profiles}
            profiles = [by_key[k] for k in chosen.formats if k in by_key]
        by_source[source] = compile_formats(profiles) or (BARE,)
    return by_source[source]
