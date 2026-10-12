"""Format profiles: the words a group writes come from config.yaml, not code.

A post is tried against each format of its source in order and the first that
reads a title and a type wins; with none, it stays unreadable (parse_failed).
The words are compiled once, and a pattern that does not compile is an error
anchored to its line. The parser package itself carries no group's wording.
"""

import contextlib
import io
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from vettarr.config import cli, runtime
from vettarr.config.loader import ConfigError, load, load_text
from vettarr.config.schema import Config, FormatProfile, Source
from vettarr.models import MediaType
from vettarr.parser import formats as formats_mod
from vettarr.parser.formats import BARE, FormatCompileError, compile_format, compile_formats, formats_for
from vettarr.parser.regex_parser import classify_non_media, has_release_structure, parse_message

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"
PARSER_DIR = ROOT / "src" / "vettarr" / "parser"

EXAMPLE_CONFIG = load(EXAMPLE, env={})
CARD, SCENE = compile_formats(EXAMPLE_CONFIG.formats)

# A card with a header line, which the example does not ship. The marker is
# listed nowhere else: a header line is metadata by being one.
CATEGORIES = {"movie": ["Movies"], "series": ["Series"]}
HEADER = compile_format(FormatProfile(key="header-card", header_markers=["Category:"], category_words=CATEGORIES))
RELEASE = "An Invented Film 2024 1080p WEB-DL-GROUP"


class ExampleFormatTests(unittest.TestCase):
    """The two formats config.example.yaml ships."""

    def test_the_card_reads_every_labelled_line(self) -> None:
        text = (
            "NEW MOVIE\n"
            "Title: Some Film\n"
            "Year: 2024\n"
            "Type: Movie - 1080p - WEB-DL - 4.2 GB\n"
            "Link: https://tracker-a.example/details.php?id=42"
        )
        item = parse_message(text, (CARD,))
        self.assertTrue(item.is_valid)
        self.assertEqual(item.media_type, MediaType.MOVIE)
        self.assertEqual(item.title, "Some Film")
        self.assertEqual(item.year, 2024)
        self.assertEqual(item.external_links, ["https://tracker-a.example/details.php?id=42"])
        # The card's own Type line, below its Title line.
        self.assertEqual((item.quality, item.source, item.size_text), ("1080P", "WEB-DL", "4.2 GB"))

    def test_a_title_card_without_a_type_line_states_no_quality(self) -> None:
        item = parse_message("NEW MOVIE\nTitle: Some Film\nYear: 2024", (CARD,))
        self.assertEqual((item.title, item.year), ("Some Film", 2024))
        self.assertEqual((item.quality, item.source, item.size_text), (None, None, None))

    def test_the_card_reads_a_type_line(self) -> None:
        text = "Some Show S02 1080p WEB-DL\nType: Series - 1080p - WEB-DL - 12.0 GiB"
        item = parse_message(text, (CARD,))
        self.assertEqual(item.media_type, MediaType.SERIES)
        self.assertEqual(item.title, "Some Show")
        self.assertEqual(item.quality, "1080P")
        self.assertEqual(item.source, "WEB-DL")
        self.assertEqual(item.size_text, "12.0 GiB")
        self.assertEqual(item.season, "S02")

    def test_the_card_header_decides_a_book(self) -> None:
        item = parse_message("NEW EBOOK\nTitle: An Invented Book", (CARD,))
        self.assertEqual(item.media_type, MediaType.EBOOK)
        self.assertEqual(item.title, "An Invented Book")

    def test_the_scene_format_reads_a_release_name(self) -> None:
        item = parse_message("Some.Film.2024.1080p.WEB-DL.x264-GROUP\n8.4 GiB", (SCENE,))
        self.assertTrue(item.is_valid)
        self.assertEqual(item.media_type, MediaType.MOVIE)
        self.assertEqual((item.title, item.year), ("Some Film", 2024))
        self.assertEqual(item.quality, "1080P")
        self.assertEqual(item.source, "WEB-DL")
        self.assertEqual(item.release_group, "GROUP")
        self.assertEqual(item.size_text, "8.4 GiB")

    def test_the_example_s_two_posts_read_as_documented_with_both_formats(self) -> None:
        """A source that lists no formats tries every profile in file order."""
        card = parse_message(
            "NEW MOVIE\nTitle: Some Film\nYear: 2024\nType: Movie - 1080p - WEB-DL - 4.2 GB\n"
            "Link: https://tracker-a.example/details.php?id=42",
            (CARD, SCENE),
        )
        scene = parse_message("Some.Film.2024.1080p.WEB-DL.x264-GROUP", (CARD, SCENE))
        for item in (card, scene):
            self.assertEqual(
                (item.media_type, item.title, item.year, item.quality, item.source),
                (MediaType.MOVIE, "Some Film", 2024, "1080P", "WEB-DL"),
            )
        self.assertEqual(card.size_text, "4.2 GB")
        self.assertEqual(scene.release_group, "GROUP")

    def test_the_scene_format_reads_a_season_pack(self) -> None:
        item = parse_message("Some Show S03 1080p WEB-DL AAC-GROUP [PACK]\n18.0 GiB", (SCENE,))
        self.assertEqual(item.media_type, MediaType.SERIES)
        self.assertEqual(item.season, "S03")

    def test_the_scene_format_knows_no_card_words(self) -> None:
        """The card's header means nothing to the scene format."""
        item = parse_message("NEW EBOOK\nTitle: An Invented Book", (SCENE,))
        self.assertEqual(item.media_type, MediaType.UNKNOWN)
        self.assertFalse(item.is_valid)

    def test_a_card_label_does_not_match_inside_a_word(self) -> None:
        """`Title` is a whole word: a `Subtitle:` line is not the title."""
        item = parse_message("NEW EBOOK\nSubtitle: English\nTitle: The Real One", (CARD,))
        self.assertEqual(item.title, "The Real One")


