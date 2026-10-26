"""The policy engine: every signal and operator, the decision order, shadow
mode, structured reasons, the legacy mapping, preview impact, events, the
notifier and the Jellyfin client.

Pure where the code is pure; the database, qBittorrent, Prowlarr, Jellyfin
and the webhook receiver are faked or local.
"""

import asyncio
import contextlib
import http.server
import json
import logging
import os
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

logging.disable(logging.CRITICAL)

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

import httpx

import policy_fixture
from vettarr import events, notify, pipeline
from vettarr.checker import gather, imdb_client
from vettarr.checker.book_client import BookVerdict
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.jellyfin_client import JellyfinClient
from vettarr.checker.policy import (
    APPROVE,
    IDENTITY,
    LEGACY,
    MISSING,
    REJECT,
    REVIEW,
    RULE,
    SCOPE,
    UNAVAILABLE,
    WAIT,
    Reason,
    act,
    applicable_rules,
    applying_scope,
    check_condition,
    check_rule,
    compare,
    evaluate,
    is_settings_review,
    legacy_reasons,
    needed_signals,
    policy_version,
    type_policy,
    worth_rerunning,
)
from vettarr.checker.tmdb_client import MATCH_WITH_YEAR, TMDBResult
from vettarr.config.schema import Config, Policy, Rule, Scope, TypePolicy
from vettarr.config.signals import SIGNALS
from vettarr.models import (
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
    SeverityRating,
    TorrentSearch,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def rule(**fields) -> Rule:
    return Rule.model_validate({"id": "r", **fields})


def hit(rule_fields: dict, value, lists=None, min_votes=0):
    return check_rule(rule(**rule_fields), value, lists or {}, min_votes)[0]


def policy(*rules, lists=None, types=None, **fields) -> Policy:
    return Policy.model_validate({
        "auto_approve": "on", "rules": list(rules), "lists": lists or {}, "types": types or {}, **fields,
    })


def movie(**fields):
    return policy_fixture.item(created_at=NOW, **fields)


# --- operators, one signal kind at a time -----------------------------------


class NumberOperatorTests(unittest.TestCase):
    def test_at_least_at_most_between(self) -> None:
        self.assertTrue(hit({"check": "size_gb", "at_least": 10}, 12.5))
        self.assertFalse(hit({"check": "size_gb", "at_least": 10}, 9.9))
        self.assertTrue(hit({"check": "seeders", "at_most": 2}, 2))
        self.assertFalse(hit({"check": "seeders", "at_most": 2}, 3))
        self.assertTrue(hit({"check": "runtime_minutes", "between": [80, 120]}, 80))
        self.assertTrue(hit({"check": "runtime_minutes", "between": [80, 120]}, 120))
        self.assertFalse(hit({"check": "runtime_minutes", "between": [80, 120]}, 121))

    def test_no_number_is_missing_data(self) -> None:
        for value in (None, {"missing": "x"}, "12", True):
            with self.subTest(value=value):
                self.assertIsNone(hit({"check": "year", "at_least": 2000}, value))

    def test_every_number_signal_takes_its_operators(self) -> None:
        for name, signal in SIGNALS.items():
            if signal.kind != "number":
                continue
            for op in signal.operators:
                value = [1, 5] if op == "between" else 3
                with self.subTest(signal=name, op=op):
                    self.assertIsNotNone(hit({"check": name, op: value}, 3))


class BoolOperatorTests(unittest.TestCase):
    def test_is(self) -> None:
        for name, signal in SIGNALS.items():
            if signal.kind != "bool":
                continue
            with self.subTest(signal=name):
                self.assertTrue(hit({"check": name, "is": True}, True))
                self.assertFalse(hit({"check": name, "is": True}, False))
                self.assertTrue(hit({"check": name, "is": False}, False))
                self.assertIsNone(hit({"check": name, "is": True}, {"missing": "x"}))


class TermsOperatorTests(unittest.TestCase):
    def test_any_of_is_case_and_accent_blind(self) -> None:
        self.assertTrue(hit({"check": "audio_language", "any_of": ["ES"]}, ["es", "en"]))
        self.assertTrue(hit({"check": "studio", "any_of": ["Estudio Peñón"]}, ["estudio penon"]))
        self.assertFalse(hit({"check": "codec", "any_of": ["av1"]}, ["h265"]))

    def test_none_of(self) -> None:
        self.assertTrue(hit({"check": "original_language", "none_of": ["es", "en"]}, ["fr"]))
        self.assertFalse(hit({"check": "original_language", "none_of": ["es", "en"]}, ["en"]))
        self.assertIsNone(hit({"check": "original_language", "none_of": ["es"]}, []))

    def test_matches_any_takes_whole_words(self) -> None:
        listed = {"check": "keyword", "matches_any": ["bee"]}
        self.assertTrue(hit(listed, ["bee biology"]))
        self.assertFalse(hit(listed, ["beekeeping"]))
        self.assertTrue(hit({"check": "cast", "matches_any": ["Ana Ejemplo"]}, ["Ana Ejemplo", "B"]))

    def test_a_named_list_is_read_from_policy_lists(self) -> None:
        self.assertTrue(hit({"check": "keyword", "matches_any": "words"}, ["a submarine film"],
                            lists={"words": ["submarine"]}))

    def test_a_partial_list_flags_but_never_clears(self) -> None:
        listed = {"check": "subject", "matches_any": ["knitting"]}
        self.assertTrue(hit(listed, {"partial": ["Knitting"]}))
        self.assertIsNone(hit(listed, {"partial": ["Fiction"]}))
        self.assertFalse(hit(listed, ["Fiction", "Adventure", "Essays"]))

    def test_genre_by_id_or_name(self) -> None:
        self.assertTrue(hit({"check": "genre", "any_of": [10402]}, [18, 10402, "Drama", "Música"]))
        self.assertTrue(hit({"check": "genre", "any_of": ["musica"]}, [18, 10402, "Drama", "Música"]))


class CertificationOperatorTests(unittest.TestCase):
    CERTS = {"US": "R", "ES": "16", "GB": "15"}

    def test_any_of(self) -> None:
        self.assertTrue(hit({"check": "certification", "any_of": ["ES:16"]}, self.CERTS))
        self.assertFalse(hit({"check": "certification", "any_of": ["ES:18"]}, self.CERTS))
        self.assertFalse(hit({"check": "certification", "any_of": ["DE:18"]}, self.CERTS))

    def test_none_of(self) -> None:
        self.assertTrue(hit({"check": "certification", "none_of": ["ES:7", "ES:12"]}, self.CERTS))
        self.assertFalse(hit({"check": "certification", "none_of": ["ES:16"]}, self.CERTS))
        self.assertIsNone(hit({"check": "certification", "none_of": ["DE:12"]}, self.CERTS))

    def test_at_least_orders_each_country_s_scale(self) -> None:
        self.assertTrue(hit({"check": "certification", "at_least": "US:PG-13"}, self.CERTS))
        self.assertFalse(hit({"check": "certification", "at_least": "US:NC-17"}, self.CERTS))
        self.assertTrue(hit({"check": "certification", "at_least": "ES:16"}, self.CERTS))
        self.assertFalse(hit({"check": "certification", "at_least": "DE:16"}, self.CERTS))

    def test_a_rating_off_the_scale_is_missing(self) -> None:
        self.assertIsNone(hit({"check": "certification", "at_least": "US:R"}, {"US": "Unrated"}))


class ResolutionAndTimeOperatorTests(unittest.TestCase):
    def test_resolution(self) -> None:
        self.assertTrue(hit({"check": "resolution", "at_least": "1080p"}, "2160p"))
        self.assertFalse(hit({"check": "resolution", "at_most": "720p"}, "1080p"))
        self.assertTrue(hit({"check": "resolution", "any_of": ["720p"]}, "720p"))
        self.assertTrue(hit({"check": "resolution", "none_of": ["2160p"]}, "1080p"))
        self.assertIsNone(hit({"check": "resolution", "at_least": "720p"}, {"missing": "x"}))

    def test_a_time_window_may_cross_midnight(self) -> None:
        night = {"check": "time_window", "between": ["23:00", "06:00"]}
        self.assertTrue(hit(night, "23:30"))
        self.assertTrue(hit(night, "05:59"))
        self.assertFalse(hit(night, "12:00"))
        self.assertTrue(hit({"check": "time_window", "between": ["09:00", "17:00"]}, "09:00"))


class SeverityOperatorTests(unittest.TestCase):
    def test_at_most_with_a_split_vote(self) -> None:
        value = {"severity": "moderate", "votes": 100,
                 "breakdown": {"none": 30, "mild": 20, "moderate": 50, "severe": 0}}
        self.assertFalse(hit({"check": "imdb.profanity", "at_most": "mild"}, value))
        self.assertTrue(hit({"check": "imdb.profanity", "at_most": "mild", "split_vote": 0.5}, value))

    def test_min_votes_counts_as_missing(self) -> None:
        value = {"severity": "severe", "votes": 8, "breakdown": {}}
        self.assertIsNone(hit({"check": "imdb.alcohol", "at_least": "mild"}, value, min_votes=20))
        self.assertTrue(hit({"check": "imdb.alcohol", "at_least": "mild"}, value, min_votes=5))

    def test_a_rule_s_own_min_votes_wins(self) -> None:
        guide = {"imdb.violence": {"severity": "severe", "votes": 8, "breakdown": {}}}
        strict = policy({"id": "v", "check": "imdb.violence", "at_least": "severe", "min_votes": 5},
                        min_votes=50)
        self.assertEqual(evaluate(movie(), guide, strict).outcome, REVIEW)
        loose = policy({"id": "v", "check": "imdb.violence", "at_least": "severe"}, min_votes=50)
        self.assertEqual(evaluate(movie(), guide, loose).outcome, WAIT)

    def test_the_imdb_guide_default_applies_last(self) -> None:
        guide = {"imdb.violence": {"severity": "severe", "votes": 8, "breakdown": {}}}
        bare = policy({"id": "v", "check": "imdb.violence", "at_least": "severe"})
        self.assertEqual(evaluate(movie(), guide, bare, default_min_votes=20).outcome, WAIT)
        self.assertEqual(evaluate(movie(), guide, bare, default_min_votes=5).outcome, REVIEW)


# --- the decision ----------------------------------------------------------


class DecisionOrderTests(unittest.TestCase):
    def test_an_approve_rule_ends_the_walk(self) -> None:
        p = policy(
            {"id": "trusted", "check": "release_group", "any_of": ["GOOD"], "on_hit": "approve"},
            {"id": "big", "check": "size_gb", "at_least": 50},
        )
        decision = evaluate(movie(), {"release_group": ["GOOD"], "size_gb": 80}, p)
        self.assertEqual(decision.outcome, APPROVE)
        self.assertEqual(decision.reasons[0].rule, "trusted")

    def test_an_earlier_block_beats_a_later_approve(self) -> None:
        p = policy(
            {"id": "big", "check": "size_gb", "at_least": 50},
            {"id": "trusted", "check": "release_group", "any_of": ["GOOD"], "on_hit": "approve"},
        )
        self.assertEqual(evaluate(movie(), {"release_group": ["GOOD"], "size_gb": 80}, p).outcome, REVIEW)

    def test_an_approve_rule_does_not_skip_missing_data_before_it(self) -> None:
        p = policy(
            {"id": "big", "check": "size_gb", "at_least": 50},
            {"id": "trusted", "check": "release_group", "any_of": ["GOOD"], "on_hit": "approve"},
        )
        self.assertEqual(evaluate(movie(), {"release_group": ["GOOD"]}, p).outcome, WAIT)

    def test_notify_records_and_changes_nothing(self) -> None:
        p = policy({"id": "fyi", "check": "seeders", "at_most": 1, "on_hit": "notify"})
        decision = evaluate(movie(), {"seeders": 0}, p)
        self.assertEqual(decision.outcome, APPROVE)
        self.assertIn("notify", [r.kind for r in decision.reasons])

    def test_when_data_missing_review_and_approve(self) -> None:
        rules = [{"id": "s", "check": "seeders", "at_least": 1}]
        for wdm, outcome in (("review", REVIEW), ("approve", APPROVE), ("wait", WAIT)):
            with self.subTest(when_data_missing=wdm):
                p = policy(*rules, types={"movie": {"mode": "rules", "when_data_missing": wdm}})
                self.assertEqual(evaluate(movie(), {}, p).outcome, outcome)

    def test_an_integration_that_is_off_is_unavailable_and_the_type_decides(self) -> None:
        p = policy({"id": "lib", "check": "in_library", "is": True},
                   types={"movie": {"mode": "rules", "when_data_missing": "review"}})
        decision = evaluate(movie(), {"in_library": {"unavailable": "jellyfin"}}, p)
        self.assertEqual(decision.outcome, REVIEW)
        self.assertEqual(decision.reasons[0].kind, "unavailable")
        self.assertIn("integrations.jellyfin is not enabled", decision.reasons[0].text)

    def test_type_modes(self) -> None:
        p = policy({"id": "s", "check": "seeders", "at_least": 1},
                   types={"movie": {"mode": "always"}, "series": {"mode": "never"}})
        self.assertEqual(evaluate(movie(), {}, p).outcome, APPROVE)
        self.assertEqual(evaluate(movie(media_type="series"), {}, p).outcome, REVIEW)

    def test_a_rule_for_another_type_is_not_read(self) -> None:
        p = policy({"id": "b", "check": "pages", "at_least": 1000},
                   {"id": "s", "check": "size_gb", "at_least": 50})
        self.assertEqual(needed_signals(p, "movie"), {"size_gb"})
        self.assertEqual(needed_signals(p, "ebook"), {"pages", "size_gb"})
        self.assertEqual(evaluate(movie(), {"size_gb": 4}, p).outcome, APPROVE)

    def test_needed_signals_follow_the_rules(self) -> None:
        cfg = policy_fixture.config()
        self.assertEqual(
            needed_signals(cfg.policy, "movie"),
            {"genre", "keyword", "certification", "imdb.nudity", "imdb.violence", "imdb.frightening"},
        )
        self.assertEqual(needed_signals(cfg.policy, "magazine"), set())


BIG = {"check": "size_gb", "at_least": 10}
OLD = {"check": "year", "at_most": 1999}
FEW_SEEDERS = {"check": "seeders", "at_most": 2}


def combined(signals, min_votes=0, **fields):
    return check_condition(rule(**fields), signals, {}, min_votes)


class CombinedConditionTests(unittest.TestCase):
    """A rule that combines checks with all, any and not, over True, False and missing."""

    def test_all_is_false_when_any_child_is_false_even_with_a_missing_one(self) -> None:
        # The year is missing and the size is not big: no year can make it true.
        self.assertEqual(combined({"size_gb": 4}, all=[OLD, BIG]), (False, ""))
        self.assertEqual(combined({"size_gb": 4}, all=[BIG, OLD]), (False, ""))

    def test_all_is_missing_when_no_child_is_false_and_one_is_missing(self) -> None:
        self.assertEqual(combined({"size_gb": 12}, all=[BIG, OLD]), (None, "year: no data"))
        self.assertEqual(combined({"size_gb": 12, "year": 1990}, all=[BIG, OLD]), (True, "size_gb: 12; year: 1990"))

    def test_any_is_true_when_one_child_hits_even_with_a_missing_one(self) -> None:
        self.assertEqual(combined({"size_gb": 12}, any=[OLD, BIG]), (True, "size_gb: 12"))
        self.assertEqual(combined({"size_gb": 12}, any=[BIG, OLD]), (True, "size_gb: 12"))

    def test_any_is_false_only_when_every_child_is_false(self) -> None:
        self.assertEqual(combined({"size_gb": 4, "year": 2005}, any=[OLD, BIG]), (False, ""))
        self.assertEqual(combined({"size_gb": 4}, any=[OLD, BIG]), (None, "year: no data"))

    def test_not_flips_true_and_false_and_keeps_missing(self) -> None:
        self.assertEqual(combined({"size_gb": 4}, **{"not": BIG}), (True, "size_gb: 4"))
        self.assertEqual(combined({"size_gb": 12}, **{"not": BIG}), (False, ""))
        self.assertEqual(combined({}, **{"not": BIG}), (None, "size_gb: no data"))
        # A check that is false with nothing to say, and a group, are named.
        english = {"check": "original_language", "any_of": ["en"]}
        self.assertEqual(combined({"original_language": ["fr"]}, **{"not": english}), (True, "not original_language"))
        self.assertEqual(
            combined({"size_gb": 4, "year": 1990}, **{"not": {"all": [BIG, OLD]}}), (True, "not all")
        )

    def test_nested_groups_evaluate_depth_first_with_a_leaf_min_votes_and_split_vote(self) -> None:
        fields = {"all": [OLD, {"any": [
            {"check": "imdb.violence", "at_least": "severe", "min_votes": 5},
            {"check": "imdb.nudity", "at_least": "moderate", "split_vote": 0.4},
        ]}]}
        self.assertEqual([leaf.check for leaf in rule(**fields).leaves()], ["year", "imdb.violence", "imdb.nudity"])
        thin_violence = {"severity": "severe", "votes": 8, "breakdown": {}}
        split_nudity = {"severity": "mild", "votes": 90, "breakdown": {"mild": 50, "moderate": 40}}
        # The violence check's own min_votes (5) counts its 8 votes; the default (20) does not.
        self.assertEqual(
            combined({"year": 1990, "imdb.violence": thin_violence}, min_votes=20, **fields),
            (True, "year: 1990; imdb.violence: severe"),
        )
        # The nudity check's split_vote hits on a mild label with 44% of the votes moderate.
        self.assertEqual(
            combined({"year": 1990, "imdb.violence": {**thin_violence, "votes": 2}, "imdb.nudity": split_nudity},
                     min_votes=20, **fields),
            (True, "year: 1990; imdb.nudity: labelled mild, but 44% of the votes are moderate or worse"),
        )
        self.assertEqual(
            combined({"year": 2005, "imdb.violence": thin_violence}, min_votes=20, **fields), (False, "")
        )

    def test_a_group_hit_names_the_children_that_hit_and_a_missing_group_names_the_missing_child(self) -> None:
        hits = policy({"id": "any", "any": [BIG, OLD, FEW_SEEDERS]})
        decision = evaluate(movie(), {"size_gb": 12, "year": 2005, "seeders": 1}, hits)
        self.assertEqual((decision.outcome, decision.reasons), (REVIEW, (Reason(RULE, "any", "size_gb: 12; seeders: 1"),)))
        guide = {"check": "imdb.violence", "at_least": "severe"}
        waits = policy({"id": "all", "all": [BIG, guide]})
        decision = evaluate(movie(), {"size_gb": 12, "imdb.violence": {"missing": "parents guide not fetched"}}, waits)
        self.assertEqual(
            (decision.outcome, decision.reasons),
            (WAIT, (Reason(MISSING, "all", "imdb.violence: parents guide not fetched"),)),
        )
        decision = evaluate(movie(), {"size_gb": 12, "imdb.violence": {"unavailable": "imdb_guide"}}, waits)
        self.assertEqual(decision.reasons[0].kind, UNAVAILABLE)

    def test_a_group_rule_applies_to_the_types_its_conditions_share(self) -> None:
        guide = {"check": "imdb.violence", "at_least": "severe"}
        p = policy({"id": "g", "all": [BIG, guide]})
        self.assertEqual(p.rules[0].media_types, ("movie", "series"))
        self.assertEqual(applicable_rules(p, "ebook"), [])
        self.assertEqual(evaluate(movie(media_type="ebook"), {"size_gb": 12}, p).reasons[0].kind, "type")
        only = policy({"id": "g", "types": ["series"], "all": [BIG, guide]})
        self.assertEqual(only.rules[0].media_types, ("series",))
        self.assertEqual(applicable_rules(only, "movie"), [])

    def test_needed_signals_and_the_identity_guard_read_every_leaf(self) -> None:
        p = policy({"id": "g", "all": [BIG, {"any": [FEW_SEEDERS, {"check": "imdb.violence", "at_least": "severe"}]}]})
        self.assertEqual(needed_signals(p, "movie"), {"size_gb", "seeders", "imdb.violence"})
        # The guide is read by the last leaf only, and that is enough to hold a wrong match.
        wrong = {"ok": False, "text": "TMDB matched another year"}
        decision = evaluate(movie(), {"size_gb": 4, "_identity": wrong}, p)
        self.assertEqual((decision.outcome, decision.reasons[0].kind), (REVIEW, IDENTITY))

    def test_a_leaf_rule_decides_exactly_as_before(self) -> None:
        cfg = policy_fixture.config()
        severe = {"severity": "severe", "votes": 8, "breakdown": {"severe": 8}}
        mild = {"severity": "mild", "votes": 100, "breakdown": {"mild": 50, "severe": 50}}
        snapshots = [
            {},
            {"genre": [policy_fixture.FLAGGED_GENRE_ID], "keyword": ["old lighthouse keeper"]},
            {"certification": {"US": "R", "ES": "18"}, "imdb.violence": severe, "imdb.nudity": mild},
            {"imdb.frightening": {"missing": "parents guide not fetched"}, "subject": {"partial": ["Beekeeping"]}},
            {"imdb.violence": {"unavailable": "imdb_guide"}, "keyword": [], "subject": ["knitting"]},
        ]
        for default in (0, 5, 20):
            for n, signals in enumerate(snapshots):
                for leaf in cfg.policy.rules:
                    with self.subTest(default=default, snapshot=n, rule=leaf.id):
                        min_votes = leaf.min_votes if leaf.min_votes is not None else default
                        before = check_rule(leaf, signals.get(leaf.check), cfg.policy.lists, min_votes)
                        self.assertEqual(check_condition(leaf, signals, cfg.policy.lists, default), before)


TELEGRAM = {"check": "source", "any_of": ["group", "inbox"]}
KIDS = {"all": [{"check": "source", "any_of": ["seerr"]}, {"check": "sender", "any_of": [17, 23]}]}
GUIDE = {"check": "imdb.violence", "at_least": "severe"}


def scoped(*scopes, rules=(), types=None) -> Policy:
    return policy(*rules, types=types, scopes=list(scopes))


def under(scope_id: str) -> Reason:
    return Reason(SCOPE, scope_id, f"decided under the scope {scope_id}")


# What the fixture policy decided before scopes existed, for the snapshots in
# `test_a_policy_without_scopes_decides_as_before`, recorded from main at
# a57dd1a: (media type, snapshot, hours since the item arrived) -> decision.
_SEVERE = {"severity": "severe", "votes": 8, "breakdown": {"severe": 8}}
_NONE = {"severity": "none", "votes": 50, "breakdown": {"none": 50}}
_SPLIT = {"severity": "mild", "votes": 100, "breakdown": {"mild": 55, "moderate": 45}}
_CLEAR_GUIDE = {"imdb.violence": _NONE, "imdb.nudity": _NONE, "imdb.frightening": _NONE}
_NO_DATA = [
    (MISSING, "flagged-genre", "genre: no data"), (MISSING, "flagged-keywords", "keyword: no data"),
    (MISSING, "adult-certification", "certification: no data"), (MISSING, "nudity", "imdb.nudity: no data"),
    (MISSING, "violence", "imdb.violence: no data"), (MISSING, "frightening", "imdb.frightening: no data"),
]
_THIN = "subject: only 1 known, too few to clear it"
BEFORE_SCOPES = [
    ("movie", {}, 0, (WAIT, _NO_DATA)),
    ("movie", {}, 30, (REVIEW, [("expired", None, "no data after 24 hours"), *_NO_DATA])),
    ("movie", {"_duplicate": "duplicate of 7"}, 0, (REVIEW, [("duplicate", None, "duplicate of 7")])),
    ("movie", {"_no_title": True}, 0, (REVIEW, [(IDENTITY, None, "the post carries no title")])),
    ("movie", {"_identity": {"ok": False, "text": "TMDB matched another year"}}, 0,
     (REVIEW, [(IDENTITY, None, "could not confirm this is the same title"),
               (IDENTITY, None, "TMDB matched another year")])),
    ("movie", {"genre": [10402], "keyword": [], "certification": {"US": "R"}, **_CLEAR_GUIDE}, 0,
     (REVIEW, [(RULE, "flagged-genre", "genre: 10402")])),
    ("movie", {"genre": [], "keyword": ["old lighthouse"], "certification": {"US": "R"}, **_CLEAR_GUIDE,
               "imdb.violence": _SEVERE, "imdb.nudity": _SPLIT}, 0,
     (REJECT, [(RULE, "violence", "imdb.violence: severe"), (RULE, "flagged-keywords", "keyword: old lighthouse"),
               (RULE, "nudity", "imdb.nudity: labelled mild, but 45% of the votes are moderate or worse")])),
    ("movie", {"genre": [], "keyword": [], "certification": {"US": "PG"}, **_CLEAR_GUIDE,
               "_identity": {"ok": True, "text": "same title and year"}}, 0,
     (APPROVE, [("clear", None, "same title and year"), ("clear", None, "no rule hit (6 checked)")])),
    ("series", {"keyword": [], "certification": {"US": "TV-14"}, **_CLEAR_GUIDE,
                "imdb.frightening": {"unavailable": "imdb_guide"}}, 0,
     (WAIT, [(UNAVAILABLE, "frightening", "imdb.frightening: integrations.imdb_guide is not enabled")])),
    ("ebook", {"subject": ["Beekeeping"]}, 0, (REVIEW, [(RULE, "subject-a", "subject: Beekeeping")])),
    ("ebook", {"subject": {"partial": ["history"]}}, 0,
     (APPROVE, [(MISSING, "subject-a", _THIN), (MISSING, "subject-b", _THIN), (MISSING, "subject-c", _THIN)])),
    ("magazine", {}, 0, (APPROVE, [("type", None, "magazine is always approved")])),
    ("music", {}, 0, (REVIEW, [("type", None, "music is always reviewed by hand")])),
]


class ScopeTests(unittest.TestCase):
    """Scoped policies: the first scope whose `when` holds gives the type settings and the rules."""

    def test_the_first_scope_whose_when_is_true_applies_and_a_missing_when_does_not(self) -> None:
        p = scoped({"id": "kids", "when": KIDS}, {"id": "telegram", "when": TELEGRAM},
                   {"id": "late", "when": {"check": "time_window", "between": ["22:00", "06:00"]}})
        cases = (
            ({"source": ["seerr"], "sender": ["17"]}, "kids"),
            # Both would match; the first in the file wins.
            ({"source": ["group"], "sender": ["17"], "time_window": "23:00"}, "telegram"),
            # The sender is not recorded: `all` cannot be settled, so the scope does not apply.
            ({"source": ["seerr"], "sender": {"missing": "the item does not record its sender"}}, None),
            ({"source": ["seerr"], "sender": ["99"], "time_window": "12:00"}, None),
            ({"source": ["seerr"], "sender": ["99"], "time_window": "23:30"}, "late"),
            # An old snapshot with none of the context signals matches no scope.
            ({}, None),
        )
        for signals, expected in cases:
            with self.subTest(signals=signals):
                found = applying_scope(p, signals)
                self.assertEqual(found.id if found is not None else None, expected)

    def test_a_scope_s_types_override_only_the_fields_it_gives(self) -> None:
        p = scoped(
            {"id": "telegram", "when": TELEGRAM, "types": {"movie": {"when_data_missing": "approve"}}},
            rules=[GUIDE | {"id": "violence"}],
            types={"movie": {"mode": "rules", "when_data_missing": "wait", "max_wait_hours": 48}},
        )
        scope = p.scopes[0]
        self.assertEqual(type_policy(p, "movie", scope), TypePolicy(mode="rules", when_data_missing="approve",
                                                                    max_wait_hours=48))
        self.assertEqual(type_policy(p, "movie"), TypePolicy(mode="rules", when_data_missing="wait", max_wait_hours=48))
        self.assertEqual(type_policy(p, "series", scope), type_policy(p, "series"))
        missing = {"imdb.violence": {"missing": "parents guide not fetched"}}
        self.assertEqual(evaluate(movie(), {**missing, "source": ["group"]}, p).outcome, APPROVE)
        self.assertEqual(evaluate(movie(), {**missing, "source": ["seerr"]}, p).outcome, WAIT)

    def test_a_scope_s_rules_run_before_the_base_rules_and_skip_leaves_base_rules_out(self) -> None:
        base = [
            {"id": "big", "check": "size_gb", "at_least": 50},
            {"id": "trusted", "check": "release_group", "any_of": ["GOOD"], "on_hit": "approve"},
        ]
        p = scoped(
            {"id": "telegram", "when": TELEGRAM, "skip": ["trusted"],
             "rules": [{"id": "tg-trusted", "check": "release_group", "any_of": ["GOOD"], "on_hit": "approve"}]},
            {"id": "kids", "when": KIDS, "skip": ["big"]},
            rules=base,
        )
        self.assertEqual([r.id for r in applicable_rules(p, "movie", p.scopes[0])], ["tg-trusted", "big"])
        self.assertEqual([r.id for r in applicable_rules(p, "movie", p.scopes[1])], ["trusted"])
        self.assertEqual([r.id for r in applicable_rules(p, "movie")], ["big", "trusted"])
        big_good = {"size_gb": 80, "release_group": ["GOOD"]}
        # The scope's approve rule runs first and ends the walk before the base review rule.
        decision = evaluate(movie(), {**big_good, "source": ["group"]}, p)
        self.assertEqual((decision.outcome, decision.reasons[0].rule), (APPROVE, "tg-trusted"))
        # Under the base policy the review rule comes first.
        decision = evaluate(movie(), {**big_good, "source": ["seerr"], "sender": ["5"]}, p)
        self.assertEqual((decision.outcome, decision.reasons[0].rule), (REVIEW, "big"))
        # A skipped rule is not checked at all.
        decision = evaluate(movie(), {"size_gb": 80, "release_group": ["BAD"], "source": ["seerr"], "sender": ["17"]}, p)
        self.assertEqual(decision.outcome, APPROVE)
        self.assertEqual(decision.reasons[-2].text, "no rule hit (1 checked)")

    def test_every_decision_under_a_scope_ends_with_the_scope_reason_and_a_settings_review_still_leads_with_its_setting(
        self,
    ) -> None:
        p = scoped(
            {"id": "telegram", "when": TELEGRAM, "types": {"music": {"mode": "never"}, "magazine": {"mode": "always"}}},
            rules=[{"id": "big", "types": ["movie"], "check": "size_gb", "at_least": 50}],
        )
        tg = {"source": ["group"]}
        cases = (
            (movie(), {**tg, "_duplicate": "duplicate of 7"}, REVIEW),
            (movie(media_type="music"), tg, REVIEW),
            (movie(media_type="magazine"), tg, APPROVE),
            (movie(media_type="ebook"), tg, REVIEW),
            (movie(), {**tg, "_no_title": True}, REVIEW),
            (movie(), {**tg, "size_gb": 80}, REVIEW),
            (movie(), {**tg, "size_gb": 4}, APPROVE),
            (movie(), tg, WAIT),
        )
        for item, signals, outcome in cases:
            with self.subTest(media_type=item.media_type, signals=signals):
                decision = evaluate(item, signals, p, now=NOW)
                self.assertEqual(decision.outcome, outcome)
                self.assertEqual(decision.reasons[-1], under("telegram"))
                self.assertNotEqual(decision.reasons[0].kind, SCOPE)
        held = evaluate(movie(media_type="music"), tg, p)
        self.assertTrue(is_settings_review(held.reasons_json()))
        shadow = act(evaluate(movie(), {**tg, "size_gb": 4}, p), "shadow")
        self.assertEqual((shadow.reasons[0].kind, shadow.reasons[-1]), ("shadow", under("telegram")))
        # The base policy says nothing about scopes.
        self.assertNotIn(SCOPE, [r.kind for r in evaluate(movie(), {"source": ["seerr"], "size_gb": 4}, p).reasons])

    def test_needed_signals_follows_the_applying_scope_and_is_the_union_without_a_snapshot(self) -> None:
        p = scoped(
            {"id": "telegram", "when": TELEGRAM, "skip": ["big"], "rules": [GUIDE | {"id": "violence"}]},
            {"id": "kids", "when": KIDS, "types": {"movie": {"mode": "never"}}},
            rules=[{"id": "big", "check": "size_gb", "at_least": 50}],
        )
        self.assertEqual(needed_signals(p, "movie", {"source": ["group"]}), {"imdb.violence"})
        self.assertEqual(needed_signals(p, "movie", {"source": ["seerr"], "sender": ["5"]}), {"size_gb"})
        self.assertEqual(needed_signals(p, "movie", {"source": ["seerr"], "sender": ["17"]}), set())
        self.assertEqual(needed_signals(p, "movie"), {"size_gb", "imdb.violence"})
        self.assertEqual(needed_signals(p, "ebook", {"source": ["group"]}), set())
        self.assertEqual(needed_signals(p, "ebook"), {"size_gb"})

    def test_compare_picks_each_row_s_scope_from_its_stored_snapshot(self) -> None:
        def row(item_id, snapshot, outcome):
            return SimpleNamespace(id=item_id, title=f"t{item_id}", media_type=MediaType.MOVIE, created_at=NOW,
                                   auto_decided_at=NOW, signals=snapshot, policy_outcome=outcome)

        missing = {"imdb.violence": {"missing": "parents guide not fetched"}}
        rows = [
            row(1, {**missing, "source": ["group"]}, WAIT),
            row(2, {**missing, "source": ["seerr"]}, WAIT),
            # Stored before the scope signals existed: no scope applies.
            row(3, dict(missing), WAIT),
        ]
        p = scoped({"id": "telegram", "when": TELEGRAM, "types": {"movie": {"when_data_missing": "approve"}}},
                   rules=[GUIDE | {"id": "violence"}])
        impact = compare(rows, p, now=NOW)
        self.assertEqual([(c["item_id"], c["after"]) for c in impact.changed], [(1, APPROVE)])
        self.assertEqual(impact.changed[0]["reasons"][-1], "decided under the scope telegram")
        self.assertEqual(impact.counts[WAIT], 2)

    def test_worth_rerunning_a_type_review_sees_a_scope_that_now_takes_the_type(self) -> None:
        held = [Reason("type", None, "ebook is always reviewed by hand").to_json()]
        base = {"ebook": {"mode": "never"}}
        for scope, expected in (
            (None, False),
            ({"id": "telegram", "when": TELEGRAM, "types": {"ebook": {"mode": "always"}}}, True),
            ({"id": "telegram", "when": TELEGRAM, "types": {"ebook": {"mode": "rules"}},
              "rules": [{"id": "thick", "check": "pages", "at_least": 900}]}, True),
            # In mode rules with no rule that applies, the type is still held.
            ({"id": "telegram", "when": TELEGRAM, "types": {"ebook": {"mode": "rules"}}}, False),
        ):
            with self.subTest(scope=scope):
                p = scoped(*([scope] if scope else []), types=base)
                self.assertEqual(worth_rerunning(held, None, "v", "ebook", p), expected)

    def test_a_policy_without_scopes_decides_as_before(self) -> None:
        p = policy_fixture.config().policy
        self.assertEqual(p.scopes, [])
        for media_type, signals, hours, (outcome, reasons) in BEFORE_SCOPES:
            with self.subTest(media_type=media_type, signals=signals, hours=hours):
                item = policy_fixture.item(media_type=media_type, created_at=NOW - timedelta(hours=hours))
                decision = evaluate(item, signals, p, now=NOW, default_min_votes=20)
                self.assertEqual(
                    (decision.outcome, [(r.kind, r.rule, r.text) for r in decision.reasons]),
                    (outcome, [tuple(r) for r in reasons]),
                )
                # The context signals every stored snapshot carries change nothing without a scope.
                decision = evaluate(item, {**signals, "source": ["feed"], "sender": ["5"]}, p, now=NOW,
                                    default_min_votes=20)
                self.assertEqual(decision.outcome, outcome)


SEEDERS = {"check": "seeders", "at_least": 1}
SIZE = {"check": "size_gb", "at_least": 50}
YEAR = {"check": "year", "at_most": 1999}


class MissingDataPerRuleTests(unittest.TestCase):
    """A rule's `when_missing` says what its missing data does; unset, the type's `when_data_missing` says."""

    WAITING = {"movie": {"mode": "rules", "when_data_missing": "wait", "max_wait_hours": 24}}

    def test_a_rule_s_when_missing_approve_holds_nothing_and_its_gap_is_recorded(self) -> None:
        p = policy(GUIDE | {"id": "violence", "when_missing": "approve"}, types=self.WAITING)
        decision = evaluate(movie(), {}, p, now=NOW)
        self.assertEqual(decision.outcome, APPROVE)
        self.assertEqual(decision.reasons, (Reason(MISSING, "violence", "imdb.violence: no data"),))
        # Its data, once in, is checked as ever.
        severe = {"severity": "severe", "votes": 50, "breakdown": {"severe": 50}}
        self.assertEqual(evaluate(movie(), {"imdb.violence": severe}, p, now=NOW).outcome, REVIEW)

    def test_a_review_gap_beats_a_wait_gap_and_a_wait_gap_beats_an_approve_gap(self) -> None:
        for modes, outcome in (
            (("approve", "wait", "review"), REVIEW),
            (("review", "approve", None), REVIEW),
            (("approve", "wait", "approve"), WAIT),
            (("approve", None, "approve"), WAIT),
            (("approve", "approve", "approve"), APPROVE),
        ):
            with self.subTest(modes=modes):
                rules = [
                    check | {"id": f"r{n}", **({"when_missing": mode} if mode else {})}
                    for n, (check, mode) in enumerate(zip((SEEDERS, SIZE, YEAR), modes))
                ]
                decision = evaluate(movie(), {}, policy(*rules, types=self.WAITING), now=NOW)
                self.assertEqual(decision.outcome, outcome)
                # Every gap is recorded, whatever it asked for.
                self.assertEqual(sorted(r.rule for r in decision.reasons), ["r0", "r1", "r2"])
        # A wait still ends in review once the type's max_wait_hours have passed.
        p = policy(SEEDERS | {"id": "r0", "when_missing": "approve"}, SIZE | {"id": "r1", "when_missing": "wait"},
                   types={"movie": {"mode": "rules", "when_data_missing": "approve", "max_wait_hours": 6}})
        late = evaluate(policy_fixture.item(created_at=NOW - timedelta(hours=7)), {}, p, now=NOW)
        self.assertEqual((late.outcome, late.reasons[0].text), (REVIEW, "no data after 6 hours"))

    def test_a_review_names_first_the_gaps_that_asked_for_a_person(self) -> None:
        rules = [SEEDERS | {"id": "r0", "when_missing": "approve"},
                 SIZE | {"id": "r1", "when_missing": "review"},
                 YEAR | {"id": "r2"},
                 GUIDE | {"id": "r3", "when_missing": "review"}]
        decision = evaluate(movie(), {}, policy(*rules, types=self.WAITING), now=NOW)
        self.assertEqual((decision.outcome, [r.rule for r in decision.reasons]),
                         (REVIEW, ["r1", "r3", "r0", "r2"]))

    def test_a_waived_gap_still_stops_an_approve_rule_from_ending_the_walk(self) -> None:
        p = policy(
            GUIDE | {"id": "violence", "when_missing": "approve"},
            {"id": "trusted", "check": "release_group", "any_of": ["GOOD"], "on_hit": "approve"},
            SIZE | {"id": "big", "on_hit": "reject"},
            types=self.WAITING,
        )
        decision = evaluate(movie(), {"release_group": ["GOOD"], "size_gb": 80}, p, now=NOW)
        self.assertEqual((decision.outcome, decision.reasons[0].rule), (REJECT, "big"))

    def test_a_hit_still_reviews_when_every_gap_is_waived(self) -> None:
        p = policy(GUIDE | {"id": "violence", "when_missing": "approve"}, SIZE | {"id": "big"},
                   types={"movie": {"mode": "rules", "when_data_missing": "approve"}})
        decision = evaluate(movie(), {"size_gb": 80}, p, now=NOW)
        self.assertEqual((decision.outcome, [r.rule for r in decision.reasons]), (REVIEW, ["big"]))

    def test_telegram_posts_with_no_data_approve_and_seerr_requests_wait(self) -> None:
        """The maintainer's case: a concert with no IMDb votes waited 24 hours for votes that never came."""
        base = policy_fixture.config().policy
        telegram = Scope.model_validate({
            "id": "telegram", "name": "Telegram posts with no data are approved", "when": TELEGRAM,
            "types": {"movie": {"when_data_missing": "approve"}, "series": {"when_data_missing": "approve"}},
        })
        p = base.model_copy(update={"scopes": [telegram]})
        concert = {"genre": [], "keyword": [], "_identity": {"ok": True, "text": "same title and year"}}
        gaps = [(MISSING, "adult-certification", "certification: no data"), (MISSING, "nudity", "imdb.nudity: no data"),
                (MISSING, "violence", "imdb.violence: no data"), (MISSING, "frightening", "imdb.frightening: no data")]
        decision = evaluate(movie(), {**concert, "source": ["group"]}, p, now=NOW, default_min_votes=20)
        self.assertEqual(decision.outcome, APPROVE)
        self.assertEqual([(r.kind, r.rule, r.text) for r in decision.reasons],
                         [*gaps, (SCOPE, "telegram", "decided under the scope telegram")])
        # The same post from the bot inbox too; a Seerr request waits as the base policy says.
        self.assertEqual(evaluate(movie(), {**concert, "source": ["inbox"]}, p, now=NOW).outcome, APPROVE)
        seerr = evaluate(movie(), {**concert, "source": ["seerr"], "sender": ["17"]}, p, now=NOW)
        self.assertEqual((seerr.outcome, [(r.kind, r.rule, r.text) for r in seerr.reasons]), (WAIT, gaps))
        # A finding is not missing data: a hit, a wrong match and no title still go to a person.
        severe = {"severity": "severe", "votes": 50, "breakdown": {"severe": 50}}
        hit_ = evaluate(movie(), {**concert, "source": ["group"], "imdb.violence": severe}, p, now=NOW)
        self.assertEqual((hit_.outcome, hit_.reasons[0].rule), (REJECT, "violence"))
        for found in ({"_identity": {"ok": False, "text": "TMDB matched another year"}}, {"_no_title": True}):
            with self.subTest(found=found):
                self.assertEqual(evaluate(movie(), {**concert, "source": ["group"], **found}, p, now=NOW).outcome,
                                 REVIEW)

    def test_the_type_s_when_data_missing_is_the_default(self) -> None:
        rules = [SEEDERS | {"id": "a"}, SIZE | {"id": "b"}]
        gaps = [(MISSING, "a", "seeders: no data"), (MISSING, "b", "size_gb: no data")]
        expired = [("expired", None, "no data after 24 hours"), *gaps]
        for wdm, (fresh, late) in {
            "approve": ((APPROVE, gaps), (APPROVE, gaps)),
            "review": ((REVIEW, gaps), (REVIEW, gaps)),
            "wait": ((WAIT, gaps), (REVIEW, expired)),
        }.items():
            types = {"movie": {"mode": "rules", "when_data_missing": wdm, "max_wait_hours": 24}}
            unset = policy(*rules, types=types)
            # The same rules saying it themselves, under a type that says something else.
            other = "review" if wdm != "review" else "approve"
            said = policy(*(r | {"when_missing": wdm} for r in rules),
                          types={"movie": {"mode": "rules", "when_data_missing": other, "max_wait_hours": 24}})
            for hours, (outcome, reasons) in ((0, fresh), (30, late)):
                with self.subTest(when_data_missing=wdm, hours=hours):
                    item = policy_fixture.item(created_at=NOW - timedelta(hours=hours))
                    for p in (unset, said):
                        decision = evaluate(item, {}, p, now=NOW)
                        self.assertEqual((decision.outcome, [(r.kind, r.rule, r.text) for r in decision.reasons]),
                                         (outcome, [tuple(r) for r in reasons]))
        # And the fixture policy, with no when_missing anywhere, decides as it did before.
        p = policy_fixture.config().policy
        for media_type, signals, hours, (outcome, reasons) in BEFORE_SCOPES:
            with self.subTest(media_type=media_type, signals=signals, hours=hours):
                item = policy_fixture.item(media_type=media_type, created_at=NOW - timedelta(hours=hours))
                decision = evaluate(item, signals, p, now=NOW, default_min_votes=20)
                self.assertEqual((decision.outcome, [(r.kind, r.rule, r.text) for r in decision.reasons]),
                                 (outcome, [tuple(r) for r in reasons]))


class PolicyVersionTests(unittest.TestCase):
    def test_equal_blocks_equal_versions(self) -> None:
        a = policy_fixture.config().policy
        b = policy_fixture.config().policy
        self.assertEqual(policy_version(a), policy_version(b))
        self.assertEqual(len(policy_version(a)), 12)

    def test_any_change_changes_the_version(self) -> None:
        a = policy_fixture.config().policy
        b = a.model_copy(update={"min_votes": 6})
        self.assertNotEqual(policy_version(a), policy_version(b))

    def test_every_decision_carries_it(self) -> None:
        p = policy_fixture.config().policy
        self.assertEqual(evaluate(movie(), {}, p).policy_version, policy_version(p))


class ReasonTests(unittest.TestCase):
    def test_reasons_are_structured(self) -> None:
        p = policy({"id": "big", "check": "size_gb", "at_least": 50})
        decision = evaluate(movie(), {"size_gb": 80}, p)
        self.assertEqual(decision.reasons_json(), [{"kind": "rule", "rule": "big", "text": "size_gb: 80"}])
        json.dumps(decision.reasons_json())

    def test_old_rows_map_to_legacy(self) -> None:
        mapped = legacy_reasons(None, ["sex & nudity: moderate", "x"])
        self.assertEqual([r.kind for r in mapped], [LEGACY, LEGACY])
        self.assertEqual(mapped[0].text, "sex & nudity: moderate")
        self.assertEqual(legacy_reasons(None, None), [])

    def test_new_rows_read_back_as_written(self) -> None:
        stored = [{"kind": "rule", "rule": "big", "text": "size_gb: 80"}]
        self.assertEqual(legacy_reasons(stored, ["ignored"]), [Reason("rule", "big", "size_gb: 80")])


class ShadowTests(unittest.TestCase):
    def test_shadow_decides_and_acts_on_nothing(self) -> None:
        p = policy({"id": "big", "check": "size_gb", "at_least": 50})
        decision = evaluate(movie(), {"size_gb": 4}, p)
        applied = act(decision, "shadow")
        self.assertEqual(decision.outcome, APPROVE)
        self.assertEqual(applied.outcome, REVIEW)
        self.assertTrue(applied.shadow)
        self.assertEqual(applied.reasons[0].kind, "shadow")
        self.assertIn("would have approved", applied.reasons[0].text)

    def test_shadow_keeps_a_review_as_it_is(self) -> None:
        decision = evaluate(movie(), {"size_gb": 80}, policy({"id": "big", "check": "size_gb", "at_least": 50}))
        applied = act(decision, "shadow")
        self.assertEqual(applied.reasons, decision.reasons)
        self.assertTrue(applied.shadow)

    def test_on_acts_on_a_rejection(self) -> None:
        decision = evaluate(movie(), {"size_gb": 80},
                            policy({"id": "big", "check": "size_gb", "at_least": 50, "on_hit": "reject"}))
        self.assertEqual(act(decision, "on").outcome, REJECT)
        self.assertEqual(act(decision, "off").outcome, REVIEW)


# --- preview impact ----------------------------------------------------------


class PreviewImpactTests(unittest.TestCase):
    def _row(self, item_id, snapshot, outcome):
        return SimpleNamespace(
            id=item_id, title=f"t{item_id}", media_type=MediaType.MOVIE, created_at=NOW,
            signals=snapshot, policy_outcome=outcome,
        )

    def test_a_changed_rule_shows_the_items_it_changes(self) -> None:
        rows = [
            self._row(1, {"size_gb": 80}, APPROVE),
            self._row(2, {"size_gb": 4}, APPROVE),
            self._row(3, None, None),
            self._row(4, {"size_gb": 60}, REVIEW),
        ]
        candidate = policy({"id": "big", "check": "size_gb", "at_least": 50})
        impact = compare(rows, candidate, now=NOW)
        self.assertEqual(impact.compared, 3)
        self.assertEqual(impact.not_comparable, 1)
        self.assertEqual(impact.counts[APPROVE], 1)
        self.assertEqual(impact.counts[REVIEW], 2)
        self.assertEqual([c["item_id"] for c in impact.changed], [1])
        self.assertEqual((impact.changed[0]["before"], impact.changed[0]["after"]), (APPROVE, REVIEW))

    def test_the_same_policy_changes_nothing(self) -> None:
        """The positive control's mirror: replaying the deciding policy is a no-op."""
        cfg = policy_fixture.config()
        item = movie()
        snapshot = gather.base_signals(
            item, cfg, tmdb=TMDBResult(tmdb_id=1, imdb_id="tt1", title="Example", original_title="Example",
                                       year=2024, match_mode=MATCH_WITH_YEAR, exact_title_hits=1,
                                       keywords=["x"]),
            guide=ParentsGuide(fetched=True, sex_rating=SeverityRating.NONE, violence_rating=SeverityRating.NONE,
                               frightening_rating=SeverityRating.NONE,
                               votes={"NUDITY": 50, "VIOLENCE": 50, "FRIGHTENING": 50}),
            book=None, book_looked_up=False, duplicate_of=None, twin_of=None, now=NOW,
        )
        decided = evaluate(item, json.loads(json.dumps(snapshot)), cfg.policy, now=NOW)
        row = self._row(1, json.loads(json.dumps(snapshot)), decided.outcome)
        self.assertEqual(compare([row], cfg.policy, now=NOW).changed, [])
        stricter = cfg.policy.model_copy(update={"rules": [
            *cfg.policy.rules, Rule.model_validate({"id": "y", "check": "year", "at_least": 2020})
        ]})
        self.assertEqual(len(compare([row], stricter, now=NOW).changed), 1)

    def test_preview_reads_only_the_database(self) -> None:
        rows = [self._row(1, {"size_gb": 80}, APPROVE)]

        class _Session:
            async def execute(self, _statement):
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        from vettarr.checker import policy as policy_module

        with (
            mock.patch("vettarr.database.async_session", lambda: _Session()),
            mock.patch("vettarr.config.runtime.get_config", policy_fixture.config),
            mock.patch.object(pipeline.tmdb_client, "search", side_effect=AssertionError("refetch")),
        ):
            impact = asyncio.run(policy_module.preview_impact(
                {"rules": [{"id": "big", "check": "size_gb", "at_least": 50}]}, limit=200,
            ))
        self.assertEqual(impact.counts[REVIEW], 1)
        self.assertEqual(len(impact.changed), 1)

    def test_a_candidate_naming_a_missing_list_is_refused(self) -> None:
        from vettarr.checker import policy as policy_module

        with mock.patch("vettarr.config.runtime.get_config", policy_fixture.config):
            with self.assertRaises(ValueError):
                asyncio.run(policy_module.preview_impact(
                    {"rules": [{"id": "k", "check": "keyword", "matches_any": "nope"}]}
                ))


# --- gathering the signals ---------------------------------------------------


class ReleaseSignalTests(unittest.TestCase):
    def test_a_scene_post(self) -> None:
        item = movie(
            raw_text="Some.Film.2024.2160p.WEB-DL.DV.HDR10.SPANISH.ENGLISH.x265-GROUP\n"
                     "Subs: English, Spanish\n12,4 GiB\n100% Free",
            quality="2160P", size_text="12,4 GiB", release_group="GROUP", title="Some Film",
        )
        out = gather.release_signals(item)
        self.assertEqual(out["size_gb"], 12.4)
        self.assertEqual(out["resolution"], "2160p")
        self.assertIn("WEB-DL", out["release_source"])
        self.assertEqual(out["codec"], ["h265"])
        self.assertIs(out["hdr"], True)
        self.assertEqual(sorted(out["audio_language"]), ["en", "es"])
        self.assertEqual(sorted(out["subtitles"]), ["en", "es"])
        self.assertEqual(out["release_group"], ["GROUP"])
        self.assertIs(out["freeleech"], True)

    def test_a_cam_and_an_sdr_release(self) -> None:
        out = gather.release_signals(movie(raw_text="Film 2024 HDCAM 720p x264", title="Film"))
        self.assertIn("CAM", out["release_source"])
        self.assertIs(out["hdr"], False)
        self.assertEqual(out["resolution"], "720p")

    def test_silence_is_missing_not_false(self) -> None:
        out = gather.release_signals(movie(raw_text="Film", title="Film"))
        for name in ("size_gb", "resolution", "release_source", "codec", "hdr", "audio_language",
                     "subtitles", "release_group", "freeleech", "format"):
            with self.subTest(signal=name):
                self.assertIn("missing", out[name])

    def test_half_leech_is_not_freeleech(self) -> None:
        self.assertIs(gather.release_signals(movie(raw_text="50% Free"))["freeleech"], False)

    def test_book_formats(self) -> None:
        out = gather.release_signals(movie(media_type="ebook", raw_text="Libro (EPUB, PDF)"))
        self.assertEqual(out["format"], ["epub", "pdf"])

    def test_size_units(self) -> None:
        self.assertEqual(gather.size_gb("700 MB"), round(700 / 1024, 3))
        self.assertEqual(gather.size_gb("1.5 TB"), 1536.0)
        self.assertIsNone(gather.size_gb(None))


class SeriesSignalTests(unittest.TestCase):
    TMDB = TMDBResult(tmdb_id=3, status="Returning Series", number_of_episodes=26,
                      season_episodes={1: 8, 2: 18})

    def test_a_season_pack(self) -> None:
        out = gather.series_signals(self.TMDB, movie(media_type="series", season="S02", raw_text="Show S02 [PACK]"))
        self.assertIs(out["season_complete"], True)
        self.assertEqual(out["status"], ["returning"])
        self.assertEqual(out["episode_count"], 18)

    def test_a_single_episode(self) -> None:
        out = gather.series_signals(self.TMDB, movie(media_type="series", season="S01", raw_text="Show S01E03"))
        self.assertIs(out["season_complete"], False)
        self.assertEqual(out["episode_count"], 1)

    def test_a_whole_show(self) -> None:
        out = gather.series_signals(self.TMDB, movie(media_type="series", raw_text="Show complete"))
        self.assertEqual(out["episode_count"], 26)

    def test_tmdb_off_is_unavailable(self) -> None:
        out = gather.series_signals(None, movie(media_type="series"), tmdb_on=False)
        self.assertEqual(out["status"], {"unavailable": "tmdb"})


class VotesAfterReleaseTests(unittest.TestCase):
    """IMDb votes count only from the day after the title came out."""

    RULE = {"id": "v", "check": "imdb.violence", "at_least": "severe"}
    MILD = {"severity": "mild", "votes": 40, "breakdown": {}}
    THIN = {"severity": "mild", "votes": 4, "breakdown": {}}
    WAITS = {"movie": {"mode": "rules", "when_data_missing": "wait", "max_wait_hours": 24}}

    def decide(self, guide, released_on, now):
        signals = {"imdb.violence": guide, "_released_on": released_on}
        return evaluate(movie(), signals, policy(self.RULE, types=self.WAITS, min_votes=5), now=now)

    def test_votes_before_the_first_full_day_out_do_not_count(self) -> None:
        d = self.decide(self.MILD, "2026-10-11", NOW)
        self.assertEqual(d.outcome, WAIT)
        self.assertIn("votes count from 2026-10-12", d.reasons[0].text)

    def test_an_unreleased_film_waits_whatever_its_votes(self) -> None:
        self.assertEqual(self.decide(self.MILD, "2026-12-01", NOW).outcome, WAIT)
        # Weeks after the item arrived it still waits: the clock has not started.
        self.assertEqual(self.decide(self.MILD, "2026-12-01", NOW + timedelta(days=30)).outcome, WAIT)

    def test_from_the_day_after_release_the_votes_decide(self) -> None:
        self.assertEqual(self.decide(self.MILD, "2026-10-10", NOW).outcome, APPROVE)

    def test_a_new_film_waits_for_votes_then_goes_to_a_person(self) -> None:
        self.assertEqual(self.decide(self.THIN, "2026-10-10", NOW).outcome, WAIT)
        late = self.decide(self.THIN, "2026-10-10", datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(late.outcome, REVIEW)
        self.assertIn("too few votes 24 hours", late.reasons[0].text)

    def test_an_old_film_with_too_few_votes_goes_to_a_person_at_once(self) -> None:
        d = self.decide(self.THIN, "2007-05-04", NOW)
        self.assertEqual(d.outcome, REVIEW)

    def test_without_a_release_date_the_wait_runs_from_arrival(self) -> None:
        self.assertEqual(self.decide(self.THIN, None, NOW + timedelta(hours=1)).outcome, WAIT)
        self.assertEqual(self.decide(self.THIN, None, NOW + timedelta(hours=25)).outcome, REVIEW)

    def test_a_gap_that_is_not_a_vote_keeps_the_arrival_clock(self) -> None:
        rules = (self.RULE, {"id": "k", "check": "keyword", "any_of": ["x"]})
        signals = {"imdb.violence": self.THIN, "keyword": {"missing": "none"}, "_released_on": "2007-05-04"}
        d = evaluate(movie(), signals, policy(*rules, types=self.WAITS, min_votes=5), now=NOW + timedelta(hours=1))
        self.assertEqual(d.outcome, WAIT)

    def test_the_snapshot_carries_the_release_day(self) -> None:
        cfg = policy_fixture.config()
        t = TMDBResult(tmdb_id=1, imdb_id="tt1", title="T", released_on="2026-10-01")
        out = gather.base_signals(movie(), cfg, tmdb=t, guide=None, book=None, book_looked_up=False,
                                  duplicate_of=None, twin_of=None, now=NOW)
        self.assertEqual(out["_released_on"], "2026-10-01")


class TitleAndGuideSignalTests(unittest.TestCase):
    def test_title_signals(self) -> None:
        t = TMDBResult(tmdb_id=1, genre_ids=[18], genres=["Drama"], keywords=["k"], original_language="en",
                       countries=["US"], runtime=100, vote_average=7.0, vote_count=10, adult=False,
                       collection=True, cast=["A"], crew=["B"], studios=["C"], certifications={"US": "R"})
        out = gather.title_signals(t)
        self.assertEqual(out["genre"], [18, "Drama"])
        self.assertEqual(out["rating"], 7.0)
        self.assertEqual(out["certification"], {"US": "R"})
        self.assertEqual(set(out), set(gather.TITLE_SIGNALS))

    def test_released_is_read_against_today(self) -> None:
        t = TMDBResult(tmdb_id=1, released_on="2026-10-26")
        self.assertIs(gather.title_signals(t, today=date(2026, 10, 26))["released"], True)
        self.assertIs(gather.title_signals(t, today=date(2026, 10, 25))["released"], False)
        self.assertIn("missing", gather.title_signals(TMDBResult(tmdb_id=1))["released"])

    def test_an_unreleased_film_goes_to_a_person(self) -> None:
        rule = {"id": "out", "check": "released", "is": False, "on_hit": "review", "when_missing": "review"}
        p = policy(rule)
        self.assertEqual(evaluate(movie(), {"released": False}, p).outcome, REVIEW)
        self.assertEqual(evaluate(movie(), {"released": True}, p).outcome, APPROVE)
        self.assertEqual(evaluate(movie(), {"released": {"missing": "x"}}, p).outcome, REVIEW)

    def test_tmdb_off_and_no_match(self) -> None:
        self.assertEqual(gather.title_signals(None, tmdb_on=False)["keyword"], {"unavailable": "tmdb"})
        self.assertIn("missing", gather.title_signals(None)["keyword"])

    def test_the_breakdown_is_kept_for_preview(self) -> None:
        g = ParentsGuide(fetched=True, violence_rating=SeverityRating.MILD, votes={"VIOLENCE": 40},
                         breakdown={"VIOLENCE": {"mild": 30, "severe": 10}})
        out = gather.guide_signals(g, TMDBResult(tmdb_id=1, imdb_id="tt1"))
        self.assertEqual(out["imdb.violence"],
                         {"severity": "mild", "votes": 40, "breakdown": {"mild": 30, "severe": 10}})
        self.assertEqual(gather.guide_signals(g, None, enabled=False)["imdb.alcohol"],
                         {"unavailable": "imdb_guide"})


class BookSignalTests(unittest.TestCase):
    def test_a_matched_record(self) -> None:
        verdict = BookVerdict(matched=True, subjects=["A", "B", "C"], languages=["spa"],
                              publishers=["P"], pages=300)
        out = gather.book_signals(verdict, looked_up=True)
        self.assertEqual(out, {"subject": ["A", "B", "C"], "language": ["es"], "publisher": ["P"], "pages": 300})

    def test_too_few_subjects_are_partial(self) -> None:
        out = gather.book_signals(BookVerdict(matched=True, subjects=["A"]), looked_up=True)
        self.assertEqual(out["subject"], {"partial": ["A"]})


class TorrentFileTests(unittest.TestCase):
    @staticmethod
    def _bencode(value) -> bytes:
        if isinstance(value, int):
            return b"i%de" % value
        if isinstance(value, (bytes, str)):
            raw = value.encode() if isinstance(value, str) else value
            return b"%d:%s" % (len(raw), raw)
        if isinstance(value, list):
            return b"l" + b"".join(TorrentFileTests._bencode(v) for v in value) + b"e"
        return b"d" + b"".join(
            TorrentFileTests._bencode(k) + TorrentFileTests._bencode(v) for k, v in sorted(value.items())
        ) + b"e"

    def multi(self) -> bytes:
        return self._bencode({"info": {"name": "Pack", "files": [
            {"length": 1, "path": ["Pack", "a.mkv"]},
            {"length": 2, "path": ["Pack", "Sample", "b.MKV"]},
            {"length": 3, "path": ["Pack", "setup.exe"]},
        ]}})

    def test_a_multi_file_torrent(self) -> None:
        names = gather.torrent_files(self.multi())
        self.assertEqual(names, ["a.mkv", "b.MKV", "setup.exe"])
        self.assertEqual(gather.file_signals(names), {"file_count": 3, "file_extensions": ["exe", "mkv"]})

    def test_a_single_file_torrent(self) -> None:
        single = self._bencode({"info": {"name": "Book.epub", "length": 5}})
        self.assertEqual(gather.torrent_files(single), ["Book.epub"])

    def test_garbage_is_unreadable(self) -> None:
        self.assertIsNone(gather.torrent_files(b"not a torrent"))
        self.assertIsNone(gather.torrent_files(b"d4:infod4:name5:xe"))

    def test_bencode_that_is_no_torrent_is_unreadable_not_an_error(self) -> None:
        """The file can be one a stranger posted; no shape of it may raise."""
        deep = b"d4:info" + b"l" * 200_000 + b"e" * 200_000 + b"e"
        self.assertIsNone(gather.torrent_files(deep))
        self.assertIsNone(gather.torrent_files(b"d4:infoi5ee"))
        self.assertIsNone(gather.torrent_files(b"d4:infodl1:ae1:b4:name1:xee"))
        self.assertEqual(gather.torrent_files(b"d4:infod5:filesld4:pathli5eeeeee"), [])


class _Rows:
    def __init__(self, rows=(), scalar=None, first=None):
        self._rows = list(rows)
        self._scalar = scalar
        self._first = first

    def __iter__(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(first=lambda: self._first)


class _Session:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.answers.pop(0) if self.answers else _Rows()


def _item(**fields) -> MediaItem:
    item = MediaItem(id=7, telegram_message_id=1, telegram_chat_id=-1, raw_text="",
                     media_type=MediaType.MOVIE, title="Some Film", year=2024)
    item.created_at = NOW
    for key, value in fields.items():
        setattr(item, key, value)
    return item


def _costly(needed, *, session=None, cfg=None, item=None, tmdb=None, size_known=True):
    return asyncio.run(gather.costly_signals(
        session or _Session(), item or _item(), cfg or policy_fixture.config(), set(needed),
        tmdb=tmdb, now=NOW, size_known=size_known,
    ))


class LibrarySignalTests(unittest.TestCase):
    def setUp(self) -> None:
        gather.forget_kept()
        self.addCleanup(gather.forget_kept)

    def test_in_library_without_jellyfin_is_unavailable(self) -> None:
        self.assertEqual(_costly({"in_library"})["in_library"], {"unavailable": "jellyfin"})

    def test_in_library_asks_jellyfin_by_tmdb_id(self) -> None:
        client = mock.Mock(has_tmdb_id=mock.AsyncMock(return_value=True))
        with mock.patch("vettarr.checker.jellyfin_client.client_for", return_value=client):
            out = _costly({"in_library"}, tmdb=TMDBResult(tmdb_id=99, title="Some Film"))
        self.assertIs(out["in_library"], True)
        self.assertEqual(client.has_tmdb_id.await_args.args[0], 99)

    def test_already_requested(self) -> None:
        out = _costly({"already_requested"}, session=_Session(_Rows([("Some Film",)])))
        self.assertIs(out["already_requested"], True)
        out = _costly({"already_requested"}, session=_Session(_Rows([("Other Thing Entirely",)])))
        self.assertIs(out["already_requested"], False)

    def test_in_download_client_reads_qbittorrent_names(self) -> None:
        with mock.patch.object(pipeline, "_get_qbt_names", mock.AsyncMock(return_value=["Some.Film.2024.1080p"])):
            self.assertIs(_costly({"in_download_client"})["in_download_client"], True)

    def test_disk_free_comes_from_qbittorrent(self) -> None:
        resp = mock.Mock(json=lambda: {"server_state": {"free_space_on_disk": 50 * 1024 ** 3}})
        with mock.patch.object(pipeline._qbt, "_request", mock.AsyncMock(return_value=resp)) as request:
            self.assertEqual(_costly({"disk_free_gb"})["disk_free_gb"], 50.0)
        self.assertEqual(request.await_args.args, ("GET", "/api/v2/sync/maindata"))

    def test_qbittorrent_off_is_unavailable(self) -> None:
        cfg = policy_fixture.config()
        cfg = cfg.model_copy(update={"integrations": cfg.integrations.model_copy(update={"qbittorrent": None})})
        out = _costly({"disk_free_gb", "in_download_client"}, cfg=cfg)
        self.assertEqual(out["disk_free_gb"], {"unavailable": "qbittorrent"})
        self.assertEqual(out["in_download_client"], {"unavailable": "qbittorrent"})

    def test_active_downloads_are_counted(self) -> None:
        self.assertEqual(_costly({"max_active_downloads"}, session=_Session(_Rows(scalar=4)))["max_active_downloads"], 4)


class ContextAndSearchSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        gather.forget_kept()
        self.addCleanup(gather.forget_kept)

    def test_source_time_and_missing_sender(self) -> None:
        cfg = policy_fixture.config()
        out = gather.base_signals(_item(), cfg, tmdb=None, guide=None, book=None, book_looked_up=False,
                                  duplicate_of=None, twin_of=None, now=NOW)
        self.assertEqual(out["source"], ["feed"])
        self.assertRegex(out["time_window"], r"^\d{2}:\d{2}$")
        self.assertIn("missing", out["sender"])
        self.assertIn("missing", out["post_age_hours"])

    def test_a_recorded_sender_and_post_time_are_read(self) -> None:
        item = SimpleNamespace(**vars(policy_fixture.item()), sender_id=555, posted_at=NOW - timedelta(hours=30))
        out = gather.base_signals(item, policy_fixture.config(), tmdb=None, guide=None, book=None,
                                  book_looked_up=False, duplicate_of=None, twin_of=None, now=NOW)
        self.assertEqual(out["sender"], ["555"])
        self.assertEqual(out["post_age_hours"], 30.0)

    def test_seeders_from_a_stored_search(self) -> None:
        stored = TorrentSearch(best_match_title="x", best_match_seeders=12, best_match_size=2 * 1024 ** 3)
        out = _costly({"seeders", "size_gb"}, session=_Session(_Rows(first=stored)), size_known=False)
        self.assertEqual(out["seeders"], 12)
        self.assertEqual(out["size_gb"], 2.0)

    def test_seeders_from_a_fresh_search(self) -> None:
        found = SimpleNamespace(error=None, results=[SimpleNamespace(seeders=3, size=0)])
        with mock.patch("vettarr.searcher.search_service.find_releases",
                        mock.AsyncMock(return_value=found)):
            self.assertEqual(_costly({"seeders"})["seeders"], 3)

    def test_file_signals_from_the_linked_torrent(self) -> None:
        from vettarr.searcher.search_service import LinkedFetch

        data = TorrentFileTests().multi()
        fetched = LinkedFetch(data, "u", provider=None, torrent_id="1")
        with mock.patch("vettarr.searcher.search_service.fetch_linked_torrent",
                        mock.AsyncMock(return_value=fetched)):
            out = _costly({"file_count", "file_extensions"}, item=_item(external_links=["https://x/details.php?id=1"]))
        self.assertEqual(out, {"file_count": 3, "file_extensions": ["exe", "mkv"]})

    def _own_torrent_config(self):
        from vettarr.config.schema import Source

        source = Source.model_validate({"key": "feed", "kind": "userbot", "chat": -1, "direct_adds": "anyone"})
        return policy_fixture.config().model_copy(update={"sources": [source]})

    def test_file_signals_from_a_direct_add_s_own_torrent(self) -> None:
        """A .torrent the post carried is what downloads, so it is what a
        file rule reads; nothing is fetched."""
        import pathlib
        import tempfile

        from vettarr import ingest

        linked = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)), \
                mock.patch("vettarr.searcher.search_service.fetch_linked_torrent", linked):
            (pathlib.Path(tmp) / "7.torrent").write_bytes(TorrentFileTests().multi())
            out = _costly({"file_count", "file_extensions"}, cfg=self._own_torrent_config())
        self.assertEqual(out, {"file_count": 3, "file_extensions": ["exe", "mkv"]})
        linked.assert_not_awaited()

    def test_a_magnet_direct_add_has_no_file_list(self) -> None:
        item = _item(raw_text="magnet:?xt=urn:btih:" + "a" * 40)
        out = _costly({"file_count", "file_extensions"}, cfg=self._own_torrent_config(), item=item)
        for name in ("file_count", "file_extensions"):
            self.assertIn("magnet", out[name]["missing"])

    def test_issue_newer_than_owned(self) -> None:
        item = _item(media_type=MediaType.MAGAZINE, title="Revista Ejemplo Nº 257", year=2026)
        session = _Session(_Rows([("Revista Ejemplo Nº 256", 2026), ("Otra Cosa Nº 900", 2026)]))
        self.assertIs(_costly({"issue_newer_than_owned"}, session=session, item=item)["issue_newer_than_owned"], True)
        session = _Session(_Rows([("Revista Ejemplo Nº 258", 2026)]))
        self.assertIs(_costly({"issue_newer_than_owned"}, session=session, item=item)["issue_newer_than_owned"], False)

    def test_issue_newer_than_owned_reads_english_months(self) -> None:
        item = _item(media_type=MediaType.MAGAZINE, title="Workshop Monthly - November", year=2026)
        session = _Session(_Rows([("Workshop Monthly - October 2026", 2026)]))
        self.assertIs(_costly({"issue_newer_than_owned"}, session=session, item=item)["issue_newer_than_owned"], True)
        session = _Session(_Rows([("Workshop Monthly - Diciembre 2026", 2026)]))
        self.assertIs(_costly({"issue_newer_than_owned"}, session=session, item=item)["issue_newer_than_owned"], False)

    def test_every_catalogue_signal_gets_real_data(self) -> None:
        """Each signal in the catalogue is answered with data by some builder.

        A value of {missing}, {unavailable} or {partial} does not count: a
        signal that can only ever be missing is one no rule can read.
        """
        from vettarr.searcher.search_service import LinkedFetch

        cfg = policy_fixture.config()
        severity = SeverityRating.NONE
        categories = ("NUDITY", "VIOLENCE", "PROFANITY", "FRIGHTENING", "ALCOHOL")
        tmdb = TMDBResult(
            tmdb_id=1, imdb_id="tt1", title="Some Show", original_title="Some Show", year=2024,
            match_mode=MATCH_WITH_YEAR, exact_title_hits=1, genres=["Drama"], genre_ids=[18],
            keywords=["x"], certifications={"US": "TV-14"}, original_language="en", countries=["US"],
            runtime=50, vote_average=7.1, vote_count=900, adult=False, collection=False,
            cast=["A"], crew=["B"], studios=["C"], status="Ended", number_of_episodes=20,
            season_episodes={1: 10}, released_on="2024-01-01",
        )
        guide = ParentsGuide(
            fetched=True, sex_rating=severity, violence_rating=severity, profanity_rating=severity,
            frightening_rating=severity, alcohol_rating=severity, votes={c: 50 for c in categories},
        )
        posted = {"sender_id": 555, "posted_at": NOW - timedelta(hours=2)}
        series = _item(
            media_type=MediaType.SERIES, title="Some Show", season="S01", release_group="GRP",
            raw_text="Some.Show.S01.1080p.WEB-DL.x265.SPANISH-GRP\nSubs: English\n12 GiB\n100% Free",
            external_links=["https://x/details.php?id=1"], **posted,
        )
        book = _item(media_type=MediaType.EBOOK, title="Some Book", raw_text="Some Book (EPUB)", **posted)
        verdict = BookVerdict(matched=True, subjects=["A", "B", "C"], languages=["spa"],
                              publishers=["P"], pages=300)
        produced: dict = {}

        def keep(answers: dict) -> None:
            produced.update({
                name: value for name, value in answers.items()
                if not (isinstance(value, dict) and {"missing", "unavailable", "partial"} & set(value))
            })

        for item, kwargs in (
            (series, {"tmdb": tmdb, "guide": guide, "book": None, "book_looked_up": False}),
            (book, {"tmdb": None, "guide": None, "book": verdict, "book_looked_up": True}),
        ):
            keep(gather.base_signals(item, cfg, duplicate_of=None, twin_of=None, now=NOW, **kwargs))

        class _Answers:
            """Every query of the lookups, answered: a count of 4 and one other title."""

            async def execute(self, _statement):
                return _Rows([("Other Thing Entirely",)], scalar=4)

        resp = mock.Mock(json=lambda: {"server_state": {"free_space_on_disk": 2 * 1024 ** 3}})
        found = SimpleNamespace(error=None, results=[SimpleNamespace(seeders=3, size=1024 ** 3)])
        jellyfin = mock.Mock(has_tmdb_id=mock.AsyncMock(return_value=False))
        fetched = LinkedFetch(TorrentFileTests().multi(), "u", provider=None, torrent_id="1")
        with (
            mock.patch.object(pipeline, "_get_qbt_names", mock.AsyncMock(return_value=[])),
            mock.patch.object(pipeline._qbt, "_request", mock.AsyncMock(return_value=resp)),
            mock.patch("vettarr.checker.jellyfin_client.client_for", return_value=jellyfin),
            mock.patch("vettarr.searcher.search_service.find_releases",
                       mock.AsyncMock(return_value=found)),
            mock.patch("vettarr.searcher.search_service.fetch_linked_torrent",
                       mock.AsyncMock(return_value=fetched)),
        ):
            keep(_costly(set(SIGNALS), session=_Answers(), item=series, tmdb=tmdb))
            magazine = _item(media_type=MediaType.MAGAZINE, title="Revista Nº 2")
            owned = _Session(_Rows([("Revista Nº 1", 2026)]))
            keep(_costly({"issue_newer_than_owned"}, session=owned, item=magazine))
        self.assertEqual(set(SIGNALS) - set(produced), set())
        self.assertEqual((produced["sender"], produced["per_sender_per_week"]), (["555"], 4))
        self.assertEqual(produced["post_age_hours"], 2.0)


# --- the pipeline under shadow and reject ------------------------------------


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        while events.pending():
            events._pending.get_nowait()

    def _run(self, *, enabled="on", guide=None):
        from test_pipeline_auto_approve import MOVIE_POST, full_guide, make_item, matched_tmdb, run_process

        item = make_item(MOVIE_POST)
        run_process(item, settings=policy_fixture.settings_stub(auto_approve_enabled=enabled),
                    content=(matched_tmdb(), guide or full_guide()))
        return item

    def _queued(self):
        out = []
        while events.pending():
            out.append(events._pending.get_nowait())
        return out

    def test_shadow_records_the_decision_and_acts_on_nothing(self) -> None:
        item = self._run(enabled="shadow")
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertIsNone(item.reviewed_at)
        self.assertEqual(item.policy_outcome, APPROVE)
        self.assertEqual(item.reasons[0]["kind"], "shadow")
        kinds = [kind for _at, kind, _id, _payload in self._queued()]
        self.assertIn(events.WOULD_HAVE, kinds)
        self.assertIn(events.DECISION, kinds)

    def test_the_decision_is_persisted_with_its_signals(self) -> None:
        item = self._run()
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.policy_version, policy_version(policy_fixture.config().policy))
        self.assertTrue(all(set(r) == {"kind", "rule", "text"} for r in item.reasons))
        self.assertEqual(item.signals["imdb.violence"]["votes"], 100)
        json.dumps(item.signals)
        self.assertEqual([k for _a, k, _i, _p in self._queued()], [events.DECISION])

    def test_a_policy_rejection_sets_the_status_and_nothing_else(self) -> None:
        from test_pipeline_auto_approve import full_guide

        item = self._run(guide=full_guide(violence_rating=SeverityRating.SEVERE))
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(item.policy_outcome, REJECT)
        self.assertIsNone(item.auto_decision)
        self.assertIn("imdb.violence: severe", item.error_message)
        self.assertEqual(item.auto_recommendation, Recommendation.REJECT)

    def test_a_review_tells_someone(self) -> None:
        item = self._run(enabled="off")
        self.assertEqual(item.reasons[0]["kind"], "setting")
        self.assertIn(events.NEEDS_YOU, [k for _a, k, _i, _p in self._queued()])


