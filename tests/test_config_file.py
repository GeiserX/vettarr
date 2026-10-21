"""config.yaml: schema, ${VAR} expansion, line-anchored errors, writer, import-env, CLI."""

import contextlib
import io
import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from vettarr.config import cli
from vettarr.config.components import components
from vettarr.config.import_env import render, secret_names
from vettarr.config.loader import ConfigError, check, load, load_text, read_document
from vettarr.config.signals import MEDIA_TYPES, SIGNALS
from vettarr.config.writer import ConfigReadOnly, readonly, write_document, write_text

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def yaml(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def errors_of(text: str, env=None) -> list[str]:
    try:
        load_text(yaml(text), "c.yaml", env or {})
    except ConfigError as exc:
        return exc.errors
    raise AssertionError("expected ConfigError")


class ExampleTests(unittest.TestCase):
    def test_the_example_loads_with_an_empty_environment(self) -> None:
        cfg = load(EXAMPLE, env={})
        self.assertEqual(cfg.policy.auto_approve, "shadow")
        self.assertEqual([f.key for f in cfg.formats], ["card", "scene"])
        self.assertEqual(
            [r.id for r in cfg.policy.rules],
            ["violence", "nudity", "frightening", "adult-certification"],
        )
        self.assertFalse(cfg.integrations.enabled("prowlarr"))
        self.assertFalse(cfg.integrations.enabled("qbittorrent"))
        self.assertTrue(cfg.integrations.enabled("imdb_guide"))
        self.assertEqual(cfg.integrations.imdb_guide.min_votes, 20)
        names = [name for name, _ in components(cfg)]
        self.assertEqual(names, ["web", "imdb_guide", "policy"])

    def test_the_commented_provider_templates_are_valid(self) -> None:
        """The xbtit, NexusPHP and UNIT3D blocks work once uncommented."""
        lines = EXAMPLE.read_text().splitlines()
        start = lines.index("providers: []")
        block: list[str] = []
        for line in lines[start + 1:]:
            if not line.startswith("#"):
                break
            if not line.startswith("##"):
                block.append(line[1:])
        # The NexusPHP template takes auth from prowlarr, which needs prowlarr.
        text = (
            "integrations:\n  prowlarr:\n    url: http://prowlarr:9696\n    api_key: k\n"
            "providers:\n" + "\n".join(block)
        )
        providers = load_text(text, "x", {"TRACKER_C_PASSWORD": "x"}).providers
        self.assertEqual([p.key for p in providers], ["tracker-a", "tracker-b", "tracker-c"])
        self.assertEqual([p.auth.from_ for p in providers], ["cookie_file", "prowlarr", "login"])

    def test_the_example_is_what_config_example_prints(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["example"]), 0)
        self.assertEqual(out.getvalue(), EXAMPLE.read_text())

    def test_media_types_match_the_app(self) -> None:
        from vettarr.models import MediaType

        self.assertEqual(set(MEDIA_TYPES), {m.value for m in MediaType} - {"unknown"})


class ExpansionTests(unittest.TestCase):
    TEXT = """
    integrations:
      tmdb:
        api_key: ${TMDB_KEY}
      prowlarr:
        url: http://prowlarr:9696
        api_key: pre-${PROWLARR_KEY}-post
    notify:
      url: generic://host/$${LITERAL}
    """

    def test_variables_are_read_from_the_environment(self) -> None:
        cfg = load_text(yaml(self.TEXT), "c.yaml", {"TMDB_KEY": "t-123", "PROWLARR_KEY": "p-456"})
        self.assertEqual(cfg.integrations.tmdb.api_key.get_secret_value(), "t-123")
        self.assertEqual(cfg.integrations.prowlarr.api_key.get_secret_value(), "pre-p-456-post")
        self.assertEqual(cfg.notify.url.get_secret_value(), "generic://host/${LITERAL}")

    def test_a_missing_variable_names_the_variable_and_its_line(self) -> None:
        errors = errors_of(self.TEXT, {"TMDB_KEY": "t-123"})
        self.assertEqual(
            errors,
            ["c.yaml:6: integrations.prowlarr.api_key: environment variable PROWLARR_KEY is not set or is empty"],
        )

    def test_the_error_never_carries_a_value(self) -> None:
        errors = errors_of(self.TEXT, {"TMDB_KEY": "t-secret-123", "PROWLARR_KEY": ""})
        joined = "\n".join(errors)
        self.assertIn("PROWLARR_KEY", joined)
        self.assertNotIn("t-secret-123", joined)

    def test_every_missing_variable_is_reported_at_once(self) -> None:
        errors = errors_of(self.TEXT, {})
        self.assertEqual(len(errors), 2)
        self.assertIn("TMDB_KEY", errors[0])
        self.assertIn("PROWLARR_KEY", errors[1])

    def test_a_disabled_block_is_not_expanded(self) -> None:
        cfg = load_text(
            yaml("""
            integrations:
              radarr:
                enabled: false
                url: http://radarr:7878
                api_key: ${RADARR_KEY}
            """),
            "c.yaml",
            {},
        )
        self.assertFalse(cfg.integrations.enabled("radarr"))

    def test_secrets_never_show_in_a_printed_config(self) -> None:
        cfg = load_text(yaml(self.TEXT), "c.yaml", {"TMDB_KEY": "t-secret-123", "PROWLARR_KEY": "p"})
        self.assertNotIn("t-secret-123", repr(cfg))
        self.assertNotIn("t-secret-123", str(cfg.model_dump()))


