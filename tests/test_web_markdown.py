"""The PO feed's Markdown subset: what it renders, and what it never lets through.

Pure functions over strings; no store, no server. The page-level half (agent entries rendered,
owner entries shown as typed, the document itself -- which `/po/api/sessions/ID` serves as JSON --
left holding the raw text) is pinned at the bottom against `pages.po_session` with a hand-built
document.
"""

from __future__ import annotations

import copy
import re
import time
import unittest
from typing import Any, ClassVar

from ummanu.web import page_assets, pages
from ummanu.web.markdown import render

ALLOWED_TAGS = {
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "hr",
    "a",
}


def tags(markup: str) -> list[str]:
    return re.findall(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)", markup)


class SubsetTest(unittest.TestCase):
    def test_atx_headings_render_below_the_page_heading(self) -> None:
        self.assertEqual(render("# One"), "<h3>One</h3>")
        self.assertEqual(render("## Two ##"), "<h4>Two</h4>")
        self.assertEqual(render("### Three"), "<h5>Three</h5>")
        for level in range(4, 7):
            self.assertEqual(render("#" * level + " Deep"), "<h6>Deep</h6>")
        self.assertEqual(render("####### seven"), "<p>####### seven</p>")
        self.assertEqual(render("#hashtag"), "<p>#hashtag</p>")

    def test_blank_lines_separate_paragraphs_and_single_newlines_are_breaks(self) -> None:
        self.assertEqual(render("one\ntwo\n\nthree"), "<p>one<br>two</p><p>three</p>")
        self.assertEqual(render("a\r\nb"), "<p>a<br>b</p>")

    def test_bold_and_italic(self) -> None:
        self.assertEqual(render("**bold**"), "<p><strong>bold</strong></p>")
        self.assertEqual(render("*it* and _it_"), "<p><em>it</em> and <em>it</em></p>")
        self.assertEqual(render("**a _b_ c**"), "<p><strong>a <em>b</em> c</strong></p>")
        self.assertEqual(render("snake_case_name and 2 * 3 * 4"), "<p>snake_case_name and 2 * 3 * 4</p>")

    def test_inline_code_is_verbatim(self) -> None:
        self.assertEqual(render("run `a *b* [c](http://x)`"), "<p>run <code>a *b* [c](http://x)</code></p>")

    def test_fenced_code_is_verbatim_with_the_language_as_a_class(self) -> None:
        self.assertEqual(
            render("```python\n**not bold**\n  # kept\n\n```"),
            '<pre><code class="language-python">**not bold**\n  # kept\n</code></pre>',
        )
        self.assertEqual(render("```\nx\n```\nafter"), "<pre><code>x</code></pre><p>after</p>")

    def test_unordered_and_ordered_lists_with_one_nested_level(self) -> None:
        self.assertEqual(render("- a\n* b"), "<ul><li>a</li><li>b</li></ul>")
        self.assertEqual(render("1. one\n2. two"), "<ol><li>one</li><li>two</li></ol>")
        self.assertEqual(
            render("- a\n  1. x\n  2. y\n- b\n    - deeper\n  continued"),
            "<ul><li>a<ol><li>x</li><li>y</li></ol></li><li>b<ul><li>deeper<br>continued</li></ul></li></ul>",
        )
        self.assertEqual(render("- a\n\n- b"), "<ul><li>a</li><li>b</li></ul>")

    def test_blockquotes(self) -> None:
        self.assertEqual(
            render("> quoted\n> **line**"), "<blockquote><p>quoted<br><strong>line</strong></p></blockquote>"
        )
        self.assertEqual(
            render("> a\n> > b"), "<blockquote><p>a</p><blockquote><p>b</p></blockquote></blockquote>"
        )

    def test_horizontal_rule(self) -> None:
        self.assertEqual(render("above\n\n---\n\nbelow"), "<p>above</p><hr><p>below</p>")

    def test_links_and_bare_urls(self) -> None:
        self.assertEqual(
            render("see [the *docs*](https://example.com/a?b=1&c=2)"),
            '<p>see <a href="https://example.com/a?b=1&amp;c=2" rel="noopener noreferrer" target="_blank">'
            "the <em>docs</em></a></p>",
        )
        self.assertEqual(
            render("at https://example.com/a_b_c."),
            '<p>at <a href="https://example.com/a_b_c" rel="noopener noreferrer" target="_blank">'
            "https://example.com/a_b_c</a>.</p>",
        )
        self.assertIn('href="mailto:po@example.com"', render("[mail](mailto:po@example.com)"))

    def test_pipe_tables_are_preformatted(self) -> None:
        self.assertEqual(
            render("| a | b |\n|---|---|\n| 1 | 2 |"),
            '<pre class="table">| a | b |\n|---|---|\n| 1 | 2 |</pre>',
        )