# --- events and the notifier -------------------------------------------------


class _Receiver(http.server.BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append(json.loads(self.rfile.read(length)))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *_args):
        pass


class NotifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Receiver)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/hook"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Receiver.received = []
        _Receiver.status = 200
        notify._client = None
        notify._last_failure_log = float("-inf")
        notify._failures_since_log = 0
        notify._paused_until = float("-inf")

    def _send(self, event, url=None):
        # One client per event loop: each asyncio.run here is a new loop,
        # where the service runs one loop for its whole life.
        notify._client = None
        with mock.patch.object(notify, "_url", lambda: url or self.url):
            return asyncio.run(notify.send(event))

    def test_an_event_is_posted_as_json(self) -> None:
        event = {"id": 1, "at": "2026-10-11T12:00:00+00:00", "kind": "needs_you", "item_id": 7,
                 "payload": {"title": "Some Film", "outcome": "review", "reasons": ["imdb.nudity: severe"]}}
        self.assertTrue(self._send(event))
        got = _Receiver.received[0]
        self.assertEqual({k: got[k] for k in event}, event)
        self.assertEqual(got["title"], "vettarr: Needs your decision")
        self.assertIn("Some Film", got["body"])
        self.assertEqual(got["type"], "warning")

    def test_no_notify_block_sends_nothing(self) -> None:
        with mock.patch.object(notify, "_url", lambda: None):
            self.assertFalse(asyncio.run(notify.send({"kind": "decision"})))
        self.assertEqual(_Receiver.received, [])

    def test_failures_are_logged_once_an_hour_and_never_retried(self) -> None:
        _Receiver.status = 500
        clock = [1000.0]
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with (
            mock.patch.object(notify, "_clock", lambda: clock[0]),
            self.assertLogs(notify.log, level="WARNING") as logs,
        ):
            for _ in range(3):
                self.assertFalse(self._send({"kind": "decision"}))
            clock[0] += notify.FAILURE_LOG_SECONDS
            self.assertFalse(self._send({"kind": "decision"}))
        self.assertEqual(len(_Receiver.received), 4, "one attempt per event, no retry")
        self.assertEqual(len(logs.records), 2)
        self.assertNotIn(self.url, "\n".join(logs.output))

    def test_the_timeout_is_five_seconds(self) -> None:
        """The constant, and that it is the one the client is built with."""
        self.assertEqual(notify.TIMEOUT_SECONDS, 5.0)
        self.assertTrue(self._send({"kind": "decision"}))
        self.assertEqual(notify._client.timeout, httpx.Timeout(5.0))

    def test_a_non_http_url_is_refused_without_a_request(self) -> None:
        self.assertFalse(self._send({"kind": "decision"}, url="tgram://token/chat"))
        self.assertEqual(_Receiver.received, [])