class ValidationTests(unittest.TestCase):
    def test_an_unknown_signal_lists_the_known_ones(self) -> None:
        errors = errors_of("""
        policy:
          rules:
            - id: gore
              check: imdb.gore
              at_least: severe
        """)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("c.yaml:4: policy.rules[0].check: unknown signal 'imdb.gore'"))
        for known in ("imdb.violence", "certification", "keyword", "size_gb"):
            self.assertIn(known, errors[0])
        self.assertEqual(len(re.findall(r", ", errors[0].split("known signals: ")[1])) + 1, len(SIGNALS))

    def test_when_missing_takes_wait_review_or_approve(self) -> None:
        for mode in ("wait", "review", "approve"):
            with self.subTest(mode=mode):
                cfg = load_text(
                    f"policy:\n  rules:\n    - {{id: r, check: seeders, at_least: 1, when_missing: {mode}}}\n"
                    f"    - {{id: g, all: [{{check: year, at_most: 1999}}], when_missing: {mode}}}\n",
                    "c.yaml", {},
                )
                self.assertEqual([r.when_missing for r in cfg.policy.rules], [mode, mode])
        cfg = load_text("policy:\n  rules:\n    - {id: r, check: seeders, at_least: 1}\n", "c.yaml", {})
        self.assertIsNone(cfg.policy.rules[0].when_missing)
        self.assertEqual(
            errors_of("policy:\n  rules:\n    - {id: r, check: seeders, at_least: 1, when_missing: skip}\n"),
            ["c.yaml:3: policy.rules[0].when_missing: Input should be 'wait', 'review' or 'approve'"],
        )
        # It belongs to the rule, never to a condition inside it.
        self.assertEqual(
            errors_of("policy:\n  rules:\n    - {id: g, all: [{check: year, at_most: 1999, when_missing: wait}]}\n"),
            ["c.yaml:3: policy.rules[0].all[0].when_missing: Extra inputs are not permitted"],
        )

    def test_an_unknown_key_is_an_error_not_ignored(self) -> None:
        errors = errors_of("""
        web:
          port: 4850
          bnid: 127.0.0.1
        """)
        self.assertEqual(errors, ["c.yaml:3: web.bnid: Extra inputs are not permitted"])

    def test_a_missing_field_points_at_its_block(self) -> None:
        errors = errors_of("""
        sources:
          - key: films
            chat: -100
        """)
        self.assertEqual(errors, ["c.yaml:2: sources[0].kind: Field required"])

    def test_a_rule_takes_exactly_one_operator_the_signal_allows(self) -> None:
        two = errors_of("""
        policy:
          rules:
            - id: r
              check: imdb.violence
              at_least: severe
              at_most: mild
        """)
        self.assertIn("exactly one operator", two[0])
        wrong = errors_of("""
        policy:
          rules:
            - id: r
              check: imdb.violence
              any_of: [severe]
        """)
        self.assertIn("imdb.violence does not take any_of", wrong[0])
        scale = errors_of("""
        policy:
          rules:
            - id: r
              check: imdb.violence
              at_least: gory
        """)
        self.assertIn("severity is one of none, mild, moderate, severe", scale[0])

    def test_rule_values_are_checked_per_kind(self) -> None:
        cfg = load_text(
            yaml("""
            policy:
              lists:
                unwanted: [list-a-word]
              rules:
                - {id: a, check: size_gb, between: [1, 40]}
                - {id: b, check: certification, any_of: ['US:NC-17']}
                - {id: c, check: keyword, matches_any: unwanted, on_hit: reject}
                - {id: d, check: time_window, between: ['23:00', '06:00']}
                - {id: e, check: imdb.nudity, at_least: Moderate, min_votes: 10}
            """),
            "c.yaml",
            {},
        )
        rules = {r.id: r for r in cfg.policy.rules}
        self.assertEqual((rules["a"].operator, rules["a"].value), ("between", [1, 40]))
        self.assertEqual(rules["c"].value, "unwanted")
        self.assertEqual(rules["e"].value, "moderate")
        for text, expected in (
            ("{id: x, check: size_gb, between: [40, 1]}", "low <= high"),
            ("{id: x, check: certification, any_of: ['18']}", "COUNTRY:rating"),
            ("{id: x, check: keyword, matches_any: nolist}", "no list 'nolist'"),
            ("{id: x, check: time_window, between: ['25:00', '06:00']}", "HH:MM"),
            ("{id: x, check: size_gb, at_least: 4, split_vote: 0.4}", "voted signals"),
            ("{id: x, check: subject, any_of: [war], types: [movie]}", "not movie"),
        ):
            with self.subTest(text=text):
                errors = errors_of("policy:\n  rules:\n    - " + text + "\n")
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(expected, errors[0])
                self.assertIn("c.yaml:3: policy.rules[0]", errors[0])

    def test_cross_block_references_are_checked(self) -> None:
        errors = errors_of("""
        telegram:
          bot_token: t
        sources:
          - key: inbox
            kind: bot
            chat: private
            formats: [cards]
        deliver:
          movie: radarr
        """)
        self.assertEqual(
            errors,
            [
                "c.yaml:7: sources[0].formats[0]: no format 'cards'; formats: none defined",
                "c.yaml:9: deliver.movie: deliver to radarr needs integrations.radarr enabled",
            ],
        )

    def test_source_kinds_keep_their_limits(self) -> None:
        for text, expected in (
            ("- {key: a, kind: bot, chat: -100, backfill: full}", "backfill must be off"),
            ("- {key: a, kind: userbot, chat: private}", "chat: private is for bot sources"),
            ("- {key: a, kind: userbot}", "needs a chat"),
            ("- {key: a, kind: seerr, on_download: delete}", "use none"),
        ):
            with self.subTest(text=text):
                errors = errors_of("sources:\n  " + text + "\n")
                self.assertIn(expected, errors[0])

    def test_source_defaults_depend_on_the_kind(self) -> None:
        cfg = load_text(
            yaml("""
            telegram: {api_id: 1, api_hash: h, bot_token: t, reviewers: [7]}
            sources:
              - {key: group, kind: userbot, chat: '-100123'}
              - {key: inbox, kind: bot, chat: private}
            """),
            "c.yaml",
            {},
        )
        group, inbox = cfg.sources
        self.assertEqual((group.chat, group.backfill, group.allowed_senders), (-100123, "since_last", "anyone"))
        self.assertEqual((inbox.backfill, inbox.allowed_senders), ("off", "reviewers"))
        self.assertEqual((group.on_download, group.on_reject), ("none", "none"))

    def test_link_examples_must_match_their_pattern(self) -> None:
        errors = errors_of(r"""
        integrations:
          prowlarr: {url: 'http://p', api_key: k}
        providers:
          - key: tracker-a
            base_url: https://tracker-a.example
            links:
              - pattern: 'details\.php\?id=(?P<id>\d+)'
                download_url: '{base}/download.php?id={id}'
                examples:
                  - {url: 'https://tracker-a.example/details.php?id=42', id: '43'}
        """)
        self.assertEqual(len(errors), 1)
        self.assertIn("c.yaml:7: providers[0].links[0]", errors[0])
        self.assertIn("the id the pattern reads is not the example's id", errors[0])

    def test_an_enabled_integration_needs_its_fields(self) -> None:
        errors = errors_of("""
        integrations:
          radarr:
            url: http://radarr:7878
        """)
        self.assertEqual(errors, ["c.yaml:2: integrations.radarr: enabled, so it needs api_key"])

    def test_auto_approve_accepts_yaml_booleans(self) -> None:
        cfg = load_text("policy:\n  auto_approve: true\n", "c.yaml", {})
        self.assertEqual(cfg.policy.auto_approve, "on")
        errors = errors_of("policy:\n  auto_approve: maybe\n")
        self.assertIn("c.yaml:2: policy.auto_approve", errors[0])

    def test_broken_yaml_names_the_line(self) -> None:
        errors = errors_of("web:\n  port: 1\n  port: 2\n")
        self.assertTrue(errors[0].startswith("c.yaml:3: not valid YAML"), errors)

    def test_an_empty_file_starts_nothing(self) -> None:
        self.assertEqual(components(load_text("", "c.yaml", {})), [])