class DottedReleaseNameTests(unittest.TestCase):
    """A release name whose words are joined by dots."""

    def read(self, name: str):
        item = parse_message(name, ())
        return item.title, item.year, item.season

    def test_dots_are_separators_and_the_year_ends_the_title(self) -> None:
        self.assertEqual(self.read("Some.Film.2024.1080p.WEB-DL.x264-GROUP"), ("Some Film", 2024, None))

    def test_tags_between_the_year_and_the_quality_are_not_the_title(self) -> None:
        self.assertEqual(self.read("Some.Film.2024.PROPER.MULTi.1080p.WEB-DL.x264-GROUP"), ("Some Film", 2024, None))

    def test_a_year_in_brackets(self) -> None:
        self.assertEqual(self.read("Some.Film.(2024).1080p.WEB-DL.x264-GROUP"), ("Some Film", 2024, None))

    def test_a_title_that_is_a_year_keeps_it(self) -> None:
        self.assertEqual(self.read("2012.2009.1080p.BluRay.x264-GROUP"), ("2012", 2009, None))
        self.assertEqual(self.read("1917.1080p.BluRay.x264-GROUP"), ("1917", None, None))

    def test_a_series_ends_at_its_season(self) -> None:
        self.assertEqual(self.read("Some.Show.S02E05.1080p.WEB-DL.x264-GROUP"), ("Some Show", None, "S02"))
        self.assertEqual(self.read("Some.Show.2019.S01.720p.HDTV.x264-GROUP"), ("Some Show", 2019, "S01"))

    def test_a_source_word_that_opens_the_name_is_a_word_of_the_title(self) -> None:
        self.assertEqual(self.read("Sky.High.2005.1080p.BluRay.x264-GROUP"), ("Sky High", 2005, None))
        self.assertEqual(self.read("Sky.High.2005.BluRay.x264-GROUP"), ("Sky High", 2005, None))

    def test_a_name_with_spaces_is_read_as_before(self) -> None:
        self.assertEqual(self.read("Mr. Nobody (2009) 1080p BluRay x264-GROUP"), ("Mr. Nobody", 2009, None))
        self.assertEqual(self.read(RELEASE), ("An Invented Film 2024", None, None))

    def test_a_dotted_line_with_no_release_tag_is_not_a_release_name(self) -> None:
        item = parse_message("www.example.org", ())
        self.assertEqual((item.title, item.year), ("www.example.org", None))


