"""The page is one screen: the library workflow and nothing else.

Two things can silently go wrong with a page: the markup keeps an element
nothing uses, or the script reaches for an element the markup does not have.
The last test below catches the second by loading the real script against the
real markup.

Skipped, not failed, when Node is missing.
"""

import os
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "web")
NODE = shutil.which("node")


def read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as handle:
        return handle.read()


#: Evaluates app.js in a vm context. `querySelector("#x")` answers null unless
#: the markup has id="x", so a script that wires an element the page lacks
#: throws at load and Node exits non-zero.
LOADER = r"""
const fs = require("fs");
const vm = require("vm");

const [appPath, htmlPath] = process.argv.slice(-2);
const html = fs.readFileSync(htmlPath, "utf8");
const ids = new Set(Array.from(html.matchAll(/\bid="([^"]+)"/g), (m) => m[1]));

function fakeElement() {
  return {
    innerHTML: "", textContent: "", value: "", placeholder: "", title: "",
    hidden: false, checked: false, dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    remove() {}, scrollIntoView() {}, focus() {}, click() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; },
  };
}

const found = new Map();
const document = {
  querySelector(sel) {
    const m = /^#([\w-]+)$/.exec(sel);
    if (m && !ids.has(m[1])) return null;
    if (!found.has(sel)) found.set(sel, fakeElement());
    return found.get(sel);
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return fakeElement(); },
};

const sandbox = {
  document, console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: () => new Promise(() => {}),
  FormData: class { forEach() {} },
  URL, URLSearchParams,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx, { filename: "app.js" });

const missing = ["library", "loadLibrary", "$", "$$", "api", "toast", "reportError",
                 "escapeHtml", "modal", "busy", "formatDuration"]
  .filter((name) => vm.runInContext("typeof " + name, ctx) === "undefined");
if (missing.length) {
  console.error("missing top-level names: " + missing.join(", "));
  process.exit(2);
}
"""


