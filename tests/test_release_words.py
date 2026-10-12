"""The words that name a language, subtitles or a whole season come from config.

`release_words` in config.yaml feeds three signals: `audio_language`,
`subtitles` and `season_complete`. Its defaults are English; an operator's own
words live in their config file and never in this repository, so the words
used here are invented stand-ins.
"""

import os
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

for name, value in {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "test",
    "TELEGRAM_PHONE": "+10000000000",
    "TELEGRAM_GROUP_ID": "-1",
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_OWNER_ID": "1",
    "TMDB_API_KEY": "test",
    "PROWLARR_URL": "http://prowlarr",
    "PROWLARR_API_KEY": "test",
    "QBITTORRENT_URL": "http://qbittorrent",
}.items():
    os.environ.setdefault(name, value)

import policy_fixture
from vettarr.checker import gather
from vettarr.config.loader import ConfigError, load, load_text
from vettarr.config.schema import Config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"
GATHER = ROOT / "src" / "vettarr" / "checker" / "gather.py"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def words(**block):
    """A `release_words` block as the loader would build it."""
    return Config.model_validate({"release_words": block}).release_words


def release(raw: str, title: str = "Example", **block) -> dict:
    item = policy_fixture.item(media_type="movie", title=title, raw_text=raw)
    return gather.release_signals(item, words(**block) if block else None)


def season(raw: str, **block) -> object:
    item = policy_fixture.item(media_type="series", title="Example", raw_text=raw)
    out = gather.series_signals(None, item, tmdb_on=False, words=words(**block) if block else None)
    return out["season_complete"]


class DefaultWordsTests(unittest.TestCase):
    def test_the_defaults_read_english(self) -> None:
        out = release("Example 2024 1080p WEB-DL x264 SPANISH ENGLISH\nSubs: French, German")
        self.assertEqual(out["audio_language"], ["es", "en"])
        self.assertEqual(out["subtitles"], ["fr", "de"])

    def test_the_defaults_know_no_other_language_s_words(self) -> None:
        """What the code used to carry: none of it is read without a config that says so."""
        out = release("Example 2024 1080p WEB-DL x264 Zorvish\nZubtitles: Zorvish")
        self.assertIn("missing", out["audio_language"])
        self.assertIn("missing", out["subtitles"])
        self.assertIn("missing", release("Example 2024 1080p WEB-DL x264 VOZE")["subtitles"])
        self.assertIn("missing", season("Example zeason entire"))

    def test_the_default_full_season_words(self) -> None:
        self.assertIs(season("Example complete"), True)
        self.assertIs(season("Example Season 2"), True)
        self.assertIs(season("Example [PACK]"), True)
        self.assertIs(season("Example S01E03 complete"), False)
        self.assertIn("missing", season("Example"))

    def test_the_example_file_writes_out_the_defaults(self) -> None:
        self.assertEqual(load(EXAMPLE, env={}).release_words, Config().release_words)

    def test_the_example_file_s_block_is_what_the_doc_tabulates(self) -> None:
        doc = (ROOT / "docs" / "CONFIG.md").read_text().split("### `release_words`")[1].split("### `notify`")[0]
        for field in type(Config().release_words).model_fields:
            with self.subTest(field=field):
                self.assertIn(f"| `{field}` |", doc)