class CategoryHintTests(unittest.TestCase):
    """A category word hints its type where a category is written: opening a
    line. Inside a title it is a word like any other."""

    @staticmethod
    def kind(text: str, formats=(CARD, SCENE)) -> MediaType:
        return parse_message(text, formats).media_type

    def test_a_title_that_ends_in_a_category_word_is_not_typed_by_it(self) -> None:
        self.assertEqual(
            self.kind("NEW MOVIE\nTitle: The Invented Show - 1998\nLink: https://example.invalid/a"),
            MediaType.MOVIE,
        )
        self.assertEqual(
            self.kind("The Invented Show - 1998 - 1080p BluRay x264-GROUP\n8 GiB"), MediaType.MOVIE
        )
        self.assertEqual(self.kind("A Short Film - Example Author"), MediaType.EBOOK)

    def test_a_category_that_opens_a_line_hints_its_type(self) -> None:
        release = "An Invented Show 1080p WEB-DL AAC-GROUP"
        for line in ("Series → WEB-DL", "» Series - WEB-DL", "**Series** -> WEB-DL", "Type: Series - WEB-DL"):
            with self.subTest(line=line):
                self.assertEqual(self.kind(f"{release}\n{line}\n12.0 GiB"), MediaType.SERIES)

    def test_after_a_header_marker_too(self) -> None:
        text = "An Invented Show 1080p WEB-DL AAC-GROUP\nCategory: Series → WEB-DL\n12.0 GiB"
        self.assertEqual(self.kind(text, (HEADER,)), MediaType.SERIES)


class HeaderLineTests(unittest.TestCase):
    """A card with a header line: `<marker> <category> -> <source>`, and the
    release name on a line of its own."""

    def test_the_header_line_is_never_the_title_wherever_it_sits(self) -> None:
        below = parse_message(f"{RELEASE}\nCategory: Movies → WEB-DL\n8.4 GiB", (HEADER,))
        above = parse_message(f"Category: Movies → WEB-DL\n{RELEASE}\n8.4 GiB", (HEADER,))
        for item in (below, above):
            self.assertEqual(
                (item.media_type, item.title, item.quality, item.source),
                (MediaType.MOVIE, "An Invented Film 2024", "1080P", "WEB-DL"),
            )

    def test_an_emoji_marker_reads_the_same(self) -> None:
        marker = "\U0001f39e"
        fmt = compile_format(FormatProfile(key="emoji", header_markers=[marker], category_words=CATEGORIES))
        item = parse_message(f"{marker} Movies → WEB-DL\n{RELEASE}\n8.4 GiB", (fmt,))
        self.assertEqual((item.title, item.quality), ("An Invented Film 2024", "1080P"))

    def test_what_follows_the_arrow_is_the_source_never_the_title(self) -> None:
        item = parse_message("Category: Movies -> Some Film\n8.4 GiB", (HEADER,))
        self.assertEqual(item.source, "Some Film")
        self.assertIsNone(item.title)
        self.assertFalse(item.is_valid)

    def test_a_header_card_with_no_release_line_has_no_title(self) -> None:
        """Neither the header line nor the size is what the post is called: it
        is not valid, and it is not ignored either, so it lands in parse_failed."""
        text = "Category: Series → WEB-DL\n12.0 GiB\n100% Free"
        item = parse_message(text, (HEADER,))
        self.assertEqual(item.media_type, MediaType.SERIES)
        self.assertIsNone(item.title)
        self.assertFalse(item.is_valid)
        self.assertIsNone(classify_non_media(text, (HEADER,)))

    def test_a_line_with_an_ignored_prefix_is_never_the_title(self) -> None:
        fmt = compile_format(FormatProfile(
            key="prefixed", header_markers=["Category:"], category_words=CATEGORIES,
            ignore_line_prefixes=["Uploaded by", "Note:"],
        ))
        text = (
            "Uploaded by someone this morning\n"
            "Note: thanks to whoever shared it\n"
            f"{RELEASE}\n"
            "Category: Movies → WEB-DL\n8.4 GiB"
        )
        self.assertEqual(parse_message(text, (fmt,)).title, "An Invented Film 2024")