class CombinedRuleTests(unittest.TestCase):
    """A rule that combines checks with all, any and not: its shape, its types, and what cross_check reads."""

    def one_error(self, rule: str) -> str:
        errors = errors_of("policy:\n  rules:\n    - " + rule + "\n")
        self.assertEqual(len(errors), 1, errors)
        return errors[0]

    def test_a_rule_is_one_check_or_one_group(self) -> None:
        year = "{check: year, at_most: 1999}"
        guide = "{check: imdb.violence, at_least: severe}"
        for rule, expected in (
            ("{id: x, on_hit: review}", "c.yaml:3: policy.rules[0]: a rule is one check, or one of all, any, not"),
            (f"{{id: x, check: year, at_most: 1, all: [{year}]}}",
             "c.yaml:3: policy.rules[0]: a rule is one check, or one of all, any, not"),
            (f"{{id: x, all: [{year}], any: [{year}]}}",
             "c.yaml:3: policy.rules[0]: a rule is one check, or one of all, any, not"),
            ("{id: x, all: []}", "c.yaml:3: policy.rules[0].all: all takes a list of at least one condition"),
            (f"{{id: x, any: {year}}}", "c.yaml:3: policy.rules[0].any: any takes a list of at least one condition"),
            (f"{{id: x, not: [{year}]}}", "c.yaml:3: policy.rules[0].not: not takes one condition"),
            (f"{{id: x, all: [{year}, {{name: n}}]}}",
             "c.yaml:3: policy.rules[0].all[1].name: Extra inputs are not permitted"),
            (f"{{id: x, all: [{year}, {{any: [{{check: year, any_of: [1]}}]}}]}}",
             "c.yaml:3: policy.rules[0].all[1].any[0]: year does not take any_of; it takes: at_least, at_most, between"),
            (f"{{id: x, all: [{year}], at_most: 3}}",
             "c.yaml:3: policy.rules[0]: at_most goes with a check inside all, not beside it"),
            (f"{{id: x, all: [{guide}, {{check: subject, any_of: [a]}}]}}",
             "c.yaml:3: policy.rules[0]: the conditions of this rule share no media type"),
            (f"{{id: x, types: [movie, ebook], all: [{year}, {guide}]}}",
             "c.yaml:3: policy.rules[0]: this rule applies to movie, series, not ebook"),
            ("{id: x, types: [ebook], check: imdb.violence, at_least: severe}",
             "c.yaml:3: policy.rules[0]: imdb.violence applies to movie, series, not ebook"),
        ):
            with self.subTest(rule=rule):
                self.assertEqual(self.one_error(rule), expected)

    def test_split_vote_on_a_group_rule_is_refused(self) -> None:
        guide = "{check: imdb.violence, at_least: severe}"
        for extra in ("split_vote: 0.4", "min_votes: 10"):
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.one_error(f"{{id: x, any: [{guide}], {extra}}}"),
                    "c.yaml:3: policy.rules[0]: split_vote and min_votes go on the voted condition inside all, any "
                    "or not, not on the rule",
                )
        cfg = load_text(
            f"policy:\n  rules:\n    - {{id: x, any: [{{check: imdb.violence, at_least: severe, split_vote: 0.4}}]}}\n",
            "c.yaml", {},
        )
        self.assertEqual(cfg.policy.rules[0].leaves()[0].split_vote, 0.4)

    def test_cross_check_and_warnings_reach_a_leaf_inside_a_group(self) -> None:
        from vettarr.config.schema import warnings

        errors = errors_of("""
        telegram: {bot_token: t}
        sources:
          - {key: inbox, kind: bot, chat: private}
        policy:
          rules:
            - id: g
              all:
                - {check: size_gb, at_least: 1}
                - any:
                    - {check: keyword, matches_any: nolist}
                    - {check: source, any_of: [elsewhere]}
        """)
        self.assertEqual(errors, [
            "c.yaml:10: policy.rules[0].all[1].any[0].matches_any: no list 'nolist' under policy.lists; "
            "lists: none defined",
            "c.yaml:11: policy.rules[0].all[1].any[1].any_of: source takes the keys of configured sources "
            "(inbox); the release's origin (CAM, TS, WEB-DL, ...) is release_source",
        ])
        cfg = load_text(
            yaml("""
            policy:
              lists:
                empty: []
              rules:
                - id: g
                  all:
                    - {check: size_gb, at_least: 1}
                    - not: {check: keyword, matches_any: empty}
                    - {check: keyword, any_of: [list-a-word]}
            """),
            "c.yaml",
            {},
        )
        said = [(loc, message) for loc, message in warnings(cfg) if loc[:2] == ("policy", "rules")]
        self.assertEqual(said, [
            (("policy", "rules", 0), "rule g reads keyword but integrations.tmdb is not enabled; "
             "the rule sees missing data"),
            (("policy", "rules", 0, "all", 1, "not", "matches_any"),
             "rule g points at the empty list 'empty'; it never hits"),
        ])

    def test_the_commented_group_example_in_config_example_is_valid(self) -> None:
        lines = EXAMPLE.read_text().splitlines()
        start = next(n for n, line in enumerate(lines) if line.startswith("    # A rule can combine checks"))
        block = []
        for line in lines[start + 1:]:
            if not line.startswith("    # "):
                break
            block.append("    " + line[len("    # "):])
        self.assertTrue(block)
        cfg = load_text("policy:\n  rules:\n" + "\n".join(block) + "\n", "c.yaml", {})
        group = cfg.policy.rules[0]
        self.assertEqual((group.id, group.is_group), ("old-and-rough", True))
        self.assertEqual([leaf.check for leaf in group.leaves()], ["year", "imdb.violence", "imdb.nudity"])