class ConfiguredWordsTests(unittest.TestCase):
    def test_a_configured_language_word_is_read(self) -> None:
        out = release("Example 2024 1080p WEB-DL x264 Zorvish", languages={"zz": ["zorvish"], "en": ["english"]})
        self.assertEqual(out["audio_language"], ["zz"])

    def test_a_list_in_the_file_replaces_its_default(self) -> None:
        out = release("Example 2024 1080p WEB-DL x264 SPANISH", languages={"zz": ["zorvish"]})
        self.assertIn("missing", out["audio_language"])

    def test_a_language_after_a_subtitle_word_is_a_subtitle_not_the_audio(self) -> None:
        out = release(
            "Example 2024 1080p WEB-DL x264 English\nZubtitles: Zorvish",
            languages={"zz": ["zorvish"], "en": ["english"]}, subtitles=["zubtitles"],
        )
        self.assertEqual(out["audio_language"], ["en"])
        self.assertEqual(out["subtitles"], ["zz"])

    def test_a_subtitle_tag_names_its_language_alone(self) -> None:
        block = {"subtitle_tags": {"zz": ["voze", "voz"], "en": ["vozen"]}}
        self.assertEqual(release("Example 2024 1080p WEB-DL x264 VOZE", **block)["subtitles"], ["zz"])
        self.assertEqual(release("Example 2024 1080p WEB-DL x264 VOZEN", **block)["subtitles"], ["en"])
        self.assertIn("missing", release("Example 2024 1080p WEB-DL x264 VOZES", **block)["subtitles"])

    def test_no_subtitle_word_reads_no_subtitles_and_every_language_as_audio(self) -> None:
        out = release("Example 2024 1080p x264 English\nSubs: French", subtitles=[])
        self.assertEqual(out["audio_language"], ["en", "fr"])
        self.assertIn("missing", out["subtitles"])

    def test_a_configured_full_season_phrase(self) -> None:
        self.assertIs(season("Example zeason   entire", full_season=["zeason entire"]), True)
        self.assertIn("missing", season("Example complete", full_season=["zeason entire"]))
        self.assertIs(season("Example [PACK]", full_season=[]), True)

    def test_a_word_never_matches_inside_a_longer_word(self) -> None:
        self.assertIn("missing", release("Example 2024 1080p x264 Zorvishness", languages={"zz": ["zorvish"]})["audio_language"])
        self.assertIn("missing", season("Example incomplete"))

    def test_a_word_between_slashes_is_a_regular_expression(self) -> None:
        out = release("Example 2024 1080p x264 Zorvian", languages={"zz": ["/zorv(?:ish|ian)/"]})
        self.assertEqual(out["audio_language"], ["zz"])

    def test_the_decision_reads_the_config_s_words(self) -> None:
        """The wire: the snapshot a decision reads is built with the config's block."""
        cfg = policy_fixture.config().model_copy(update={
            "release_words": words(languages={"zz": ["zorvish"]}, full_season=["zeason entire"]),
        })
        item = policy_fixture.item(media_type="series", title="Example", raw_text="Example Zorvish zeason entire")
        out = gather.base_signals(item, cfg, tmdb=None, guide=None, book=None, book_looked_up=False,
                                  duplicate_of=None, twin_of=None, now=NOW)
        self.assertEqual(out["audio_language"], ["zz"])
        self.assertIs(out["season_complete"], True)


class SchemaTests(unittest.TestCase):
    def test_a_pattern_that_does_not_compile_is_an_error_on_its_own_line(self) -> None:
        text = "release_words:\n  languages:\n    en: [english]\n    zz:\n      - zorvish\n      - '/zorv(/'\n"
        with self.assertRaises(ConfigError) as caught:
            load_text(text, "config.yaml", env={})
        self.assertEqual(len(caught.exception.errors), 1)
        self.assertRegex(
            caught.exception.errors[0],
            r"^config\.yaml:6: release_words\.languages\.zz\[1\]: .*pattern does not compile",
        )

    def refused(self, block: str) -> list[str]:
        with self.assertRaises(ConfigError) as caught:
            load_text("release_words:\n" + block, "config.yaml", env={})
        return caught.exception.errors

    def test_a_word_that_captures_is_refused(self) -> None:
        """The subtitle pattern reads what follows its word through a group of
        its own. A group of the word's would be read in its place: the
        languages lost, or a TypeError for the post when the group is empty."""
        errors = self.refused("  subtitles:\n    - sub\n    - '/sub(s|titles)?/'\n")
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0], r"^config\.yaml:4: release_words\.subtitles\[1\]: .*may not capture")

    def test_a_named_group_is_refused_wherever_it_is_written(self) -> None:
        """Two words of one list with the same group name do not compile once joined."""
        errors = self.refused("  languages:\n    en: ['/(?P<value>eng)/', '/(?P<value>english)/']\n")
        self.assertEqual(len(errors), 2)
        for n, error in enumerate(errors):
            self.assertRegex(error, rf"^config\.yaml:3: release_words\.languages\.en\[{n}\]: .*may not capture")
        for block in (
            "  subtitle_tags:\n    en: ['/(?P<tag>subbed)/']\n",
            "  full_season:\n    - '/(whole) season/'\n",
        ):
            with self.subTest(block=block):
                self.assertRegex(self.refused(block)[0], "may not capture")

    def test_a_word_that_only_compiles_alone_is_refused(self) -> None:
        """A word runs inside a group, where a global flag is an error on every item."""
        errors = self.refused("  full_season:\n    - '/(?i)complete/'\n")
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0], r"^config\.yaml:3: release_words\.full_season\[0\]: .*pattern does not compile")

    def test_a_word_that_only_compiles_inside_a_group_is_refused(self) -> None:
        """A stray parenthesis would close the group the words are joined in."""
        errors = self.refused("  subtitles:\n    - '/sub)|(?:subs/'\n")
        self.assertRegex(errors[0], r"release_words\.subtitles\[0\]: .*pattern does not compile")

    def test_every_word_the_check_accepts_runs(self) -> None:
        """What `config check` lets through reads a post, and never raises on one."""
        block = {
            "languages": {"es": ["spanish"], "zz": ["/zorv(?:ish|ian)/"], "en": ["/(?i:eng)(?:lish)?/"]},
            "subtitles": ["/sub(?:s|titles)?/"],
            "subtitle_tags": {"en": [r"/subbed(?=\W|$)/"]},
            "full_season": ["/(?i:whole) season/"],
        }
        out = release("Example 2024 1080p WEB-DL x264 English\nSubs: Spanish, Zorvian", **block)
        self.assertEqual(out["audio_language"], ["en"])
        self.assertEqual(out["subtitles"], ["es", "zz"])
        self.assertEqual(release("Example 2024 1080p x264\nSub: Spanish", **block)["subtitles"], ["es"])
        self.assertIn("missing", release("Example 2024 1080p x264\nSub", **block)["subtitles"])
        self.assertEqual(release("Example 2024 1080p x264 SUBBED", **block)["subtitles"], ["en"])
        self.assertIs(season("Example Whole Season", **block), True)

    def test_the_defaults_and_the_example_pass_the_same_check(self) -> None:
        from vettarr.config.schema import ReleaseWords

        ReleaseWords.model_validate(Config().release_words.model_dump())
        ReleaseWords.model_validate(load(EXAMPLE, env={}).release_words.model_dump())

    def test_a_language_code_is_two_or_three_lowercase_letters(self) -> None:
        with self.assertRaises(ConfigError):
            load_text("release_words:\n  languages:\n    Zorvish: [zorvish]\n", "config.yaml", env={})

    def test_an_unknown_key_is_an_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_text("release_words:\n  seasons: [complete]\n", "config.yaml", env={})


