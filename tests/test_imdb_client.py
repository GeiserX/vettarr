import asyncio
import json
import unittest
import urllib.error
from unittest import mock

from vettarr.checker import imdb_client
from vettarr.models import SeverityRating


def _guide_payload(categories):
    return {"data": {"title": {"titleText": {"text": "Example"},
                               "parentsGuide": {"categories": categories}}}}


def _category(cat_id, severity, votes, voted_for=None, breakdown=None):
    severity_node = None if severity is None else {
        "text": severity, "votedFor": voted_for if voted_for is not None else votes}
    node = {"category": {"id": cat_id}, "severity": severity_node,
            "totalSeverityVotes": votes}
    if breakdown is not None:
        node["severityBreakdown"] = [
            {"text": text, "votedFor": count} for text, count in breakdown
        ]
    return node


# Shape copied from a real answer for a public film (tt0993846).
FULL_GUIDE = _guide_payload([
    _category("NUDITY", "Severe", 3998, 3594),
    _category("VIOLENCE", "Moderate", 1796, 842),
    _category("PROFANITY", "Severe", 2187, 1900),
    _category("ALCOHOL", "Severe", 1791, 1500),
    _category("FRIGHTENING", "Moderate", 1523, 800),
])


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImdbClientTestCase(unittest.TestCase):
    """Base case: no real sleeping, and a clean pacing clock per test."""

    def setUp(self):
        self._sleeps = []
        self.enterContext(mock.patch.object(imdb_client, "_sleep", self._sleeps.append))
        self.enterContext(mock.patch.object(imdb_client, "_last_request_at", float("-inf")))

    def fetch(self, responder):
        """Run one fetch with urlopen replaced by `responder`."""
        self.requests = []

        def fake_urlopen(req, timeout=None):
            self.requests.append((req, timeout))
            return responder(req)

        with mock.patch.object(imdb_client.urllib.request, "urlopen", fake_urlopen):
            return asyncio.run(imdb_client.fetch_parents_guide("tt0993846"))


class ParseTests(ImdbClientTestCase):
    def test_full_guide_maps_every_category(self):
        guide = self.fetch(lambda req: _FakeResponse(json.dumps(FULL_GUIDE)))

        self.assertTrue(guide.fetched)
        self.assertIsNone(guide.error)
        self.assertEqual(guide.sex_rating, SeverityRating.SEVERE)
        self.assertEqual(guide.violence_rating, SeverityRating.MODERATE)
        self.assertEqual(guide.profanity_rating, SeverityRating.SEVERE)
        self.assertEqual(guide.alcohol_rating, SeverityRating.SEVERE)
        self.assertEqual(guide.frightening_rating, SeverityRating.MODERATE)
        self.assertEqual(guide.votes["NUDITY"], 3998)
        self.assertEqual(guide.votes["VIOLENCE"], 1796)

    def test_request_carries_the_headers_imdb_requires(self):
        self.fetch(lambda req: _FakeResponse(json.dumps(FULL_GUIDE)))

        req, timeout = self.requests[0]
        self.assertEqual(req.full_url, "https://caching.graphql.imdb.com/")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, 15.0)
        headers = {k.lower(): v for k, v in req.header_items()}
        # Only the client-name header is needed; no browser disguise is sent.
        self.assertNotIn("origin", headers)
        self.assertNotIn("referer", headers)
        self.assertNotIn("Mozilla", headers.get("user-agent", ""))
        self.assertEqual(headers["x-imdb-client-name"], "imdb-web-next-localized")
        self.assertEqual(json.loads(req.data)["variables"], {"id": "tt0993846"})

    def test_empty_categories_is_a_successful_fetch_with_no_ratings(self):
        guide = self.fetch(lambda req: _FakeResponse(json.dumps(_guide_payload([]))))

        self.assertTrue(guide.fetched)
        self.assertIsNone(guide.error)
        self.assertEqual(guide.votes, {})
        self.assertIsNone(guide.sex_rating)
        self.assertIsNone(guide.violence_rating)
        self.assertIsNone(guide.profanity_rating)
        self.assertIsNone(guide.frightening_rating)
        self.assertIsNone(guide.alcohol_rating)

    def test_zero_votes_or_missing_severity_leaves_the_rating_unset(self):
        payload = _guide_payload([
            _category("NUDITY", "Severe", 0),
            _category("VIOLENCE", None, 120),
        ])

        guide = self.fetch(lambda req: _FakeResponse(json.dumps(payload)))

        self.assertTrue(guide.fetched)
        self.assertIsNone(guide.sex_rating)
        self.assertIsNone(guide.violence_rating)
        self.assertEqual(guide.votes, {"NUDITY": 0, "VIOLENCE": 120})

    def test_missing_parents_guide_node_is_not_a_fetch(self):
        payload = {"data": {"title": {"titleText": {"text": "Example"},
                                      "parentsGuide": None}}}

        guide = self.fetch(lambda req: _FakeResponse(json.dumps(payload)))

        self.assertFalse(guide.fetched)
        self.assertEqual(guide.error, "title not found")


