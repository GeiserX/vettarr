from __future__ import annotations

import re
import logging
from collections.abc import Sequence

from vettarr.models import MediaType
from vettarr.parser.formats import BARE, HINT_ORDER, VIDEO_TYPES, Format, formats_for
from vettarr.parser.types import ParsedItem

log = logging.getLogger(__name__)

# --- Core extraction regexes ---
#
# How a release name is read: the same for every group, so it stays in code.

_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")
_QUALITY_RE = re.compile(r"\b(2160p|1080p|1080i|720p|480p|4K|UHD)\b", re.IGNORECASE)
_SOURCE_RE = re.compile(
    r"\b(WEB-DL|WEBDL|WEBRip|WEB-Rip|Full\s+BluRay|BluRay\s+Remux|BluRay|BDRip|BRRip"
    r"|REMUX|HDTV|DVDRip|HDRip|AMZN|NF|DSNP|HMAX|ATVP|FLMN|FILMN|SKY"
    r"|Encode|UHD\s*Custom|CUSTOM)\b",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?\s*(?:GiB|GB|MiB|MB|TiB|TB))", re.IGNORECASE
)
_GROUP_RE = re.compile(r"-(\w+)\s*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")
_SEASON_RE = re.compile(r"\bS(\d{2})", re.IGNORECASE)
_PACK_RE = re.compile(r"\[PACK\]", re.IGNORECASE)
_FREELEECH_RE = re.compile(r"(\d+)%\s*Free", re.IGNORECASE)

_INSTAGRAM_RE = re.compile(r"(?:instagram\.com|instagr\.am)", re.IGNORECASE)

# --- Formats ---
#
# Everything a group writes (its headers, labels, category names, the emoji
# that open its metadata lines) comes from the formats block of config.yaml,
# compiled once in formats.py. A post is tried against each format of its
# source in order and the first that reads it wins.
#
# A type is decided by the words the group writes, never by an emoji on its
# own: an emoji can turn up anywhere, in a film card or in a note, and reading
# one as a type once sent a post down the magazine lane, which skips every
# content check, downloads, and then deletes the group post.


def _formats(formats: Sequence[Format] | None, source: str | None = None) -> Sequence[Format]:
    """The formats given, or the ones the running config gives ``source``.

    ``source`` is the key of the source the post came from; without it the
    formats are those of the first userbot source.
    """
    if formats is not None:
        return formats or (BARE,)
    from vettarr.config.runtime import get_config

    return formats_for(get_config(), source)


def has_release_structure(
    text: str, formats: Sequence[Format] | None = None, *, source: str | None = None
) -> bool:
    """True when the message looks like a release post to any of the formats.

    This is deliberately generous: anything with a recognised media type, a
    title field, a type line, a header line, or a quality plus a size is
    treated as a real post even when the parser then fails to read it.
    """
    return any(_has_structure(fmt, text) for fmt in _formats(formats, source))


def _has_structure(fmt: Format, text: str) -> bool:
    if _detect_type(fmt, text) != MediaType.UNKNOWN:
        return True
    for pattern in (fmt.title_field, fmt.header_line, fmt.type_line):
        if pattern is not None and pattern.search(text):
            return True
    if _QUALITY_RE.search(text) and _SIZE_RE.search(text):
        return True
    return False


# --- Is there anything here somebody could be asking for? ------------------
#
# A message that gets a reason from classify_non_media is finished with: never
# reviewed, never re-parsed, never shown again. That is right for a reel and
# for a note about the shopping, and badly wrong for a bare title in capitals
# or "I want to read this book", which are requests typed in plain words
# rather than release cards. Both shapes turn up in a real group's history.
#
# So the bar to be finished with a message is high, and everything uncertain
# stays in the reviewable failure status it lands in today. Being wrong that
# way costs a line in a queue; being wrong the other way loses a request.

_QUOTED_PHRASE = re.compile(r"[\"“”«»][^\"“”«»]{3,}[\"“”«»]")
_FOUR_DIGIT_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# A whole word of two or more letters; it is shouted when every letter is a capital.
_LETTER_WORD = re.compile(r"\b[^\W\d_]{2,}\b")
_SENTENCE_PUNCTUATION = re.compile(r"[.!?;:]")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_MARKUP = re.compile(r"[*_~`\[\]()]+")

# A line this short, written without sentence punctuation, is how a title is
# typed. A note tends to be longer, or to end in something.
_TITLE_LINE_MIN_WORDS = 2
_TITLE_LINE_MAX_WORDS = 6


def _without_links(text: str) -> str:
    return _URL_RE.sub(" ", text)