class EnvironmentBootTests(unittest.TestCase):
    """The words left the code, so a boot with no file reads English only, and says so."""

    def test_the_boot_warning_says_the_release_words_are_english_only(self) -> None:
        from vettarr.config import runtime

        for phrase in (
            "the release words",
            "read with the English release words only",
            "audio_language, subtitles or season_complete",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime.ENVIRONMENT_ONLY)

    def test_the_doc_says_the_same_of_an_environment_boot(self) -> None:
        boot = (ROOT / "docs" / "CONFIG.md").read_text().split("## The rules of the file")[0]
        self.assertIn("[release words](#release_words) of any language but English", boot)
        self.assertIn("read with the English release words only", boot)

    def test_import_env_writes_the_block_and_says_what_it_lacks(self) -> None:
        from vettarr.config.import_env import render

        env = {name: os.environ[name] for name in (
            "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE", "TELEGRAM_GROUP_ID",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "TMDB_API_KEY", "PROWLARR_URL",
            "PROWLARR_API_KEY", "QBITTORRENT_URL",
        )}
        text = render(env, EXAMPLE.read_text())
        self.assertIn("\nrelease_words:\n", text)
        before = text.split("\nrelease_words:\n")[0].split("\npolicy:\n")[1]
        self.assertIn("season are not in the environment", before)
        self.assertEqual(load_text(text, "imported", env).release_words, Config().release_words)


# The wording gather.py carried before the block existed, by its roots. None of
# it may come back into the code: it belongs to an operator's config file.
REMOVED = re.compile(
    r"castellan|espa\(|espa[nñ]ol|latino|\bingl|fran\(|italiano|deutsch|portugu\(|catal\(|"
    r"subt\[|subtitulad|temporada|\\bvos",
    re.IGNORECASE,
)


class NoVocabularyInCodeTests(unittest.TestCase):
    def test_gather_holds_no_language_s_wording(self) -> None:
        self.assertEqual(REMOVED.findall(GATHER.read_text(encoding="utf-8")), [])

    def test_the_check_finds_a_word_put_back(self) -> None:
        """Positive control: the same scan over the source with one pattern reinserted."""
        text = GATHER.read_text(encoding="utf-8") + '\n_X = re.compile(r"\\btemporada\\s+completa\\b")\n'
        self.assertEqual(REMOVED.findall(text), ["temporada"])


if __name__ == "__main__":
    unittest.main()