class OrderTests(unittest.TestCase):
    """The first format that reads the post wins."""

    def setUp(self) -> None:
        self.by_title = compile_format(FormatProfile(
            key="by-title", type_headers={"ebook": ["NEW EBOOK"]}, fields={"title": ["Title"]},
        ))
        self.by_name = compile_format(FormatProfile(
            key="by-name", type_headers={"ebook": ["NEW EBOOK"]}, fields={"title": ["Name"]},
        ))
        self.text = "NEW EBOOK\nName: From The Name Line\nTitle: From The Title Line"

    def test_the_first_format_wins(self) -> None:
        self.assertEqual(parse_message(self.text, (self.by_title, self.by_name)).title, "From The Title Line")
        self.assertEqual(parse_message(self.text, (self.by_name, self.by_title)).title, "From The Name Line")

    def test_a_format_that_cannot_read_the_post_passes_it_on(self) -> None:
        item = parse_message("Some Film (2024) 1080p WEB-DL-GROUP", (CARD, SCENE))
        self.assertTrue(item.is_valid)
        item = parse_message("NEW EBOOK\nTitle: An Invented Book", (SCENE, CARD))
        self.assertEqual(item.media_type, MediaType.EBOOK)

    def test_a_source_uses_its_own_formats_in_its_own_order(self) -> None:
        cfg = Config(
            formats=[FormatProfile(key="a"), FormatProfile(key="b", kind="scene")],
            sources=[Source(key="films", kind="userbot", chat=-1, formats=["b", "a"])],
        )
        self.assertEqual([f.key for f in formats_for(cfg)], ["b", "a"])
        self.assertEqual([f.key for f in formats_for(cfg, "films")], ["b", "a"])

    def test_a_source_with_no_formats_uses_every_profile_in_file_order(self) -> None:
        cfg = Config(formats=[FormatProfile(key="a"), FormatProfile(key="b")])
        self.assertEqual([f.key for f in formats_for(cfg)], ["a", "b"])

    def test_the_partial_reading_kept_is_the_first_formats(self) -> None:
        """No format reads the post: what the first one read is what is stored."""
        by_title = compile_format(FormatProfile(key="by-title", fields={"title": ["Title"]}))
        by_name = compile_format(FormatProfile(key="by-name", fields={"title": ["Name"]}))
        text = "Name: From The Name Line\nTitle: From The Title Line"
        first = parse_message(text, (by_title, by_name))
        self.assertFalse(first.is_valid)
        self.assertEqual(first.title, "From The Title Line")
        self.assertEqual(parse_message(text, (by_name, by_title)).title, "From The Name Line")


class SourceFormatsTests(unittest.TestCase):
    """A post is tried against the formats of the source it came from."""

    CARD_POST = "NEW MAGAZINE\nTitle: whatever was typed in the scene-only source"
    NOTE = "remember to call the plumber tomorrow in the morning, please."

    def setUp(self) -> None:
        self.sources = [
            Source(key="cards", kind="userbot", chat=-1, formats=["card"]),
            Source(key="inbox", kind="userbot", chat=-2, formats=["scene"]),
            Source(key="open", kind="userbot", chat=-3),
        ]
        self.cfg = Config(formats=EXAMPLE_CONFIG.formats, sources=self.sources)
        self.addCleanup(setattr, formats_mod, "_cache", None)

    @staticmethod
    def keys(cfg: Config, source: str | None = None) -> list[str]:
        return [f.key for f in formats_for(cfg, source)]

    def test_each_source_gets_its_own_list(self) -> None:
        self.assertEqual(self.keys(self.cfg, "cards"), ["card"])
        self.assertEqual(self.keys(self.cfg, "inbox"), ["scene"])
        self.assertEqual(self.keys(self.cfg, "open"), ["card", "scene"])

    def test_whatever_order_the_sources_are_in(self) -> None:
        cfg = Config(formats=EXAMPLE_CONFIG.formats, sources=list(reversed(self.sources)))
        self.assertEqual(self.keys(cfg, "cards"), ["card"])
        self.assertEqual(self.keys(cfg, "inbox"), ["scene"])

    def test_a_bot_source_uses_its_own_list(self) -> None:
        cfg = Config(
            formats=EXAMPLE_CONFIG.formats,
            sources=[Source(key="bot", kind="bot", chat="private", formats=["scene"])],
        )
        self.assertEqual(self.keys(cfg, "bot"), ["scene"])

    def test_no_source_named_is_the_first_userbot_source(self) -> None:
        self.assertEqual(self.keys(self.cfg), ["card"])

    def test_a_key_no_source_has_uses_every_profile(self) -> None:
        self.assertEqual(self.keys(self.cfg, "gone"), ["card", "scene"])

    def test_each_source_is_compiled_once(self) -> None:
        cards, inbox = formats_for(self.cfg, "cards"), formats_for(self.cfg, "inbox")
        self.assertIs(formats_for(self.cfg, "cards"), cards)
        self.assertIs(formats_for(self.cfg, "inbox"), inbox)

    def test_the_same_post_reads_differently_in_each_source(self) -> None:
        """The parser, asked by source key, reads with that source's formats."""
        with mock.patch.object(runtime, "get_config", lambda: self.cfg):
            card = parse_message(self.CARD_POST, source="cards")
            self.assertEqual((card.media_type, card.is_valid), (MediaType.MAGAZINE, True))
            scene = parse_message(self.CARD_POST, source="inbox")
            self.assertEqual((scene.media_type, scene.is_valid), (MediaType.UNKNOWN, False))
            self.assertTrue(has_release_structure(self.CARD_POST, source="cards"))
            self.assertFalse(has_release_structure(self.CARD_POST, source="inbox"))
            # The card format lists media words and the note holds none; the
            # scene profile lists none, so there the note stays reviewable.
            self.assertEqual(
                classify_non_media(self.NOTE, source="cards"), "no release information in the message"
            )
            self.assertIsNone(classify_non_media(self.NOTE, source="inbox"))