SCOPE_SOURCES = """
telegram: {bot_token: t, api_id: 1, api_hash: h}
sources:
  - {key: group, kind: userbot, chat: -1}
  - {key: inbox, kind: bot, chat: private}
"""


class ScopeConfigTests(unittest.TestCase):
    """policy.scopes: ids, skip, what `when` may read, and what cross_check and warnings reach."""

    def test_scope_ids_rule_ids_and_skip_are_checked(self) -> None:
        errors = errors_of(SCOPE_SOURCES + """
        policy:
          rules:
            - {id: big, check: size_gb, at_least: 50}
          scopes:
            - id: tg
              when: {check: source, any_of: [group]}
              rules:
                - {id: own, check: size_gb, at_least: 1}
                - {id: own, check: size_gb, at_least: 2}
                - {id: big, check: size_gb, at_least: 3}
              skip: [big, nope]
            - id: tg
              when: {check: source, any_of: [inbox]}
        """.replace("\n        ", "\n"))
        self.assertEqual(errors, [
            "c.yaml:14: policy.scopes[0].rules[1].id: duplicate rule id 'own'",
            "c.yaml:15: policy.scopes[0].rules[2].id: rule id 'big' is already a base rule; a scope's rule needs "
            "its own id",
            "c.yaml:16: policy.scopes[0].skip[1]: skip names no base rule 'nope'; base rules: big",
            "c.yaml:17: policy.scopes[1].id: duplicate scope id 'tg'",
        ])

    def test_a_scope_s_when_takes_only_pre_lookup_signals(self) -> None:
        for when, expected in (
            ("{check: imdb.violence, at_least: severe}", "c.yaml:4: policy.scopes[0].when: a scope's when reads "
             "what is known before any lookup (source, sender, time_window, post_age_hours), not imdb.violence"),
            ("{all: [{check: sender, any_of: [1]}, {not: {check: size_gb, at_least: 1}}]}",
             "c.yaml:4: policy.scopes[0].when: a scope's when reads what is known before any lookup (source, "
             "sender, time_window, post_age_hours), not size_gb"),
            ("{}", "c.yaml:4: policy.scopes[0].when: a rule is one check, or one of all, any, not"),
        ):
            with self.subTest(when=when):
                self.assertEqual(errors_of(f"policy:\n  scopes:\n    - id: s\n      when: {when}\n"), [expected])
        errors = errors_of("policy:\n  scopes:\n    - {id: s}\n")
        self.assertEqual(errors, ["c.yaml:3: policy.scopes[0].when: Field required"])
        cfg = load_text(
            yaml(SCOPE_SOURCES + """
            policy:
              scopes:
                - id: late
                  when:
                    any:
                      - {check: time_window, between: ['22:00', '06:00']}
                      - {check: post_age_hours, at_least: 48}
                      - {check: sender, matches_any: [17]}
            """.replace("\n            ", "\n")),
            "c.yaml", {},
        )
        self.assertEqual([leaf.check for leaf in cfg.policy.scopes[0].when.leaves()],
                         ["time_window", "post_age_hours", "sender"])

    def test_cross_check_reaches_a_scope_s_when_and_rules(self) -> None:
        errors = errors_of(SCOPE_SOURCES + """
        policy:
          scopes:
            - id: tg
              when: {all: [{check: source, any_of: [group, seerr]}, {check: sender, any_of: kids}]}
              rules:
                - {id: own, any: [{check: keyword, matches_any: nolist}]}
        """.replace("\n        ", "\n"))
        self.assertEqual(errors, [
            "c.yaml:9: policy.scopes[0].when.all[0].any_of: source takes the keys of configured sources (group, "
            "inbox); the release's origin (CAM, TS, WEB-DL, ...) is release_source",
            "c.yaml:9: policy.scopes[0].when.all[1].any_of: no list 'kids' under policy.lists; lists: none defined",
            "c.yaml:11: policy.scopes[0].rules[0].any[0].matches_any: no list 'nolist' under policy.lists; "
            "lists: none defined",
        ])

    def test_warnings_reach_a_scope_s_rules(self) -> None:
        from vettarr.config.schema import warnings

        cfg = load_text(
            yaml(SCOPE_SOURCES + """
            policy:
              lists:
                empty: []
              rules:
                - {id: base-ok, check: size_gb, at_most: 1, on_hit: approve}
              scopes:
                - id: tg
                  when: {check: source, any_of: [group]}
                  rules:
                    - {id: tg-ok, check: size_gb, at_most: 2, on_hit: approve}
                    - {id: tg-guide, check: imdb.violence, at_least: severe}
                    - {id: tg-words, check: keyword, matches_any: empty}
            """.replace("\n            ", "\n")),
            "c.yaml", {},
        )
        said = [(loc, message) for loc, message in warnings(cfg) if loc[:2] == ("policy", "scopes")]
        self.assertEqual(said, [
            (("policy", "scopes", 0, "rules", 1), "rule tg-guide comes after the approve rule tg-ok; when that one "
             "hits first, tg-guide is never checked. Put rules that review or reject first"),
            (("policy", "scopes", 0, "rules", 2), "rule tg-words reads keyword but integrations.tmdb is not "
             "enabled; the rule sees missing data"),
            (("policy", "scopes", 0, "rules", 2, "matches_any"), "rule tg-words points at the empty list 'empty'; "
             "it never hits"),
            (("policy", "scopes", 0, "rules", 2), "rule tg-words comes after the approve rule tg-ok; when that one "
             "hits first, tg-words is never checked. Put rules that review or reject first"),
        ])
        # The policy line counts a scope's rules with the base ones.
        acting = cfg.model_copy(update={"policy": cfg.policy.model_copy(update={"auto_approve": "on"})})
        self.assertIn(("policy", "auto_approve on, 4 rules"), components(acting))

    def test_a_scope_whose_when_points_at_an_empty_list_is_warned_about(self) -> None:
        from vettarr.config.schema import warnings

        cfg = load_text(
            yaml(SCOPE_SOURCES + """
            policy:
              lists:
                kids: []
              scopes:
                - id: kids
                  when:
                    all:
                      - {check: source, any_of: [group]}
                      - {check: sender, any_of: kids}
            """.replace("\n            ", "\n")),
            "c.yaml", {},
        )
        said = [(loc, message) for loc, message in warnings(cfg) if loc[:2] == ("policy", "scopes")]
        self.assertEqual(said, [
            (("policy", "scopes", 0, "when", "all", 1, "any_of"),
             "scope kids points at the empty list 'kids'; it never applies"),
        ])

    def test_a_scope_that_leaves_a_type_with_no_rule_is_warned_about(self) -> None:
        from vettarr.config.schema import warnings

        cfg = load_text(
            yaml(SCOPE_SOURCES + """
            policy:
              auto_approve: on
              types:
                series: {mode: never}
                audiobook: {mode: never}
                ebook: {mode: never}
                magazine: {mode: never}
                comic: {mode: never}
                music: {mode: never}
              rules:
                - {id: big, check: size_gb, at_least: 50}
              scopes:
                - id: tg
                  when: {check: source, any_of: [group]}
                  types:
                    music: {mode: rules}
                  skip: [big]
            """.replace("\n            ", "\n")),
            "c.yaml", {},
        )
        said = [(loc, message) for loc, message in warnings(cfg) if "no rule applies" in message]
        self.assertEqual(said, [
            (("policy", "scopes", 0), "under the scope tg, no rule applies to movie, music, so every such item is "
             "held for a person; give the type a rule, or set its mode to always or never to say which you mean"),
        ])

    def test_the_commented_scopes_example_in_config_example_is_valid(self) -> None:
        lines = EXAMPLE.read_text().splitlines()
        start = lines.index("  # scopes:")
        block = []
        for line in lines[start:]:
            if not line.startswith("  # "):
                break
            block.append("  " + line[len("  # "):])
        cfg = load_text(
            "telegram: {bot_token: t}\nintegrations:\n  seerr: {url: 'http://seerr', api_key: k}\n"
            "sources:\n  - {key: seerr, kind: seerr}\n"
            "policy:\n  rules:\n    - {id: nudity, check: imdb.nudity, at_least: moderate}\n" + "\n".join(block) + "\n",
            "c.yaml", {},
        )
        scope = cfg.policy.scopes[0]
        self.assertEqual((scope.id, scope.skip, [r.id for r in scope.rules]), ("kids", ["nudity"], ["kids-rating"]))
        self.assertEqual(cfg.policy.type_policy("movie", scope).when_data_missing, "review")


