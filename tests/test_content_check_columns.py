"""ContentCheck keeps its database columns but reads in neutral words.

The columns were named before the project was public and migrations only
add, so the table keeps them; the Python attributes say what they hold.
"""

from __future__ import annotations

import unittest

from vettarr.models import ContentCheck


class ContentCheckColumnTests(unittest.TestCase):
    def test_the_title_rule_attributes_map_to_the_existing_columns(self) -> None:
        self.assertEqual(ContentCheck.title_rule_hit.property.columns[0].name, "has_magic")
        self.assertEqual(ContentCheck.title_rule_words.property.columns[0].name, "magic_indicators")

    def test_the_table_keeps_its_column_names(self) -> None:
        names = set(ContentCheck.__table__.columns.keys())
        self.assertLessEqual({"has_magic", "magic_indicators"}, names)
        self.assertNotIn("title_rule_hit", names)
        self.assertNotIn("title_rule_words", names)

    def test_the_old_attribute_names_are_gone(self) -> None:
        check = ContentCheck(title_rule_hit=True, title_rule_words=["a word"])
        self.assertTrue(check.title_rule_hit)
        self.assertEqual(check.title_rule_words, ["a word"])
        self.assertFalse(hasattr(ContentCheck, "has_magic"))
        self.assertFalse(hasattr(ContentCheck, "magic_indicators"))


if __name__ == "__main__":
    unittest.main()
