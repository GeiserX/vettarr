"""The parity gate, scripts/replay_posts.py: what it compares, and that it can fail.

A gate that reports zero whatever the format says is no gate. These tests run
it over a small invented dump, once with the format the dump was written with
and once with a word of it changed, for the parsed fields and for the
`non_media` reason.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("replay_posts", ROOT / "scripts" / "replay_posts.py")
replay_posts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_posts)

CONFIG = """\
formats:
  - key: card
    type_headers:
      ebook: [NEW EBOOK]
    fields:
      title: [{title}]
    ignore_hosts: [{host}]
    media_words: [{word}]
"""
REEL = "https://www.instagram.com/reel/DAbC12xyz/"
REQUEST = "could you look for that book we were all talking about yesterday, thanks."
NOTE = "remember to take the bins out before nine tonight, please."
ROWS = [
    {"id": 1, "raw_text": "NEW EBOOK\nTitle: An Invented Book", "media_type": "ebook",
     "title": "An Invented Book", "non_media": None},
    {"id": 2, "raw_text": REEL, "media_type": "unknown", "non_media": "instagram.com link, not a media post"},
    {"id": 3, "raw_text": REQUEST, "media_type": "unknown", "non_media": None},
    {"id": 4, "raw_text": NOTE, "media_type": "unknown", "non_media": "no release information in the message"},
]


class ReplayTests(unittest.TestCase):
    def run_gate(self, rows, *, title="Title", host="instagram.com", word="book") -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "posts.json"
            dump.write_text(json.dumps(rows), encoding="utf-8")
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG.format(title=title, host=host, word=word), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = replay_posts.main([str(dump), "--config", str(config)])
        return code, out.getvalue()

    def test_the_format_the_dump_was_written_with_gives_no_diffs(self) -> None:
        code, out = self.run_gate(ROWS)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], ["rows: 4", "diffs: 0"])

    def test_a_changed_label_shows_in_the_parsed_fields(self) -> None:
        code, out = self.run_gate(ROWS, title="Name")
        self.assertEqual(code, 1)
        self.assertIn("  title: 1", out)

    def test_a_changed_ignore_host_shows_in_non_media(self) -> None:
        """No parsed field changes; only the reason does."""
        code, out = self.run_gate(ROWS, host="example.invalid")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[:3], ["rows: 4", "diffs: 1", "  non_media: 1"])

    def test_a_changed_media_word_shows_in_non_media(self) -> None:
        code, out = self.run_gate(ROWS, word="film")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[:3], ["rows: 4", "diffs: 1", "  non_media: 1"])

    def test_a_dump_without_the_column_is_not_compared_on_it(self) -> None:
        rows = [{k: v for k, v in row.items() if k != "non_media"} for row in ROWS]
        code, out = self.run_gate(rows, host="example.invalid", word="film")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], ["rows: 4", "diffs: 0"])


if __name__ == "__main__":
    unittest.main()