class MediaWordsTests(unittest.TestCase):
    """A media word is a whole word, whatever it starts or ends with."""

    SENTENCE = "could you look for that {} we were all talking about yesterday, thanks."

    def setUp(self) -> None:
        words = ["\U0001f4da", "sci-fi!", "film"]
        self.formats = (compile_format(FormatProfile(key="words", kind="scene", media_words=words)),)

    def kept(self, word: str) -> bool:
        return classify_non_media(self.SENTENCE.format(word), self.formats) is None

    def test_a_word_may_start_or_end_with_a_symbol(self) -> None:
        for word in ("\U0001f4da", "sci-fi!", "film"):
            with self.subTest(word=word):
                self.assertTrue(self.kept(word))

    def test_it_never_matches_inside_a_longer_word(self) -> None:
        for word in ("microfilm", "films", "thing"):
            with self.subTest(word=word):
                self.assertFalse(self.kept(word))

    def test_no_profile_at_all_leaves_the_scene_parser(self) -> None:
        cfg = Config()
        self.assertEqual(formats_for(cfg), (BARE,))
        self.assertTrue(parse_message("Some Film (2024) 1080p WEB-DL-GROUP", ()).is_valid)


class NoFormatMatchesTests(unittest.TestCase):
    def test_a_post_no_format_reads_is_not_valid(self) -> None:
        """It lands in parse_failed, with what the first format could read."""
        text = "Type: Documentary - 4.2 GiB\nLink: https://example.invalid/x"
        item = parse_message(text, (CARD, SCENE))
        self.assertFalse(item.is_valid)
        self.assertEqual(item.media_type, MediaType.UNKNOWN)
        self.assertEqual(item.external_links, ["https://example.invalid/x"])

    def test_it_still_has_release_structure_so_it_is_not_ignored(self) -> None:
        text = "Some Unreadable Thing 1080p 4.2 GiB"
        self.assertTrue(has_release_structure(text, (CARD,)))
        self.assertIsNone(classify_non_media(text, (CARD,)))


