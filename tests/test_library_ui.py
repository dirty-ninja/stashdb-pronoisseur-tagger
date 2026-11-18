"""The Library workflow strip, exercised against a stub DOM.

``web/app.js`` is a plain browser script with no module boundary, so the harness
below evaluates the whole file inside a Node ``vm`` context whose ``document``
is a dictionary of fake elements. That is enough for the one thing worth
asserting here: given a ``/api/library/state`` payload, which of the five steps
is done, which is current, which is dim, and what each one says.

The pills only show the result of the first action, so a large library of
unidentified videos would read as a handful. The strip says how big the library
is and how far through it the user has got, and the states it can be in are the
thing that can silently go wrong.

Skipped, not failed, when Node is missing - the Python suite must stay runnable
without a JavaScript toolchain.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "web")
APP_JS = os.path.join(WEB, "app.js")
NODE = shutil.which("node")

#: Loads app.js into a stub DOM and leaves the context ready to drive. Every fake
#: element is shared by selector, so `$("#library-workflow")` hands back the same
#: object the render wrote into - which is also how a driver below can reach the
#: Show-more button's click handler. The two harnesses bolt their own driver
#: script onto the end of this one.
DOM_STUB = r"""
const fs = require("fs");
const vm = require("vm");

const [appPath, inputPath] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));

function fakeElement() {
  return {
    innerHTML: "", textContent: "", value: "", placeholder: "", title: "",
    hidden: false, checked: false, dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    remove() {}, scrollIntoView() {}, focus() {}, click() {},
    // The strip wires its scroll-to buttons through querySelectorAll on the
    // rendered HTML, which a string-only stub cannot parse. Returning nothing
    // leaves them unwired, which is why the test asserts the targets are in
    // the markup rather than that clicking one scrolls.
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; },
  };
}

const bySelector = new Map();
const document = {
  querySelector(sel) {
    if (!bySelector.has(sel)) bySelector.set(sel, fakeElement());
    return bySelector.get(sel);
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return fakeElement(); },
};

const sandbox = {
  document,
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  // Never settles: the initial `loadLibrary()` at the end of the script is not what is under test, and
  // a rejection here would only fill the fake toast list with noise.
  fetch: () => new Promise(() => {}),
  FormData: class { forEach() {} },
  URL, URLSearchParams,
};
sandbox.window = sandbox;
// The page listens on `window` for scroll and resize. Collected, so a driver
// can fire one by hand; `requestAnimationFrame` runs at once, which keeps the
// once-per-frame throttle in play without a clock.
const windowListeners = {};
sandbox.addEventListener = (type, fn) => {
  (windowListeners[type] = windowListeners[type] || []).push(fn);
};
sandbox.requestAnimationFrame = (fn) => { fn(); return 0; };
sandbox.__windowListeners = windowListeners;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx, { filename: "app.js" });

// `library`, `libraryWorkflowSteps` and friends are top-level `const`/`function`
// declarations, so they live in the context's global lexical scope rather than
// on `globalThis` - reachable from a second script in the same context, which
// is what this is.
sandbox.__input = input;
"""

#: Drives the workflow strip: one state in, the five steps and their markup out.
HARNESS = DOM_STUB + r"""
const answer = vm.runInContext(`
  library.state = __input.state;
  library.preview = __input.preview;
  const steps = libraryWorkflowSteps(library.state, library.preview);
  // What loadLibrary does before it draws anything. Every render downstream
  // reads the steps off the library object rather than working them out again,
  // so the strip, the section heads and the sticky bar cannot disagree about
  // which step you are on. (No backticks in here: this is inside a template
  // literal, and one would end it.)
  library.steps = steps;
  renderLibraryWorkflow();
  JSON.stringify({
    steps,
    html: document.querySelector("#library-workflow").innerHTML,
  });
`, ctx);
process.stdout.write(answer);
"""

#: Drives `#library-questions` over a sequence of frames, so a *second* render of
#: the same state can be told apart from the first. Each frame may stamp a
#: sentinel into the box before rendering: if the render is skipped the sentinel
#: is still there afterwards, which is the only way a string-only stub can prove
#: "nothing was rebuilt".
#:
#: `onclick` is cleared before every render because the fake elements are shared
#: by selector: a Show-more handler wired in frame 1 would otherwise still be
#: hanging off the button in frame 3, and the test would not notice it had gone.
#:
#: `library.steps` is deliberately left empty here, which makes
#: `renderLibraryQuestions` treat step 4's section as open — these frames are
#: about the guards inside the box, and the section's own open/closed rule is
#: `LibrarySectionsTest`'s.
QUESTIONS_HARNESS = DOM_STUB + r"""
const answer = vm.runInContext(`
  const box = document.querySelector("#library-questions");
  const hint = document.querySelector("#lib-review-filter-hint");
  const fresh = document.querySelector("#lib-review-fresh");
  const expandRow = document.querySelector("#lib-review-expand-row");
  const moreOf = (key) => document.querySelector("#lib-more-" + key);
  // The model groups whose Show-more buttons a test watches, by group id.
  const watched = __input.watch || ["nomodel"];
  const wiring = () => Object.fromEntries(
    watched.map((key) => [key, !!moreOf(key).onclick]));
  const frames = [];
  // The clock the 5-second rule reads. It stands still unless a frame moves it,
  // so frames inside one test are "a moment apart" by default.
  let clock = 1000000;
  Date.now = () => clock;
  // What the user is doing in the list, which a stub DOM cannot do: a field in
  // it has focus, or a Search by name panel is open. And the group heads' boxes
  // before and after the list is rebuilt, for the scroll anchor.
  const fake = { panel: false, geometry: null, phase: "before" };
  const scrolls = [];
  window.scrollBy = (x, y) => scrolls.push([x, y]);
  let html = box.innerHTML;
  Object.defineProperty(box, "innerHTML", {
    configurable: true, get() { return html; },
    set(v) { html = v; fake.phase = "after"; },
  });
  box.contains = (node) => node === document.activeElement;
  box.querySelectorAll = (sel) => {
    if (fake.panel && sel.includes("assisted")) return [{}];
    if (fake.geometry && sel === "[data-group]") {
      return Object.keys(fake.geometry[fake.phase]).map((id) => {
        const el = { dataset: { group: id },
          getBoundingClientRect() {
            const [top, bottom] = fake.geometry[fake.phase][id];
            return { top, bottom };
          } };
        el.querySelector = () => el;
        return el;
      });
    }
    return [];
  };
  for (const frame of __input.frames) {
    fake.phase = "before";
    if ("now" in frame) clock = frame.now;
    if ("focus" in frame) {
      document.activeElement = frame.focus ? { tagName: frame.focus } : null;
    }
    if ("panel" in frame) fake.panel = frame.panel;
    if ("geometry" in frame) fake.geometry = frame.geometry;
    // What the user did to the page before this frame, which a stub DOM
    // cannot click: switch a match, step a frame, open or shut a group.
    if (frame.tab) Object.assign(library.reviewTab, frame.tab);
    if (frame.frameAt) Object.assign(library.reviewFrame, frame.frameAt);
    if (frame.groupOpen) Object.assign(library.reviewOpen, frame.groupOpen);
    if ("expandAll" in frame) {
      // Expand all / Collapse all, which render on their own.
      setAllReviewGroups(frame.expandAll);
      frames.push({ html: box.innerHTML, wired: wiring(), clicked: false,
                    shown: Object.assign({}, library.shown),
                    filterHint: hint.innerHTML, fresh: fresh.innerHTML,
                    expandHidden: expandRow.hidden });
      continue;
    }
    if (frame.showFresh) {
      // The "show them" button on the line that holds new rows back mid-run.
      const btn = document.querySelector("#lib-review-fresh-show");
      const handler = btn.onclick;
      btn.onclick = null;
      if (handler) handler();
      frames.push({ html: box.innerHTML, wired: wiring(), clicked: !!handler,
                    shown: Object.assign({}, library.shown),
                    filterHint: hint.innerHTML, fresh: fresh.innerHTML,
                    expandHidden: expandRow.hidden });
      continue;
    }
    if (frame.click) {
      // Clear it first, then call the handler we saved: whatever is on the
      // button afterwards was wired by the render the click triggered, not left
      // over from the render before it.
      const btn = moreOf(frame.click);
      const handler = btn.onclick;
      btn.onclick = null;
      if (handler) handler();
      frames.push({ html: box.innerHTML, wired: wiring(), clicked: !!handler,
                    shown: Object.assign({}, library.shown),
                    filterHint: hint.innerHTML, fresh: fresh.innerHTML,
                    expandHidden: expandRow.hidden });
      continue;
    }
    if (frame.sentinel) box.innerHTML = frame.sentinel;
    if (frame.state) library.state = frame.state;
    if ("accepted" in frame) library.accepted = frame.accepted;
    // Cleared before the filter is applied, because setReviewFilter renders on
    // its own and the render below it would then be skipped as a no-op.
    watched.forEach((key) => { moreOf(key).onclick = null; });
    if ("filter" in frame) setReviewFilter(frame.filter);
    renderLibraryQuestions();
    frames.push({ html: box.innerHTML, wired: wiring(), clicked: false,
                  shown: Object.assign({}, library.shown),
                  filterHint: hint.innerHTML, fresh: fresh.innerHTML,
                    expandHidden: expandRow.hidden });
  }
  JSON.stringify({ frames, scrolls, pollMs: LIBRARY_POLL_MS, page: QUESTION_PAGE });
`, ctx);
process.stdout.write(answer);
"""

#: Drives the whole step-driven half of the page: the strip, the five collapsible
#: sections, the review box and the sticky bar, in the order `loadLibrary` draws
#: them. One frame is one paint plus, optionally, one thing the user did to it.
TAB_HARNESS = DOM_STUB + r"""
const answer = vm.runInContext(`
  const KEYS = ["connect", "scan", "identify", "review"];
  const bar = document.querySelector("#library-bar");
  const box = document.querySelector("#library-questions");
  const frames = [];

  const render = () => {
    renderLibraryIdentify();
    renderLibrarySteps();
  };
  const snapshot = (extra) => {
    const sections = {};
    for (const key of KEYS) {
      sections[key] = {
        open: document.querySelector("#library-section-" + key).dataset.open === "1",
        state: document.querySelector("#library-section-" + key).dataset.state,
        klass: document.querySelector("#library-section-" + key).className,
        head: document.querySelector("#library-head-" + key).innerHTML,
        hidden: !!document.querySelector("#library-body-" + key).hidden,
      };
    }
    return Object.assign({
      sections, questions: box.innerHTML,
      bar: bar.innerHTML, barHidden: !!bar.hidden,
      identify: document.querySelector("#library-identify").innerHTML,
    }, extra || {});
  };

  for (const frame of __input.frames) {
    if (frame.state) library.state = frame.state;
    if ("preview" in frame) library.preview = frame.preview;
    if ("accepted" in frame) library.accepted = frame.accepted;
    // Kept fresh before any interaction, because a click reads the step it is
    // acting on out of here.
    library.steps = libraryWorkflowSteps(library.state, library.preview);
    if (frame.sentinel) box.innerHTML = frame.sentinel;
    let clicked = false;
    if (frame.toggle) {
      const head = document.querySelector("#library-head-" + frame.toggle);
      clicked = !!head.onclick;
      if (head.onclick) head.onclick();
    }
    if (frame.open) openLibrarySection(frame.open);
    if (frame.dismiss) dismissLibraryError(frame.dismiss);
    if (frame.render !== false) render();
    frames.push(snapshot({ clicked }));
  }
  JSON.stringify({ frames });
`, ctx);
process.stdout.write(answer);
"""

#: Drives `renderLibraryPreview`, which the three harnesses above cannot: it is
#: the one library render that fetches before it draws. `fetch` is replaced with
#: one that answers the frame's canned `/api/library/preview` payload, so the box
#: under the Apply section can be asserted the same way as everything else.
#:
#: The bar is rendered from the same frame, because "nothing left to write" has
#: to be said in both places or the page contradicts itself: the section says the
#: queue is empty and the bar goes on counting noughts.
PREVIEW_HARNESS = DOM_STUB + r"""
const box = document.querySelector("#library-preview");
const bar = document.querySelector("#library-bar");
const list = document.querySelector("#library-questions");
const kinds = document.querySelector("#lib-review-kinds");
sandbox.fetch = () => Promise.resolve(
  { ok: true, json: () => Promise.resolve(sandbox.__answer) });

const drive = async () => {
  const frames = [];
  for (const frame of input.frames) {
    sandbox.__frame = frame;
    sandbox.__answer = frame.preview;
    // A sentinel left standing after the render proves the render was skipped.
    if (frame.sentinel) box.innerHTML = frame.sentinel;
    // The order `loadLibrary` uses: the preview lands first and the steps, the
    // list and the bar are worked out from it, so the bar can never count rows
    // the list is not showing. `unpick`, `groupOpen` and `kind` stand in for
    // the user unticking rows, opening a group and picking a chip, which a stub
    // DOM cannot click.
    await vm.runInContext(`
      library.state = __frame.state;
      if (__frame.now != null) { const t = __frame.now; Date.now = () => t; }
      if (__frame.unpick) library.unpicked = new Set(__frame.unpick);
      if (__frame.groupOpen) Object.assign(library.reviewOpen, __frame.groupOpen);
      if (__frame.kind) library.reviewKind = __frame.kind;
      if (__frame.filter != null) library.reviewFilter = __frame.filter;
      renderLibraryPreview();
    `, ctx);
    vm.runInContext(`
      library.steps = libraryWorkflowSteps(library.state, library.preview);
      renderLibraryQuestions();
      renderLibraryBar();
    `, ctx);
    frames.push({ preview: box.innerHTML, bar: bar.innerHTML, list: list.innerHTML,
                  kinds: kinds.innerHTML,
                  applyDisabled: /id="lib-apply"[^>]*disabled/.test(box.innerHTML) });
  }
  return frames;
};

drive().then((frames) => process.stdout.write(JSON.stringify({ frames })),
             (err) => { console.error(err); process.exit(1); });
"""

IDLE = {"running": False, "label": "idle", "done": 0, "total": 0,
        "error": None, "details": None, "finished": None}


def run_state(**over):
    """One of the three `_idle()` blocks in the state payload."""
    out = dict(IDLE)
    out.update(over)
    return out


def scan(counts=None, buckets=None, considered=0, **over):
    out = run_state(**over)
    out.update({"counts": counts or {}, "buckets": buckets or {},
                "considered": considered})
    return out


def model(name):
    """One entry of a row's `models`, keyed the way the server keys it."""
    return {"name": name, "key": re.sub(r"[^a-z0-9]", "", name.lower())}


def match_fields(video_id, scene):
    """What picking `scene` would write to video `video_id`, as the server plans it."""
    return [
        {"field": "title", "action": "rename", "current": "Video %d" % video_id,
         "proposed": "Scene %s" % scene},
        {"field": "release_date", "action": "same", "current": "2026-01-01",
         "proposed": "2026-01-01", "current_estimated": False,
         "proposed_estimated": False},
        {"field": "studio", "action": "set", "current": None,
         "proposed": "A Studio", "new": True},
        {"field": "models", "action": "add", "kept": [],
         "added": [{"name": "Someone", "new": False}]},
        {"field": "categories", "action": "add", "kept": ["4K"],
         "added": [{"name": "Outdoor %s" % scene, "new": True}]},
        {"field": "external_ids", "action": "add", "current": None,
         "proposed": scene},
    ]


def question(video_id, scenes=("sc-a", "sc-b"), models=()):
    """One ambiguous row as `/api/library/state` reports it, covers and all."""
    return {
        "models": [model(name) for name in models],
        "videoId": video_id,
        "relPath": "library/scenes/%d.mp4" % video_id,
        # The absolute path, because the header has to be unmistakably the file
        # on disk rather than one more card in the grid below it.
        "path": "D:\\library\\scenes\\%d.mp4" % video_id,
        "title": "Video %d" % video_id,
        "durationSecs": 1200, "note": None, "whisparrSceneId": None,
        "terms": {"studio": "A Studio", "date": "2026-01-01",
                  "performers": ["Someone"]},
        # Ranked on the server: the first card is the recommendation, and its
        # length agrees with the file's, so it is safe to accept unseen.
        "noDurationMatch": False, "recommendationSafe": True,
        "candidates": [
            {"sceneId": scene, "title": "Scene %s" % scene,
             "cover": "https://cdn.example/%s.jpg" % scene,
             "date": "2026-01-01", "studio": "A Studio", "duration": 1200,
             "tagCount": 12, "performerCount": 2,
             "kind": "phash", "distance": 3, "durationDelta": 0,
             "durationAgrees": True, "recommended": index == 0,
             "recommendedByTags": False,
             "fields": match_fields(video_id, scene)}
            for index, scene in enumerate(scenes)],
    }


def unsafe(video_id, scenes=("sc-a", "sc-b")):
    """One ambiguous row no length supports — the kind Accept-all leaves behind."""
    row = question(video_id, scenes)
    row["noDurationMatch"] = True
    row["recommendationSafe"] = False
    for card in row["candidates"]:
        card["durationAgrees"] = False
        card["durationDelta"] = -300
    return row


def identify(counts=None, questions=None, **over):
    out = run_state(**over)
    out.update({"counts": counts or {}, "questions": questions or []})
    return out


def library_state(connected=True, can_write=True, **over):
    out = {
        "connected": connected, "url": "http://pron", "hasToken": True,
        "stashdbConfigured": True, "info": {}, "scopes": [], "canWrite": can_write,
        "settings": {}, "categories": [], "disagreements": [], "logCount": 0,
        "scan": scan(), "identify": identify(), "apply": run_state(),
    }
    out.update(over)
    return out


#: A library read end to end, with rows a fingerprint run could still act on.
#: Step 3 is the current one here.
SCANNED = library_state(scan=scan(
    considered=2050,
    counts={"verified": 7, "uuid-mismatch": 12, "no-identity": 2031}))

#: The state after an identify run has been over the whole library: nothing is
#: left to fingerprint, some ambiguous and unmatched rows are waiting on an
#: answer, and the write preview is holding a large queue.
REVIEWABLE = library_state(
    scan=scan(considered=2050,
              counts={"verified": 2019, "ambiguous": 12, "unmatched": 19},
              buckets={"unmatched": [
                           {"video_id": 9000 + n, "rel_path": "u/%d.mp4" % n,
                            "title": "Unmatched %d" % n, "note": None}
                           for n in range(19)],
                       "likely": [{"video_id": 900}]}),
    identify=identify(counts={"ambiguous": 12},
                      questions=[question(n) for n in range(12)],
                      label="done",
                      finished={"considered": 2031, "verified": 2000,
                                "likely": 1, "ambiguous": 12, "unmatched": 19,
                                "failed": 0, "phashes": 1200}))

#: The same library after "Accept all recommended" has taken everything it
#: safely could: the ambiguous rows are down to three, and all three are
#: rows whose lengths disagree, so the button has nothing left to offer and the
#: sticky bar hands back to Apply.
#: Deep-copied, never shared: these fixtures are module-level and a test that
#: reached into one would silently rewrite the other for every test after it.
REVIEWED = library_state(
    scan=json.loads(json.dumps(REVIEWABLE["scan"])),
    identify=identify(counts={"ambiguous": 3},
                      questions=[unsafe(n) for n in range(3)], label="done",
                      finished=json.loads(
                          json.dumps(REVIEWABLE["identify"]["finished"]))))

#: What `/api/library/preview` answered for that library.
PREVIEW = {"rows": [{"video_id": n, "status": "verified"} for n in range(1090)],
           "entities": {"models": 231, "studios": 5, "categories": 100,
                        "aliases": 0},
           "skipped": 4}