class EventTests(unittest.TestCase):
    def setUp(self) -> None:
        while events.pending():
            events._pending.get_nowait()

    def test_emit_never_raises_and_flush_writes_then_notifies(self) -> None:
        events.emit(events.GUARD, provider="tracker-a", state="cooldown")
        events.emit(events.DECISION, 7, outcome="approve")
        written, sent = [], []

        class _Session:
            def add_all(self, rows):
                for n, row in enumerate(rows, 1):
                    row.id = n
                written.extend(rows)

            async def commit(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        async def _send(event):
            sent.append(event)
            return True

        with (
            mock.patch("vettarr.database.async_session", lambda: _Session()),
            mock.patch.object(notify, "send", _send),
        ):
            self.assertEqual(asyncio.run(events.flush()), 2)
        self.assertEqual([r.kind for r in written], [events.GUARD, events.DECISION])
        self.assertEqual(written[1].item_id, 7)
        self.assertEqual(sent[0]["payload"], {"provider": "tracker-a", "state": "cooldown"})
        self.assertEqual(events.pending(), 0)

    def test_the_guard_reports_its_state_changes(self) -> None:
        from vettarr.searcher.tracker_guard import TrackerGuard

        guard = TrackerGuard("tracker-a", clock=lambda: 0.0)
        guard.note_failure("session could not be established")
        guard.note_success()
        kinds = []
        while events.pending():
            _at, kind, _id, payload = events._pending.get_nowait()
            kinds.append((kind, payload.get("state")))
        self.assertEqual(kinds, [(events.GUARD, "cooldown"), (events.COOKIE_EXPIRED, None), (events.GUARD, "ok")])

    def test_the_scheduler_flushes_events(self) -> None:
        from vettarr import scheduler

        s = scheduler.Scheduler()
        with mock.patch.object(s._scheduler, "start"):
            s.start()
        self.assertIsNotNone(s._scheduler.get_job("flush_events"))


# --- Jellyfin and IMDb --------------------------------------------------------

# Jellyfin's /Items answer, trimmed to the fields the client reads.
_JELLYFIN_ITEMS = {
    "Items": [
        {"Name": "Some Film", "Type": "Movie", "ProviderIds": {"Tmdb": "123", "Imdb": "tt1"}},
        {"Name": "Some Film", "Type": "Movie", "ProviderIds": {"Imdb": "tt2"}},
    ],
    "TotalRecordCount": 2,
}


class JellyfinClientTests(unittest.TestCase):
    def _client(self, handler):
        return JellyfinClient("http://jellyfin:8096/", "k",
                              http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    def test_a_title_held_under_its_tmdb_id(self) -> None:
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=_JELLYFIN_ITEMS)

        found = asyncio.run(self._client(handler).has_tmdb_id(123, "movie", ["Some Film"]))
        self.assertIs(found, True)
        self.assertEqual(seen[0].url.path, "/Items")
        self.assertEqual(seen[0].url.params["IncludeItemTypes"], "Movie")
        self.assertEqual(seen[0].url.params["Fields"], "ProviderIds")
        self.assertIn('Token="k"', seen[0].headers["Authorization"])

    def test_the_same_name_with_another_id_is_not_in_the_library(self) -> None:
        found = asyncio.run(self._client(lambda r: httpx.Response(200, json=_JELLYFIN_ITEMS))
                            .has_tmdb_id(999, "movie", ["Some Film", "Original"]))
        self.assertIs(found, False)

    def test_an_error_is_unknown_not_absent(self) -> None:
        found = asyncio.run(self._client(lambda r: httpx.Response(401)).has_tmdb_id(1, "movie", ["X"]))
        self.assertIsNone(found)

    def test_a_book_is_not_looked_up(self) -> None:
        self.assertIsNone(asyncio.run(self._client(lambda r: httpx.Response(200)).has_tmdb_id(1, "ebook", ["X"])))


class ImdbHeaderTests(unittest.TestCase):
    def test_no_browser_disguise_is_sent(self) -> None:
        sent = {k.lower() for k in imdb_client._HEADERS}
        for header in ("user-agent", "origin", "referer"):
            self.assertNotIn(header, sent)
        self.assertEqual(imdb_client._HEADERS["x-imdb-client-name"], "imdb-web-next-localized")

    def test_the_pacing_is_kept(self) -> None:
        self.assertEqual(imdb_client._MIN_INTERVAL, 2.0)

    def test_the_guide_is_not_fetched_while_off_or_unread(self) -> None:
        from test_pipeline_auto_approve import MOVIE_POST, make_item

        item = make_item(MOVIE_POST, title="Some Film", year=2024, media_type=MediaType.MOVIE)
        fetch = mock.AsyncMock(return_value=ParentsGuide())
        base = policy_fixture.config()
        no_imdb_rules = base.model_copy(update={"policy": base.policy.model_copy(update={
            "rules": [r for r in base.policy.rules if not r.check.startswith("imdb.")]})})
        guide_off = base.model_copy(update={"integrations": base.integrations.model_copy(update={
            "imdb_guide": base.integrations.imdb_guide.model_copy(update={"enabled": False})})})
        for cfg, expected in ((base, 1), (no_imdb_rules, 0), (guide_off, 0)):
            fetch.reset_mock()
            session = mock.Mock(execute=mock.AsyncMock(return_value=_Rows()), commit=mock.AsyncMock(),
                                add=mock.Mock())
            from vettarr.config import Settings

            with (
                mock.patch.object(pipeline, "settings", Settings(cfg)),
                mock.patch.object(pipeline.tmdb_client, "search",
                                  mock.AsyncMock(return_value=TMDBResult(tmdb_id=1, imdb_id="tt1"))),
                mock.patch.object(pipeline, "fetch_parents_guide", fetch),
            ):
                asyncio.run(pipeline._run_content_check(session, item))
            self.assertEqual(fetch.await_count, expected)


class MigrationTests(unittest.TestCase):
    def _module(self):
        import importlib.util
        import pathlib

        path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0003_policy_engine.py"
        spec = importlib.util.spec_from_file_location("_revision_0003", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_follows_the_sources_revision(self) -> None:
        module = self._module()
        self.assertEqual(module.down_revision, "0002_sources")
        self.assertEqual(module.revision, "0003_policy_engine")

    def test_it_only_adds_nullable_columns_and_a_table(self) -> None:
        module = self._module()
        statements = []
        with mock.patch.object(module.op, "execute", statements.append):
            module.upgrade()
        sql = "\n".join(statements)
        for column in ("reasons JSONB", "policy_version TEXT", "signals JSONB", "policy_outcome TEXT"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS events", sql)
        for statement in statements:
            self.assertIn("IF NOT EXISTS", statement)
        upper = sql.upper()
        for forbidden in ("DELETE ", "UPDATE ", "DROP ", "RENAME", "ALTER COLUMN"):
            self.assertNotIn(forbidden, upper.replace("ON DELETE SET NULL", ""))
        altered = next(s for s in statements if "ALTER TABLE media_items" in s)
        self.assertNotIn("NOT NULL", altered)


if __name__ == "__main__":
    unittest.main()
