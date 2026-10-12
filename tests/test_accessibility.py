"""What every template owes a keyboard and a screen reader.

Read from the template files, so a page added later is held to the same
list: a heading, a label on every field, no focus ring switched off, a link
that opens a tab saying so. What only a browser can measure (contrast, the
order of focus) is ``scripts/axe.js``.
"""

import pathlib
import re
import unittest

import vettarr.web

WEB_DIR = pathlib.Path(vettarr.web.__file__).parent
REPO = WEB_DIR.parents[2]
TEMPLATES = sorted((WEB_DIR / "templates").rglob("*.html"))
PAGES = [t for t in TEMPLATES if '{% extends "base.html" %}' in t.read_text()]
FIELD = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.S)


def attr(tag: str, name: str) -> str | None:
    found = re.search(rf'\b{name}="([^"]*)"', tag)
    return found.group(1) if found else None


class TemplateTests(unittest.TestCase):
    def test_there_are_pages_to_check(self) -> None:
        self.assertGreaterEqual(len(PAGES), 7)
        self.assertIn("activity.html", [p.name for p in PAGES])

    def test_every_page_has_exactly_one_h1(self) -> None:
        for page in PAGES:
            with self.subTest(page=page.name):
                text = page.read_text()
                included = "".join(
                    (WEB_DIR / "templates" / name).read_text()
                    for name in re.findall(r'{% include "([^"]+)" %}', text)
                )
                self.assertGreaterEqual(len(re.findall(r"<h1\b", text + included)), 1)
                # A page that is either the item or "no longer exists" has one h1 per branch.
                one_branch = re.sub(r"{% else %}.*?{% endif %}", "", text + included, flags=re.S)
                self.assertEqual(len(re.findall(r"<h1\b", one_branch)), 1)

    def test_no_heading_level_is_skipped(self) -> None:
        for page in PAGES:
            with self.subTest(page=page.name):
                levels = [int(n) for n in re.findall(r"<h([1-6])\b", page.read_text())]
                for before, after in zip(levels, levels[1:]):
                    self.assertLessEqual(after, before + 1, levels)

    def test_every_field_has_a_label(self) -> None:
        for template in TEMPLATES:
            text = template.read_text()
            for kind, attrs in FIELD.findall(text):
                tag = f"<{kind}{attrs}>"
                if attr(tag, "type") == "hidden":
                    continue
                with self.subTest(template=template.name, field=attr(tag, "name") or attr(tag, "id")):
                    field_id = attr(tag, "id")
                    labelled = (
                        attr(tag, "aria-label")
                        or (field_id and re.search(rf'<label[^>]*\bfor="{re.escape(field_id)}"', text))
                        # A field inside its own label, after the label's words.
                        or re.search(r"<label[^>]*>[^<]*" + re.escape(tag), text)
                    )
                    self.assertTrue(labelled, tag)

    def test_no_field_is_a_control_only_the_mouse_reaches(self) -> None:
        """A hidden checkbox behind a click handler is a control the keyboard cannot use."""
        for template in TEMPLATES:
            for kind, attrs in FIELD.findall(template.read_text()):
                if 'type="checkbox"' in attrs:
                    with self.subTest(template=template.name):
                        self.assertNotRegex(attrs, r'class="[^"]*\bhidden\b')

    def test_no_template_switches_the_focus_ring_off(self) -> None:
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                self.assertNotIn("outline-none", template.read_text())

    def test_a_link_that_opens_a_tab_says_so_and_hands_nothing_over(self) -> None:
        for template in TEMPLATES:
            for link in re.findall(r"<a\b[^>]*target=\"_blank\"[^>]*>.*?</a>", template.read_text(), re.S):
                with self.subTest(template=template.name, link=link[:60]):
                    self.assertIn('rel="noopener noreferrer"', link)
                    self.assertIn("(opens in a new tab)", link)

    def test_an_image_has_an_alt_and_a_drawing_is_hidden_or_named(self) -> None:
        for template in TEMPLATES:
            text = template.read_text()
            for img in re.findall(r"<img\b[^>]*>", text):
                with self.subTest(template=template.name, img=img[:60]):
                    self.assertIsNotNone(attr(img, "alt"))
            for svg in re.findall(r"<svg\b[^>]*>", text):
                with self.subTest(template=template.name, svg=svg[:60]):
                    self.assertTrue(attr(svg, "aria-hidden") == "true" or attr(svg, "aria-label") or "{{" in svg, svg)

    def test_a_button_of_two_letters_says_the_rest_to_a_screen_reader(self) -> None:
        """In words after the letters, never an aria-label: a name must contain what the button shows."""
        for template in TEMPLATES:
            text = template.read_text()
            with self.subTest(template=template.name):
                self.assertEqual(re.findall(r"<button\b[^>]*>\s*[^<{\s]{1,3}\s*</button>", text), [])


class LayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = (WEB_DIR / "templates" / "base.html").read_text()

    def test_the_skip_link_is_first_and_its_target_takes_focus(self) -> None:
        body = self.base.split("<body>")[1]
        self.assertRegex(body, r'^\s*{% block shell %}\s*<a href="#main" class="skip-link">')
        self.assertIn('<main id="main" class="shell-main" tabindex="-1">', self.base)
        login = (WEB_DIR / "templates" / "login.html").read_text()
        self.assertIn('<main id="main" class="login" tabindex="-1">', login)

    def test_a_toast_is_announced_and_the_dialog_names_itself(self) -> None:
        self.assertIn('<div id="toast" class="toasts" aria-live="polite"></div>', self.base)
        self.assertIn('<dialog id="confirm" class="confirm" aria-labelledby="confirm-title" aria-describedby="confirm-text">', self.base)
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("toast.setAttribute('role', error ? 'alert' : 'status')", script)
        self.assertIn("dialog.showModal()", script)  # a modal dialog keeps focus inside it

    def test_the_nav_names_itself_and_marks_the_open_page(self) -> None:
        self.assertIn('<nav aria-label="Main">', self.base)
        self.assertIn('aria-current="page"', self.base)

    def test_the_stylesheet_honours_reduced_motion_and_never_hides_focus(self) -> None:
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertRegex(css, r":focus-visible \{\s*outline: 2px solid var\(--focus\)")
        # The one place an outline is removed is the page's content, which is not a control.
        self.assertEqual(re.findall(r"([^\n{}]+)\{\s*outline: none;", css), ["  main:focus "])

    def test_the_inbox_says_how_many_are_selected_as_they_are(self) -> None:
        inbox = (WEB_DIR / "templates" / "inbox.html").read_text()
        self.assertIn('<span data-count role="status">0 selected</span>', inbox)
        row = (WEB_DIR / "templates" / "partials" / "inbox_row.html").read_text()
        self.assertIn('<input type="checkbox" name="ids" value="{{ card.id }}"><span class="sr-only">Select {{ card.title }}</span>', row)


class AxeScriptTests(unittest.TestCase):
    def test_the_check_is_in_the_repo_and_in_the_dev_guide(self) -> None:
        script = (REPO / "scripts" / "axe.js").read_text()
        for part in ("axe-core/axe.min.js", "'wcag2aa'", "'wcag22aa'", "colorScheme", "confirmDialog", "process.exit(total ? 1 : 0)"):
            with self.subTest(part=part):
                self.assertIn(part, script)
        self.assertIn("scripts/axe.js", (REPO / "docs" / "DEV.md").read_text())


if __name__ == "__main__":
    unittest.main()