class CompileTests(unittest.TestCase):
    def test_a_word_between_slashes_is_a_regular_expression(self) -> None:
        fmt = compile_format(FormatProfile(key="re", type_hints={"series": [r"/\bSeason\s+\d+/"]}))
        self.assertEqual(parse_message("Some Show Season 2 1080p WEB-DL", (fmt,)).media_type, MediaType.SERIES)
        self.assertEqual(parse_message("Some Show 1080p WEB-DL", (fmt,)).media_type, MediaType.MOVIE)

    def test_a_plain_word_is_literal(self) -> None:
        fmt = compile_format(FormatProfile(key="lit", type_headers={"ebook": ["NEW (BOOK)"]}))
        self.assertEqual(parse_message("NEW (BOOK)\nAuthor - Title", (fmt,)).media_type, MediaType.EBOOK)
        self.assertEqual(parse_message("NEW BOOK\nsomething", (fmt,)).media_type, MediaType.UNKNOWN)

    def test_a_space_matches_any_run_of_whitespace(self) -> None:
        fmt = compile_format(FormatProfile(key="ws", type_headers={"magazine": ["NEW MAGAZINE"]}))
        self.assertEqual(parse_message("new \t  magazine\nTitle", (fmt,)).media_type, MediaType.MAGAZINE)

    def test_an_invalid_pattern_names_the_word(self) -> None:
        profile = FormatProfile(key="bad", type_hints={"series": ["NEW SERIES", "/(unclosed/"]})
        with self.assertRaises(FormatCompileError) as caught:
            compile_format(profile)
        self.assertEqual(caught.exception.loc, ("type_hints", "series", 1))
        self.assertIn("pattern does not compile", caught.exception.message)

    def test_an_invalid_pattern_in_a_field_names_the_field(self) -> None:
        profile = FormatProfile(key="bad", fields={"title": ["/[/"]})
        with self.assertRaises(FormatCompileError) as caught:
            compile_format(profile)
        self.assertEqual(caught.exception.loc, ("fields", "title", 0))

    def test_the_loader_anchors_it_to_its_line(self) -> None:
        text = textwrap.dedent("""\
            formats:
              - key: ok
              - key: bad
                type_hints:
                  series:
                    - NEW SERIES
                    - '/(unclosed/'
            """)
        with self.assertRaises(ConfigError) as caught:
            load_text(text, "c.yaml", {})
        self.assertEqual(len(caught.exception.errors), 1)
        self.assertRegex(
            caught.exception.errors[0],
            r"^c\.yaml:7: formats\[1\]\.type_hints\.series\[1\]: pattern does not compile: ",
        )

    def test_a_word_that_only_compiles_alone_names_the_word(self) -> None:
        """`(?i)` compiles on its own and not inside the pattern it joins."""
        profile = FormatProfile(key="bad", type_hints={"series": ["NEW SERIES", "/(?i)season/"]})
        with self.assertRaises(FormatCompileError) as caught:
            compile_format(profile)
        self.assertEqual(caught.exception.loc, ("type_hints", "series", 1))
        self.assertIn("pattern does not compile", caught.exception.message)

    def test_a_group_name_the_app_uses_names_the_word(self) -> None:
        profile = FormatProfile(
            key="bad", header_markers=["Category:"], category_words={"movie": ["/(?P<cat>Movies)/"]},
        )
        with self.assertRaises(FormatCompileError) as caught:
            compile_format(profile)
        self.assertEqual(caught.exception.loc, ("category_words", "movie", 0))
        self.assertIn("'cat'", caught.exception.message)

    def test_one_group_name_in_two_words_names_the_setting(self) -> None:
        profile = FormatProfile(
            key="bad",
            category_words={"movie": ["/(?P<kind>Movies)/"], "series": ["/(?P<kind>Series)/"]},
        )
        with self.assertRaises(FormatCompileError) as caught:
            compile_format(profile)
        self.assertEqual(caught.exception.loc, ("category_words",))
        self.assertIn("do not compile together", caught.exception.message)

    TOGETHER = textwrap.dedent("""\
        formats:
          - key: flag
            type_hints:
              series:
                - NEW SERIES
                - '/(?i)season/'
          - key: reserved
            header_markers: ['Category:']
            category_words:
              movie: ['/(?P<cat>Movies)/']
          - key: twice
            category_words:
              movie: ['/(?P<kind>Movies)/']
              series: ['/(?P<kind>Series)/']
        """)
    TOGETHER_ERRORS = (
        r"^{}:6: formats\[0\]\.type_hints\.series\[1\]: pattern does not compile: ",
        r"^{}:10: formats\[1\]\.category_words\.movie\[0\]: group name 'cat' is one the app uses itself",
        r"^{}:12: formats\[2\]\.category_words: the words do not compile together: ",
    )

    def test_the_loader_anchors_words_that_do_not_compile_together(self) -> None:
        """Each of these words compiles alone. The load fails with one anchored
        line per profile, never with a traceback from `re`."""
        with self.assertRaises(ConfigError) as caught:
            load_text(self.TOGETHER, "c.yaml", {})
        self.assertEqual(len(caught.exception.errors), 3)
        for line, pattern in zip(caught.exception.errors, self.TOGETHER_ERRORS):
            self.assertRegex(line, pattern.format(r"c\.yaml"))

    def test_config_check_reports_them_and_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(self.TOGETHER, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["check", str(path)])
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertEqual(lines[0], f"{path}: not valid")
        self.assertEqual(len(lines), 4)
        for line, pattern in zip(lines[1:], self.TOGETHER_ERRORS):
            self.assertRegex(line.strip(), pattern.format(re.escape(str(path))))