def has_title_like_content(text: str, formats: Sequence[Format] | None = None) -> bool:
    """True when the message holds something that could be the name of a work."""
    stripped = _MARKUP.sub(" ", _without_links(text))

    media_words = [f.media_words for f in _formats(formats) if f.media_words is not None]
    if media_words:
        if any(pattern.search(stripped) for pattern in media_words):
            return True
    elif _WORD.search(stripped):
        # No format lists the words that name a kind of media, so nothing
        # here can tell a request typed in plain words from a note. Being
        # finished with a message is terminal: any words at all keep it.
        return True
    if _QUOTED_PHRASE.search(stripped):
        return True
    if _FOUR_DIGIT_YEAR.search(stripped):
        return True
    if any(m.group().isupper() for m in _LETTER_WORD.finditer(stripped)):
        return True

    for line in stripped.split("\n"):
        words = _WORD.findall(line)
        if not words:
            continue
        # A capital that is not starting the line is a name, not grammar.
        if any(word[0].isupper() for word in words[1:]):
            return True
        if (
            _TITLE_LINE_MIN_WORDS <= len(words) <= _TITLE_LINE_MAX_WORDS
            and not _SENTENCE_PUNCTUATION.search(line)
        ):
            return True
    return False


def _ignored_host(text: str, hosts: Sequence[str]) -> str | None:
    """The ignored host this message links to, if that is all it links to."""
    urls = _URL_RE.findall(text)
    if not urls or not hosts:
        return None
    found = None
    for url in urls:
        lowered = url.lower()
        host_here = None
        for host in hosts:
            if f"//{host}" in lowered or f".{host}" in lowered or f"//www.{host}" in lowered:
                host_here = host
                break
        if host_here is None:
            return None
        found = found or host_here
    return found


def classify_non_media(
    text: str, formats: Sequence[Format] | None = None, *, source: str | None = None
) -> str | None:
    """Why this message is finished with, or None when it stays reviewable.

    A message that gets a reason here is left alone for good: never reviewed,
    never re-parsed, and its Telegram post is never deleted. Only two shapes
    earn that: a link to somewhere nothing is ever downloaded from, and a
    message with nothing in it anybody could be asking for.
    """
    if not text or not text.strip():
        return "the message is empty"
    formats = _formats(formats, source)
    if has_release_structure(text, formats):
        return None
    if has_title_like_content(text, formats):
        # Not a release card, but there is a name in here. It belongs in the
        # review queue, where the operator can act on it.
        return None
    hosts = list(dict.fromkeys(h for f in formats for h in f.ignore_hosts))
    host = _ignored_host(text, hosts)
    if host:
        return f"{host} link, not a media post"
    return "no release information in the message"


def should_skip_message(text: str) -> bool:
    """Returns True if the message should not be ingested (e.g. Instagram-only)."""
    if _INSTAGRAM_RE.search(text):
        stripped = re.sub(r"https?://\S+", "", text).strip()
        stripped = re.sub(r"\s+", "", stripped)
        if len(stripped) < 10:
            return True
    return False


def parse_message(
    raw_text: str, formats: Sequence[Format] | None = None, *, source: str | None = None
) -> ParsedItem:
    """Read a post with each format in order; the first that reads it wins.

    When none does, the first format's partial reading is returned; it is not
    valid, so the item lands in parse_failed. ``formats`` defaults to the ones
    the running config gives ``source``, the key of the source the post came
    from.
    """
    first: ParsedItem | None = None
    for fmt in _formats(formats, source):
        item = _parse_with(fmt, raw_text)
        if item.is_valid:
            return item
        if first is None:
            first = item
    assert first is not None
    return first


def _parse_with(fmt: Format, raw_text: str) -> ParsedItem:
    text = raw_text.replace("**", "").replace("__", "").strip()
    item = ParsedItem()

    all_urls = _URL_RE.findall(text)
    item.external_links = [u for u in all_urls if not _INSTAGRAM_RE.search(u)]

    item.media_type = _detect_type(fmt, text)

    if fmt.title_field is not None and fmt.title_field.search(text):
        _extract_structured_fields(fmt, text, item)
        # A card with a title line can carry a type line as well.
        _read_type_line(fmt, text, item)
    elif fmt.header_line is not None and fmt.header_line.search(text):
        _extract_header_fields(fmt, text, item)
    elif fmt.type_line is not None and fmt.type_line.search(text):
        _extract_type_line_fields(fmt, text, item)
    else:
        _extract_release_fields(fmt, text, item)

    if not item.year:
        m = fmt.year_field.search(text) if fmt.year_field is not None else None
        if m:
            item.year = int(m.group("value"))
        else:
            m = _YEAR_PAREN_RE.search(text)
            if m:
                item.year = int(m.group(1))

    return item