class WriterTests(unittest.TestCase):
    ORIGINAL = yaml("""
    # The operator's note about the web block.
    web:
      port: 4850   # the port the proxy points at
    notify:
      url: ${NOTIFY_URL}
    """)
    ENV = {"NOTIFY_URL": "generic://secret-host/token-123"}

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.yaml"
        self.path.write_text(self.ORIGINAL)

    def _edit_port(self, env) -> None:
        doc = read_document(self.path)
        doc["web"]["port"] = 8080
        write_document(self.path, doc, env=env)

    def test_round_trip_keeps_comments_and_references(self) -> None:
        self._edit_port(self.ENV)
        text = self.path.read_text()
        self.assertIn("# The operator's note about the web block.", text)
        self.assertIn("# the port the proxy points at", text)
        self.assertIn("port: 8080", text)
        self.assertIn("${NOTIFY_URL}", text)
        self.assertNotIn("token-123", text)
        self.assertEqual(load(self.path, env=self.ENV).web.port, 8080)

    def test_the_old_file_is_kept_as_bak(self) -> None:
        self._edit_port(self.ENV)
        self.assertEqual((Path(str(self.path) + ".bak")).read_text(), self.ORIGINAL)

    def test_readonly_refuses_every_write(self) -> None:
        env = {**self.ENV, "CONFIG_READONLY": "1"}
        self.assertTrue(readonly(env))
        with self.assertRaises(ConfigReadOnly):
            self._edit_port(env)
        with self.assertRaises(ConfigReadOnly):
            write_text(self.path, "web: {}\n", env=env)
        self.assertEqual(self.path.read_text(), self.ORIGINAL)
        self.assertFalse(Path(str(self.path) + ".bak").exists())

    def test_readonly_reads_the_process_environment_by_default(self) -> None:
        with mock.patch.dict(os.environ, {"CONFIG_READONLY": "1"}):
            self.assertTrue(readonly())
        with mock.patch.dict(os.environ, {"CONFIG_READONLY": "0"}):
            self.assertFalse(readonly())

    def test_an_invalid_document_is_never_written(self) -> None:
        doc = read_document(self.path)
        doc["web"]["port"] = "not-a-port"
        with self.assertRaises(ConfigError):
            write_document(self.path, doc, env=self.ENV)
        self.assertEqual(self.path.read_text(), self.ORIGINAL)