class GroupsInLabelWordsTests(unittest.TestCase):
    """A group inside a label word changes nothing about what is read."""

    def setUp(self) -> None:
        self.formats = (compile_format(FormatProfile(
            key="grouped",
            type_headers={"ebook": ["NEW EBOOK"]},
            fields={
                "title": ["/(Title|Name)/"],
                "year": ["/(Year|Released)/"],
                "link": ["/(Link|Page)/"],
            },
        )),)

    def test_the_title_and_the_year_are_the_values_not_the_labels(self) -> None:
        for text in (
            "NEW EBOOK\nTitle: Some Book\nYear: 2024",
            "NEW EBOOK\nName: Some Book\nReleased: 2024",
        ):
            with self.subTest(text=text):
                item = parse_message(text, self.formats)
                self.assertTrue(item.is_valid)
                self.assertEqual((item.title, item.year), ("Some Book", 2024))

    def test_a_link_line_that_names_the_work_is_read_the_same(self) -> None:
        """With no title to read, the text of the link line is the title."""
        item = parse_message("NEW EBOOK\nTitle: (\nPage: An Invented Book", self.formats)
        self.assertEqual(item.title, "An Invented Book")


# Wording of the kind that lived in regex_parser.py before format profiles.
# None of it may come back into the parser. The words of one group's own card
# are not written here either: the release gate (scripts/leak-gate.sh) checks
# every tracked file for them.
REMOVED_WORDS = (
    r"nuev[oa]", r"libros?", r"audiolibros?", r"revistas?", r"cómics?",
    r"discos?", r"álbum(?:es)?", r"albumes", r"m[uú]sica", r"pel[ií]culas?", r"pelis?",
    r"animaci[oó]n", r"documental(?:es)?", r"serie", r"temporadas?", r"tipo",
    r"año", r"fecha", r"cap[ií]tulos?", r"novelas?",
)
# The card's emoji header and metadata markers, literal or escaped.
REMOVED_EMOJI = ("\U0001f4fa", "\u2699", "\U0001f4be", "\U0001f525")
_WORDS_RE = re.compile(r"\b(?:" + "|".join(REMOVED_WORDS) + r")\b", re.IGNORECASE)


def vocabulary_in(directory: Path) -> list[str]:
    """Every removed word or emoji found in the .py files under ``directory``."""
    found = []
    for path in sorted(directory.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in _WORDS_RE.finditer(text):
            found.append(f"{path.name}: {m.group()}")
        for emoji in REMOVED_EMOJI:
            escaped = emoji.encode("unicode_escape").decode()
            for form in (emoji, escaped, escaped[:2] + escaped[2:].upper()):
                if form in text:
                    found.append(f"{path.name}: {escaped}")
                    break
    return found


class NoVocabularyInCodeTests(unittest.TestCase):
    def test_the_parser_package_holds_no_group_wording(self) -> None:
        self.assertEqual(vocabulary_in(PARSER_DIR), [])

    def test_the_check_finds_a_word_put_back(self) -> None:
        """Positive control: the same scan over a copy with one word reinserted."""
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "parser"
            shutil.copytree(PARSER_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
            target = copy / "regex_parser.py"
            target.write_text(
                target.read_text(encoding="utf-8") + '\n_X = re.compile(r"Temporada")\n',
                encoding="utf-8",
            )
            self.assertEqual(vocabulary_in(copy), ["regex_parser.py: Temporada"])

    def test_the_check_finds_an_emoji_put_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "parser"
            shutil.copytree(PARSER_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
            target = copy / "formats.py"
            target.write_text(
                target.read_text(encoding="utf-8") + '\n_X = "\\U0001f4fa"\n', encoding="utf-8"
            )
            self.assertEqual(vocabulary_in(copy), ["formats.py: \\U0001f4fa"])


class CacheTests(unittest.TestCase):
    def test_formats_are_compiled_once_per_config(self) -> None:
        cfg = Config(formats=[FormatProfile(key="a")])
        first = formats_for(cfg)
        self.assertIs(formats_for(cfg), first)
        other = Config(formats=[FormatProfile(key="a")])
        self.assertIsNot(formats_for(other), first)
        formats_mod._cache = None


if __name__ == "__main__":
    unittest.main()