def _has_video_markers(text: str) -> bool:
    """A video quality marker *and* a source marker: this is a release file."""
    return bool(_QUALITY_RE.search(text) and _SOURCE_RE.search(text))


def _detect_type(fmt: Format, text: str) -> MediaType:
    # A post that names a video quality and a source is a film or a series,
    # whatever else it happens to mention, so the written book and magazine
    # headers only get a say when it does not.
    #
    # Deciding film-or-series here as well would be too early: the season
    # number is not the only thing that says "series". A series header line,
    # a type line naming a series category and a series hint word all say it
    # too, and all three sit below this point.
    video = _has_video_markers(text)
    for media_type, pattern in fmt.headers:
        if video and media_type not in VIDEO_TYPES:
            continue
        if pattern.search(text):
            return media_type

    if fmt.type_line is not None:
        m = fmt.type_line.search(text)
        if m:
            return fmt.category_type(m.group("cat"))

    hints = dict(fmt.hints)
    series_hint = hints.get(MediaType.SERIES)
    if (
        _PACK_RE.search(text)
        or _SEASON_RE.search(text)
        or (series_hint is not None and series_hint.search(text))
    ):
        return MediaType.SERIES
    for media_type in HINT_ORDER:
        pattern = hints.get(media_type)
        if media_type != MediaType.SERIES and pattern is not None and pattern.search(text):
            return media_type

    if fmt.header_line is not None:
        m = fmt.header_line.search(text)
        if m:
            return fmt.category_type(m.group("cat"))

    if video:
        if _SEASON_RE.search(text):
            return MediaType.SERIES
        return MediaType.MOVIE

    if _is_plain_book(text):
        return MediaType.EBOOK

    return MediaType.UNKNOWN


def _is_plain_book(text: str) -> bool:
    """Detect plain text like 'Author Name - Book Title'."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) > 3:
        return False
    joined = " ".join(lines)
    if len(joined) > 200:
        return False
    if _QUALITY_RE.search(joined) or _SOURCE_RE.search(joined):
        return False
    if _URL_RE.search(joined):
        return False
    return " - " in joined


_SIZE_LINE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:GiB|GB|MiB|MB)", re.IGNORECASE)
_FREELEECH_LINE = re.compile(r"\d+%\s*Free", re.IGNORECASE)


def _is_metadata_line(fmt: Format, line: str) -> bool:
    """Check if a line is a card's metadata line rather than a release name.

    A line that opens with one of the format's ignore_line_prefixes or header
    markers, or that states a size or a freeleech percentage.
    """
    s = line.strip()
    if not s:
        return True
    if fmt.ignore_prefix is not None and fmt.ignore_prefix.match(s):
        return True
    return bool(_SIZE_LINE.match(s) or _FREELEECH_LINE.match(s))


def _extract_structured_fields(fmt: Format, text: str, item: ParsedItem) -> None:
    """Extract title/year from a card with labelled lines (books, magazines, ...)."""
    assert fmt.title_field is not None
    m = fmt.title_field.search(text)
    if m:
        raw_title = m.group("value").strip()
        raw_title = re.sub(r"[\s(]+$", "", raw_title)
        item.title = raw_title

    link_m = fmt.link_field.search(text) if fmt.link_field is not None else None
    if link_m:
        link_text = link_m.group("value").strip()
        urls = _URL_RE.findall(link_text)
        for u in urls:
            if u not in item.external_links and not _INSTAGRAM_RE.search(u):
                item.external_links.append(u)
        if not item.title and not urls:
            item.title = re.sub(r"[\s(]+$", "", link_text.strip())

    if not item.title:
        item.title = _fallback_title(fmt, text, item.media_type)


def _extract_header_fields(fmt: Format, text: str, item: ParsedItem) -> None:
    """Extract from a card with the release line at the top and a header line below."""
    assert fmt.header_line is not None
    header_m = fmt.header_line.search(text)
    header_source = header_m.group("rest").strip() if header_m else None

    lines = text.split("\n")
    release_line = None
    for line in lines:
        stripped = line.strip()
        if _is_metadata_line(fmt, stripped):
            continue
        if len(stripped) > 5:
            release_line = stripped
            break

    if release_line:
        _extract_release_from_line(release_line, item)

    m = _SIZE_RE.search(text)
    if m and not item.size_text:
        item.size_text = m.group(1)

    if not item.source and header_source:
        item.source = header_source

    if not item.season:
        sm = _SEASON_RE.search(text)
        if sm:
            item.season = f"S{sm.group(1)}"

    if not item.title:
        item.title = _fallback_title(fmt, text, item.media_type)


def _extract_type_line_fields(fmt: Format, text: str, item: ParsedItem) -> None:
    """Extract from a card with the release name first and a type line below."""
    assert fmt.type_line is not None
    first_line = text.split("\n")[0].strip()
    _extract_release_from_line(first_line, item)
    _read_type_line(fmt, text, item)

    if not item.season:
        sm = _SEASON_RE.search(text)
        if sm:
            item.season = f"S{sm.group(1)}"


def _read_type_line(fmt: Format, text: str, item: ParsedItem) -> None:
    """Quality, source and size from the card's type line, when it has one."""
    type_m = fmt.type_line.search(text) if fmt.type_line is not None else None
    if type_m:
        item.quality = type_m.group("quality").upper()
        item.source = type_m.group("source").strip()
        item.size_text = type_m.group("size")