FAKE_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "fake-hash-a1b2c3",
    "TELEGRAM_PHONE": "+15550001111",
    "TELEGRAM_GROUP_ID": "-1009876543210",
    "TELEGRAM_BOT_TOKEN": "999:fake-bot-token",
    "TELEGRAM_OWNER_ID": "4242",
    "TMDB_API_KEY": "fake-tmdb-key",
    "PROWLARR_URL": "http://prowlarr:9696",
    "PROWLARR_API_KEY": "fake-prowlarr-key",
    "QBITTORRENT_URL": "http://qbittorrent:8080",
    "QBITTORRENT_USER": "admin",
    "QBITTORRENT_PASS": "fake-qbit-pass",
    "TRACKER_1_NAME": "Tracker A",
    "TRACKER_1_URL": "https://tracker-a.example",
    "TRACKER_1_COOKIE_PATH": "/data/cookies/a.txt",
    "TRACKER_1_USERNAME": "someone",
    "TRACKER_1_PASSWORD": "fake-tracker-pass",
    "TRACKER_2_NAME": "Tracker B",
    "TRACKER_2_URL": "https://tracker-b.example",
    "TRACKER_2_COOKIE_PATH": "/data/cookies/b.txt",
    "GOOGLE_BOOKS_API_KEY": "fake-books-key",
    "AUTO_APPROVE_ENABLED": "true",
    "AUTO_APPROVE_TYPES": "magazine,ebook,movie,series",
    "MIN_GUIDE_VOTES": "5",
    "WEB_PORT": "4850",
    "SESSION_DIR": "/data/sessions",
}


def leaked(text: str, env) -> list[str]:
    return [name for name in secret_names(env) if env[name] in text]


class ImportEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = render(FAKE_ENV, EXAMPLE.read_text())

    def test_no_secret_value_reaches_the_file(self) -> None:
        # Positive control: the grep finds a value that is in the file, and
        # finds the secrets when they are put there on purpose.
        self.assertIn(FAKE_ENV["TELEGRAM_GROUP_ID"], self.text)
        self.assertEqual(len(secret_names(FAKE_ENV)), 9)
        planted = self.text + "\n# " + " ".join(FAKE_ENV[n] for n in secret_names(FAKE_ENV))
        self.assertEqual(leaked(planted, FAKE_ENV), secret_names(FAKE_ENV))
        self.assertEqual(leaked(self.text, FAKE_ENV), [])
        for name in secret_names(FAKE_ENV):
            self.assertIn("${" + name + "}", self.text)

    def test_the_file_loads_and_matches_the_environment(self) -> None:
        cfg = load_text(self.text, "import", FAKE_ENV)
        self.assertEqual(cfg.telegram.api_hash.get_secret_value(), FAKE_ENV["TELEGRAM_API_HASH"])
        self.assertEqual(cfg.telegram.reviewers, [4242])
        self.assertEqual(cfg.telegram.session_path, "/data/sessions/media_triage")
        (group,) = cfg.sources
        self.assertEqual((group.kind, group.chat), ("userbot", -1009876543210))
        self.assertEqual((group.on_download, group.on_reject), ("delete", "delete"))
        a, b = cfg.providers
        self.assertEqual(a.key, "tracker-a")
        self.assertEqual(a.auth.from_, "login")
        self.assertEqual(a.search, "unit3d")
        self.assertEqual(a.auth.username, FAKE_ENV["TRACKER_1_USERNAME"])
        self.assertEqual(b.key, "tracker-b")
        self.assertEqual(b.auth.from_, "cookie_file")
        # Opt-in: the environment has no variable that turns either on.
        for tracker in (a, b):
            self.assertFalse(tracker.thank.enabled)
            self.assertFalse(tracker.keep_alive.enabled)
        # UNIT3D's values are written, ready for `enabled: true`.
        self.assertEqual((a.thank.path, a.thank.method), ("/torrents/{id}", "LIVEWIRE"))
        self.assertEqual((a.keep_alive.path, a.keep_alive.every_minutes), ("/torrents", 360))
        self.assertEqual(cfg.policy.auto_approve, "on")
        self.assertEqual(cfg.policy.min_votes, 5)
        modes = {t: cfg.policy.type_policy(t).mode for t in MEDIA_TYPES}
        self.assertEqual(
            modes,
            {"movie": "rules", "series": "rules", "ebook": "rules", "magazine": "always",
             "audiobook": "never", "comic": "never", "music": "never"},
        )
        self.assertEqual(set(cfg.deliver.values()), {"qbittorrent"})
        self.assertEqual(cfg.web.port, 4850)
        self.assertEqual(
            [name for name, _ in components(cfg)][:3], ["userbot", "bot", "web"],
        )

    def test_a_tracker_with_only_a_url_is_never_contacted(self) -> None:
        """0.11.2 skipped it ("no cookie file or credentials"); nothing may reach it now either."""
        env = {k: v for k, v in FAKE_ENV.items() if not k.startswith("TRACKER_")}
        env["TRACKER_1_URL"] = "https://tracker-a.example"
        text = render(env, EXAMPLE.read_text())
        (tracker,) = load_text(text, "import", env).providers
        self.assertEqual(tracker.auth.from_, "prowlarr")
        self.assertEqual((tracker.search, tracker.links), ("none", []))
        self.assertFalse(tracker.thank.enabled)
        self.assertFalse(tracker.keep_alive.enabled)
        self.assertEqual(text.count("enabled: false"), 2)
        self.assertNotIn("enabled: true", text.split("providers:")[1].split("integrations:")[0])
        self.assertIn("Thank and keep-alive are off", text)

    def test_auto_approval_off_stays_off(self) -> None:
        env = {**FAKE_ENV, "AUTO_APPROVE_ENABLED": "false"}
        self.assertEqual(load_text(render(env, EXAMPLE.read_text()), "i", env).policy.auto_approve, "off")

    def test_a_url_with_a_password_is_a_reference(self) -> None:
        env = {**FAKE_ENV, "PROWLARR_URL": "http://user:pw-in-url@prowlarr:9696"}  # trufflehog:ignore (fake credential)
        text = render(env, EXAMPLE.read_text())
        self.assertNotIn("pw-in-url", text)
        self.assertIn("${PROWLARR_URL}", text)

    def test_the_cli_writes_through_the_readonly_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "config.yaml"
            with mock.patch.dict(os.environ, {**FAKE_ENV, "CONFIG_READONLY": "1"}, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["import-env", "--output", str(out)]), 1)
            self.assertFalse(out.exists())
            with mock.patch.dict(os.environ, FAKE_ENV, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["import-env", "--output", str(out)]), 0)
            self.assertEqual(leaked(out.read_text(), FAKE_ENV), [])