class PageMarkupTest(unittest.TestCase):
    def test_page_has_no_leftover_navigation_or_panels(self):
        html = read("index.html")
        for gone in ('id="tabs"', "stat-tracked", "stat-disk", "tab-performers",
                     "tab-catalogue", 'id="drawer"'):
            self.assertNotIn(gone, html)
        self.assertIn('id="library"', html)

    def test_each_step_says_what_it_does_first_thing_in_its_body(self):
        html = " ".join(read("index.html").split())
        for key, line in (
                ("scan", "Reads your untagged videos from Pronoisseur. Where a "
                         "folder name has a StashDB scene id, fetches that scene "
                         "and checks it against the video's fingerprint. Nothing "
                         "is written."),
                ("identify", "Sends each remaining video's fingerprints to "
                             "StashDB, 40 at a time. Nothing is written."),
                ("review", "Writes only the ticked videos to Pronoisseur. "
                           "StashDB is only read.")):
            self.assertRegex(
                html, r'id="library-body-%s">\s*<p class="hint step-line">%s</p>'
                % (key, re.escape(line)))

    def test_expand_and_collapse_sit_directly_above_the_first_model(self):
        html = read("index.html")
        # Not in the filter row any more.
        filter_row = html[html.index('class="lib-filter"'):
                          html.index('id="library-preview"')]
        self.assertNotIn("lib-review-expand", filter_row)
        order = [html.index(marker) for marker in (
            'id="library-preview"', 'id="lib-review-fresh"',
            'id="lib-review-expand-row"', 'id="lib-review-expand"',
            'id="lib-review-collapse"', 'id="library-questions"')]
        self.assertEqual(order, sorted(order))
        # Directly above: no other element sits between the row's buttons and
        # the list.
        between = html[html.index('id="lib-review-collapse"'):
                       html.index('id="library-questions"')]
        between = between.split("</button>", 1)[1]
        self.assertEqual(between.count('id="'), 0)

    def test_the_tool_is_named_for_what_it_is_and_who_it_is_for(self):
        # "X for Y", the way a third-party tool is named: its own product,
        # not one Pronoisseur ships.
        html = read("index.html")
        self.assertIn("<title>StashDB Tagger for Pronoisseur</title>", html)
        self.assertIn('>StashDB Tagger <span>for Pronoisseur</span></div>', html)
        with open(os.path.join(os.path.dirname(WEB), "README.md"),
                  encoding="utf-8") as fh:
            heading = next(line for line in fh if line.startswith("#"))
        self.assertEqual(heading.strip(), "# StashDB Tagger for Pronoisseur")

    def test_the_mark_is_the_tab_icon_and_sits_beside_the_name(self):
        html = read("index.html")
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">',
                      html)
        # Decorative beside the name, so it says nothing to a screen reader.
        self.assertIn('<div class="brand"><img class="brand-mark" src="/favicon.svg"'
                      ' alt="" width="24" height="24">StashDB Tagger'
                      " <span>for Pronoisseur</span></div>", html)
        self.assertIn(".brand-mark {", read("style.css"))

    def test_the_readme_links_to_pronoisseur_and_stashdb(self):
        # A stranger lands on the README first: it says what Pronoisseur is and
        # where to get it before anything else, and links each place a key or
        # token comes from.
        with open(os.path.join(os.path.dirname(WEB), "README.md"),
                  encoding="utf-8") as fh:
            readme = fh.read()
        intro = readme[readme.index("\n# "):readme.index("\n## ")]
        for link in ("[Pronoisseur](https://pronoisseur.com/)",
                     "[StashDB](https://stashdb.org/)",
                     "multi-monitor video wall player for Windows"):
            self.assertIn(link, intro)
        needs = readme[readme.index("## What it needs"):
                       readme.index("## Install and run")]
        for link in ("[Pronoisseur](https://pronoisseur.com/)",
                     "[StashDB](https://stashdb.org/)"):
            self.assertIn(link, needs)
        connect = readme[readme.index("1. **Connect.**"):readme.index("2. **Scan.**")]
        for link in ("(https://stashdb.org/)",
                     "(https://pronoisseur.com/help/14-connected-apps/)"):
            self.assertIn(link, connect)

    def test_the_readme_shows_the_tool_and_every_picture_ships(self):
        # A stranger decides from the README whether to clone, so it shows the
        # page. Every picture it points at must be in the repo (a broken image
        # on GitHub reads as an abandoned project), and a screenshot must carry
        # pixels only: PNG text chunks are where tools leave prompts and paths.
        root = os.path.dirname(WEB)
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        local = [p for p in (re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", readme)
                             + re.findall(r'src="([^"]+)"', readme))
                 if not p.startswith(("http://", "https://"))]
        intro = readme[readme.index("\n# "):readme.index("\n## ")]
        self.assertIn("](docs/screenshots/matched.png)", intro)
        for name in ("steps", "connect", "groups", "choose"):
            self.assertIn("docs/screenshots/%s.png" % name, local)
        allowed = {b"IHDR", b"IDAT", b"IEND", b"PLTE", b"tRNS", b"sRGB", b"gAMA",
                   b"cHRM", b"pHYs"}
        for rel in local:
            path = os.path.join(root, rel)
            self.assertTrue(os.path.isfile(path), rel)
            if not rel.endswith(".png"):
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", rel)
            at = 8
            while at < len(data):
                size = int.from_bytes(data[at:at + 4], "big")
                self.assertIn(data[at + 4:at + 8], allowed, rel)
                at += 12 + size

    def test_the_readme_opens_with_the_tile(self):
        with open(os.path.join(os.path.dirname(WEB), "README.md"),
                  encoding="utf-8") as fh:
            first = fh.readline()
        self.assertIn('src="web/icon.svg"', first)

    def test_both_icons_draw_the_same_mark(self):
        # The tab icon is the mark alone; the README's tile is the mark on a
        # dark square. Drawn twice, so a change to one must reach the other.
        def mark(name):
            return re.search(r'<mask id="tag".*?</mask>', read(name), re.S).group(0)
        self.assertEqual(mark("favicon.svg"), mark("icon.svg"))

    def test_the_icons_are_plain_svg_that_fetch_nothing(self):
        for name in ("favicon.svg", "icon.svg"):
            text = read(name)
            root = ET.fromstring(text)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg", name)
            self.assertNotIn("<script", text.lower(), name)
            self.assertNotIn("href", text, name)
            # The namespace is the only address in the file, and every paint
            # or mask points inside it.
            self.assertEqual(re.findall(r"https?://[^\"'\s]+", text),
                             ["http://www.w3.org/2000/svg"], name)
            self.assertEqual([u for u in re.findall(r"url\(([^)]*)\)", text)
                              if not u.startswith("#")], [], name)
            # Pronoisseur's pink runs through the mark.
            self.assertIn("#FF4D9D", text, name)

    def test_the_colours_are_pronoisseurs(self):
        # The app's hot pink accent, and its own red, amber and green for the
        # three kinds of card: No match is red, not grey.
        css = read("style.css")
        root = css[css.index(":root {"):css.index("}")]
        for token in ("--accent: #FF4D9D;", "--bad: #E84B4B;", "--warn: #FFB84A;",
                      "--good: #4FB477;"):
            self.assertIn(token, root)
        none = re.search(r"\.kind-badge\.none \{([^}]*)\}", css).group(1)
        self.assertIn("color: var(--bad)", none)

    def test_the_last_step_opens_with_how_tagging_works(self):
        html = read("index.html")
        body = html[html.index('id="library-body-review"'):]
        how = body[:body.index('id="library-mapping"')]
        self.assertIn('<details class="how-box" id="lib-how" open>', how)
        self.assertEqual(how.count("<li>"), 6)
        text = " ".join(re.sub(r"<[^>]+>", " ", how).split())
        for words in ("How this works", "your file", "Matched",
                      "Untick any match that's wrong", "Choose a match",
                      "Use this match", "None of these match", "Recommended",
                      "Use recommended", "only where its length matches",
                      "moves to Matched, ticked", "No match", "Pending hashing",
                      "Search for it by name",
                      "Tag writes", "the list is showing",
                      "The tabs and the filter narrow both", "Nothing is removed",
                      "never opened or changed"):
            self.assertIn(words, text)

    def test_one_solid_pink_and_a_ring_for_the_keyboard(self):
        css = read("style.css")
        # The button beside the main one: outlined in the accent, not filled.
        self.assertRegex(css, r"\.secondary \{[^}]*border-color: var\(--accent\)")
        # Every control shows where the keyboard is.
        self.assertRegex(css, r"button:focus-visible,[^{]*summary:focus-visible[^{]*"
                              r"\{[^}]*outline: 2px solid var\(--accent\)")

    def test_the_current_step_is_marked_on_its_head_not_round_its_list(self):
        css = read("style.css")
        self.assertNotRegex(css, r"\.lib-section\.is-current \{[^}]*border-color")
        self.assertIn(".lib-section.is-current > .lib-head {", css)
        # The model count badges inside the list stay grey.
        self.assertNotIn(".lib-section.is-current .lib-badge", css)
        self.assertIn(".lib-section.is-current > .lib-head .lib-badge", css)

    def test_wide_screens_set_the_fields_beside_the_pictures(self):
        css = read("style.css")
        start = css.index("@media (min-width: 1280px)")
        block = css[start:css.index("\n}", start)]
        compare = block[block.index(".compare {"):]
        self.assertIn("grid-template-columns", compare[:compare.index("}")])
        self.assertIn(".compare > .compare-rows", block)

    def test_corners_come_from_pronoisseurs_radius_scale(self):
        css = read("style.css")
        root = css[css.index(":root {"):css.index("}")]
        for token in ("--radius-sm: 4px;", "--radius: 7px;", "--radius-lg: 12px;"):
            self.assertIn(token, root)
        allowed = {"var(--radius-sm)", "var(--radius)", "var(--radius-lg)",
                   "50%", "999px"}
        used = set(re.findall(r"border-radius:\s*([^;]+);", css))
        self.assertEqual(used - allowed, set())

    def test_the_category_picker_folds_like_how_this_works(self):
        js = read("app.js")
        self.assertIn('<details class="fold"', js)
        self.assertRegex(read("style.css"), r"\.how-box, \.fold \{")

    def test_script_only_calls_library_routes(self):
        routes = re.findall(r"""api\(\s*[`"'](/api/[^`"'?]*)""", read("app.js"))
        self.assertTrue(routes)
        self.assertEqual([r for r in routes if not r.startswith("/api/library/")], [])

    def test_progress_banner_styling_survives(self):
        css = read("style.css")
        self.assertIn(".estimate {", css)
        self.assertIn(".estimate .figure", css)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_script_loads_against_the_real_markup(self):
        result = subprocess.run(
            [NODE, "-e", LOADER, os.path.join(WEB, "app.js"),
             os.path.join(WEB, "index.html")],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
class ReadmeRulesAndLicenceTest(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(os.path.dirname(WEB), name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_readme_states_stashdbs_rules(self):
        readme = self._read("README.md")
        rules = readme[readme.index("## StashDB's rules"):]
        rules = rules[:rules.index("\n## ", 4)]
        for needle in ("(https://discourse.stashapp.cc/t/rules-for-api-access/21336)",
                       "User-Agent", "60 requests a minute", "scene search",
                       "commercial"):
            self.assertIn(needle, rules)

    def test_the_code_is_mit_licensed(self):
        licence = self._read("LICENSE")
        self.assertTrue(licence.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 dirty-ninja", licence)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", licence)
        readme = self._read("README.md")
        self.assertIn("## Licence", readme)
        self.assertIn("[MIT](LICENSE)", readme)