# A scene release name: no space in it, its words joined by dots.
_DOTTED_NAME = re.compile(r"^[^\s.]+(?:\.+[^\s.]+){2,}$")
_BARE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _dotted_title(line: str, item: ParsedItem) -> str | None:
    """The title of a dotted release name, with its year and season read off.

    ``Some.Film.2024.1080p.WEB-DL.x264-GROUP``: the name ends where the tags
    start (the quality or the season; without either, the source), the last
    year before that is the year, and what stands before the year is the
    title. None when the line is not such a name; it is then read as words
    with spaces between them.
    """
    if not _DOTTED_NAME.match(line):
        return None
    name = re.sub(r"\.+", " ", line)
    season_m = _SEASON_RE.search(name)
    ends = [m.start() for m in (_QUALITY_RE.search(name), season_m) if m]
    if not ends:
        # A source word that opens the name is a word of the title.
        ends = [m.start() for m in _SOURCE_RE.finditer(name) if m.start() > 0][:1]
    if not ends:
        return None
    if season_m and not item.season:
        item.season = f"S{season_m.group(1)}"
    name = name[: min(ends)]
    years = [m for m in _BARE_YEAR.finditer(name) if m.start() > 0]
    if years:
        item.year = int(years[-1].group(1))
        name = name[: years[-1].start()]
    return re.sub(r"[\s(\[]+$", "", name)


def _extract_release_from_line(line: str, item: ParsedItem) -> None:
    """Extract title, year, quality, source from a release name line."""
    m = _QUALITY_RE.search(line)
    if m:
        item.quality = m.group(1).upper()

    m = _SOURCE_RE.search(line)
    if m:
        item.source = m.group(1)

    m = _GROUP_RE.search(line)
    if m:
        item.release_group = m.group(1)

    title = re.sub(r"\[.*?\]", "", line).strip()

    dotted = _dotted_title(title, item)
    year_m = _YEAR_PAREN_RE.search(title)
    if dotted is not None:
        title = dotted
    elif year_m:
        item.year = int(year_m.group(1))
        title = title[: year_m.start()].strip()
    else:
        bare_m = re.search(r"\b((?:19|20)\d{2})\s*$", title)
        if bare_m:
            item.year = int(bare_m.group(1))
            title = title[: bare_m.start()].strip()
        else:
            for pat in (_QUALITY_RE, _SOURCE_RE):
                pm = pat.search(title)
                if pm:
                    title = title[: pm.start()].strip()
                    break

    title = re.sub(r"\s+", " ", title).strip()
    season_m = _SEASON_RE.search(title)
    if season_m and not item.season:
        item.season = f"S{season_m.group(1)}"
    title = re.sub(r"\s+S\d{2}(?:E\d{2,3})?$", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s*[-–]+\s*$", "", title).strip()
    if title:
        item.title = title


def _extract_release_fields(fmt: Format, text: str, item: ParsedItem) -> None:
    """Extract from scene-style release names (movies, series)."""
    if item.media_type == MediaType.EBOOK and _is_plain_book(text):
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        item.title = lines[0] if lines else None
        return

    first_line = text.split("\n")[0].strip()
    _extract_release_from_line(first_line, item)

    m = _SIZE_RE.search(text)
    if m and not item.size_text:
        item.size_text = m.group(1)

    if not item.title:
        item.title = _fallback_title(fmt, text, item.media_type)


def _fallback_title(fmt: Format, text: str, media_type: MediaType) -> str | None:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for line in lines:
        clean = fmt.strip.sub("", line).strip()
        if fmt.label_line is not None:
            clean = fmt.label_line.sub("", clean).strip()
        # A size or a freeleech line is never what the post is called: with
        # nothing else to read, the post has no title and stays unreadable.
        if _SIZE_LINE.match(clean) or _FREELEECH_LINE.match(clean):
            continue
        if len(clean) > 5 and not clean.startswith("http"):
            return clean
    return None