class SafetyTest(unittest.TestCase):
    def assert_only_allowed_markup(self, markup: str) -> None:
        self.assertLessEqual(set(tags(markup)), ALLOWED_TAGS, markup)
        self.assertNotRegex(markup, r"(?i)<[^>]*\s(on\w+|style|src)\s*=")
        self.assertNotIn("<img", markup.lower())

    def test_raw_html_is_text(self) -> None:
        for raw in (
            "<script>alert(1)</script>",
            '<img src=x onerror="alert(1)">',
            '<a href="javascript:alert(1)">x</a>',
            '<iframe src="https://evil"></iframe>',
            "&lt;script&gt; &#60;b&#62;",
            "**<b>bold</b>**",
            "- <script>x</script>\n> <svg onload=alert(1)>",
        ):
            with self.subTest(raw=raw):
                markup = render(raw)
                self.assert_only_allowed_markup(markup)
                self.assertNotIn("<script", markup)
        self.assertEqual(render("&lt;b&gt;"), "<p>&amp;lt;b&amp;gt;</p>")

    def test_only_http_https_and_mailto_become_links(self) -> None:
        for target in (
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "//evil.example/x",
            "java%0ascript:alert(1)",
            "javascript&#58;alert(1)",
            "&#106;avascript:alert(1)",
            "\tjavascript:alert(1)",
            "file:///etc/passwd",
            "evil.example",
        ):
            with self.subTest(target=target):
                markup = render(f"[x]({target})")
                self.assertNotIn("<a", markup)
                self.assertNotIn("href", markup)
        markup = render("[x](java\nscript:alert(1))")
        self.assertNotIn("<a", markup)

    def test_links_carry_rel_and_target_consistently(self) -> None:
        markup = render("[a](http://a.example) https://b.example [c](mailto:c@example.com)")
        anchors = re.findall(r"<a [^>]*>", markup)
        self.assertEqual(len(anchors), 3)
        for anchor in anchors:
            self.assertIn('rel="noopener noreferrer"', anchor)
            self.assertIn('target="_blank"', anchor)

    def test_no_attribute_can_be_broken_out_of(self) -> None:
        for raw in (
            '[x](https://e.example/"onmouseover="alert(1))',
            "[x](https://e.example/'onmouseover='alert(1))",
            "[x](https://e.example/`onmouseover=`alert(1))",
            'https://e.example/"><script>alert(1)</script>',
            '```x"onclick="y\ncode\n```',
            '[a"b](https://e.example)',
        ):
            with self.subTest(raw=raw):
                markup = render(raw)
                self.assert_only_allowed_markup(markup)
                for value in re.findall(r'=\s*"([^"]*)"', markup):
                    self.assertNotIn("`", value)
                    self.assertNotIn("<", value)
                for anchor in re.findall(r"<a [^>]*>", markup):
                    self.assertRegex(
                        anchor, r'^<a href="[^"`<>\s]*" rel="noopener noreferrer" target="_blank">$'
                    )

    def test_malformed_markers_degrade_to_text_without_swallowing_the_rest(self) -> None:
        self.assertEqual(
            render("a lone ** marker\n**then bold**"), "<p>a lone ** marker<br><strong>then bold</strong></p>"
        )
        self.assertEqual(render("half *open and `tick"), "<p>half *open and `tick</p>")
        self.assertEqual(render("**a *b** c*"), "<p><strong>a *b</strong> c*</p>")
        self.assertEqual(
            render("```\nnever closed\n\n# still a heading"),
            "<p>```<br>never closed</p><h3>still a heading</h3>",
        )
        self.assertEqual(render("[text](unclosed **x**"), "<p>[text](unclosed <strong>x</strong></p>")

    def test_pathological_input_renders_in_bounded_time(self) -> None:
        samples = (
            "*" * 10_000,
            "a " + "*" * 10_000,
            "**x " * 5_000,
            "_a " * 5_000,
            "`" * 10_000,
            "[a](" * 5_000,
            "[a](http://x" * 2_000,
            "&gt;" * 10_000,
            ">" * 10_000 + " deep",
            "\n".join(">" * n + " q" for n in range(1, 400)),
            "\n".join(" " * n + "- item" for n in range(2_000)),
            "```py\n" * 5_000,
            "# " + " " * 10_000 + "x",
            "- - - " * 3_000 + "x",
            "https://" * 5_000,
            "| " * 10_000 + "\n" + "| " * 10_000,
        )
        for sample in samples:
            with self.subTest(sample=sample[:20]):
                started = time.perf_counter()
                markup = render(sample)
                self.assertLess(time.perf_counter() - started, 1.0)
                self.assert_only_allowed_markup(markup)