class BreakdownTests(ImdbClientTestCase):
    """IMDb publishes how many people voted for each severity, not only which
    one won. Shape taken from a live answer for tt0111161."""

    def test_the_per_severity_votes_are_kept(self):
        payload = _guide_payload([
            _category("NUDITY", "Mild", 1672, 714, breakdown=[
                ("None", 206), ("Mild", 714), ("Moderate", 605), ("Severe", 147),
            ]),
        ])
        guide = self.fetch(lambda req: _FakeResponse(json.dumps(payload)))

        self.assertEqual(guide.sex_rating, SeverityRating.MILD)
        self.assertEqual(
            guide.breakdown["NUDITY"],
            {"none": 206, "mild": 714, "moderate": 605, "severe": 147},
        )

    def test_a_category_without_a_breakdown_records_none(self):
        payload = _guide_payload([_category("NUDITY", "Mild", 100)])
        guide = self.fetch(lambda req: _FakeResponse(json.dumps(payload)))
        self.assertEqual(guide.breakdown, {})

    def test_a_malformed_breakdown_is_dropped_not_raised(self):
        payload = _guide_payload([
            _category("NUDITY", "Mild", 100),
        ])
        payload["data"]["title"]["parentsGuide"]["categories"][0][
            "severityBreakdown"
        ] = ["nonsense", {"text": "", "votedFor": 3}, {"text": "Mild"}]
        guide = self.fetch(lambda req: _FakeResponse(json.dumps(payload)))
        self.assertTrue(guide.fetched)
        self.assertEqual(guide.breakdown, {})

    def test_the_query_asks_for_the_breakdown(self):
        self.fetch(lambda req: _FakeResponse(json.dumps(FULL_GUIDE)))
        body = json.loads(self.requests[0][0].data.decode())
        self.assertIn("severityBreakdown", body["query"])


class FailureTests(ImdbClientTestCase):
    def test_http_403_is_reported_and_not_retried(self):
        def responder(req):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        guide = self.fetch(responder)

        self.assertFalse(guide.fetched)
        self.assertEqual(guide.error, "http 403")
        self.assertEqual(len(self.requests), 1)

    def test_http_429_is_reported_and_not_retried(self):
        def responder(req):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        guide = self.fetch(responder)

        self.assertEqual(guide.error, "http 429")
        self.assertEqual(len(self.requests), 1)

    def test_timeout_is_reported(self):
        def responder(req):
            raise TimeoutError("timed out")

        guide = self.fetch(responder)

        self.assertFalse(guide.fetched)
        self.assertEqual(guide.error, "timeout")

    def test_socket_timeout_wrapped_in_urlerror_is_reported_as_timeout(self):
        def responder(req):
            raise urllib.error.URLError(TimeoutError("timed out"))

        guide = self.fetch(responder)

        self.assertEqual(guide.error, "timeout")

    def test_malformed_json_is_reported(self):
        guide = self.fetch(lambda req: _FakeResponse("<html>challenge</html>"))

        self.assertFalse(guide.fetched)
        self.assertEqual(guide.error, "malformed response")

    def test_graphql_errors_with_null_data_is_not_a_fetch(self):
        payload = {"data": None, "errors": [{"message": "Title not found"}]}

        guide = self.fetch(lambda req: _FakeResponse(json.dumps(payload)))

        self.assertFalse(guide.fetched)
        self.assertEqual(guide.error, "title not found")
        self.assertIsNone(guide.sex_rating)


class PacingTests(ImdbClientTestCase):
    def test_two_calls_are_spaced_by_the_pacing_lock(self):
        clock = [1000.0]

        def fake_sleep(seconds):
            self._sleeps.append(seconds)
            clock[0] += seconds

        self.enterContext(mock.patch.object(imdb_client, "_clock", lambda: clock[0]))
        self.enterContext(mock.patch.object(imdb_client, "_sleep", fake_sleep))

        body = json.dumps(FULL_GUIDE)
        self.fetch(lambda req: _FakeResponse(body))
        self.assertEqual(self._sleeps, [])

        self.fetch(lambda req: _FakeResponse(body))
        self.assertEqual(self._sleeps, [2.0])
        self.assertEqual(clock[0], 1002.0)


if __name__ == "__main__":
    unittest.main()