#: The library after a run wrote everything and the rows it wrote were re-read:
#: nothing left to fingerprint, nothing waiting on an answer, an apply that
#: finished, and a preview that has converged to nothing.
APPLIED = library_state(
    scan=scan(considered=160, counts={"in-sync": 160}, label="done",
              finished={"considered": 160}),
    identify=identify(label="done",
                      finished={"considered": 160, "verified": 160, "likely": 0,
                                "ambiguous": 0, "unmatched": 0, "failed": 0,
                                "phashes": 0}),
    apply=run_state(label="done",
                    finished={"applied": 160, "entities": 0, "aliases": 210,
                              "refused": 0, "refusals": [], "note": None}))

#: And what `/api/library/preview` answers once it has converged: nothing at all.
APPLIED_PREVIEW = {"rows": [],
                   "entities": {"models": 0, "studios": 0, "categories": 0,
                                "aliases": 0},
                   "skipped": 2}


def plain(html):
    """Hint text as a reader sees it: no tags, no entities, one space between."""
    text = re.sub(r"<[^>]+>", "", html)
    for entity, char in (("&mdash;", "-"), ("&check;", "/"), ("&amp;", "&"),
                         ("&middot;", "-"), ("&rarr;", "->"), ("&hellip;", "..."),
                         ("&ldquo;", '"'), ("&rdquo;", '"'), ("&#9662;", ""),
                         ("&#8599;", "")):
        text = text.replace(entity, char)
    return " ".join(text.split())