class CheckCommandTests(unittest.TestCase):
    def run_check(self, path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["check", str(path)])
        return code, out.getvalue(), err.getvalue()

    def test_check_lists_what_would_start(self) -> None:
        code, out, _ = self.run_check(EXAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("valid", out)
        self.assertRegex(out, r"web\s+0\.0\.0\.0:4850 \(no auth\)")
        self.assertIn("Warnings:", out)

    def test_check_fails_with_anchored_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("web:\n  port: 99999\n")
            code, _, err = self.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn(f"{path}:2: web.port:", err)

    def test_check_returns_warnings_separately(self) -> None:
        _, warnings = check(EXAMPLE, env={})
        self.assertTrue(any("integrations.tmdb is not enabled" in w for w in warnings))

    def test_the_doc_says_what_check_accepts_and_the_code_does_not_act_on(self) -> None:
        """`check` lists these as starting; until the code acts on them the doc says so.

        The milestone that implements one removes its line from the note and
        its name from this list.
        """
        text = textwrap.dedent("""\
            sources:
              - {key: requests, kind: seerr}
            integrations:
              qbittorrent: {url: 'http://qbittorrent'}
              radarr: {url: 'http://radarr', api_key: k}
              sonarr: {url: 'http://sonarr', api_key: k}
              seerr: {url: 'http://seerr', api_key: k, user_id: 2}
            deliver: {movie: radarr, series: sonarr, ebook: qbittorrent}
            web:
              base_url: https://vettarr.example
              auth: {trusted_header: Remote-User}
            """)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text)
            code, out, _ = self.run_check(path)
        self.assertEqual(code, 0)
        for name in ("seerr-source", "radarr", "sonarr", "seerr", "delivery"):
            self.assertRegex(out, rf"(?m)^\s+{name}\s")
        doc = (EXAMPLE.parent / "docs" / "CONFIG.md").read_text()
        note = doc.split("## Not enforced yet")[1]
        # Delivery is acted on now (tests/test_delivery.py), and so are seerr
        # sources (tests/test_seerr_source.py), the login (web.auth) and the
        # bot's link to the item's page (web.base_url), so their lines are gone.
        for name in (
            "`deliver`", "`integrations.radarr`", "`integrations.sonarr`",
            "`sources` of kind `seerr`", "`integrations.seerr`", "`web.auth`", "`web.base_url`",
        ):
            with self.subTest(gone=name):
                self.assertNotIn(name, note)

    def test_the_app_entry_point_dispatches_config(self) -> None:
        app_env = {
            "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "test", "TELEGRAM_PHONE": "+10000000000",
            "TELEGRAM_GROUP_ID": "-1", "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_OWNER_ID": "1",
            "TMDB_API_KEY": "test", "PROWLARR_URL": "http://prowlarr", "PROWLARR_API_KEY": "test",
            "QBITTORRENT_URL": "http://qbittorrent",
        }
        before = dict(os.environ)
        out = io.StringIO()
        # patch.dict restores the environment afterwards, so nothing leaks into later tests.
        with mock.patch.dict(os.environ, {**app_env, **os.environ}):
            from vettarr.__main__ import main

            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
                main(["config", "check", str(EXAMPLE)])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn("Would start:", out.getvalue())
        for name in app_env:
            self.assertEqual(os.environ.get(name), before.get(name), name)