class FeedPageTest(unittest.TestCase):
    DOCUMENT: ClassVar[dict[str, Any]] = {
        "session": {"session_id": "abcdef0123456789", "cli": "claude", "model": "fable", "state": "idle"},
        "turns": [{"seq": 1, "state": "completed"}],
        "feed": [
            {"turn_seq": 1, "role": "owner", "text": "**as typed** <b>x</b>\nline"},
            {"turn_seq": 1, "role": "agent", "text": "## Plan\n- **one**\n- <script>alert(1)</script>"},
        ],
        "running": False,
    }

    def test_agent_entries_are_rendered_and_owner_entries_are_shown_as_typed(self) -> None:
        document = copy.deepcopy(self.DOCUMENT)
        page = pages.po_session(document, request_id="req-1")
        self.assertEqual(document, self.DOCUMENT)
        self.assertIn(
            '<div class="text">**as typed** &lt;b&gt;x&lt;/b&gt;\nline</div>',
            page,
        )
        self.assertIn(
            '<div class="md"><h4>Plan</h4><ul><li><strong>one</strong></li>'
            "<li>&lt;script&gt;alert(1)&lt;/script&gt;</li></ul></div>",
            page,
        )
        self.assertNotIn("<script>alert(1)", page)

    def test_feed_is_newest_first_and_composer_is_above_history(self) -> None:
        document = copy.deepcopy(self.DOCUMENT)
        document["turns"] = [
            {"seq": 1, "state": "completed"},
            {"seq": 2, "state": "completed"},
        ]
        document["feed"] = [
            {"turn_seq": 1, "role": "owner", "text": "old question"},
            {"turn_seq": 1, "role": "agent", "text": "old answer"},
            {"turn_seq": 2, "role": "owner", "text": "new question"},
            {"turn_seq": 2, "role": "agent", "text": "new answer"},
        ]

        page = pages.po_session(document, request_id="req-1")

        self.assertLess(page.index('id="po-send"'), page.index('id="po-feed"'))
        self.assertLess(page.index("new answer"), page.index("new question"))
        self.assertLess(page.index("new question"), page.index("old answer"))
        self.assertLess(page.index("old answer"), page.index("old question"))

    def test_rendered_agent_block_does_not_pre_wrap_while_owner_text_does(self) -> None:
        self.assertIn(".po-entry .text { white-space: pre-wrap;", page_assets.STYLE)
        md_rules = [rule for rule in page_assets.STYLE.splitlines() if ".po-entry .md" in rule]
        self.assertTrue(md_rules)
        self.assertFalse([rule for rule in md_rules if "pre-wrap" in rule])
        self.assertTrue([rule for rule in md_rules if ".md pre" in rule and "overflow-x:auto" in rule])

    def test_feed_content_is_constrained_to_the_panel_width(self) -> None:
        self.assertIn(".po-feed {", page_assets.STYLE)
        self.assertIn("min-width:0; max-width:100%;", page_assets.STYLE)
        self.assertIn(".po-entry .md { overflow-wrap:anywhere; word-break:break-word;", page_assets.STYLE)
        self.assertIn(".po-entry .md pre { white-space:pre; max-width:100%; min-width:0; overflow-x:auto;", page_assets.STYLE)
        self.assertIn(".panel > .body { padding: .75rem .9rem; min-width:0; max-width:100%; }", page_assets.STYLE)


class PageShellTest(unittest.TestCase):
    def test_shell_has_a_persistent_sun_moon_theme_toggle(self) -> None:
        page = pages.error(404, "missing", "not here")

        self.assertIn('id="theme-toggle"', page)
        self.assertIn("☀", page)
        self.assertIn("☾", page)
        self.assertIn("ummanu.web.theme", page)
        self.assertIn("document.documentElement.dataset.theme", page)

    def test_explicit_light_and_dark_themes_pin_the_browser_color_scheme(self) -> None:
        self.assertIn(':root[data-theme="light"] { color-scheme: light; }', page_assets.STYLE)
        self.assertIn(':root[data-theme="dark"] {', page_assets.STYLE)
        self.assertIn("color-scheme: dark;", page_assets.STYLE)


if __name__ == "__main__":
    unittest.main()