@unittest.skipIf(NODE is None, "node is not on PATH")
class WorkflowStripTest(unittest.TestCase):
    """Four steps, and the state each one is in for a given library."""

    def render(self, state, preview=None):
        """Returns (steps by key, the rendered HTML)."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            payload = os.path.join(tmp, "input.json")
            with open(harness, "w", encoding="utf-8") as fh:
                fh.write(HARNESS)
            with open(payload, "w", encoding="utf-8") as fh:
                json.dump({"state": state, "preview": preview}, fh)
            proc = subprocess.run([NODE, harness, APP_JS, payload],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        steps = {step["key"]: step for step in out["steps"]}
        for step in steps.values():
            step["text"] = plain(step["hint"])
        return steps, out["html"]

    def states(self, steps):
        return {key: step["state"] for key, step in steps.items()}

    # ------------------------------------------------------------ the strip

    def test_four_steps_in_order_with_a_scroll_target_each(self):
        steps, html = self.render(library_state(connected=False))
        self.assertEqual([s["step"] for s in sorted(steps.values(),
                                                    key=lambda s: s["step"])],
                         [1, 2, 3, 4])
        self.assertEqual(html.count("<li"), 4)
        # The collapsible section wrapper, not a box inside it: scrolling to a
        # body that is currently collapsed would land on nothing.
        for key in ("connect", "scan", "identify", "review"):
            self.assertIn('data-step-go="#library-section-%s"' % key, html)
            self.assertIn('data-step-key="%s"' % key, html)
        self.assertIn("Identify by fingerprint", html)
        # Reviewing and writing are one step: one list, one Write button.
        self.assertIn("Check matches and tag", html)
        self.assertNotIn("apply", steps)

    def test_a_done_step_shows_a_tick_and_a_pending_one_its_number(self):
        steps, html = self.render(library_state())
        self.assertEqual(steps["connect"]["state"], "done")
        # The tick replaces the number, so the step's own digit is gone.
        self.assertIn('<span class="step-num">&check;</span>', html)
        self.assertIn('<span class="step-num">2</span>', html)
        # Nothing has been counted yet, so step 3 does not print three zeroes.
        self.assertNotIn("0 to identify by fingerprint",
                         plain(steps["identify"]["hint"]))

    # ------------------------------------------------------- the four states

    def test_nothing_connected_puts_the_user_on_step_one(self):
        steps, _ = self.render(library_state(connected=False))
        self.assertEqual(self.states(steps),
                         {"connect": "current", "scan": "next",
                          "identify": "next", "review": "next"})
        self.assertIn("Update video metadata", steps["connect"]["text"])

    def test_connected_but_unscanned_puts_the_user_on_scan(self):
        steps, _ = self.render(library_state())
        self.assertEqual(self.states(steps),
                         {"connect": "done", "scan": "current",
                          "identify": "next", "review": "next"})
        self.assertIn("read + write", steps["connect"]["text"])
        self.assertIn("Reads every video from Pronoisseur",
                      steps["scan"]["text"])

    def test_a_read_only_token_still_completes_step_one(self):
        steps, _ = self.render(library_state(can_write=False))
        self.assertEqual(steps["connect"]["state"], "done")
        self.assertIn("read-only", steps["connect"]["text"])

    def test_the_scan_step_names_the_size_of_the_library(self):
        # The pills show three counts, none of them the size of the library. Step 2
        # has to say the size out loud.
        state = library_state(scan=scan(
            considered=2050,
            counts={"verified": 7, "uuid-mismatch": 12, "no-identity": 2031},
            finished={"considered": 2050}))
        steps, _ = self.render(state)
        self.assertEqual(self.states(steps),
                         {"connect": "done", "scan": "done",
                          "identify": "current", "review": "next"})
        self.assertEqual(
            steps["scan"]["text"],
            "2,050 videos seen - 7 identified by UUID, 12 UUID mismatches, "
            "2,031 still to identify.")
        # The "N to identify by fingerprint" sentence belongs to step 3.
        self.assertIn("2,043 to identify by fingerprint - 2,031 with no "
                      "identity yet, 12 Whisparr disagreements",
                      steps["identify"]["text"])
        self.assertIn("Nothing is hashed here", steps["identify"]["text"])

    def test_a_running_identify_shows_its_progress_on_step_three(self):
        state = library_state(
            scan=scan(considered=2050, counts={"no-identity": 2031}),
            identify=identify(running=True, label="looking up",
                              done=300, total=2031))
        steps, _ = self.render(state)
        self.assertEqual(steps["identify"]["state"], "current")
        self.assertTrue(steps["identify"]["text"].startswith(
            "Asking StashDB about fingerprints, 40 at a time: 300 of 2,031."),
            steps["identify"]["text"])
        # Results land a batch at a time, so Review and write is open for the
        # rows already settled - and the step says so, or nobody would look.
        self.assertIn("Results show up in Check matches and tag",
                      steps["identify"]["text"])
        self.assertEqual(steps["review"]["state"], "current")

    # A server label is a stable key; the page says where the work is going.
    LABELS = [
        ("listing videos", "reading Pronoisseur",
         "Reading your library from Pronoisseur"),
        ("reading identities", "reading folder ids",
         "Finding StashDB ids in folder names"),
        ("fetching scenes", "fetching from StashDB",
         "Fetching scenes named in folder names from StashDB"),
        ("checking folder ids", "checking folder ids",
         "Checking each folder's scene against Pronoisseur's fingerprint"),
        ("remembering decisions", "using earlier answers",
         "Using your earlier answers"),
        ("looking up", "asking StashDB",
         "Asking StashDB about fingerprints, 40 at a time"),
        ("upserting entities", "creating in Pronoisseur",
         "Creating models, studios and categories in Pronoisseur"),
        ("writing videos", "writing to Pronoisseur",
         "Writing videos to Pronoisseur"),
    ]

    def test_a_running_identify_names_where_it_is_asking(self):
        state = library_state(
            scan=scan(considered=2050, counts={"no-identity": 2031}),
            identify=identify(running=True, label="looking up",
                              done=441, total=1460))
        steps, _ = self.render(state)
        # The long form on the progress line, the short one on the step head.
        self.assertTrue(steps["identify"]["text"].startswith(
            "Asking StashDB about fingerprints, 40 at a time: 441 of 1,460."),
            steps["identify"]["text"])
        self.assertEqual(steps["identify"]["summary"], "asking StashDB 441/1,460")

    def test_every_label_the_server_sends_has_a_short_and_a_long_form(self):
        for key, short, long in self.LABELS:
            run = scan if key in ("listing videos", "reading identities",
                                  "fetching scenes", "checking folder ids") \
                else identify
            state = library_state(**{
                ("scan" if run is scan else "identify"):
                    run(running=True, label=key, done=2, total=9)})
            if run is scan:
                state["scan"]["considered"] = 0
            steps, _ = self.render(state)
            step = steps["scan" if run is scan else "identify"]
            self.assertEqual(step["summary"], "%s 2/9" % short, key)
            # The page escapes apostrophes in what it prints.
            self.assertTrue(step["text"].startswith(
                "%s: 2 of 9" % long.replace("'", "&#39;")), (key, step["text"]))
        # Tagging has its own two.
        for key, short, long in self.LABELS[6:]:
            steps, _ = self.render(library_state(
                apply=run_state(running=True, label=key, done=2, total=9)))
            self.assertEqual(steps["review"]["summary"], "%s 2/9" % short, key)
            self.assertTrue(steps["review"]["text"].startswith(
                "%s: 2 of 9" % long), key)

    def test_a_label_the_page_does_not_know_is_shown_as_it_came(self):
        state = library_state(
            scan=scan(considered=2050, counts={"no-identity": 2031}),
            identify=identify(running=True, label="warming up",
                              done=3, total=9))
        steps, _ = self.render(state)
        self.assertTrue(steps["identify"]["text"].startswith("warming up: 3 of 9"))
        self.assertEqual(steps["identify"]["summary"], "warming up 3/9")

    def test_rows_the_run_has_not_reached_count_as_still_to_identify(self):
        # A row the run has taken up is reported as `waiting`, not as the
        # verdict it arrived with, and it is step 3's work, not step 4's.
        state = library_state(
            scan=scan(considered=2000,
                      counts={"verified": 96, "unmatched": 300, "waiting": 1500},
                      buckets={"unmatched": [{"video_id": n} for n in range(300)]}),
            identify=identify(running=True, label="looking up",
                              done=300, total=1800))
        steps, _ = self.render(state)
        self.assertEqual(steps["scan"]["summary"],
                         "96 identified, 1,500 still to identify")
        # The strip card says the same as the section head, not "0 still to
        # identify" because none of them carries `no-identity` while held.
        self.assertIn("1,500 still to identify", steps["scan"]["text"])
        self.assertEqual(steps["identify"]["badge"], 1500)
        self.assertIn("1,500 still being identified", steps["review"]["text"])
        self.assertEqual(steps["review"]["summary"], "300 no match")

    def test_a_finished_identify_opens_review_and_write(self):
        # Nothing fingerprintable left, so step 3 is done, and the one list has
        # rows ready to write beside rows waiting on an answer.
        state = library_state(
            scan=scan(considered=2050,
                      counts={"verified": 2019, "ambiguous": 12,
                              "unmatched": 19},
                      buckets={"unmatched": [{"video_id": n} for n in range(19)],
                               "likely": [{"video_id": 900}]}),
            identify=identify(counts={"ambiguous": 12},
                              questions=[{"videoId": n} for n in range(12)],
                              label="done",
                              finished={"considered": 2031, "verified": 2000,
                                        "likely": 1, "ambiguous": 12,
                                        "unmatched": 19, "failed": 0,
                                        "phashes": 1200}))
        steps, _ = self.render(state, preview={"rows": [{"video_id": n}
                                                        for n in range(40)]})
        self.assertEqual(self.states(steps),
                         {"connect": "done", "scan": "done", "identify": "done",
                          "review": "current"})
        self.assertIn("Nothing left for a fingerprint run",
                      steps["identify"]["text"])
        # "Not found", not "waiting": waiting is what a row the run has not
        # reached yet is called.
        self.assertIn("40 matched - 12 to choose - 19 no match.",
                      steps["review"]["text"])
        self.assertIn("untick any match that's wrong, or a whole model with its tick",
                      steps["review"]["text"])
        self.assertIn("1 likely and 0 unverified are held back",
                      steps["review"]["text"])
        self.assertEqual(steps["review"]["summary"],
                         "40 matched &middot; 12 to choose &middot; 19 no match")
        self.assertEqual(steps["review"]["badge"], 71)

    def test_an_empty_review_queue_and_an_empty_preview_finish_the_strip(self):
        state = library_state(
            scan=scan(considered=2050, counts={"in-sync": 2050}),
            identify=identify(label="done",
                              finished={"considered": 2031, "verified": 2031,
                                        "likely": 0, "ambiguous": 0,
                                        "unmatched": 0, "failed": 0,
                                        "phashes": 1200}),
            apply=run_state(label="done",
                            finished={"applied": 40, "entities": 12,
                                      "aliases": 3}))
        steps, _ = self.render(state, preview={"rows": []})
        self.assertEqual(set(self.states(steps).values()), {"done"})
        self.assertIn("Nothing left to tag. Tagged 40 videos.",
                      steps["review"]["text"])

    def test_nothing_left_to_write_with_files_still_not_found_stays_current(self):
        # The write is finished, but the files StashDB has nothing for are still
        # this step's work - looked up by name - so it is not ticked off.
        state = library_state(
            scan=scan(considered=50, counts={"in-sync": 45, "unmatched": 5},
                      buckets={"unmatched": [{"video_id": n} for n in range(5)]}),
            identify=identify(label="done", finished={"considered": 50}),
            apply=run_state(label="done", finished={"applied": 45, "entities": 0,
                                                    "aliases": 0}))
        steps, _ = self.render(state, preview={"rows": []})
        self.assertEqual(steps["review"]["state"], "current")
        self.assertIn("5 no match.", steps["review"]["text"])
        self.assertIn("Nothing left to tag. Tagged 45 videos.",
                      steps["review"]["text"])

    def test_the_last_step_is_never_called_done_on_a_preview_that_never_arrived(self):
        # `preview` is null until `renderLibraryPreview` has fetched one. Zero
        # rows then means "not asked", not "nothing to write", and calling the
        # last step done off it would tick the whole strip on a library nobody
        # has written to.
        state = library_state(
            scan=scan(considered=10, counts={"verified": 10}),
            identify=identify(label="done", finished={"considered": 10}))
        steps, _ = self.render(state, preview=None)
        self.assertEqual(steps["review"]["state"], "current")
        self.assertEqual(steps["review"]["summary"], "no preview yet")

    def test_a_scan_in_flight_holds_the_strip_on_step_two(self):
        state = library_state(scan=scan(running=True, label="reading videos",
                                        done=140, total=2050, considered=0))
        steps, _ = self.render(state)
        self.assertEqual(steps["scan"]["state"], "current")
        self.assertEqual(steps["scan"]["text"], "reading videos: 140 of 2,050.")
        self.assertEqual(steps["identify"]["state"], "next")


@unittest.skipIf(NODE is None, "node is not on PATH")
class QuestionGridTest(unittest.TestCase):
    """`#library-questions`, the box of question blocks.

    Every question block carries a grid of cover `<img>` tags, and the box is polled
    repeatedly for the length of a whole-library identify, by which point the
    ambiguous set can be very large. Three guards, asserted here: don't rebuild what
    has not changed, don't draw the grid at all mid-run, and don't put more than
    `QUESTION_PAGE` blocks in the document at once.
    """

    SENTINEL = "<!--kept-->"

    def frames(self, *frames, watch=None):
        """Drive a sequence of renders; returns (frame results, the constants).

        ``watch`` names the model groups whose Show-more wiring is reported,
        by group id; the default is the one group rows with no models land in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            payload = os.path.join(tmp, "input.json")
            with open(harness, "w", encoding="utf-8") as fh:
                fh.write(QUESTIONS_HARNESS)
            with open(payload, "w", encoding="utf-8") as fh:
                json.dump({"frames": list(frames),
                           "watch": watch or ["nomodel"]}, fh)
            proc = subprocess.run([NODE, harness, APP_JS, payload],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        return out["frames"], out

    def blocks(self, html):
        """How many question blocks are actually in the document."""
        return html.count("data-question=")

    def state(self, questions, running=False, unmatched=0, unmatched_models=None):
        return library_state(
            scan=scan(considered=2050,
                      buckets={"unmatched": [
                          {"video_id": 9000 + n, "rel_path": "u/%d.mp4" % n,
                           "title": "Unmatched %d" % n, "note": None,
                           "models": unmatched_models or []}
                          for n in range(unmatched)]}),
            identify=identify(questions=questions, running=running,
                              label="looking up" if running else "done",
                              done=300, total=2031))

    # ------------------------------------------------- guard 1: the signature

    def test_a_poll_that_brings_back_the_same_questions_rebuilds_nothing(self):
        state = self.state([question(n) for n in range(3)])
        got, _ = self.frames({"state": state},
                             {"sentinel": self.SENTINEL, "state": state})
        self.assertEqual(self.blocks(got[0]["html"]), 3)
        # The second render was skipped, so the sentinel is still standing.
        self.assertEqual(got[1]["html"], self.SENTINEL)

    def test_a_candidate_changing_rebuilds_even_on_the_same_row_set(self):
        # Same three videoIds, one of them now offering a different scene. An
        # identify pass can re-decide a row without adding or removing one, and a
        # signature over row ids alone would leave the wrong covers on screen.
        first = self.state([question(0), question(1), question(2)])
        second = self.state([question(0), question(1, scenes=("sc-z",)),
                             question(2)])
        got, _ = self.frames({"state": first},
                             {"sentinel": self.SENTINEL, "state": second})
        self.assertNotEqual(got[1]["html"], self.SENTINEL)
        self.assertIn("sc-z", got[1]["html"])

    def test_a_new_question_arriving_rebuilds(self):
        got, _ = self.frames(
            {"state": self.state([question(0)])},
            {"sentinel": self.SENTINEL, "state": self.state([question(0),
                                                             question(1)])})
        self.assertEqual(self.blocks(got[1]["html"]), 2)

    # ------------------------------------- guard 2: mid-run, new rows join

    # A run settles rows a batch at a time and never touches one again, so the
    # rows it has settled can be answered while it goes on. New rows join the
    # list on their own, with three limits: never while the user is working in
    # a card (a field in the list has focus, or a Search by name panel is
    # open), never more than one redraw in five seconds, and never losing the
    # user's place. A busy user keeps a line and a Show them button.

    T0 = 1000000                      # the harness clock's starting point
    SOON = T0 + 1000                  # inside the 5-second window
    LATER = T0 + 6000                 # outside it

    def test_settled_questions_are_drawn_while_a_run_is_in_flight(self):
        rows = [question(n) for n in range(3)]
        got, _ = self.frames({"state": self.state(rows, running=True)})
        self.assertEqual(self.blocks(got[0]["html"]), 3)
        self.assertEqual(got[0]["fresh"], "")

    def test_new_rows_mid_run_join_the_list_on_their_own(self):
        two = [question(0), question(1)]
        four = two + [question(2)]
        got, _ = self.frames(
            {"state": self.state(two, running=True)},
            {"now": self.LATER,
             "state": self.state(four, running=True, unmatched=1)})
        self.assertEqual(self.blocks(got[0]["html"]), 2)
        # No click: both kinds of row that arrived are in, and no line is left.
        self.assertEqual(self.blocks(got[1]["html"]), 4)     # 3 ambiguous, 1 not
        self.assertIn("Unmatched 0", got[1]["html"])
        self.assertEqual(got[1]["fresh"], "")

    def test_new_rows_wait_while_a_field_in_the_list_has_focus(self):
        two = [question(0), question(1)]
        four = two + [question(2)]
        got, _ = self.frames(
            {"state": self.state(two, running=True)},
            {"now": self.LATER, "focus": "INPUT", "sentinel": self.SENTINEL,
             "state": self.state(four, running=True, unmatched=1)},
            # Focus leaves the list: the next poll releases them.
            {"now": self.LATER, "focus": None,
             "state": self.state(four, running=True, unmatched=1)})
        # Nothing rebuilt under the user's hands, and the line says why.
        self.assertEqual(got[1]["html"], self.SENTINEL)
        self.assertIn("2 new results will appear when you're done with this "
                      "card.", plain(got[1]["fresh"]))
        self.assertIn("Show them", plain(got[1]["fresh"]))
        self.assertEqual(self.blocks(got[2]["html"]), 4)
        self.assertEqual(got[2]["fresh"], "")

    def test_a_select_or_textarea_in_the_list_is_busy_too(self):
        for tag in ("SELECT", "TEXTAREA"):
            got, _ = self.frames(
                {"state": self.state([question(0)], running=True)},
                {"now": self.LATER, "focus": tag, "sentinel": self.SENTINEL,
                 "state": self.state([question(0), question(1)], running=True)})
            self.assertEqual(got[1]["html"], self.SENTINEL, tag)
            self.assertIn("1 new result will appear", plain(got[1]["fresh"]), tag)

    def test_a_button_with_focus_is_not_busy(self):
        # Focus on a button or a link is not typing: only a field is.
        got, _ = self.frames(
            {"state": self.state([question(0)], running=True)},
            {"now": self.LATER, "focus": "BUTTON",
             "state": self.state([question(0), question(1)], running=True)})
        self.assertEqual(self.blocks(got[1]["html"]), 2)

    def test_new_rows_wait_while_a_search_by_name_panel_is_open(self):
        one = [question(0)]
        two = one + [question(1)]
        got, _ = self.frames(
            {"state": self.state(one, running=True)},
            {"now": self.LATER, "panel": True, "sentinel": self.SENTINEL,
             "state": self.state(two, running=True)},
            # The panel is closed: released on the next poll.
            {"now": self.LATER, "panel": False,
             "state": self.state(two, running=True)})
        self.assertEqual(got[1]["html"], self.SENTINEL)
        self.assertIn("1 new result will appear when you're done with this "
                      "card.", plain(got[1]["fresh"]))
        self.assertEqual(self.blocks(got[2]["html"]), 2)
        self.assertEqual(got[2]["fresh"], "")

    def test_show_them_is_the_way_out_while_the_user_is_busy(self):
        two = [question(0), question(1)]
        got, _ = self.frames(
            {"state": self.state([question(0)], running=True)},
            {"now": self.LATER, "focus": "INPUT", "sentinel": self.SENTINEL,
             "state": self.state(two, running=True)},
            {"showFresh": True})
        self.assertEqual(got[1]["html"], self.SENTINEL)
        self.assertTrue(got[2]["clicked"])
        self.assertEqual(self.blocks(got[2]["html"]), 2)
        self.assertEqual(got[2]["fresh"], "")

    def test_at_most_one_automatic_redraw_every_five_seconds(self):
        rows = [question(n) for n in range(5)]
        got, _ = self.frames(
            {"state": self.state(rows[:2], running=True)},
            # Inside the window: held, and silently - no line to read.
            {"now": self.SOON, "sentinel": self.SENTINEL,
             "state": self.state(rows[:3], running=True)},
            # Five seconds after the list was drawn: released.
            {"now": self.T0 + 5000, "state": self.state(rows[:3], running=True)},
            # That was a redraw, so the next batch waits for the next window.
            {"now": self.T0 + 6000, "sentinel": self.SENTINEL,
             "state": self.state(rows, running=True)},
            {"now": self.T0 + 10000, "state": self.state(rows, running=True)})
        self.assertEqual(self.blocks(got[0]["html"]), 2)
        self.assertEqual(got[1]["html"], self.SENTINEL)
        self.assertEqual(got[1]["fresh"], "")
        self.assertEqual(self.blocks(got[2]["html"]), 3)
        self.assertEqual(got[3]["html"], self.SENTINEL)
        self.assertEqual(got[3]["fresh"], "")
        self.assertEqual(self.blocks(got[4]["html"]), 5)

    def test_a_busy_user_gets_the_line_even_inside_the_window(self):
        got, _ = self.frames(
            {"state": self.state([question(0)], running=True)},
            {"now": self.SOON, "focus": "INPUT",
             "state": self.state([question(0), question(1)], running=True)})
        self.assertIn("will appear when you're done with this card",
                      plain(got[1]["fresh"]))

    def test_the_users_place_is_kept_when_the_list_is_rebuilt(self):
        # The first group head whose bottom is below the top of the viewport
        # was at -20 and is now at 30: scroll by the 50 it moved.
        two = [question(0, models=("Jane Example",)),
               question(1, models=("Kay Example",))]
        three = two + [question(2, models=("Jane Example",))]
        geometry = {"before": {"m-janeexample": [-300, -250],
                               "m-kayexample": [-20, 60]},
                    "after": {"m-janeexample": [-250, -200],
                              "m-kayexample": [30, 110]}}
        got, out = self.frames(
            {"state": self.state(two, running=True)},
            {"now": self.LATER, "geometry": geometry,
             "state": self.state(three, running=True)})
        self.assertEqual(out["scrolls"], [[0, 50]])

    def test_a_group_that_has_gone_leaves_the_scroll_alone(self):
        two = [question(0, models=("Jane Example",)),
               question(1, models=("Kay Example",))]
        geometry = {"before": {"m-kayexample": [-20, 60]},
                    "after": {"m-janeexample": [30, 110]}}
        got, out = self.frames(
            {"state": self.state(two[:1], running=True)},
            {"now": self.LATER, "geometry": geometry,
             "state": self.state(two, running=True)})
        self.assertEqual(out["scrolls"], [])

    def test_new_rows_go_straight_in_while_nothing_is_on_screen(self):
        # Nothing to disturb, so nothing to hold back.
        got, _ = self.frames({"state": self.state([], running=True)},
                             {"state": self.state([question(0)], running=True)})
        self.assertEqual(self.blocks(got[1]["html"]), 1)
        self.assertEqual(got[1]["fresh"], "")

    def test_the_run_finishing_brings_in_every_row_held_back(self):
        got, _ = self.frames(
            {"state": self.state([question(0)], running=True)},
            {"state": self.state([question(0), question(1)], running=True)},
            {"state": self.state([question(0), question(1)], running=False)})
        self.assertEqual(self.blocks(got[1]["html"]), 1)
        self.assertEqual(self.blocks(got[2]["html"]), 2)
        self.assertEqual(got[2]["fresh"], "")

    def test_the_run_finishing_does_not_redraw_rows_already_on_screen(self):
        rows = [question(n) for n in range(3)]
        got, _ = self.frames({"state": self.state(rows, running=True)},
                             {"sentinel": self.SENTINEL,
                              "state": self.state(rows, running=False)})
        self.assertEqual(self.blocks(got[0]["html"]), 3)
        self.assertEqual(got[1]["html"], self.SENTINEL)

    def test_a_run_over_a_library_with_no_questions_yet_draws_nothing(self):
        got, _ = self.frames({"state": self.state([], running=True)})
        self.assertEqual(got[0]["html"], "")

    # ------------------------------------------------- grouped by model

    # One model's videos are looked at, and accepted, together. A row's models
    # arrive from the server; the page only groups them.

    def grouped(self):
        """Three of Jane's, two of Sam's - one of them shared - and two with none."""
        return self.state(
            [question(1, models=("Jane Example",)),
             question(2, models=("Jane Example", "Sam Sample")),
             question(3, models=("Jane Example",)),
             question(4, models=("Sam Sample",)),
             question(5)],
            unmatched=1)

    def heads(self, html):
        """Each model group's head as a reader sees it, in page order."""
        return [plain(head) for head in re.findall(
            r'<details class="lib-group"[^>]*>\s*<summary>(.*?)</summary>', html, re.S)]

    def test_review_is_grouped_by_model_biggest_first_and_no_model_last(self):
        got, _ = self.frames({"state": self.grouped()})
        heads = self.heads(got[0]["html"])
        self.assertEqual([head.split(" - ")[0] for head in heads],
                         ["Jane Example 3", "Sam Sample 2", "No model 2"])
        self.assertIn('data-group="m-janeexample"', got[0]["html"])
        self.assertIn('data-group="nomodel"', got[0]["html"])

    def test_every_group_starts_shut_when_there_is_more_than_one(self):
        # A library of hundreds of models reads as a list of names, not a wall
        # of pictures, until one is opened.
        got, _ = self.frames({"state": self.grouped()})
        self.assertEqual(self.blocks(got[0]["html"]), 0)
        self.assertNotRegex(got[0]["html"], r'data-group="[^"]+"\s+open')

    def test_expand_all_opens_every_group_and_collapse_all_shuts_them(self):
        # Five rows, but six drawings: the video under two models is in both.
        got, _ = self.frames({"state": self.grouped()},
                             {"expandAll": True},
                             {"sentinel": self.SENTINEL, "state": self.grouped()},
                             {"expandAll": False})
        self.assertEqual(self.blocks(got[0]["html"]), 0)
        self.assertEqual(self.blocks(got[1]["html"]), 7)
        self.assertNotRegex(got[1]["html"], r'data-group="[^"]+"\s+>')
        # Held as a choice: the next poll keeps them open, and redraws nothing.
        self.assertEqual(got[2]["html"], self.SENTINEL)
        self.assertEqual(self.blocks(got[3]["html"]), 0)

    def test_expand_and_collapse_are_hidden_when_there_are_no_groups(self):
        got, _ = self.frames({"state": self.state([question(0)])},
                             {"state": self.state([])})
        self.assertFalse(got[0]["expandHidden"])
        self.assertTrue(got[1]["expandHidden"])

    def test_a_lone_group_starts_open(self):
        got, _ = self.frames({"state": self.state([question(n) for n in range(3)])})
        self.assertEqual(self.blocks(got[0]["html"]), 3)

    def test_a_group_opened_by_hand_draws_its_rows_and_only_its_rows(self):
        got, _ = self.frames({"state": self.grouped(),
                              "groupOpen": {"m-samsample": True}})
        html = got[0]["html"]
        self.assertEqual(self.blocks(html), 2)
        self.assertIn('data-question="2"', html)
        self.assertIn('data-question="4"', html)

    def test_a_video_with_two_models_is_drawn_in_both_groups_and_says_so(self):
        got, _ = self.frames({"state": self.grouped(),
                              "groupOpen": {"m-janeexample": True,
                                            "m-samsample": True}})
        html = got[0]["html"]
        self.assertEqual(html.count('data-question="2"'), 2)
        text = plain(html)
        self.assertIn("also under Sam Sample", text)
        self.assertIn("also under Jane Example", text)
        # Each drawing keeps its own search boxes, so a search reads the boxes
        # it was typed into and not the first copy on the page.
        self.assertIn('data-search-title="m-janeexample~2"', html)
        self.assertIn('data-search-title="m-samsample~2"', html)

    def test_inside_a_group_the_rows_with_matches_come_first(self):
        state = self.state([question(1)], unmatched=1)
        html = self.frames({"state": state})[0][0]["html"]
        self.assertLess(html.index('data-question="1"'),
                        html.index('data-question="9000"'))

    def test_each_group_offers_to_accept_its_own_safe_rows(self):
        got, _ = self.frames({"state": self.grouped()})
        heads = self.heads(got[0]["html"])
        self.assertIn("Jane Example 3 - 3 to choose Use 3 recommended", heads[0])
        self.assertIn("Sam Sample 2 - 2 to choose Use 2 recommended", heads[1])
        self.assertIn('data-accept-model="m-janeexample"', got[0]["html"])

    def test_a_group_with_nothing_safe_offers_a_dead_button(self):
        state = self.state([unsafe(1)])
        state["identify"]["questions"][0]["models"] = [model("Jane Example")]
        html = self.frames({"state": state})[0][0]["html"]
        self.assertIn("Jane Example 1 - 1 to choose Use 0 recommended",
                      self.heads(html)[0])
        self.assertRegex(html, r'data-accept-model="m-janeexample"\s+disabled')

    def test_a_group_of_files_stashdb_found_nothing_for_offers_no_accept(self):
        state = self.state([question(1, models=("Jane Example",))], unmatched=2,
                           unmatched_models=[model("Kay Example")])
        html = self.frames({"state": state})[0][0]["html"]
        kay = [head for head in self.heads(html) if head.startswith("Kay")][0]
        self.assertEqual(kay, "Kay Example 2 - 2 no match")
        self.assertNotIn('data-accept-model="m-kayexample"', html)
        # Nothing ready in it, so no model tick either - only a spacer.
        self.assertNotIn('data-group-pick="m-kayexample"', html)

    def test_new_results_mid_run_do_not_redraw_the_groups_for_their_counts(self):
        # The accept counts include rows a run settled after the list was drawn,
        # because the button takes those too. They are set in place, never by
        # rebuilding the list under the user.
        first = self.state([question(1, models=("Jane Example",))], running=True)
        later = self.state([question(1, models=("Jane Example",)),
                            question(2, models=("Jane Example",))], running=True)
        # Inside the 5-second window, so the box is left alone.
        got, _ = self.frames({"state": first},
                             {"sentinel": self.SENTINEL, "state": later})
        self.assertEqual(got[1]["html"], self.SENTINEL)

    # ------------------------------------------------------ guard 3: the cap

    def test_each_group_is_capped_and_show_more_appends_a_page_at_a_time(self):
        rows = [question(n) for n in range(130)]
        got, consts = self.frames({"state": self.state(rows)},
                                  {"click": "nomodel"},
                                  {"click": "nomodel"},
                                  {"sentinel": self.SENTINEL,
                                   "state": self.state(rows)})
        page = consts["page"]
        self.assertEqual(page, 50)
        self.assertEqual(self.blocks(got[0]["html"]), page)
        # A page at a time, so the button promises what it will actually do
        # rather than offering all 80 and drawing 80 sets of pictures.
        self.assertIn("Show 50 more", plain(got[0]["html"]))
        self.assertTrue(got[0]["wired"]["nomodel"])

        self.assertTrue(got[1]["clicked"])
        self.assertEqual(self.blocks(got[1]["html"]), 2 * page)
        self.assertIn("Show 30 more", plain(got[1]["html"]))

        self.assertEqual(self.blocks(got[2]["html"]), 130)
        # Everything is on screen, so there is no button left to wire.
        self.assertNotIn("lib-more-nomodel", got[2]["html"])
        self.assertFalse(got[2]["wired"]["nomodel"])
        # An expanded list stays expanded: a later poll neither collapses it back
        # to the cap nor rebuilds it.
        self.assertEqual(got[3]["html"], self.SENTINEL)
        self.assertEqual(got[3]["shown"]["nomodel"], 3 * page)

    def test_a_set_smaller_than_the_cap_gets_no_button(self):
        got, _ = self.frames({"state": self.state([question(n)
                                                   for n in range(4)],
                                                  unmatched=2)})
        self.assertEqual(self.blocks(got[0]["html"]), 6)
        self.assertNotIn("lib-more-", got[0]["html"])

    def test_the_groups_page_independently_of_each_other(self):
        # 60 of each. Showing more of one leaves the other exactly where it was.
        rows = ([question(n, models=("Jane Example",)) for n in range(60)]
                + [question(100 + n, models=("Sam Sample",)) for n in range(60)])
        opened = {"m-janeexample": True, "m-samsample": True}
        got, _ = self.frames({"state": self.state(rows), "groupOpen": opened},
                             {"click": "m-samsample"},
                             watch=["m-janeexample", "m-samsample"])
        self.assertEqual(self.blocks(got[0]["html"]), 100)
        self.assertEqual(plain(got[0]["html"]).count("Show 10 more"), 2)
        self.assertEqual(got[1]["shown"], {"m-samsample": 100})
        self.assertEqual(self.blocks(got[1]["html"]), 110)

    # ---------------------------------------------------- the filename filter

    def test_the_filename_filter_narrows_every_group_at_once(self):
        # Question paths are `library/scenes/N.mp4`, unmatched ones `u/N.mp4`.
        state = self.state([question(n) for n in range(6)], unmatched=4)
        got, _ = self.frames({"state": state}, {"state": state, "filter": "u/"})
        self.assertEqual(self.blocks(got[0]["html"]), 10)
        self.assertEqual(self.blocks(got[1]["html"]), 4)
        self.assertIn("No model 4", plain(got[1]["html"]))
        self.assertIn("4 of 10 rows match", plain(got[1]["filterHint"]))

    def test_a_filter_opens_the_groups_it_narrows(self):
        # Shut groups under a filter would hide exactly what was asked for.
        got, _ = self.frames({"state": self.grouped(), "filter": "scenes/4"})
        self.assertEqual(self.blocks(got[0]["html"]), 1)
        self.assertIn('data-question="4"', got[0]["html"])
        self.assertEqual([head.split(" - ")[0]
                          for head in self.heads(got[0]["html"])],
                         ["Sam Sample 1"])
        self.assertIn("1 of 6 rows match", plain(got[0]["filterHint"]))

    def test_the_filter_matches_the_title_as_well_as_the_path(self):
        state = self.state([question(3)], unmatched=2)
        got, _ = self.frames({"state": state,
                              "filter": "unmatched 1"})   # a title, lower-cased
        self.assertEqual(self.blocks(got[0]["html"]), 1)
        self.assertIn("Unmatched 1", got[0]["html"])

    def test_a_filter_that_matches_nothing_says_so_rather_than_going_blank(self):
        state = self.state([question(n) for n in range(3)], unmatched=2)
        got, _ = self.frames({"state": state, "filter": "no such file"})
        self.assertEqual(self.blocks(got[0]["html"]), 0)
        self.assertIn('Nothing here matches "no such file"',
                      plain(got[0]["html"]))
        self.assertIn("0 of 5 rows match", plain(got[0]["filterHint"]))

    def test_clearing_the_filter_brings_the_rows_back_and_resets_the_paging(self):
        rows = [question(n) for n in range(130)]
        got, _ = self.frames({"state": self.state(rows)},
                             {"click": "nomodel"},
                             {"state": self.state(rows), "filter": "scenes/1"},
                             {"state": self.state(rows), "filter": ""})
        self.assertEqual(got[1]["shown"]["nomodel"], 100)
        # A page count carried across a filter would open the next list with two
        # pages of pictures already drawn.
        self.assertEqual(got[2]["shown"], {})
        self.assertEqual(got[3]["shown"], {})
        self.assertEqual(self.blocks(got[3]["html"]), 50)
        self.assertEqual(plain(got[3]["filterHint"]), "")

    # ------------------------------- the file up top, StashDB scenes below

    # A library-relative path over a row of covers reads as one more candidate,
    # so the file has to be unmistakable: the header is the file, with its
    # absolute path and a label, and every card below it is a StashDB scene.
    # Everything in this section exists so that confusion cannot arise.

    def test_the_head_shows_the_files_absolute_path(self):
        row = question(1)
        got, _ = self.frames({"state": self.state([row])})
        html, text = got[0]["html"], plain(got[0]["html"])
        self.assertIn("<code>%s</code>" % row["path"], html)
        self.assertIn("Which is your file? Use this match on the right one, or "
                      "None of these match.", text)
        # Labelled once, on its picture, beside "StashDB - <title>" on the
        # match's: a second "Your file" over the path only repeated it.
        self.assertEqual(html.count(">Your file<"), 1)

    def test_the_relative_path_is_the_fallback_when_there_is_no_absolute_one(self):
        row = question(2)
        row.pop("path")
        got, _ = self.frames({"state": self.state([row])})
        self.assertIn("<code>library/scenes/2.mp4</code>", got[0]["html"])

    def test_an_unmatched_row_gets_the_file_label_but_no_stashdb_heading(self):
        # It has no candidates, so there is no grid for a heading to introduce -
        # but it is still the same block, and its picture is still your file.
        got, _ = self.frames({"state": self.state([], unmatched=1)})
        html, text = got[0]["html"], plain(got[0]["html"])
        self.assertIn("<code>u/0.mp4</code>", html)
        self.assertEqual(html.count(">Your file<"), 1)
        self.assertNotIn("Scenes on StashDB", text)

    # Each match is a tab, and the one on show sits beside the file: two
    # pictures the same size, then what picking it would write, field by field.

    def test_every_match_is_a_tab_and_the_recommended_one_is_on_show(self):
        got, _ = self.frames({"state": self.state([question(3)])})
        html = got[0]["html"]
        self.assertEqual(html.count("data-tab-scene="), 2)
        self.assertRegex(html, r'class="match-tab is-on"[^>]*\s+data-tab-video="3"'
                               r'\s+data-tab-scene="sc-a"')
        # Shown, never recorded: picking it is still a click.
        self.assertIn('data-pick-scene="sc-a"', html)
        self.assertNotIn('data-pick-scene="sc-b"', html)

    def test_a_tab_carries_a_small_cover_of_its_match(self):
        html = self.frames({"state": self.state([question(3)])})[0][0]["html"]
        for scene in ("sc-a", "sc-b"):
            self.assertRegex(html, r'<span class="tab-thumb"><img src="https://'
                                   r'cdn\.example/%s\.jpg"' % scene)

    def test_switching_tab_shows_that_match_and_what_it_would_write(self):
        got, _ = self.frames({"state": self.state([question(3)]),
                              "tab": {"3": "sc-b"}})
        html = got[0]["html"]
        self.assertIn('data-pick-scene="sc-b"', html)
        self.assertNotIn('data-pick-scene="sc-a"', html)
        self.assertIn("+ Outdoor sc-b", plain(html))
        self.assertNotIn("+ Outdoor sc-a", plain(html))

    def test_your_file_and_the_match_sit_side_by_side(self):
        html = self.frames({"state": self.state([question(3)])})[0][0]["html"]
        text = plain(html)
        # The line a failed still shows - and it says the write does not need
        # the file, because a page of failed pictures reads like one that
        # cannot write.
        self.assertIn("Your file No frame - this file can't be read right now. "
                      "Writing to Pronoisseur doesn't need the file.",
                      text.replace("&#39;", "'"))
        self.assertIn("StashDB - Scene sc-a", text)
        # The file's own still comes from this tool, by video id and frame.
        self.assertIn('data-frame-src="/api/library/frame?video=3&amp;n=0"', html)
        self.assertEqual(html.count('class="pic'), 2)

    def test_the_frame_on_show_is_the_one_stepped_to(self):
        got, _ = self.frames({"state": self.state([question(3)]),
                              "frameAt": {"3": 2}})
        html = got[0]["html"]
        self.assertIn('data-frame-src="/api/library/frame?video=3&amp;n=2"', html)
        self.assertIn('data-frame-pos="3">3/4<', html)

    def test_each_field_says_what_picking_the_match_would_do(self):
        text = plain(self.frames({"state": self.state([question(3)])})[0][0]["html"])
        self.assertIn("Your file now If you use this match", text)
        self.assertIn("Title Video 3 Rename Scene sc-a", text)
        self.assertIn("Release date 2026-01-01 No change 2026-01-01", text)
        # (The blank studio is a dash, which this pipe does not carry intact.)
        self.assertRegex(text, r"Studio \S+ Set A Studio new to your library")
        self.assertIn("Categories 4K Add 1 4K + Outdoor sc-a new to your library",
                      text)
        self.assertIn("Using a match doesn't tag it yet - it moves to Matched, "
                      "ticked, for Tag to write. Nothing on this video is "
                      "removed.", text.replace("&#39;", "'"))

    def test_the_length_comes_first_and_says_whether_it_matches(self):
        text = plain(self.frames({"state": self.state([question(3)])})[0][0]["html"])
        self.assertIn("Length 20:00 20:00 - matches Title", text)

    def test_only_the_leader_is_recommended(self):
        html = self.frames({"state": self.state([question(3)])})[0][0]["html"]
        # Once on its tab and once beside the picture: never on the other match.
        self.assertEqual(html.count(">Recommended<"), 2)
        self.assertLess(html.index('data-tab-scene="sc-a"'),
                        html.index(">Recommended<"))
        self.assertLess(html.index(">Recommended<"),
                        html.index('data-tab-scene="sc-b"'))

    def test_the_badge_names_the_tags_only_when_the_server_says_they_decided(self):
        plainly = question(31)
        tagged = question(32)
        tagged["candidates"][0]["recommendedByTags"] = True
        got, _ = self.frames({"state": self.state([plainly, tagged])})
        text = plain(got[0]["html"])
        self.assertEqual(text.count("Recommended - most tags"), 2)
        self.assertEqual(text.count("Recommended"), 4)

    def test_a_row_where_no_length_agrees_says_so_and_offers_no_accept(self):
        # The leading match is still the closest thing on offer, but it is not
        # dressed as a recommendation: nothing here may be taken unseen.
        got, _ = self.frames({"state": self.state([unsafe(4)])})
        html = got[0]["html"]
        text = plain(html)
        self.assertIn("None of these runs the same length as your file - check "
                      "the pictures.", text)
        self.assertIn("Closest - lengths disagree", text)
        self.assertNotIn(">Recommended", html)
        self.assertIn("Use recommended matches (0)", text)
        self.assertIn("disabled", html)

    def test_a_match_whose_length_is_off_says_by_how_much(self):
        text = plain(self.frames({"state": self.state([unsafe(4)])})[0][0]["html"])
        # Five minutes short, on the runner-up's tab and in the length row.
        self.assertIn("2 - Scene sc-b 5 min short", text)
        self.assertIn("Length 20:00 20:00 - 5 min short", text)

    def test_an_unmatched_row_shows_its_own_frame_and_says_there_is_no_match(self):
        html = self.frames({"state": self.state([], unmatched=1)})[0][0]["html"]
        self.assertIn('data-frame-src="/api/library/frame?video=9000&amp;n=0"', html)
        self.assertIn("No match StashDB has no scene with this file's fingerprint",
                      plain(html).replace("&#39;", "'"))
        self.assertIn("Search for it by name below.", plain(html))
        self.assertNotIn("match-tab", html)

    def test_a_card_to_choose_says_why_and_offers_use_this_match(self):
        html = self.frames({"state": self.state([question(3)])})[0][0]["html"]
        text = plain(html).replace("&#39;", "'")
        self.assertIn("Choose a match 2 StashDB scenes share this file's "
                      "fingerprint", text)
        self.assertIn("Which is your file? Use this match on the right one, or "
                      "None of these match.", text)
        self.assertIn(">Use this match</button>", html)
        self.assertIn(">None of these match</button>", html)
        self.assertNotIn("Pick this", html)

    def test_a_lone_match_to_choose_says_it_was_not_close_enough(self):
        row = question(7, scenes=("sc-a",))
        html = self.frames({"state": self.state([row])})[0][0]["html"]
        self.assertIn("Choose a match 1 StashDB scene is close, but not close "
                      "enough to tag unseen", plain(html))

    def test_the_match_on_show_links_out_to_its_scene_on_stashdb(self):
        got, _ = self.frames({"state": self.state([question(5)])})
        html = got[0]["html"]
        self.assertIn('href="https://stashdb.org/scenes/sc-a"', html)
        # A new tab, and never one that can reach back through `window.opener`.
        self.assertEqual(html.count('target="_blank" rel="noopener"'), 1)

    def test_a_card_says_how_much_stashdb_knows_about_the_scene(self):
        got, _ = self.frames({"state": self.state([question(6)])})
        self.assertIn("12 tags - 2 models", plain(got[0]["html"]))

    def test_a_lone_tag_and_a_lone_model_are_not_pluralised(self):
        row = question(7, scenes=("sc-a",))
        row["candidates"][0].update(tagCount=1, performerCount=1)
        got, _ = self.frames({"state": self.state([row])})
        self.assertIn("1 tag - 1 model", plain(got[0]["html"]))

    def test_counts_the_lookup_never_fetched_show_nothing_rather_than_zero(self):
        row = question(8)
        for card in row["candidates"]:
            card.update(tagCount=None, performerCount=None)
        got, _ = self.frames({"state": self.state([row])})
        html = got[0]["html"]
        self.assertNotIn(" tags", html)
        self.assertNotIn(" models", html)
        # The link stays: not knowing what a scene carries is a reason to go and
        # look at it, not a reason to hide the way there.
        self.assertIn("https://stashdb.org/scenes/sc-a", html)

    # ------------------------------------------------------ the cheap covers

    def test_every_picture_is_lazy_and_decoded_off_the_main_thread(self):
        # Per row: a small cover on each of two tabs and the match's cover, all
        # lazy, and a still of the file, which is drawn with no address at all:
        # it costs an ffmpeg run, so the page's own queue loads it (FrameQueueTest).
        got, _ = self.frames({"state": self.state([question(n)
                                                   for n in range(3)])})
        html = got[0]["html"]
        self.assertEqual(html.count("<img"), 12)
        self.assertEqual(html.count('loading="lazy"'), 9)
        self.assertEqual(html.count('decoding="async"'), 12)
        self.assertEqual(html.count("data-frame-src="), 3)
        self.assertNotRegex(html, r'<img[^>]*\ssrc="/api/library/frame')

    def test_the_poll_is_no_faster_than_two_seconds(self):
        # A poll is a fetch plus several renders, and during the phash pass it
        # competes with ffmpeg for the same machine.
        _, consts = self.frames({"state": self.state([question(0)])})
        self.assertGreaterEqual(consts["pollMs"], 2000)


@unittest.skipIf(NODE is None, "node is not on PATH")
class LibrarySectionsTest(unittest.TestCase):
    """Four collapsible sections and one sticky bar.

    Four steps open at once would leave whatever you are working on in the middle
    of the three you are not, so only the current step is open and the bar keeps
    the next action in reach.
    """

    def frames(self, *frames):
        """Drive the page through a sequence of paints and interactions."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            payload = os.path.join(tmp, "input.json")
            with open(harness, "w", encoding="utf-8") as fh:
                fh.write(TAB_HARNESS)
            with open(payload, "w", encoding="utf-8") as fh:
                json.dump({"frames": list(frames)}, fh)
            proc = subprocess.run([NODE, harness, APP_JS, payload],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["frames"]

    def opened(self, frame):
        return {key: sec["open"] for key, sec in frame["sections"].items()}

    # ------------------------------------------------- which section is open

    def test_only_the_current_step_is_open_to_begin_with(self):
        got = self.frames({"state": library_state(connected=False),
                           "preview": None})
        self.assertEqual(self.opened(got[0]),
                         {"connect": True, "scan": False, "identify": False,
                          "review": False})
        # A collapsed body is hidden, not merely unstyled: `[hidden]` is the one
        # way of hiding a thing on this page that no other rule can defeat.
        self.assertTrue(got[0]["sections"]["scan"]["hidden"])
        self.assertFalse(got[0]["sections"]["connect"]["hidden"])
        self.assertIn("is-current", got[0]["sections"]["connect"]["klass"])
        self.assertIn("is-next", got[0]["sections"]["scan"]["klass"])

    def test_a_scanned_library_opens_identify_and_collapses_the_rest(self):
        got = self.frames({"state": SCANNED, "preview": None})
        self.assertEqual(self.opened(got[0]),
                         {"connect": False, "scan": False, "identify": True,
                          "review": False})
        self.assertIn("is-done", got[0]["sections"]["scan"]["klass"])

    def test_a_finished_identify_opens_review_and_write(self):
        got = self.frames({"state": REVIEWABLE, "preview": PREVIEW})
        self.assertEqual(self.opened(got[0]),
                         {"connect": False, "scan": False, "identify": False,
                          "review": True})

    # ------------------------------------------------------------- the heads

    def test_a_head_carries_its_number_title_summary_and_count(self):
        got = self.frames({"state": SCANNED, "preview": None})
        head = got[0]["sections"]["identify"]["head"]
        self.assertIn('<span class="lib-num">3</span>', head)
        self.assertIn("Identify by fingerprint", head)
        self.assertIn('<span class="lib-badge">2,043</span>', head)
        self.assertIn("2,043 to fingerprint", plain(head))

    def test_a_done_head_ticks_instead_of_numbering_and_still_says_its_figures(self):
        got = self.frames({"state": SCANNED, "preview": None})
        head = got[0]["sections"]["scan"]["head"]
        self.assertIn('<span class="lib-num">&check;</span>', head)
        self.assertIn("7 identified, 2,043 still to identify", plain(head))
        self.assertIn('<span class="lib-badge">2,050</span>', head)

    def test_the_review_head_counts_every_kind_without_being_opened(self):
        got = self.frames({"state": REVIEWABLE, "preview": PREVIEW})
        head = got[0]["sections"]["review"]["head"]
        self.assertIn("Check matches and tag", head)
        self.assertIn("1,090 matched - 12 to choose - 19 no match", plain(head))
        self.assertIn('<span class="lib-badge">1,121</span>', head)

    def test_a_step_with_nothing_outstanding_draws_no_badge(self):
        got = self.frames({"state": library_state(connected=False),
                           "preview": None})
        self.assertNotIn("lib-badge", got[0]["sections"]["connect"]["head"])

    # --------------------------------------------------- what the user asked

    def test_a_section_closed_by_hand_stays_closed_through_a_re_render(self):
        got = self.frames({"state": SCANNED, "preview": None},
                          {"toggle": "identify"},
                          {"state": SCANNED, "preview": None})
        self.assertTrue(got[0]["sections"]["identify"]["open"])
        self.assertTrue(got[1]["clicked"])
        self.assertFalse(got[1]["sections"]["identify"]["open"])
        # The poll two seconds later must not re-open it.
        self.assertFalse(got[2]["sections"]["identify"]["open"])
        self.assertTrue(got[2]["sections"]["identify"]["hidden"])

    def test_a_section_opened_by_hand_stays_open_through_a_re_render(self):
        got = self.frames({"state": SCANNED, "preview": None},
                          {"toggle": "review"},
                          {"state": SCANNED, "preview": None})
        self.assertFalse(got[0]["sections"]["review"]["open"])
        self.assertTrue(got[1]["sections"]["review"]["open"])
        self.assertTrue(got[2]["sections"]["review"]["open"])
        self.assertFalse(got[2]["sections"]["review"]["hidden"])

    def test_jumping_to_a_done_step_from_the_strip_opens_it(self):
        # The step you revisit is usually a done one, which is collapsed by
        # default - so "go to step 2" landing on a one-line summary is not what
        # the strip's title means.
        got = self.frames({"state": SCANNED, "preview": None},
                          {"open": "scan"})
        self.assertFalse(got[0]["sections"]["scan"]["open"])
        self.assertTrue(got[1]["sections"]["scan"]["open"])

    def test_a_collapsed_review_section_draws_no_cover_grids_at_all(self):
        got = self.frames({"state": REVIEWABLE, "preview": PREVIEW},
                          {"toggle": "review"})
        self.assertIn("data-question=", got[0]["questions"])
        self.assertIn("<img", got[0]["questions"])
        # Not hidden - not drawn. `hidden` would leave every cover <img> in the
        # document and every one of them fetched, and the head's badge has
        # already said how many rows are in there.
        self.assertEqual(got[1]["questions"], "")

    # --------------------------------------------------------- the sticky bar

    def test_the_bar_offers_the_current_steps_action(self):
        got = self.frames({"state": library_state(connected=False), "preview": None},
                          {"state": library_state(), "preview": None},
                          {"state": SCANNED, "preview": None},
                          {"state": REVIEWABLE, "preview": PREVIEW})
        self.assertIn('data-bar-action="connect"', got[0]["bar"])
        self.assertIn("Test connection", got[0]["bar"])
        self.assertIn('data-bar-action="scan"', got[1]["bar"])
        self.assertIn('data-bar-action="identify"', got[2]["bar"])
        self.assertIn("Identify by fingerprint", got[2]["bar"])
        # Review and write: the write is the action, and accepting the safe
        # recommendations rides beside it while there are any.
        self.assertIn('data-bar-action="review"', got[3]["bar"])
        self.assertIn("Tag 1,090 videos", plain(got[3]["bar"]))
        self.assertIn('data-bar-accept="1"', got[3]["bar"])
        # One solid pink button: Tag. Use recommended beside it is outlined.
        self.assertRegex(got[3]["bar"], r'class="secondary"\s+data-bar-accept="1"')
        self.assertIn("Use recommended matches (12)", plain(got[3]["bar"]))
        self.assertFalse(got[3]["barHidden"])

    def test_the_bar_drops_accept_all_once_nothing_is_left_to_accept(self):
        # The three rows still to pick need looking at one at a time; a dead
        # Accept-all beside the write would only be in the way.
        got = self.frames({"state": REVIEWED, "preview": PREVIEW})
        self.assertIn('data-bar-action="review"', got[0]["bar"])
        self.assertIn("Tag 1,090 videos", plain(got[0]["bar"]))
        self.assertNotIn("data-bar-accept", got[0]["bar"])

    def test_the_bar_line_is_the_write_previews_own_summary(self):
        got = self.frames({"state": REVIEWED, "preview": PREVIEW})
        self.assertIn("Step 4 - Check matches and tag", plain(got[0]["bar"]))
        self.assertIn("1,090 ticked to tag - creates or re-stamps 231 models, "
                      "5 studios, 100 categories - 4 skipped",
                      plain(got[0]["bar"]))

    def test_the_bar_carries_the_include_tick_that_makes_apply_n_that_number(self):
        got = self.frames({"state": REVIEWED, "preview": PREVIEW})
        self.assertIn('data-bar-include="likely"', got[0]["bar"])
        self.assertIn("Include 1 likely", plain(got[0]["bar"]))

    def test_a_run_in_flight_takes_over_the_bar_line_and_disables_the_button(self):
        state = library_state(
            scan=scan(considered=2050, counts={"no-identity": 2031}),
            identify=identify(running=True, label="looking up",
                              done=300, total=2031))
        got = self.frames({"state": state, "preview": None})
        self.assertIn("Asking StashDB about fingerprints, 40 at a time: 300 of "
                      "2,031", plain(got[0]["bar"]))
        self.assertIn("disabled", got[0]["bar"])
        # The one cancel worth reaching without scrolling: it is the longest run.
        self.assertIn("data-bar-cancel", got[0]["bar"])

    def test_a_read_only_token_leaves_the_bars_write_disabled(self):
        state = json.loads(json.dumps(REVIEWED))
        state["canWrite"] = False
        got = self.frames({"state": state, "preview": PREVIEW})
        self.assertRegex(got[0]["bar"], r'data-bar-action="review"\s+disabled')

    # --------------------------------------------- accept all recommended

    # Many ambiguous rows are identical clicks when the grid has already put the
    # right card first. One button takes the lot; the rows it may not take stay,
    # and say why.

    def test_the_ambiguous_list_heads_with_the_accept_all_button(self):
        got = self.frames({"state": REVIEWABLE, "preview": PREVIEW})
        text = plain(got[0]["questions"])
        self.assertIn("Use recommended matches (12)", text)
        self.assertIn("Uses the recommended match for every video this view "
                      "shows whose length matches your file. Each moves to "
                      "Matched, ticked; the rest stay for you to choose.", text)
        # One button over every group, beside each group's own.
        self.assertEqual(got[0]["questions"].count("data-accept-recommended"), 1)
        # Outlined, not solid: Tag in the bar above it is the solid pink one.
        self.assertIn('class="secondary" data-accept-recommended="1"',
                      got[0]["questions"])

    def test_the_button_counts_only_the_rows_it_may_actually_take(self):
        # Most are safe; the other three are the ones whose lengths
        # disagree, and a count that included them would promise rows the click
        # will not move.
        state = json.loads(json.dumps(REVIEWABLE))
        state["identify"]["questions"] = ([question(n) for n in range(9)]
                                          + [unsafe(n) for n in range(9, 12)])
        got = self.frames({"state": state, "preview": PREVIEW})
        self.assertIn("Use recommended matches (9)", plain(got[0]["questions"]))
        self.assertIn("Use recommended matches (9)", plain(got[0]["bar"]))

    def test_the_button_is_dead_when_nothing_is_safe_to_accept(self):
        got = self.frames({"state": REVIEWED, "preview": PREVIEW})
        html = got[0]["questions"]
        self.assertIn("Use recommended matches (0)", plain(html))
        self.assertIn("data-accept-recommended=\"1\"\n      disabled", html)

    def test_what_the_accept_did_is_said_above_the_rows_that_stayed(self):
        # The rows it took have LEFT the list, so this line is the only thing
        # that says why the ones still here stayed.
        got = self.frames(
            {"state": REVIEWABLE, "preview": PREVIEW},
            {"state": REVIEWED, "preview": PREVIEW,
             "accepted": {"accepted": 9, "left": 3,
                          "leftReasons": {"durations disagree": 3}}})
        self.assertNotIn("Used ", plain(got[0]["questions"]))
        self.assertIn("Used 9 recommended matches - 3 still to choose "
                      "(durations disagree).",
                      plain(got[1]["questions"]))
        # And the bar no longer offers an accept that has nothing to take.
        self.assertNotIn("data-bar-accept", got[1]["bar"])

    def test_an_accept_that_emptied_the_list_still_reports_what_it_did(self):
        empty = library_state(
            scan=scan(considered=2050, counts={"verified": 2050},
                      buckets={"likely": [{"video_id": 900}]}),
            identify=identify())
        got = self.frames(
            {"state": empty, "preview": {"rows": [], "entities": {}, "skipped": 0},
             "open": "review",
             "accepted": {"accepted": 12, "left": 0, "leftReasons": {}}})
        text = plain(got[0]["questions"])
        self.assertIn("Used 12 recommended matches - 0 still to choose.", text)
        self.assertIn("Nothing to tag or check.", text)

    # ------------------------------------------------------------ the errors

    ERROR = "StashDB refused the batch: 429"

    def failed_identify(self):
        return library_state(
            scan=scan(considered=2050, counts={"no-identity": 2031}),
            identify=identify(error=self.ERROR))

    def test_an_error_shows_in_the_bar_and_inline_in_its_own_section(self):
        got = self.frames({"state": self.failed_identify(), "preview": None})
        for where in ("bar", "identify"):
            self.assertIn("Identify failed: " + self.ERROR, plain(got[0][where]))
            self.assertIn('data-dismiss-error="identify:%s"' % self.ERROR,
                          got[0][where])
            self.assertIn("Dismiss", got[0][where])
        self.assertIn("is-bad", got[0]["bar"])

    def test_dismissing_an_error_clears_both_halves_and_it_stays_dismissed(self):
        state = self.failed_identify()
        got = self.frames({"state": state, "preview": None},
                          {"dismiss": "identify:" + self.ERROR},
                          {"state": state, "preview": None})
        self.assertIn("Identify failed", plain(got[0]["bar"]))
        self.assertNotIn("Identify failed", plain(got[1]["bar"]))
        self.assertNotIn("Identify failed", plain(got[1]["identify"]))
        # A poll brings the same error back off the server on every read - there
        # is no route that clears one - so the dismissal has to hold.
        self.assertNotIn("Identify failed", plain(got[2]["bar"]))
        self.assertNotIn("Identify failed", plain(got[2]["identify"]))

    def test_a_dismissed_error_hands_the_bar_line_back_to_the_step(self):
        got = self.frames({"state": self.failed_identify(), "preview": None},
                          {"dismiss": "identify:" + self.ERROR})
        self.assertIn("2,031 to fingerprint", plain(got[1]["bar"]))

    # ------------------------------------------------- once the writes land

    def test_a_converged_preview_says_the_job_is_finished_not_zero(self):
        # The bar's apply line is normally the preview's own summary, and that
        # summary with nothing left in it is a row of noughts: "0 to write"
        # beside a dead button reads as a page that has lost its rows.
        got = self.frames({"state": APPLIED, "preview": APPLIED_PREVIEW})
        self.assertIn("Step 4 - Check matches and tag", plain(got[0]["bar"]))
        self.assertIn("Nothing left to tag. Tagged 160 videos.",
                      plain(got[0]["bar"]))
        self.assertNotIn("0 ticked to tag", plain(got[0]["bar"]))
        self.assertIn("disabled", got[0]["bar"])

    def test_the_last_step_only_reads_as_done_once_a_preview_has_been_fetched(self):
        # With no preview, no rows is ignorance rather than an empty queue - so
        # the bar must not congratulate the user on a run that has not happened.
        got = self.frames({"state": APPLIED, "preview": None})
        self.assertNotIn("Nothing left to tag", plain(got[0]["bar"]))


@unittest.skipIf(NODE is None, "node is not on PATH")
class WriteSectionTest(unittest.TestCase):
    """The write half of Review and write: the toolbar, and the ready cards.

    Every video ready to write is a card in the one list, grouped by model and
    ticked; the toolbar above the list counts the ticks and writes them. A report
    such as "Applied 160 videos, 0 entities, 210 aliases." above a list still
    offering the same 160 rows and a live `Write 160` is a contradiction. The
    server side of that is covered elsewhere; this is the half the page renders.
    """

    #: A preview with rows in it, the keys a ready card reads. `PREVIEW` above
    #: counts rows and nothing else, which is all the strip needs.
    ROWS = {"rows": [{"video_id": n, "status": "verified", "confidence": "oshash",
                      "title": "Video %d" % n, "path": "D:\\a\\%d.mp4" % n,
                      "scene_title": "Scene %d" % n, "rel_path": "a/%d.mp4" % n,
                      "scene_id": "sc-%d" % n,
                      "cover": "https://cdn.example/sc-%d.jpg" % n,
                      "scene_date": "2026-10-02", "scene_date_estimated": False,
                      "note": None, "duration_secs": 1800.0,
                      "scene_duration": 1800.0, "duration_agrees": True,
                      "models": [model("Jane Example")],
                      "fields": [{"field": "title", "action": "rename",
                                  "current": "Video %d" % n,
                                  "proposed": "Scene %d" % n}],
                      "diff": [{"field": "title", "current": None,
                                "proposed": "Scene %d" % n}]}
                     for n in range(3)],
            "entities": {"models": 2, "studios": 1, "categories": 0, "aliases": 4},
            "skipped": 0}

    def frames(self, *frames):
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            payload = os.path.join(tmp, "input.json")
            with open(harness, "w", encoding="utf-8") as fh:
                fh.write(PREVIEW_HARNESS)
            with open(payload, "w", encoding="utf-8") as fh:
                json.dump({"frames": list(frames)}, fh)
            proc = subprocess.run([NODE, harness, APP_JS, payload],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["frames"]

    def test_an_empty_preview_after_a_run_says_nothing_is_left_and_what_landed(self):
        got = self.frames({"state": APPLIED, "preview": APPLIED_PREVIEW})
        self.assertIn("Nothing left to tag. Tagged 160 videos.",
                      plain(got[0]["preview"]))
        # The run's own report is untouched beside it: what the apply did, and
        # what the queue now holds, are two different sentences.
        self.assertIn("Tagged 160 videos, 0 entities, 210 aliases.",
                      plain(got[0]["preview"]))
        self.assertTrue(got[0]["applyDisabled"])

    def test_an_empty_preview_before_any_run_still_asks_for_a_scan(self):
        # Nothing has been applied, so "nothing left to write" would be a claim
        # about a run that never happened.
        state = library_state()
        empty = {"rows": [], "entities": {}, "skipped": 0}
        got = self.frames({"state": state, "preview": empty})
        self.assertIn("Nothing matched to tag yet.", plain(got[0]["preview"]))
        self.assertNotIn("Nothing left to tag", plain(got[0]["preview"]))

    def test_rows_still_to_write_are_cards_in_the_list_and_the_button_is_live(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "groupOpen": {"m-janeexample": True}})
        self.assertRegex(got[0]["list"],
                         r'<article class="question is-ready" data-question="0">')
        self.assertFalse(got[0]["applyDisabled"])
        self.assertIn("Tag 3 videos", plain(got[0]["preview"]))
        self.assertNotIn("Nothing left to tag", plain(got[0]["preview"]))

    def test_a_run_whose_rows_could_not_be_re_read_says_the_preview_may_be_stale(self):
        # The writes landed; only the page's picture of them is behind. That is
        # worth saying on the run, and it is not an error.
        state = json.loads(json.dumps(APPLIED))
        state["apply"]["finished"]["note"] = (
            "written; preview may be stale until the next scan")
        got = self.frames({"state": state, "preview": self.ROWS})
        self.assertIn("written; preview may be stale until the next scan",
                      plain(got[0]["preview"]))

    def test_the_section_and_the_bar_agree_that_nothing_is_left(self):
        got = self.frames({"state": APPLIED, "preview": APPLIED_PREVIEW})
        for where in ("preview", "bar"):
            self.assertIn("Nothing left to tag. Tagged 160 videos.",
                          plain(got[0][where]))

    # ------------------------------------------- while an identify run goes on

    def test_the_write_stays_live_while_an_identify_run_is_going(self):
        # The rows in the preview are ones the run has finished with; the ones
        # it is still deciding are in no preview at all.
        state = json.loads(json.dumps(REVIEWABLE))
        state["identify"]["running"] = True
        got = self.frames({"state": state, "preview": self.ROWS})
        self.assertFalse(got[0]["applyDisabled"])

    def test_a_row_you_unticked_stays_unticked_through_the_next_poll(self):
        # The page polls every two seconds while a run goes on, and every poll
        # redraws from the server's answer, which knows nothing of ticks.
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "unpick": [1], "groupOpen": {"m-janeexample": True}})
        self.assertRegex(got[0]["list"], r'data-pick="0"\s+checked')
        self.assertNotRegex(got[0]["list"], r'data-pick="1"\s+checked')

    def test_the_write_counts_only_what_is_ticked(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS, "unpick": [1]})
        self.assertIn("2 ticked to tag of 3 matched", plain(got[0]["preview"]))
        self.assertIn("Tag 2 videos", plain(got[0]["preview"]))
        self.assertIn("Tag 2 videos", plain(got[0]["bar"]))

    def test_nothing_ticked_leaves_the_write_dead(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "unpick": [0, 1, 2]})
        self.assertTrue(got[0]["applyDisabled"])

    def test_rows_a_run_settles_inside_the_window_are_not_tagged_unseen(self):
        # Mid-run a new row joins the list at most once in five seconds. Until
        # it has, it is not in Tag N either: nothing is tagged that the list
        # has not shown.
        running = json.loads(json.dumps(REVIEWABLE))
        running["identify"]["running"] = True
        # No questions: theirs are numbered from 0, like these rows, and a
        # question already on screen would count its id as seen.
        running["identify"]["questions"] = []
        two = dict(self.ROWS, rows=self.ROWS["rows"][:2])
        got = self.frames({"state": running, "preview": two},
                          {"state": running, "preview": self.ROWS})
        self.assertIn("Tag 2 videos", plain(got[0]["preview"]))
        self.assertIn("Tag 2 videos", plain(got[1]["preview"]))
        self.assertIn("1 new, not in the list yet", plain(got[1]["preview"]))
        self.assertNotIn("Show them", plain(got[1]["preview"]))

    def test_rows_released_into_the_list_are_counted_by_tag_n(self):
        running = json.loads(json.dumps(REVIEWABLE))
        running["identify"]["running"] = True
        running["identify"]["questions"] = []
        two = dict(self.ROWS, rows=self.ROWS["rows"][:2])
        got = self.frames({"state": running, "preview": two, "now": 1000000},
                          {"state": running, "preview": self.ROWS,
                           "now": 1006000})
        self.assertIn("Tag 2 videos", plain(got[0]["preview"]))
        # Released on their own, ticked like any other: the line is gone.
        self.assertIn("Tag 3 videos", plain(got[1]["preview"]))
        self.assertNotIn("not in the list yet", plain(got[1]["preview"]))

    def test_once_the_run_ends_every_ready_row_is_shown_and_counted(self):
        running = json.loads(json.dumps(REVIEWABLE))
        running["identify"]["running"] = True
        two = dict(self.ROWS, rows=self.ROWS["rows"][:2])
        got = self.frames({"state": running, "preview": two},
                          {"state": REVIEWABLE, "preview": self.ROWS})
        self.assertIn("Tag 3 videos", plain(got[1]["preview"]))

    # ------------------------------------- the buttons follow the view

    # What you see is what you tag. The tabs and the filename filter narrow
    # Tag and Use recommended exactly as they narrow the list, and whatever
    # ticked is left outside the view is counted, not sent.

    def test_a_filter_narrows_tag_to_the_ticked_videos_it_shows(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "filter": "a/0"})
        text = plain(got[0]["preview"])
        self.assertIn("1 ticked to tag in this view - 2 more ticked outside it, "
                      "not tagged", text)
        # The entity totals are the whole preview's, so with fewer than every
        # matched video going they can only be a ceiling.
        self.assertIn("creates or re-stamps up to 2 models, 1 studio", text)
        for where in ("preview", "bar"):
            self.assertIn("Tag 1 video", plain(got[0][where]))
            self.assertNotIn("Tag 3 videos", plain(got[0][where]))

    def test_nothing_outside_the_view_reads_as_the_plain_count(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "kind": "ready"})
        text = plain(got[0]["preview"])
        self.assertIn("3 ticked to tag - creates or re-stamps 2 models", text)
        self.assertNotIn("in this view", text)
        self.assertNotIn("outside it", text)

    def test_choose_a_match_offers_use_recommended_and_no_tag(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "kind": "ambiguous"})
        said = ("Nothing here is tagged until you use a match - it then moves "
                "to Matched, ticked.")
        self.assertIn(said, plain(got[0]["preview"]))
        self.assertNotIn('id="lib-apply"', got[0]["preview"])
        self.assertNotIn("data-preview-include", got[0]["preview"])
        self.assertIn(said, plain(got[0]["bar"]))
        self.assertNotIn('data-bar-action="review"', got[0]["bar"])
        self.assertNotIn("data-bar-include", got[0]["bar"])
        self.assertIn("Use recommended matches (12)", plain(got[0]["bar"]))
        self.assertIn("Use recommended matches (12)", plain(got[0]["list"]))

    def test_matched_offers_tag_and_no_use_recommended(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "kind": "ready"})
        self.assertNotIn("data-accept-recommended", got[0]["list"])
        self.assertNotIn("data-bar-accept", got[0]["bar"])
        for where in ("preview", "bar"):
            self.assertIn("Tag 3 videos", plain(got[0][where]))

    def test_no_match_offers_neither(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "kind": "unmatched"})
        said = "Nothing here can be tagged until you find it with Search by name."
        self.assertIn(said, plain(got[0]["preview"]))
        self.assertIn(said, plain(got[0]["bar"]))
        self.assertNotIn('id="lib-apply"', got[0]["preview"])
        self.assertNotIn('data-bar-action="review"', got[0]["bar"])
        self.assertNotIn("data-accept-recommended", got[0]["list"])
        self.assertNotIn("data-bar-accept", got[0]["bar"])

    def test_use_recommended_counts_only_the_rows_the_filter_shows(self):
        # REVIEWABLE's rows to choose are library/scenes/0..11.mp4, so
        # "scenes/1" keeps 1, 10 and 11.
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "filter": "scenes/1"})
        self.assertIn("Use recommended matches (3)", plain(got[0]["list"]))
        self.assertIn("Use recommended matches (3)", plain(got[0]["bar"]))

    def test_a_groups_use_recommended_counts_only_what_the_filter_shows(self):
        state = json.loads(json.dumps(REVIEWABLE))
        state["identify"]["questions"] = [
            question(n, models=("Kay Example",)) for n in (1, 2, 10)]
        got = self.frames({"state": state, "preview": self.ROWS,
                           "filter": "scenes/1"})
        text = plain(got[0]["list"])
        self.assertIn("Kay Example 2 - 2 to choose Use 2 recommended", text)

    def test_nothing_ready_reads_as_nothing_ready_not_as_a_nought(self):
        got = self.frames({"state": REVIEWABLE,
                           "preview": {"rows": [], "entities": {}, "skipped": 0}})
        self.assertIn("Nothing matched to tag", plain(got[0]["preview"]))
        self.assertNotIn("0 ticked", plain(got[0]["preview"]))

    def test_a_finished_write_with_files_still_not_found_says_so_in_the_bar(self):
        # The step is never done while a file is not found - it is this step's
        # work, looked up by name - so the bar cannot lean on "done" to stop
        # printing a nought beside a dead Write.
        state = json.loads(json.dumps(APPLIED))
        state["scan"]["buckets"] = {"unmatched": [{"video_id": n} for n in range(5)]}
        got = self.frames({"state": state, "preview": APPLIED_PREVIEW})
        bar = plain(got[0]["bar"])
        self.assertIn("Nothing left to tag. Tagged 160 videos.", bar)
        self.assertIn("5 no match", bar)
        self.assertNotIn("0 ticked", bar)

    # ------------------------------------------------ a model's own tick

    def test_a_models_tick_is_ticked_when_all_its_ready_videos_are(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS})
        self.assertRegex(got[0]["list"],
                         r'data-group-pick="m-janeexample"\s+checked')
        self.assertIn("3 matched (all ticked)", plain(got[0]["list"]))

    def test_a_half_ticked_model_is_marked_and_counted(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS, "unpick": [1]})
        html = got[0]["list"]
        self.assertNotRegex(html, r'data-group-pick="m-janeexample"\s+checked')
        self.assertRegex(html, r'data-group-pick="m-janeexample"[^>]*data-partial="1"')
        self.assertIn("3 matched (2 ticked)", plain(html))

    def test_an_unticked_model_says_none_ticked(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "unpick": [0, 1, 2]})
        self.assertIn("3 matched (none ticked)", plain(got[0]["list"]))

    # ---------------------------------------------- one list, three kinds

    def test_inside_a_group_to_pick_comes_before_ready_before_not_found(self):
        state = json.loads(json.dumps(REVIEWABLE))
        state["identify"]["questions"] = [question(500, models=("Jane Example",))]
        for row in state["scan"]["buckets"]["unmatched"]:
            row["models"] = [model("Jane Example")]
        got = self.frames({"state": state, "preview": self.ROWS})
        html = got[0]["list"]
        self.assertLess(html.index('data-question="500"'), html.index('data-question="0"'))
        self.assertLess(html.index('data-question="0"'), html.index('data-question="9000"'))
        self.assertIn("Jane Example 23 - 3 matched (all ticked) · 1 to choose "
                      "· 19 no match", plain(html))

    def test_each_card_says_which_kind_it_is(self):
        state = json.loads(json.dumps(REVIEWABLE))
        state["identify"]["questions"] = [question(500, models=("Jane Example",))]
        got = self.frames({"state": state, "preview": self.ROWS,
                           "groupOpen": {"m-janeexample": True, "nomodel": True}})
        html = got[0]["list"]
        for kind, label in (("ready", "Matched"), ("pick", "Choose a match"),
                            ("none", "No match")):
            self.assertIn('<span class="kind-badge %s">%s</span>' % (kind, label), html)

    def test_the_chips_count_each_kind_and_narrow_every_group_to_one(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS},
                          {"state": REVIEWABLE, "preview": self.ROWS,
                           "kind": "ready"})
        self.assertEqual(plain(got[0]["kinds"]),
                         "All 34 Choose a match 12 Matched 3 No match 19 "
                         "Pending hashing 0")
        self.assertIn('class="kind-chip is-on"', got[1]["kinds"])
        html = got[1]["list"]
        self.assertEqual(html.count("data-question="), 3)
        self.assertEqual(html.count('class="question is-ready"'), 3)

    # ------------------------------------------- your file beside its scene

    def test_a_ready_card_sets_your_frame_beside_the_scene_it_matched(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "groupOpen": {"m-janeexample": True}})
        html, text = got[0]["list"], plain(got[0]["list"])
        self.assertIn('data-frame-src="/api/library/frame?video=0&amp;n=0"', html)
        self.assertIn("StashDB - Scene 0", text)
        self.assertIn('<img src="https://cdn.example/sc-0.jpg"', html)
        self.assertIn("Length 30:00 30:00 - matches", text)
        self.assertIn("Your file now After tagging", text)
        self.assertIn('href="https://stashdb.org/scenes/sc-0"', html)

    # ------------------------------------------- how each video was matched

    def test_a_matched_card_says_in_words_how_it_was_matched(self):
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS,
                           "groupOpen": {"m-janeexample": True}})
        html, text = got[0]["list"], plain(got[0]["list"])
        self.assertIn("Matched by its exact file hash - the length matches", text)
        # The words replace the status and hash pills, which named the
        # machinery rather than what it found.
        self.assertNotIn(">verified<", html)
        self.assertNotIn(">oshash<", html)
        self.assertIn('aria-label="Tag this video"', html)

    def test_every_way_a_video_is_matched_has_its_own_words(self):
        rows = {
            "hash": {"status": "verified", "confidence": "oshash",
                     "durationAgrees": True},
            "folder-hash": {"status": "verified", "confidence": None,
                            "durationAgrees": None},
            "you": {"status": "verified", "confidence": "manual",
                    "durationAgrees": True},
            "folder": {"status": "verified-by-duration", "confidence": None,
                       "durationAgrees": True},
            "likely": {"status": "likely", "confidence": "phash",
                       "durationAgrees": True},
            "unverified": {"status": "unverified", "confidence": None,
                           "durationAgrees": None},
            "short": {"status": "verified", "confidence": "oshash",
                      "durationAgrees": False, "durationSecs": 1500,
                      "sceneDuration": 1800},
        }
        js = ("(() => { const rows = %s; return Object.fromEntries("
              "Object.entries(rows).map(([k, q]) => [k, matchedHow(q)])); })()"
              % json.dumps(rows))
        said = {key: plain(value)
                for key, value in run_eval(self, REVIEWABLE, js)["out"].items()}
        self.assertEqual(said["hash"], "by its exact file hash - the length matches")
        self.assertEqual(said["folder-hash"], "by its exact file hash - length unknown")
        self.assertEqual(said["you"], "by you - the length matches")
        self.assertEqual(said["folder"],
                         "by the StashDB id in its folder name - the length matches")
        self.assertEqual(said["likely"],
                         "by how it looks (perceptual hash), not proven - the "
                         "length matches")
        self.assertEqual(said["unverified"],
                         "by the StashDB id in its folder name, not confirmed - "
                         "length unknown")
        self.assertEqual(said["short"],
                         "by its exact file hash - the length is 5 min short")

    # ------------------------------------------ what the write does, per field

    #: One video's full per-field picture, the way `plan_change` reports it.
    FIELDS = [
        {"field": "title", "action": "rename", "current": "Janeexample Beach Day",
         "proposed": "JANE EXAMPLE Beach Day"},
        {"field": "release_date", "action": "change", "current": "2026-10-06",
         "proposed": "2026-10-02", "current_estimated": False,
         "proposed_estimated": False},
        {"field": "studio", "action": "set", "current": None,
         "proposed": "Example Studio", "new": True},
        {"field": "models", "action": "add", "kept": ["Jane Example"],
         "added": [{"name": "Sam Sample", "new": True}]},
        {"field": "categories", "action": "add", "kept": ["4K"],
         "added": [{"name": "Outdoor", "new": False},
                   {"name": "Sunset", "new": False},
                   {"name": "Beach", "new": False}]},
        {"field": "external_ids", "action": "add", "current": None,
         "proposed": "scene-1"},
    ]

    def preview_of(self, fields):
        """The ready card for one row carrying ``fields``: (html, plain text)."""
        row = dict(self.ROWS["rows"][0], fields=fields)
        preview = dict(self.ROWS, rows=[row])
        html = self.frames({"state": REVIEWABLE, "preview": preview,
                            "groupOpen": {"m-janeexample": True}})[0]["list"]
        return html, plain(html)

    def test_every_field_says_what_the_write_does_to_it(self):
        # Label, what the file has now, then the verb and what it will have.
        html, text = self.preview_of(self.FIELDS)
        self.assertIn("Title Janeexample Beach Day Rename JANE EXAMPLE Beach Day",
                      text)
        self.assertIn("Release date 2026-10-06 Change 2026-10-02", text)
        self.assertRegex(text, r"Studio \S+ Set Example Studio new to your library")
        self.assertIn("Models Jane Example Add 1 Jane Example + Sam Sample new to "
                      "your library", text)
        self.assertIn("Categories 4K Add 3 4K + Outdoor + Sunset + Beach", text)
        self.assertRegex(text, r"StashDB link \S+ Add scene-1")
        # What is kept is a plain chip, what is added a green one.
        self.assertRegex(html, r'class="fchip kept"[^>]*>4K<')
        self.assertRegex(html, r'class="fchip added"[^>]*>\+ Outdoor')

    def test_the_verb_is_coloured_by_what_kind_of_change_it_is(self):
        # Overwrites amber, filling a blank blue, adding green, nothing grey.
        fields = self.FIELDS + [{"field": "external_ids", "action": "same",
                                 "current": "scene-1", "proposed": "scene-1"}]
        html, _ = self.preview_of(fields)
        self.assertRegex(html, r'class="fverb overwrite"[^>]*>Rename<')
        self.assertRegex(html, r'class="fverb overwrite"[^>]*>Change<')
        self.assertRegex(html, r'class="fverb fill"[^>]*>Set<')
        self.assertRegex(html, r'class="fverb add"[^>]*>Add 1<')
        self.assertRegex(html, r'class="fverb same"[^>]*>No change<')

    def test_one_line_says_it_all_before_the_fields(self):
        _, text = self.preview_of(self.FIELDS)
        self.assertIn("Renames title - changes date - sets studio - adds 1 model - "
                      "adds 3 categories - links to StashDB", text)

    def test_every_video_says_nothing_is_removed(self):
        _, text = self.preview_of(self.FIELDS)
        self.assertIn("Nothing on this video is removed.", text)

    def test_what_is_already_there_shows_even_when_it_does_not_change(self):
        fields = [
            {"field": "title", "action": "same", "current": "Studio Tour",
             "proposed": "Studio Tour"},
            {"field": "studio", "action": "replace", "current": "Old Studio",
             "proposed": "Example Network", "new": False},
            {"field": "models", "action": "same", "kept": ["Jane Example"],
             "added": []},
        ]
        html, text = self.preview_of(fields)
        self.assertIn("Title Studio Tour No change Studio Tour", text)
        self.assertIn("Studio Old Studio Replace Example Network", text)
        self.assertNotIn("new to your library", text)
        self.assertIn("Models Jane Example No change Jane Example", text)
        self.assertIn("Replaces studio", text)

    def test_a_field_switched_off_says_off_not_no_change(self):
        _, text = self.preview_of([{"field": "title", "action": "off",
                                    "current": "A Mp4", "proposed": "A Mp4"}])
        self.assertIn("Title A Mp4 Off A Mp4 (turned off in settings)", text)

    def test_only_the_estimated_flag_changing_says_which_way(self):
        _, text = self.preview_of([{"field": "release_date", "action": "change",
                                    "current": "2026-10-06", "proposed": "2026-10-06",
                                    "current_estimated": True,
                                    "proposed_estimated": False}])
        self.assertIn("Release date 2026-10-06 (estimated) Change 2026-10-06", text)
        self.assertIn("Changes date", text)

    def test_a_poll_with_the_same_preview_rebuilds_nothing(self):
        # Rebuilding on every poll re-creates every picture and snaps the page
        # back under the pointer, for a run that can last hours.
        got = self.frames({"state": REVIEWABLE, "preview": self.ROWS},
                          {"sentinel": "<!--kept-->", "state": REVIEWABLE,
                           "preview": self.ROWS})
        self.assertIn('id="lib-apply"', got[0]["preview"])
        self.assertEqual(got[1]["preview"], "<!--kept-->")


#: Evaluates `input.js` against `input.state` and prints the result. For the
#: renders that need nothing but a state; `fetch` can be swapped by the frame.
EVAL_HARNESS = DOM_STUB + r"""
const calls = [];
sandbox.fetch = (url, options) => {
  calls.push((options && options.method || "GET") + " " + url);
  return Promise.resolve({ ok: true,
    json: () => Promise.resolve(url === "/api/library/state"
                                ? sandbox.__input.state
                                : (sandbox.__input.answers || {})[url] || {}) });
};
(async () => {
  vm.runInContext("library.state = __input.state;", ctx);
  const out = await vm.runInContext(input.js, ctx);
  // Exits once the answer is out, rather than waiting on timers the page left
  // running - the frame queue's give-up timer is 75 seconds.
  process.stdout.write(JSON.stringify({ out, calls }), () => process.exit(0));
})().catch((err) => { console.error(err); process.exit(1); });
"""


def run_eval(test, state, js, answers=None):
    """Run `js` in the page against `state`; `answers` maps an API path to the
    payload `fetch` returns for it. Returns `{"out": ..., "calls": [...]}`."""
    with tempfile.TemporaryDirectory() as tmp:
        harness = os.path.join(tmp, "harness.js")
        payload = os.path.join(tmp, "input.json")
        with open(harness, "w", encoding="utf-8") as fh:
            fh.write(EVAL_HARNESS)
        with open(payload, "w", encoding="utf-8") as fh:
            json.dump({"state": state, "js": js, "answers": answers or {}}, fh)
        proc = subprocess.run([NODE, harness, APP_JS, payload],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    test.assertEqual(proc.returncode, 0, proc.stderr)
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "node is not on PATH")
class SetupTest(unittest.TestCase):
    """First-run setup on the page, and wording that assumes nothing."""

    def run_js(self, state, js):
        return run_eval(self, state, js)

    def steps(self, state):
        out = self.run_js(state, "libraryWorkflowSteps(library.state, null)")
        return {s["key"]: s for s in out["out"]}

    def test_connect_step_waits_for_both_credentials(self):
        steps = self.steps(library_state(connected=True, stashdbConfigured=False))
        self.assertEqual(steps["connect"]["state"], "current")

    def test_later_steps_wait_for_setup(self):
        steps = self.steps(library_state(connected=True, stashdbConfigured=False))
        for key in ("scan", "identify", "review"):
            self.assertNotEqual(steps[key]["state"], "current", key)

    def test_setup_panel_offers_both_credentials_and_says_where_they_come_from(self):
        out = self.run_js(
            library_state(connected=False, hasToken=False, stashdbConfigured=False),
            "renderLibraryConnect(); document.querySelector('#library-connect').innerHTML")
        html = out["out"]
        for needle in ("lib-stashdb-key", "lib-stashdb-save", "lib-token",
                       "stashdb.org", "Connected apps", "Update video metadata"):
            self.assertIn(needle, html)
        self.assertNotIn("restart", html.lower())

    def test_the_stashdb_key_hint_links_to_stashdb(self):
        out = self.run_js(
            library_state(connected=False, hasToken=False, stashdbConfigured=False),
            "renderLibraryConnect(); document.querySelector('#library-connect').innerHTML")
        self.assertRegex(
            out["out"],
            r'Your API key is on your profile page at <a class="ext-link" '
            r'href="https://stashdb\.org/" target="_blank" rel="noopener">'
            r'stashdb\.org &#8599;</a>\.')

    def test_saved_token_but_no_connection_says_what_to_check(self):
        out = self.run_js(
            library_state(connected=False, hasToken=True),
            "renderLibraryConnect(); document.querySelector('#library-connect').innerHTML")
        self.assertIn("Is Pronoisseur running", out["out"])

    def test_boot_tries_a_saved_token_exactly_once(self):
        out = self.run_js(library_state(connected=False, hasToken=True),
                          "boot().then(() => 1)")
        self.assertEqual(out["calls"].count("POST /api/library/connect"), 1)

    def test_boot_does_not_connect_without_a_saved_token(self):
        out = self.run_js(library_state(connected=False, hasToken=False),
                          "boot().then(() => 1)")
        self.assertEqual(out["calls"].count("POST /api/library/connect"), 0)

    def test_wording_leaves_whisparr_out_when_nothing_disagrees(self):
        state = library_state(scan=scan(considered=10, counts={"verified": 10}))
        out = self.run_js(state, """(() => {
          library.steps = libraryWorkflowSteps(library.state, null);
          renderLibraryWorkflow(); renderLibraryConnect(); renderLibraryBuckets();
          return ["#library-workflow", "#library-connect", "#library-buckets"]
            .map((sel) => document.querySelector(sel).innerHTML).join(" ");
        })()""")
        self.assertNotIn("Whisparr", out["out"])
        fresh = self.steps(library_state())
        self.assertNotIn("Whisparr", fresh["scan"]["hint"])

    CONNECT_PANEL = "document.querySelector('#library-connect').innerHTML"

    def failed_connect(self, status, error="nope"):
        answer = {"connected": False, "url": "http://127.0.0.1:8869",
                  "error": error, "status": status, "code": None,
                  "canWrite": False, "scopes": []}
        js = ("(async () => { toast = () => {}; "
              "await connectLibrary(); await loadLibrary(); "
              "return %s; })()" % self.CONNECT_PANEL)
        return run_eval(self, library_state(connected=False, hasToken=False), js,
                        answers={"/api/library/connect": answer})["out"]

    def test_nothing_answering_says_to_try_a_different_port(self):
        html = self.failed_connect(None)
        for needle in ("http://127.0.0.1:8869", "Is Pronoisseur running",
                       "Enable the Local API", "different port", "28869"):
            self.assertIn(needle, html)

    def test_nothing_answering_points_to_pronoisseur_and_its_setup_help(self):
        # The one moment on the page where the user may not have Pronoisseur
        # yet, or has it but not the Local API switched on.
        html = self.failed_connect(None)
        self.assertIn('href="https://pronoisseur.com/help/14-connected-apps/"', html)
        # Its own plain line after the warning, not the tail of the port advice.
        self.assertRegex(html, r'</div><div class="hint pad">Don\'t have Pronoisseur '
                               r'yet\? Get it at <a [^>]*href="https://pronoisseur\.com/"')
        for link in re.findall(r"<a [^>]*>", html):
            self.assertIn('target="_blank"', link)
            self.assertIn('rel="noopener"', link)

    def test_a_refused_token_says_so_and_leaves_the_port_alone(self):
        html = self.failed_connect(401)
        self.assertIn("refused this token", html)
        self.assertNotIn("different port", html)
        # Pronoisseur answered, so there is nothing to get.
        self.assertNotIn("Don't have Pronoisseur yet?", html)

    def test_another_failure_shows_its_reason_as_text(self):
        html = self.failed_connect(404, "<img src=x onerror=alert(1)>")
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)
        self.assertIn("different port", html)

    def test_a_good_connection_clears_the_failure(self):
        js = ("(async () => { toast = () => {}; library.connectFailure = "
              "{url: 'x', error: 'y', status: null}; "
              "await connectLibrary(); await loadLibrary(); "
              "return [library.connectFailure, %s]; })()" % self.CONNECT_PANEL)
        answer = {"connected": True, "canWrite": True, "url": "x", "status": None}
        failure, html = run_eval(self, library_state(connected=True, hasToken=True), js,
                                 answers={"/api/library/connect": answer})["out"]
        self.assertIsNone(failure)
        self.assertNotIn("different port", html)

    def test_no_failure_text_before_a_connection_has_been_tried(self):
        out = self.run_js(library_state(connected=False, hasToken=False),
                          "renderLibraryConnect(); " + self.CONNECT_PANEL)
        self.assertNotIn("different port", out["out"])

    def test_a_saved_token_that_fails_at_startup_keeps_its_reason(self):
        answer = {"connected": False, "url": "http://127.0.0.1:8869",
                  "error": "nope", "status": 401}
        out = run_eval(self, library_state(connected=False, hasToken=True),
                       "boot().then(() => %s)" % self.CONNECT_PANEL,
                       answers={"/api/library/connect": answer})
        self.assertIn("refused this token", out["out"])

    def link_line(self, link_patterns, **over):
        state = library_state(**over)
        if link_patterns is not None:
            state["linkPatterns"] = link_patterns
        return self.run_js(
            state, "renderLibraryConnect(); " + self.CONNECT_PANEL)["out"]

    def test_link_patterns_registered_says_links_are_on(self):
        html = self.link_line({"status": "registered", "error": None})
        self.assertIn("StashDB links are on in Pronoisseur's Details pane.", html)

    def test_link_patterns_unsupported_says_to_update(self):
        html = self.link_line({"status": "unsupported", "error": None})
        self.assertIn("This Pronoisseur can't show StashDB links. "
                      "Update to 1.0.62 or later.", html)

    def test_link_patterns_read_only_says_a_write_token_is_needed(self):
        html = self.link_line({"status": "read-only", "error": None},
                              can_write=False)
        self.assertIn("StashDB links need a Pronoisseur token with write access.",
                      html)

    def test_link_patterns_failed_is_a_warning_with_the_error_escaped(self):
        html = self.link_line({"status": "failed", "error": HOSTILE})
        self.assertIn("Couldn't turn on StashDB links in Pronoisseur: ", html)
        self.assertIn("&lt;img src=x", html)
        self.assertNotIn("<img src=x", html)
        self.assertRegex(html, r'class="[^"]*warn[^"]*"[^>]*>\s*Couldn')

    def test_link_patterns_not_yet_says_nothing(self):
        for payload in ({"status": "not yet", "error": None}, None):
            html = self.link_line(payload)
            self.assertNotIn("StashDB links", html)
            self.assertNotIn("Couldn't turn on", html)

    def test_link_patterns_say_nothing_while_disconnected(self):
        html = self.link_line({"status": "registered", "error": None},
                              connected=False)
        self.assertNotIn("StashDB links", html)

    def test_mapping_screen_can_refresh_categories(self):
        with open(APP_JS, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("/api/library/categories/refresh", source)
        self.assertIn("lib-refresh-categories", source)


#: A string that becomes an element if any render drops it into markup unescaped.
HOSTILE = "<img src=x onerror=alert(1)>"


@unittest.skipIf(NODE is None, "node is not on PATH")
class HostileStringsTest(unittest.TestCase):
    """Every string that arrives from outside the page renders as text.

    File names, StashDB titles, studios, tags and notes are all someone else's
    words, and they are written into the page as markup.
    """

    def hostile_row(self):
        row = question(7)
        row.update(title=HOSTILE, path=HOSTILE, relPath=HOSTILE, note=HOSTILE,
                   whisparrSceneId=HOSTILE, videoId=HOSTILE,
                   terms={"studio": HOSTILE, "date": HOSTILE,
                          "performers": [HOSTILE]},
                   # A model's name and key both come from someone's library.
                   models=[{"name": HOSTILE, "key": HOSTILE},
                           {"name": HOSTILE + "2", "key": HOSTILE + "2"}])
        for card in row["candidates"]:
            card.update(title=HOSTILE, studio=HOSTILE, date=HOSTILE,
                        sceneId=HOSTILE, tagCount=HOSTILE,
                        performerCount=HOSTILE, distance=HOSTILE,
                        durationDelta=HOSTILE, cover=HOSTILE,
                        fields=[{"field": "title", "action": "rename",
                                 "current": HOSTILE, "proposed": HOSTILE},
                                {"field": HOSTILE, "action": HOSTILE,
                                 "current": HOSTILE, "proposed": HOSTILE},
                                {"field": "models", "action": "add",
                                 "kept": [HOSTILE],
                                 "added": [{"name": HOSTILE, "new": True}]}])
        return row

    def hostile_state(self):
        disagreement = {"relPath": HOSTILE, "whisparrSceneId": HOSTILE,
                        "matchedSceneId": HOSTILE, "matchedTitle": HOSTILE,
                        "status": HOSTILE}
        return library_state(
            disagreements=[disagreement],
            scan=scan(considered=5, counts={"verified": HOSTILE},
                      running=True, label=HOSTILE, done=HOSTILE, total=HOSTILE,
                      error=HOSTILE),
            identify=identify(questions=[self.hostile_row()], running=True,
                              label=HOSTILE, done=HOSTILE, total=HOSTILE),
            apply=run_state(
                running=True, label=HOSTILE, done=HOSTILE, total=HOSTILE,
                error=HOSTILE, details={"batch": HOSTILE},
                finished={"applied": HOSTILE, "entities": HOSTILE,
                          "aliases": HOSTILE, "refused": 2, "note": HOSTILE,
                          "refusals": [{"videoId": HOSTILE, "error": HOSTILE}]}),
            url=HOSTILE, info={"app_version": HOSTILE, "app": {"name": HOSTILE}},
            scopes=[HOSTILE])

    PREVIEW_ROW = {"video_id": HOSTILE, "status": HOSTILE, "confidence": HOSTILE,
                   "scene_title": HOSTILE, "rel_path": HOSTILE, "cover": HOSTILE,
                   "scene_date": HOSTILE, "scene_date_estimated": True,
                   "note": HOSTILE, "path": HOSTILE, "title": HOSTILE,
                   "scene_id": HOSTILE, "scene_duration": 60, "duration_secs": 60,
                   "duration_agrees": True,
                   "models": [{"name": HOSTILE, "key": HOSTILE}],
                   "diff": [{"field": HOSTILE, "current": HOSTILE,
                             "proposed": [HOSTILE]}],
                   "fields": [
                       {"field": HOSTILE, "action": HOSTILE, "current": HOSTILE,
                        "proposed": HOSTILE, "new": True},
                       {"field": "studio", "action": "replace", "current": HOSTILE,
                        "proposed": HOSTILE, "new": True},
                       {"field": "models", "action": "add", "kept": [HOSTILE],
                        "added": [{"name": HOSTILE, "new": True}]}]}

    def rendered(self):
        answers = {
            # A ready card as the list draws it, every string someone else's.
            "readyRow": {"path": HOSTILE, "relPath": HOSTILE, "title": HOSTILE,
                         "models": [{"name": HOSTILE, "key": HOSTILE}],
                         "status": HOSTILE, "confidence": HOSTILE, "note": HOSTILE,
                         "sceneId": HOSTILE, "sceneTitle": HOSTILE,
                         "cover": HOSTILE, "sceneDate": HOSTILE,
                         "sceneDateEstimated": True, "sceneDuration": 60,
                         "durationSecs": 60, "durationAgrees": True,
                         "fields": self.PREVIEW_ROW["fields"]},
            "/api/library/preview": {
                "rows": [self.PREVIEW_ROW], "skipped": 0,
                "entities": {"models": HOSTILE, "studios": HOSTILE,
                             "categories": HOSTILE, "aliases": HOSTILE}},
            "/api/library/mapping": {
                "allowlist": [HOSTILE],
                "categories": [{"id": HOSTILE, "name": HOSTILE}],
                "tags": [{"tag_id": HOSTILE, "name": HOSTILE, "category": HOSTILE,
                          "videos": HOSTILE, "mode": "map",
                          "resolves_to": HOSTILE, "source": HOSTILE,
                          "override": HOSTILE}]},
        }
        js = """(async () => {
          const q = library.state.identify.questions[0];
          const out = {
            questionBlock: questionBlock(q),
            groupedBlock: questionBlock(q, {id: "m-" + q.models[0].key}),
            matchTabs: matchTabsHtml(q),
            compare: compareHtml(q, q.candidates[0]),
            candidateCard: candidateCard(q.candidates[0], q.videoId),
            readyBlock: questionBlock(Object.assign({ ready: true, videoId: 1 },
              __input.answers.readyRow)),
            assistedTerms: assistedTermsHtml({terms: q.terms,
              dateWindow: [q.terms.date], unresolved: [q.terms.studio]}),
            assistedResults: assistedResultsHtml({candidates: q.candidates,
              count: q.terms.date, page: q.terms.date}, q.videoId),
          };
          library.steps = libraryWorkflowSteps(library.state, null);
          renderLibraryConnect(); renderLibraryControls();
          renderLibraryBuckets(); renderLibraryIdentify();
          await renderLibraryMapping(); await renderLibraryPreview();
          renderLibraryWorkflow(); renderLibrarySections();
          renderLibraryBar();
          for (const id of ["library-connect", "library-controls",
                            "library-buckets", "library-identify",
                            "library-mapping", "library-preview",
                            "library-workflow", "library-bar"]) {
            out[id] = document.querySelector("#" + id).innerHTML;
          }
          out.heads = library.steps.map((step) =>
            document.querySelector("#library-head-" + step.key).innerHTML)
            .join("");
          library.state.identify.running = false;
          library.sectionOpen.review = true;
          renderLibraryQuestions();
          out.questions = document.querySelector("#library-questions").innerHTML;
          return out;
        })()"""
        return run_eval(self, self.hostile_state(), js, answers)["out"]

    def test_hostile_strings_render_as_text(self):
        out = self.rendered()
        for name, html in out.items():
            self.assertNotIn("<img src=x", html, name)
        # Where the page prints the words themselves, they are there as text.
        for name in ("questionBlock", "groupedBlock", "matchTabs", "compare",
                     "candidateCard", "readyBlock",
                     "assistedTerms", "library-buckets", "library-mapping",
                     "library-preview", "library-connect", "questions"):
            self.assertIn("&lt;img src=x", out[name], name)

    def test_a_cover_address_is_only_ever_an_https_image(self):
        row = self.hostile_row()
        row["videoId"] = 1
        card = row["candidates"][0]

        def cover_html(cover):
            card["cover"] = cover
            out = run_eval(self, library_state(), """[
                candidateCard(__input.answers.row.candidates[0], 1),
                matchTabsHtml(__input.answers.row),
                compareHtml(__input.answers.row, __input.answers.row.candidates[0]),
              ].join("")""", {"row": row})["out"]
            # The file's own still is the one picture from this tool, not StashDB.
            return re.sub(r"<img[^>]*data-frame-img[^>]*>", "", out)

        for cover in ("javascript:alert(1)", "data:text/html,x",
                      "http://cdn.example/a.jpg", "//cdn.example/a.jpg"):
            self.assertNotIn("<img", cover_html(cover), cover)
        self.assertEqual(cover_html("https://cdn.example/a.jpg")
                         .count('<img src="https://cdn.example/a.jpg"'), 3)


#: Fake <img> elements for the frame queue: each records the addresses it was
#: pointed at, in order, into one shared list. `__script` is the test's own part.
FRAME_QUEUE_JS = r"""(() => {
  const loads = [];
  const img = (name) => {
    const attrs = {};
    const el = { name, dataset: {}, isConnected: true,
                 parentNode: { classList: { add() {}, remove() {} } },
                 getAttribute: (key) => attrs[key] || null };
    Object.defineProperty(el, "src", {
      get: () => attrs.src || "",
      set: (value) => { attrs.src = value; loads.push(name + "=" + value); } });
    return el;
  };
  const [a, b, c, d, e] = ["a", "b", "c", "d", "e"].map(img);
  __SCRIPT__
  return loads;
})()"""


@unittest.skipIf(NODE is None, "node is not on PATH")
class TickTest(unittest.TestCase):
    """What `Write N` sends: every ready video less the ones unticked.

    Read off the data, never off checkboxes: most ready videos sit in model
    groups that are shut, and a shut group draws no boxes at all.
    """

    PREVIEW = {"rows": [{"video_id": n} for n in range(4)], "entities": {},
               "skipped": 0}

    def ticked(self, script):
        return run_eval(self, library_state(), """(() => {
          library.preview = __input.answers.preview;
          %s
          return tickedIds(library.preview);
        })()""" % script, {"preview": self.PREVIEW})["out"]

    def test_every_ready_video_is_ticked_until_unticked(self):
        self.assertEqual(self.ticked(""), [0, 1, 2, 3])

    def test_a_models_untick_takes_its_videos_out_drawn_or_not(self):
        self.assertEqual(self.ticked("setTicked([1, 2], false);"), [0, 3])

    def test_ticking_again_puts_them_back(self):
        self.assertEqual(self.ticked("setTicked([1, 2], false); setTicked([2], true);"),
                         [0, 2, 3])

    def test_rows_held_back_mid_run_are_not_sent_until_shown(self):
        # `reviewSeen` is what the list had drawn when the run's newer rows
        # arrived; the rest wait behind "Show them", and so does their write.
        self.assertEqual(self.ticked("library.reviewSeen = new Set([0, 2]);"), [0, 2])
        self.assertEqual(self.ticked("""library.reviewSeen = new Set([0, 2]);
          showFreshReviewRows();"""), [0, 1, 2, 3])

    def test_an_older_preview_answer_never_replaces_a_newer_one(self):
        # A poll and an opt-in tick can each have a preview in flight; the
        # older one landing last would put back rows just switched off.
        out = run_eval(self, library_state(), """(async () => {
          const pending = [];
          globalThis.fetch = () => new Promise((resolve) => pending.push(resolve));
          const answer = (ids) => ({ ok: true, json: () => Promise.resolve(
            { rows: ids.map((id) => ({ video_id: id })), entities: {}, skipped: 0 }) });
          const first = renderLibraryPreview();
          const second = renderLibraryPreview();
          pending[1](answer([2]));
          await second;
          pending[0](answer([1]));
          await first;
          return library.preview.rows.map((row) => row.video_id);
        })()""")["out"]
        self.assertEqual(out, [2])

    def test_an_untick_outlives_a_fresh_preview(self):
        # A poll replaces the preview wholesale; the untick is the page's own.
        self.assertEqual(self.ticked("""setTicked([3], false);
          library.preview = { rows: [0, 1, 2, 3, 4].map((id) => ({ video_id: id })) };"""),
                         [0, 1, 2, 4])


@unittest.skipIf(NODE is None, "node is not on PATH")
class ViewScopeTest(unittest.TestCase):
    """What Tag and Use recommended send: only what the tabs and filter show.

    The dialog is answered yes and the request is caught, so each test reads
    the exact ids that would have gone to the server.
    """

    #: Rows to choose: library/scenes/{1,10,11,20}.mp4. 10 is the one whose
    #: length disagrees, so it is sent but cannot be taken.
    STATE = library_state(identify=identify(questions=[
        question(1, models=("Jane Example",)), unsafe(10),
        question(11, models=("Sam Sample",)), question(20)]))

    def sent(self, script, state=None):
        return run_eval(self, state or self.STATE, """(async () => {
          const sent = [];
          let asked = null;
          modal = async (opts) => { asked = opts; return true; };
          api = async (url, opts) => {
            sent.push({ url, body: (opts || {}).body || null });
            return {};
          };
          loadLibrary = () => {};
          toast = () => {};
          library.preview = __input.answers.preview;
          %s
          return { sent, title: asked && asked.title, html: asked && asked.html };
        })()""" % script, {"preview": WriteSectionTest.ROWS})["out"]

    def test_tag_sends_only_the_ticked_videos_the_filter_shows(self):
        out = self.sent('library.reviewFilter = "a/0"; '
                        "await applyLibrarySelection();")
        self.assertEqual(out["sent"], [{"url": "/api/library/apply",
                                        "body": {"videoIds": [0]}}])
        self.assertEqual(out["title"], "Tag 1 video in Pronoisseur?")
        self.assertIn("2 more ticked videos outside this view are not included",
                      plain(out["html"]))

    def test_tag_sends_nothing_under_choose_a_match(self):
        out = self.sent('library.reviewKind = "ambiguous"; '
                        "await applyLibrarySelection();")
        self.assertEqual(out["sent"], [])

    def test_use_recommended_sends_the_rows_to_choose_the_filter_shows(self):
        # Unsafe ones too: the server takes only the safe ones and says why it
        # left the rest.
        out = self.sent('library.reviewFilter = "scenes/1"; '
                        "await acceptAllRecommended();")
        self.assertEqual(out["sent"], [
            {"url": "/api/library/match/accept-recommended",
             "body": {"videoIds": [1, 10, 11]}}])
        self.assertEqual(out["title"], "Use 2 recommended matches?")
        self.assertIn("Only videos this view shows, and only those whose "
                      "recommended scene runs the same length as your file, "
                      "are taken.", plain(out["html"]))

    def test_a_groups_use_recommended_sends_only_its_rows_in_view(self):
        out = self.sent("await acceptAllRecommended("
                        '{ id: "m-janeexample", key: "janeexample", '
                        'name: "Jane Example" });')
        self.assertEqual(out["sent"], [
            {"url": "/api/library/match/accept-recommended",
             "body": {"videoIds": [1]}}])
        self.assertEqual(out["title"], "Use 1 recommended match for Jane Example?")

    def test_use_recommended_sends_nothing_under_matched(self):
        out = self.sent('library.reviewKind = "ready"; '
                        "await acceptAllRecommended();")
        self.assertEqual(out["sent"], [])

    def test_picking_a_tab_or_typing_a_filter_redraws_the_bar_at_once(self):
        # The bar's Tag N is pinned to the bottom of the page, in view while
        # the tabs are clicked, so it cannot wait for the next poll to agree.
        out = run_eval(self, REVIEWABLE, """(() => {
          library.preview = __input.answers.preview;
          library.steps = libraryWorkflowSteps(library.state, library.preview);
          const bar = document.querySelector("#library-bar");
          renderLibraryBar();
          const before = bar.innerHTML;
          setReviewKind("ambiguous");
          const chose = bar.innerHTML;
          setReviewKind("all");
          setReviewFilter("a/0");
          return { before, chose, filtered: bar.innerHTML };
        })()""", {"preview": WriteSectionTest.ROWS})["out"]
        self.assertIn("Tag 3 videos", plain(out["before"]))
        self.assertNotIn("Tag 3 videos", plain(out["chose"]))
        self.assertIn("Nothing here is tagged until you use a match",
                      plain(out["chose"]))
        self.assertIn("Tag 1 video", plain(out["filtered"]))

    def test_rows_held_back_mid_run_are_not_accepted_unseen(self):
        out = self.sent("library.reviewSeen = new Set([1, 10]); "
                        "await acceptAllRecommended();")
        self.assertEqual(out["sent"][0]["body"], {"videoIds": [1, 10]})
        self.assertEqual(out["title"], "Use 1 recommended match?")


#: Folds "How this works" against a store the test owns: a working one, one
#: that throws on every call (a private window, blocked site data), and none.
HOW_BOX_JS = r"""(() => {
  const kept = new Map();
  const store = { getItem: (k) => (kept.has(k) ? kept.get(k) : null),
                  setItem: (k, v) => { kept.set(k, String(v)); } };
  const broken = { getItem() { throw new Error("denied"); },
                   setItem() { throw new Error("denied"); } };
  const box = () => {
    const b = { open: true, on: {} };
    b.addEventListener = (type, fn) => { b.on[type] = fn; };
    return b;
  };
  const first = box(); wireHowBox(first, store);
  const firstOpen = first.open;
  first.open = false; first.on.toggle();
  const next = box(); wireHowBox(next, store);
  const foldedNextTime = !next.open;
  next.open = true; next.on.toggle();
  const after = box(); wireHowBox(after, store);
  const denied = box(); wireHowBox(denied, broken);
  const deniedOpen = denied.open;
  denied.open = false; denied.on.toggle();
  const none = box(); wireHowBox(none, null);
  return { firstOpen, foldedNextTime, openAfterReopening: after.open,
           deniedOpen, noStoreOpen: none.open };
})()"""


@unittest.skipIf(NODE is None, "node is not on PATH")
class HowItWorksTest(unittest.TestCase):
    """"How this works" opens the first time, and stays folded once folded."""

    def test_folding_it_is_remembered_and_a_broken_store_changes_nothing(self):
        out = run_eval(self, library_state(), HOW_BOX_JS)["out"]
        self.assertEqual(out, {"firstOpen": True, "foldedNextTime": True,
                               "openAfterReopening": True, "deniedOpen": True,
                               "noStoreOpen": True})


@unittest.skipIf(NODE is None, "node is not on PATH")
class FrameQueueTest(unittest.TestCase):
    """The file's stills load through one queue, two at a time.

    A still the server has not kept yet is an ffmpeg run over the share, and the
    browser has about six connections to the tool in all. Left to the browser, a
    page of cards could hold all six on pictures while the poll and the Pick
    button wait behind them.
    """

    def run_queue(self, script):
        return run_eval(self, library_state(),
                        FRAME_QUEUE_JS.replace("__SCRIPT__", script))["out"]

    def test_no_more_than_two_stills_load_at_once(self):
        loads = self.run_queue("""
          [a, b, c, d].forEach((el, n) => queueFrame(el, "/f" + n, false));""")
        self.assertEqual(loads, ["a=/f0", "b=/f1"])

    def test_a_finished_still_hands_its_slot_to_the_next(self):
        loads = self.run_queue("""
          [a, b, c].forEach((el, n) => queueFrame(el, "/f" + n, false));
          b.onerror();      // a still that failed frees its slot just the same
          a.onload();""")
        self.assertEqual(loads, ["a=/f0", "b=/f1", "c=/f2"])

    def test_a_click_goes_to_the_front_of_the_queue(self):
        loads = self.run_queue("""
          [a, b, c, d].forEach((el, n) => queueFrame(el, "/f" + n, false));
          queueFrame(e, "/clicked", true);
          a.onload();""")
        self.assertEqual(loads[-1], "e=/clicked")

    def test_a_still_mid_load_is_not_re_pointed_until_it_reports(self):
        # Changing `src` mid-load drops the load without an event, and the slot
        # with it. The new address waits for the old load to finish.
        loads = self.run_queue("""
          queueFrame(a, "/one", false);
          queueFrame(a, "/two", true);
          const before = loads.slice();
          a.onload();
          loads.push("before:" + before.join(","));""")
        self.assertEqual(loads, ["a=/one", "a=/two", "before:a=/one"])

    def test_scrubbing_past_frames_asks_only_for_the_last_one(self):
        loads = self.run_queue("""
          queueFrame(a, "/busy", false);
          queueFrame(b, "/busy-too", false);
          ["/1", "/2", "/3"].forEach((url) => queueFrame(c, url, true));
          a.onload();""")
        self.assertEqual(loads, ["a=/busy", "b=/busy-too", "c=/3"])

    def test_a_still_no_longer_on_the_page_is_skipped(self):
        loads = self.run_queue("""
          [a, b, c, d].forEach((el, n) => queueFrame(el, "/f" + n, false));
          c.isConnected = false;
          a.onload();""")
        self.assertEqual(loads, ["a=/f0", "b=/f1", "d=/f3"])

    def test_a_load_that_never_reports_gives_up_after_the_servers_own_limit(self):
        # The server stops ffmpeg at 60 seconds, so by then the load has
        # answered one way or the other if it ever will.
        out = run_eval(self, library_state(), "FRAME_LOAD_GIVE_UP_MS")["out"]
        self.assertGreater(out, 60000)


@unittest.skipIf(NODE is None, "node is not on PATH")
class ScanControlsTest(unittest.TestCase):
    """A scan covers the whole library: the controls carry no per-run cap."""

    def controls(self, settings):
        state = library_state(settings=settings, scan=scan(considered=5))
        js = ("renderLibraryControls(); "
              "document.querySelector('#library-controls').innerHTML")
        return run_eval(self, state, js)["out"]

    def test_there_is_no_max_videos_box(self):
        html = self.controls({"write_title": True})
        self.assertNotIn("lib-max-run", html)
        self.assertNotIn("Max videos per run", html)
        self.assertIn('id="lib-scan"', html)

    def test_a_stale_cap_from_an_older_server_shows_nothing(self):
        # An older server sent `capped`; the page no longer reads it.
        state = library_state(settings={"max_run": 2000},
                              scan=dict(scan(considered=5), capped=True))
        js = ("renderLibraryControls(); "
              "document.querySelector('#library-controls').innerHTML")
        html = run_eval(self, state, js)["out"]
        self.assertNotIn("cap.", html)
        self.assertNotIn("2,000", html)


@unittest.skipIf(NODE is None, "node is not on PATH")
class ScanFinishedLineTest(unittest.TestCase):
    """What a finished scan says it read and what it asked StashDB."""

    def line(self, **finished):
        fin = {"considered": 5}
        fin.update(finished)
        state = library_state(scan=scan(considered=5, label="done", finished=fin))
        js = ("renderLibraryControls(); "
              "document.querySelector('#library-controls').innerHTML")
        return plain(run_eval(self, state, js)["out"])

    def test_it_says_how_many_folder_ids_were_fetched_and_how_many_saved(self):
        self.assertIn("Read 5 videos from Pronoisseur. Checked 3 folder ids on "
                      "StashDB (2 fetched, 1 saved).",
                      self.line(folderIds=3, fetched=2, saved=1))

    def test_one_video_and_one_folder_id_read_in_the_singular(self):
        self.assertIn("Read 1 video from Pronoisseur. Checked 1 folder id on "
                      "StashDB (1 fetched, 0 saved).",
                      self.line(considered=1, folderIds=1, fetched=1, saved=0))

    def test_with_no_folder_ids_it_says_so(self):
        text = self.line(folderIds=0, fetched=0, saved=0)
        self.assertIn("Read 5 videos from Pronoisseur. No folder ids to check "
                      "on StashDB.", text)
        self.assertNotIn("(", text)

    def test_nothing_is_said_while_it_runs_or_when_an_old_server_sent_no_counts(self):
        self.assertNotIn("Read 5 videos", self.line())
        state = library_state(scan=scan(considered=5, running=True,
                                        label="fetching scenes"))
        js = ("renderLibraryControls(); "
              "document.querySelector('#library-controls').innerHTML")
        self.assertNotIn("Read 5 videos", plain(run_eval(self, state, js)["out"]))


#: The three reasons, as the server words them (`library_common`), because the
#: page repeats two of them in its banner and the cards carry all three.
NOT_YET = ("Pronoisseur hasn't fingerprinted this video yet, or couldn't read "
           "it. Scan and identify again later.")
SWITCH_OFF = ("Pronoisseur's perceptual fingerprints are switched off. Turn on "
              "Settings \u2192 Local API \u2192 Fingerprint videos for connected "
              "apps in Pronoisseur.")
OLD_PRONOISSEUR = ("This version of Pronoisseur doesn't share fingerprints. "
                   "Update Pronoisseur to identify by fingerprint.")


def pending_rows(count, note=NOT_YET, models=()):
    """Bucket rows as the planner reports them, snake_case, each with its reason."""
    return [{"video_id": 7000 + n, "rel_path": "p/%d.mp4" % n,
             "path": "D:\\p\\%d.mp4" % n, "title": "Pending %d" % n,
             "note": note, "models": [model(m) for m in models]}
            for n in range(count)]


def pending_state(count=2, fingerprints=None, note=NOT_YET, models=(), **over):
    """A scanned library with `count` rows Pronoisseur has no phash for."""
    fingerprints = fingerprints or {"state": "known", "phashEnabled": True,
                                    "phashFailed": 0}
    counts = {"verified": 98}
    buckets = {}
    if count:
        counts["pending-hash"] = count
        buckets["pending-hash"] = pending_rows(count, note, models)
    return library_state(
        scan=scan(considered=98 + count, counts=counts, buckets=buckets,
                  fingerprints=fingerprints),
        identify=identify(counts={"pending-hash": count} if count else {"verified": 1},
                          label="done", **over))


@unittest.skipIf(NODE is None, "node is not on PATH")
class PendingHashingTest(unittest.TestCase):
    """Rows Pronoisseur has no phash for: their own kind, and where it says why."""

    def run_harness(self, harness, frames):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "harness.js")
            payload = os.path.join(tmp, "input.json")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(harness)
            with open(payload, "w", encoding="utf-8") as fh:
                json.dump({"frames": list(frames)}, fh)
            proc = subprocess.run([NODE, script, APP_JS, payload],
                                  capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["frames"]

    def review(self, *frames):
        return self.run_harness(PREVIEW_HARNESS, frames)

    def identify_box(self, state):
        frame = self.run_harness(TAB_HARNESS, [{"state": state, "preview": None}])[0]
        return plain(frame["identify"]).replace("&#39;", "'")

    ROWS = WriteSectionTest.ROWS

    def test_a_pending_row_is_its_own_kind(self):
        state = pending_state(2)
        got = self.review({"state": state, "preview": self.ROWS,
                           "groupOpen": {"nomodel": True}},
                          {"state": state, "preview": self.ROWS,
                           "kind": "pending", "groupOpen": {"nomodel": True}})
        self.assertEqual(plain(got[0]["kinds"]),
                         "All 5 Choose a match 0 Matched 3 No match 0 "
                         "Pending hashing 2")
        html = got[0]["list"]
        self.assertEqual(html.count('<span class="kind-badge wait">'
                                    'Pending hashing</span>'), 2)
        self.assertEqual(html.count('data-question="7000"'), 1)
        # The reason is the how-line, once per card: not again beside the title.
        self.assertEqual(html.replace("&#39;", "'").count(NOT_YET), 2)
        self.assertIn('<span class="how-matched">%s</span>'
                      % NOT_YET.replace("'", "&#39;"), html)
        # Tag N is the matched rows' alone.
        self.assertIn("3 ticked to tag", plain(got[0]["preview"]))
        # Narrowed to the kind: only its cards, and nothing to tag, said so.
        self.assertEqual(got[1]["list"].count("data-question="), 2)
        self.assertIn("Nothing here can be tagged until Pronoisseur "
                      "fingerprints it, or you find it with Search by name.",
                      plain(got[1]["preview"]))
        self.assertNotIn('id="lib-apply"', got[1]["preview"])

    def test_a_pending_card_still_offers_the_search_by_name(self):
        got = self.review({"state": pending_state(1), "preview": self.ROWS,
                           "groupOpen": {"nomodel": True}})
        self.assertIn("Search by name", got[0]["list"])
        self.assertNotIn("match-tab", got[0]["list"])

    def test_the_group_head_counts_pending(self):
        with_pending = pending_state(2, models=("Jane Example",))
        without = pending_state(0)
        got = self.review({"state": with_pending, "preview": self.ROWS},
                          {"state": without, "preview": self.ROWS})
        self.assertIn("Jane Example 5 - 3 matched (all ticked) "
                      "\u00b7 2 pending hashing", plain(got[0]["list"]))
        self.assertNotIn("pending hashing", plain(got[1]["list"]))

    def test_the_banner_names_the_switch(self):
        off = {"state": "known", "phashEnabled": False, "phashFailed": 0}
        self.assertIn(SWITCH_OFF,
                      self.identify_box(pending_state(1, fingerprints=off)))
        # Nothing pending: the switch being off is not this run's problem.
        self.assertNotIn(SWITCH_OFF, self.identify_box(
            pending_state(0, fingerprints=off)))
        # Switch on: no banner.
        self.assertNotIn(SWITCH_OFF, self.identify_box(pending_state(1)))

    def test_the_banner_asks_for_a_newer_pronoisseur(self):
        old = {"state": "older", "phashEnabled": None, "phashFailed": None}
        self.assertIn(OLD_PRONOISSEUR,
                      self.identify_box(pending_state(1, fingerprints=old)))

    def test_an_unreachable_info_gives_no_banner(self):
        unknown = {"state": "unknown", "phashEnabled": None, "phashFailed": None}
        text = self.identify_box(pending_state(1, fingerprints=unknown))
        self.assertNotIn(SWITCH_OFF, text)
        self.assertNotIn(OLD_PRONOISSEUR, text)

    def test_the_collapsed_step_carries_the_banners_news(self):
        def head(state):
            frame = self.run_harness(
                TAB_HARNESS, [{"state": state, "preview": None}])[0]
            return plain(frame["sections"]["identify"]["head"])
        older = {"state": "older", "phashEnabled": None, "phashFailed": None}
        self.assertIn("Pronoisseur shares no fingerprints — update it",
                      head(pending_state(1, fingerprints=older)))
        off = {"state": "known", "phashEnabled": False, "phashFailed": 0}
        self.assertIn("perceptual fingerprints are off in Pronoisseur",
                      head(pending_state(1, fingerprints=off)))
        self.assertIn("nothing left to fingerprint", head(pending_state(1)))
        self.assertIn("nothing left to fingerprint",
                      head(pending_state(0, fingerprints=off)))

    FINISHED = {"considered": 50, "verified": 4, "likely": 1,
                "ambiguous": 2, "unmatched": 5, "pending-hash": 3,
                "failed": 0, "phashes": 40}

    def test_the_finished_line_says_where_fingerprints_came_from(self):
        known = {"state": "known", "phashEnabled": True, "phashFailed": 3}
        text = self.identify_box(pending_state(
            12, fingerprints=known, finished=self.FINISHED))
        self.assertIn("All fingerprints from Pronoisseur: 40 with a "
                      "perceptual hash, 3 pending hashing.", text)
        self.assertIn("Pronoisseur couldn't read 3 videos.", text)
        self.assertNotIn("needed a perceptual hash", text)
        self.assertNotIn("cached", text)

    def test_an_unknown_state_leaves_out_the_could_not_read_sentence(self):
        unknown = {"state": "unknown", "phashEnabled": None, "phashFailed": None}
        text = self.identify_box(pending_state(
            12, fingerprints=unknown, finished=self.FINISHED))
        self.assertIn("40 with a perceptual hash", text)
        self.assertNotIn("couldn't read", text)

    def test_one_unread_video_is_not_pluralised(self):
        known = {"state": "known", "phashEnabled": True, "phashFailed": 1}
        text = self.identify_box(pending_state(
            12, fingerprints=known, finished=self.FINISHED))
        self.assertIn("Pronoisseur couldn't read 1 video.", text)


#: Puts the step's own button wherever a test says, and the window and the bar
#: at the sizes it says. `rect` null means the button is not drawn at all.
BAR_GEOMETRY_JS = r"""
  const BTN = '[data-step-action="review"]';
  const real = document.querySelector.bind(document);
  let rect = null;
  let section = null;
  const button = () => ({ getBoundingClientRect: () => rect,
                          closest: () => section });
  document.querySelector = (sel) => (sel === BTN
    ? (rect ? button() : null) : real(sel));
  const put = (top, bottom) => {
    rect = top === null ? null
      : { top, bottom, left: 0, right: 80, width: 80, height: bottom - top };
  };
  window.innerHeight = 800;
  const bar = real("#library-bar");
  bar.offsetHeight = 60;
  library.preview = __input.answers.preview;
  library.steps = libraryWorkflowSteps(library.state, library.preview);
  const shown = () => !bar.hidden;
"""


@unittest.skipIf(NODE is None, "node is not on PATH")
class BarVisibilityTest(unittest.TestCase):
    """The sticky bar shows only while the step's own button is out of view.

    On a short page the button is on screen right above the bar, and the bar
    reads as a second copy of it. Window is 800 high and the bar 60, so the
    bar covers 740-800 and a button there is as good as hidden.
    """

    def check(self, body, state=None):
        return run_eval(self, state or REVIEWABLE,
                        "(() => {" + BAR_GEOMETRY_JS + body + "})()",
                        {"preview": WriteSectionTest.ROWS})["out"]

    def test_button_in_view_above_the_bar_hides_it(self):
        out = self.check("put(300, 340); renderLibraryBar(); return shown();")
        self.assertFalse(out)

    def test_button_scrolled_off_either_end_shows_it(self):
        out = self.check("""
          put(-200, -160); renderLibraryBar(); const above = shown();
          put(900, 940); renderLibraryBar(); const below = shown();
          return { above, below };""")
        self.assertEqual(out, {"above": True, "below": True})

    def test_button_under_the_bar_counts_as_out_of_view(self):
        out = self.check("""
          put(750, 790); renderLibraryBar(); const under = shown();
          put(690, 730); renderLibraryBar(); const clear = shown();
          put(700, 760); renderLibraryBar(); const mostlyClear = shown();
          put(720, 760); renderLibraryBar(); const mostlyUnder = shown();
          return { under, clear, mostlyClear, mostlyUnder };""")
        # The centre decides: 700-760 has it at 730, above the bar's 740 edge.
        self.assertEqual(out, {"under": True, "clear": False,
                               "mostlyClear": False, "mostlyUnder": True})

    def test_no_button_drawn_shows_it(self):
        out = self.check("put(null); renderLibraryBar(); return shown();")
        self.assertTrue(out)

    def test_a_button_in_a_hidden_section_shows_it(self):
        out = self.check("""
          put(300, 340); section = {};
          renderLibraryBar(); return shown();""")
        self.assertTrue(out)

    def test_a_zero_sized_button_is_not_drawn(self):
        out = self.check("""
          put(300, 300); rect.width = 0; renderLibraryBar(); return shown();""")
        self.assertTrue(out)

    def test_without_a_geometry_api_it_shows_as_before(self):
        out = self.check("""
          document.querySelector = (sel) => (sel === BTN ? {} : real(sel));
          renderLibraryBar(); const noRect = shown();
          put(300, 340);
          document.querySelector = (sel) => (sel === BTN ? button() : real(sel));
          window.innerHeight = undefined;
          renderLibraryBar(); const noHeight = shown();
          return { noRect, noHeight };""")
        self.assertEqual(out, {"noRect": True, "noHeight": True})

    def test_scroll_and_resize_flip_it_without_a_render(self):
        out = self.check("""
          put(300, 340); renderLibraryBar(); const before = shown();
          const html = bar.innerHTML;
          put(-200, -160);
          __windowListeners.scroll.forEach((fn) => fn());
          const afterScroll = shown();
          put(300, 340);
          __windowListeners.resize.forEach((fn) => fn());
          const afterResize = shown();
          return { before, afterScroll, afterResize,
                   rebuilt: bar.innerHTML !== html };""")
        self.assertEqual(out, {"before": False, "afterScroll": True,
                               "afterResize": False, "rebuilt": False})

    def error_state(self):
        state = json.loads(json.dumps(REVIEWABLE))
        state["scan"]["error"] = "boom"
        return state

    def test_any_undismissed_error_keeps_it_shown(self):
        # An error is never hidden: not when its section is folded, and not
        # when a copy is drawn in view either - the bar is where it is read.
        out = self.check("""
          put(300, 340); renderLibraryBar(); const withError = shown();
          const key = libraryErrors()[0].key;
          document.querySelectorAll = (sel) => [{
            dataset: { dismissError: key }, closest: () => null,
            getBoundingClientRect: () => ({ top: 10, bottom: 40,
                                            width: 80, height: 30 }) }];
          renderLibraryBar(); const withCopy = shown();
          library.dismissed.add(key);
          renderLibraryBar(); const dismissed = shown();
          return { withError, withCopy, dismissed };""", self.error_state())
        self.assertEqual(out, {"withError": True, "withCopy": True,
                               "dismissed": False})

    HEADER_JS = """
          const header = { top: 50, workflow: 219 };
          const realQ = document.querySelector;
          document.querySelector = (sel) => {
            if (sel === ".topbar") return { getBoundingClientRect:
              () => ({ height: header.top }) };
            if (sel === "#library-workflow") return { getBoundingClientRect:
              () => ({ height: header.workflow }) };
            return realQ(sel);
          };"""

    def test_a_button_under_the_sticky_header_shows_it(self):
        # Header stack is 269px: top bar 50 + step strip 219.
        out = self.check(self.HEADER_JS + """
          put(248, 285); renderLibraryBar(); const under = shown();
          put(240, 280); renderLibraryBar(); const partly = shown();
          put(270, 310); renderLibraryBar(); const below = shown();
          return { under, partly, below };""")
        self.assertEqual(out, {"under": True, "partly": True, "below": False})

    def test_a_sliver_inside_the_band_is_not_in_view(self):
        out = self.check("""
          put(-35, 5); renderLibraryBar(); const top = shown();
          put(735, 775); renderLibraryBar(); const bottom = shown();
          return { top, bottom };""")
        self.assertEqual(out, {"top": True, "bottom": True})

    def test_scrolling_does_not_measure_the_bar_again(self):
        out = self.check("""
          let reads = 0;
          Object.defineProperty(bar, "offsetHeight",
            { get() { reads += 1; return 60; }, configurable: true });
          put(300, 340); renderLibraryBar();
          const afterRender = reads;
          put(-200, -160);
          for (let n = 0; n < 5; n++) __windowListeners.scroll.forEach((f) => f());
          return { more: reads - afterRender, shown: shown() };""")
        self.assertEqual(out, {"more": 0, "shown": True})

    def test_hiding_it_reserves_its_height_so_nothing_jumps(self):
        out = self.check("""
          const gap = real("#library-bar-gap");
          put(300, 340); renderLibraryBar();
          const whileHidden = { hidden: gap.hidden, height: gap.style.height };
          put(-200, -160); renderLibraryBar();
          return { whileHidden, whileShown: gap.hidden };""")
        self.assertEqual(out, {"whileHidden": {"hidden": False, "height": "60px"},
                               "whileShown": True})


@unittest.skipIf(NODE is None, "node is not on PATH")
class StepActionMarkTest(unittest.TestCase):
    """Each step's own button carries `data-step-action`, so the bar finds the
    one it stands in for without a hard-coded id."""

    def mark(self, html, ident, key):
        self.assertRegex(html, r'id="%s"[^>]*data-step-action="%s"'
                               r'|data-step-action="%s"[^>]*id="%s"'
                               % (ident, key, key, ident))
        self.assertEqual(html.count('data-step-action="%s"' % key), 1)

    def test_connect_marks_test_connection_once_a_key_is_saved(self):
        html = run_eval(self, library_state(),
                        "renderLibraryConnect(); "
                        "document.querySelector('#library-connect').innerHTML")["out"]
        self.mark(html, "lib-connect", "connect")

    def test_connect_marks_save_key_when_there_is_no_key(self):
        html = run_eval(self, library_state(stashdbConfigured=False),
                        "renderLibraryConnect(); "
                        "document.querySelector('#library-connect').innerHTML")["out"]
        self.mark(html, "lib-stashdb-save", "connect")

    def test_scan_and_identify_mark_their_buttons(self):
        html = run_eval(self, SCANNED, "renderLibraryControls(); "
                        "document.querySelector('#library-controls').innerHTML")["out"]
        self.mark(html, "lib-scan", "scan")
        html = run_eval(self, SCANNED, "renderLibraryIdentify(); "
                        "document.querySelector('#library-identify').innerHTML")["out"]
        self.mark(html, "lib-identify", "identify")

    def test_review_marks_the_tag_button(self):
        html = run_eval(self, REVIEWABLE,
                        "(() => { library.preview = __input.answers.preview;"
                        " renderLibraryWriteBar();"
                        " return document.querySelector('#library-preview')"
                        ".innerHTML; })()",
                        {"preview": WriteSectionTest.ROWS})["out"]
        self.mark(html, "lib-apply", "review")


if __name__ == "__main__":
    unittest.main()