class ReviewRegressionTests(unittest.TestCase):
    """One test per finding of the schema review, so each fix stays fixed."""

    def test_the_plan_prefixed_signal_names_load(self) -> None:
        cfg = load_text(
            yaml("""
            policy:
              lists:
                list-a: [list-a-word, list-a-phrase, list-a-other]
              rules:
                - {id: list-a-words, check: tmdb.keyword, matches_any: list-a}
                - {id: genre-27, check: tmdb.genre, any_of: [27]}
                - {id: subject-b, check: book.subject, any_of: [list-b-word]}
            """),
            "c.yaml",
            {},
        )
        self.assertEqual([r.check for r in cfg.policy.rules], ["keyword", "genre", "subject"])
        # A prefix only names its own group.
        errors = errors_of("policy:\n  rules:\n    - {id: x, check: tmdb.subject, any_of: [list-b-word]}\n")
        self.assertIn("unknown signal 'tmdb.subject'", errors[0])

    def test_a_source_rule_takes_configured_source_keys(self) -> None:
        errors = errors_of("""
        policy:
          rules:
            - {id: no-cams, check: source, none_of: [CAM, TS, TC, SCR], on_hit: reject}
        """)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("c.yaml:3: policy.rules[0].none_of: source takes the keys of configured sources", errors[0])
        self.assertIn("release_source", errors[0])
        listed = errors_of("""
        telegram: {bot_token: t}
        sources:
          - {key: inbox, kind: bot, chat: private}
        policy:
          lists:
            places: [inbox, typo]
          rules:
            - {id: r, check: source, any_of: places}
        """)
        self.assertEqual(len(listed), 1, listed)
        self.assertIn("(inbox)", listed[0])
        cfg = load_text(
            yaml("""
            telegram: {bot_token: t}
            sources:
              - {key: inbox, kind: bot, chat: private}
            policy:
              rules:
                - {id: r, check: source, any_of: [inbox]}
                - {id: no-cams, check: release_source, none_of: [CAM, TS]}
            """),
            "c.yaml",
            {},
        )
        self.assertEqual(len(cfg.policy.rules), 2)

    def test_a_disabled_block_keeps_placeholders_in_any_field(self) -> None:
        cfg = load_text(
            yaml("""
            integrations:
              radarr:
                enabled: false
                url: ${RADARR_URL}
                api_key: ${RADARR_API_KEY}
              seerr:
                enabled: false
                url: ${SEERR_URL}
                api_key: ${SEERR_KEY}
                user_id: ${SEERR_USER}
              imdb_guide:
                enabled: false
                min_votes: ${MIN_VOTES}
            """),
            "c.yaml",
            {},
        )
        self.assertFalse(cfg.integrations.enabled("radarr"))
        self.assertFalse(cfg.integrations.enabled("seerr"))
        self.assertFalse(cfg.integrations.enabled("imdb_guide"))
        # A literal value in a disabled block is still checked.
        errors = errors_of("integrations:\n  radarr:\n    enabled: false\n    url: not-a-url\n")
        self.assertIn("c.yaml:4: integrations.radarr.url", errors[0])

    def test_enabled_may_itself_be_a_variable(self) -> None:
        text = "integrations:\n  tmdb:\n    enabled: ${TMDB_ON}\n    api_key: ${TMDB_KEY}\n"
        cfg = load_text(text, "c.yaml", {"TMDB_ON": "false"})
        self.assertFalse(cfg.integrations.enabled("tmdb"))
        errors = errors_of(text, {"TMDB_ON": "true"})
        self.assertEqual(errors, ["c.yaml:4: integrations.tmdb.api_key: environment variable TMDB_KEY is not set or is empty"])
        self.assertTrue(load_text(text, "c.yaml", {"TMDB_ON": "true", "TMDB_KEY": "k"}).integrations.enabled("tmdb"))

    def test_direct_search_with_auth_from_prowlarr_is_no_warning(self) -> None:
        """The direct search uses the cookie Prowlarr holds, so nothing to warn about."""
        from vettarr.config.schema import warnings

        cfg = load_text(
            yaml("""
            integrations:
              prowlarr: {url: 'http://p', api_key: k}
            providers:
              - key: tracker-a
                base_url: https://tracker-a.example
                auth: {from: prowlarr}
                search: unit3d
            """),
            "c.yaml",
            {},
        )
        self.assertEqual(cfg.providers[0].search, "unit3d")
        self.assertEqual(warnings(cfg), [])

    def test_login_without_a_software_is_a_warning(self) -> None:
        from vettarr.config.schema import warnings

        cfg = load_text(
            yaml("""
            providers:
              - key: tracker-a
                base_url: https://tracker-a.example
                auth: {from: login, username: me, password: pw}
            """),
            "c.yaml",
            {},
        )
        self.assertTrue(any(loc == ("providers", 0, "auth") for loc, _ in warnings(cfg)))

    def test_an_empty_secret_does_not_count_as_set(self) -> None:
        errors = errors_of("integrations:\n  tmdb:\n    api_key: ''\n")
        self.assertEqual(errors, ["c.yaml:2: integrations.tmdb: enabled, so it needs api_key"])

    def test_validation_messages_never_echo_a_value(self) -> None:
        env = {"TOKEN": "tok-SUPERSECRET"}
        for text in (
            "policy:\n  rules:\n    - {id: r, check: imdb.violence, at_least: '${TOKEN}'}\n",
            "policy:\n  rules:\n    - {id: r, check: certification, at_least: '${TOKEN}'}\n",
            "policy:\n  rules:\n    - {id: r, check: size_gb, at_least: '${TOKEN}'}\n",
            "policy:\n  rules:\n    - {id: r, check: resolution, at_least: '${TOKEN}'}\n",
            "policy:\n  rules:\n    - {id: r, check: resolution, any_of: ['${TOKEN}']}\n",
            "policy:\n  rules:\n    - {id: r, check: time_window, between: ['${TOKEN}', '06:00']}\n",
            "integrations:\n  prowlarr: {url: 'http://p', api_key: k}\n"
            "providers:\n  - key: t\n    base_url: https://t.example\n    links:\n"
            "      - pattern: 'id=(?P<id>\\d+)'\n        download_url: '{base}/d?id={id}'\n"
            "        examples: [{url: 'https://t.example/${TOKEN}', id: '1'}]\n",
            "integrations:\n  prowlarr: {url: 'http://p', api_key: k}\n"
            "providers:\n  - key: t\n    base_url: https://t.example\n    links:\n"
            "      - pattern: 'id=(?P<id>\\w+)'\n        download_url: '{base}/d?id={id}'\n"
            "        examples: [{url: 'https://t.example/?id=${TOKEN}', id: '1'}]\n",
        ):
            with self.subTest(text=text):
                errors = errors_of(text, env)
                self.assertTrue(errors)
                self.assertNotIn("SUPERSECRET", "\n".join(errors))

    def test_a_url_with_a_query_or_fragment_is_a_reference(self) -> None:
        for value in ("http://prowlarr:9696?apikey=url-SECRET", "http://prowlarr:9696/#url-SECRET"):
            with self.subTest(value=value):
                env = {**FAKE_ENV, "PROWLARR_URL": value}
                text = render(env, EXAMPLE.read_text())
                self.assertNotIn("url-SECRET", text)
                self.assertIn("url: ${PROWLARR_URL}", text)

    def test_union_member_tags_are_not_shown_as_fields(self) -> None:
        errors = errors_of("""
        telegram: {bot_token: t}
        sources:
          - {key: a, kind: bot, chat: [1]}
        """)
        self.assertTrue(errors)
        for error in errors:
            self.assertTrue(error.startswith("c.yaml:3: sources[0].chat: "), error)


if __name__ == "__main__":
    unittest.main()
