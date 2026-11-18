/* StashDB Tagger for Pronoisseur, the page.
 *
 * Talks only to the local JSON API under /api/library/. No keys live here, and
 * nothing in this file knows the shape of StashDB or Whisparr.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ------------------------------------------------------------------ api */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((payload && payload.error && payload.error.message) || res.statusText);
    err.code = payload && payload.error && payload.error.code;
    throw err;
  }
  return payload;
}

/* --------------------------------------------------------------- toasts */

function toast(title, detail, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.innerHTML = `<b></b><span></span>`;
  $("b", el).textContent = title;
  $("span", el).textContent = detail || "";
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 12000 : 6000);
}

function reportError(err) {
  toast(err.code === "UPSTREAM_ERROR" ? "Upstream error" : "Error", err.message, "bad");
}

const escapeHtml = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

//: A cover address comes from StashDB, so it is used as an image source only
//: when it is a plain https URL: never a script, data or protocol-relative one.
const safeImageUrl = (u) => (/^https:\/\//i.test(String(u || "")) ? String(u) : "");

/* ---------------------------------------------------------------- modal */

function modal({ title, html, okLabel = "Confirm", okClass = "primary" }) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    const ok = $("#modal-ok");
    ok.textContent = okLabel;
    ok.className = okClass;
    $("#modal").hidden = false;
    const field = $("#modal-body input");
    if (field) setTimeout(() => field.focus(), 30);

    const done = (accepted) => {
      const input = $("#modal-body input");
      const value = accepted ? (input ? input.value : true) : null;
      $("#modal").hidden = true;
      ok.replaceWith(ok.cloneNode(true));
      resolve(value);
    };
    $("#modal-ok").onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
  });
}

function busy(target, message) {
  $(target).innerHTML = `<div class="spinner">${escapeHtml(message)}</div>`;
}

function formatDuration(seconds) {
  const s = Number(seconds) || 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

/* ---------------------------------------------------------- library page */

//: How many question blocks the first render puts in the document. Each block
//: carries a grid of cover <img> tags. A whole library is thousands of covers, so
//: only the first page renders; the rest arrive on a button press. Declared
//: here, above `library`, because `library` reads it.
const QUESTION_PAGE = 50;

//: How often the page re-reads `/api/library/state` while something is running.
//: A poll is a fetch plus seven renders, and during a long identify run it
//: competes with the lookups for the same machine while showing a number that
//: changes once every few seconds anyway.
const LIBRARY_POLL_MS = 2000;

//: Mid-run, how often at most the list may be redrawn to take in rows the run
//: settled since. A redraw rebuilds the whole box, so a run that lands a batch
//: every few seconds is let in once per window rather than once per batch.
const REVIEW_AUTO_SHOW_MS = 5000;

//: What a server progress label (a stable key) says to the user: a short form
//: for the step head and a long one, naming where the work is going, for the
//: progress line. The keys are the server's; one the page does not know is shown
//: as it came (`progressText`).
const PROGRESS_LABELS = {
  "listing videos": ["reading Pronoisseur",
                     "Reading your library from Pronoisseur"],
  "reading identities": ["reading folder ids",
                         "Finding StashDB ids in folder names"],
  "fetching scenes": ["fetching from StashDB",
                      "Fetching scenes named in folder names from StashDB"],
  "checking folder ids": [
    "checking folder ids",
    "Checking each folder's scene against Pronoisseur's fingerprint"],
  "remembering decisions": ["using earlier answers",
                            "Using your earlier answers"],
  "looking up": ["asking StashDB",
                 "Asking StashDB about fingerprints, 40 at a time"],
  "upserting entities": [
    "creating in Pronoisseur",
    "Creating models, studios and categories in Pronoisseur"],
  "writing videos": ["writing to Pronoisseur", "Writing videos to Pronoisseur"],
};

//: The text for a run's label: `form` 0 is the short one, 1 the long one. Own
//: keys only, so a label that happens to be an object property name falls back.
function progressText(label, form) {
  const known = Object.prototype.hasOwnProperty.call(PROGRESS_LABELS, label)
    ? PROGRESS_LABELS[label] : null;
  return known ? known[form] : String(label);
}

// `questionsKey` and `shown` guard `#library-questions`, which is by a long way
// the heaviest thing the page draws — see `renderLibraryQuestions`.
//
// The rest is the page's own furniture, deliberately in memory only.
// `sectionOpen` and `reviewOpen` hold the panels the user opened or closed BY
// HAND, as an override on top of the default ("only the current step is open");
// an absent key means "no opinion, follow the workflow". Persisting them would
// fight the default, which is that the panel you need is the one that opens.
// `dismissed` is the same kind of thing, an error the user has read: there is no
// route that clears a server-side error, so dismissing one is this page's
// business and nobody else's.
const library = {
  state: null, preview: null, include: new Set(), poll: null,
  steps: [],
  questionsKey: null,
  //: Per model group, by group id, because they page independently: a hundred
  //: rows of one model should not drag a hundred of another onto the screen. An
  //: absent id is one page.
  shown: {},
  reviewFilter: "",
  sectionOpen: {},          // step key -> true/false, set only by a click
  reviewOpen: {},           // model group id -> true/false, ditto
  //: Per video in Review: which StashDB match is on show (scene id), and which
  //: of the file's own frames. Absent means the recommended match, frame 0.
  reviewTab: {},
  reviewFrame: {},
  //: The Review rows last drawn, by video id, so switching a match or a frame
  //: redraws that one video without rebuilding the box.
  reviewIndex: new Map(),
  //: Which kind of row the list is narrowed to: "all", or a `REVIEW_KINDS` key.
  reviewKind: "all",
  //: The model groups as last worked out, by id, so a tick can recount a
  //: group's head without rebuilding it.
  reviewGroups: new Map(),
  //: Which `/api/library/preview` request is the latest; older answers are
  //: dropped.
  previewSeq: 0,
  dismissed: new Set(),     // error keys the user has waved away
  //: The last failed connection test, kept so its reason is still on screen
  //: after the re-render that follows the test. Null once one succeeds.
  connectFailure: null,
  //: What the last "Accept all recommended" answered, or null. The rows it took
  //: leave the list, so without this nothing would say why the rows that stayed
  //: are still there.
  accepted: null,
  //: Write-preview rows the user unticked, by video id. The preview is redrawn
  //: from the server's answer, which knows nothing of ticks, so without this an
  //: untick lasts until the next poll - two seconds, while a run goes on.
  unpicked: new Set(),
  //: The markup the write preview last drew, so a poll that would draw the same
  //: thing draws nothing. Null whenever the box has been emptied.
  previewHtml: null,
  //: While an identify run goes on: the review rows already on screen, by video
  //: id. Rows the run settles after that join the list on their own
  //: (`holdReviewRows`), but not while the user is working in a card and not more
  //: than once per `REVIEW_AUTO_SHOW_MS`; until then they are counted, not drawn.
  //: Null when no run is going.
  reviewSeen: null,
  //: When the list last took in rows mid-run (`Date.now()`), for that limit.
  reviewShownAt: 0,
};

async function loadLibrary() {
  try {
    library.state = await api("/api/library/state");
    renderLibraryConnect();
    renderLibraryControls();
    renderLibraryBuckets();
    renderLibraryIdentify();
    await renderLibraryMapping();
    await renderLibraryPreview();
    // Only now. The strip and the section heads sit at the top of the page, but
    // step 5 counts the rows the write preview holds and that number exists only
    // once `renderLibraryPreview` has fetched it. Questions come after the
    // sections because a collapsed Review draws no cover grids at all.
    renderLibrarySteps();
    scheduleLibraryPoll();
  } catch (err) { reportError(err); }
}

//: Everything downstream of `library.steps`, in dependency order. `loadLibrary`
//: calls it, and so does anything that changes what a step would say without
//: re-reading the whole state — a section toggle, an opt-in tick, a dismissal.
function renderLibrarySteps() {
  library.steps = libraryWorkflowSteps(library.state, library.preview);
  renderLibraryWorkflow();
  renderLibrarySections();
  renderLibraryQuestions();
  renderLibraryBar();
}

function scheduleLibraryPoll() {
  clearTimeout(library.poll);
  const s = library.state;
  if (!s) return;
  // Identify is in here too: without it the progress block freezes at whatever
  // the first read said and a long run looks like a hang.
  if (s.scan.running || s.apply.running || (s.identify || {}).running) {
    library.poll = setTimeout(loadLibrary,
                              LIBRARY_POLL_MS);
  }
}

/* --------------------------------------------- library: the workflow strip */

// The strip is the map: the five steps in the order they have to happen, each
// saying what it does and how far it has got. The pills show only the result of
// the scan, which names just the videos a folder UUID identifies, so on their own
// they understate how much of the library is still unidentified.
//
// It invents nothing. Every figure below is one the pills, the progress blocks
// or the write preview already carry, out of `/api/library/state` plus the
// preview the page already fetches — no route and no server field exists for the
// strip's sake.

//: Where each step's title scrolls to: the collapsible section that step owns.
//: The section wrapper, not a box inside it, because a wrapper is always in the
//: document and carries the head — scrolling to a body that is currently
//: collapsed would land on nothing.
const WORKFLOW_TARGETS = {
  connect: "#library-section-connect",
  scan: "#library-section-scan",
  identify: "#library-section-identify",
  review: "#library-section-review",
};

const WORKFLOW_TITLES = {
  connect: "Connect",
  scan: "Scan",
  identify: "Identify by fingerprint",
  review: "Check matches and tag",
};

//: The step order, which is also the dependency order. Reviewing and writing
//: are one step: every video the run found something about is in one list,
//: grouped by model, ready ones ticked, and one button writes the ticked ones.
const WORKFLOW_ORDER = ["connect", "scan", "identify", "review"];

//: "Nothing left to write. Applied 12 videos." — one sentence, said in the
//: three places a converged preview has to read as finished: the strip's step 4,
//: the sticky bar's line and the write preview's own empty box. Three wordings
//: would be three chances for one of them to still say "0 to write", which is
//: how a finished job reads as a broken one.
function libraryNothingLeftHtml(apply) {
  const finished = (apply || {}).finished;
  return `Nothing left to tag.${finished
    ? ` Tagged ${countText(finished.applied)} videos.` : ""}`;
}

//: "12 matched · 3 to choose · 5 no match · 2 pending hashing", leaving out
//: any part that is nought.
//: `sentence` bolds the figures and ends it, for the strip's paragraph; without
//: it the line is the section head's summary.
function reviewLine(ready, pick, none, pending, sentence) {
  const figure = (n) => (sentence ? `<b>${countText(n)}</b>` : countText(n));
  const parts = [
    ready ? `${figure(ready)} matched` : "",
    pick ? `${figure(pick)} to choose` : "",
    none ? `${figure(none)} no match` : "",
    pending ? `${figure(pending)} pending hashing` : "",
  ].filter(Boolean);
  if (!parts.length) return "";
  return parts.join(" &middot; ") + (sentence ? ". " : "");
}

// Reads state, returns the four steps. Pure on purpose — `s` is the
// `/api/library/state` payload and `preview` the last `/api/library/preview`
// answer — so the whole strip can be asserted without a browser.
function libraryWorkflowSteps(s, preview) {
  const scan = s.scan || {};
  const identify = s.identify || {};
  const apply = s.apply || {};
  const counts = scan.counts || {};
  const buckets = scan.buckets || {};
  const considered = scan.considered || 0;
  const scanDone = !scan.running && considered > 0;
  const mismatch = counts["uuid-mismatch"] || 0;
  const noIdentity = counts["no-identity"] || 0;
  // Rows a run in flight has taken up and not reached yet. The server reports
  // them in place of the status they arrived with, so they are counted here or
  // nowhere.
  const waiting = counts["waiting"] || 0;
  // Step 3's own work: the rows a fingerprint run can still act on. The rows a
  // run *leaves behind* — ambiguous, unmatched, pending hashing — are step 4's,
  // and counting them here would keep Identify lit for ever on a library that
  // has already been looked up end to end.
  const fingerprintPending = noIdentity + mismatch + waiting;
  const ambiguous = (identify.questions || []).length;
  // The same two sources `renderLibraryQuestions` renders: ambiguous rows come
  // from the identify run, unmatched and pending-hash ones from the scan's
  // buckets.
  const unmatched = (buckets["unmatched"] || []).length;
  const pendingHash = (buckets["pending-hash"] || []).length;
  const reviewPending = ambiguous + unmatched + pendingHash;
  const likely = (buckets["likely"] || []).length;
  const unverified = (buckets["unverified"] || []).length;
  const toWrite = ((preview || {}).rows || []).length;
  // "Identified" is the buckets that actually name a live scene. `not-found`,
  // `ambiguous` and `unmatched` are left out on purpose: an id StashDB no
  // longer has, and a row nothing has answered, are not identities.
  const identified = ["verified", "verified-by-duration", "unverified", "likely",
                      "in-sync"].reduce((n, key) => n + (counts[key] || 0), 0);
  const identifyRan = !!identify.finished || !!identify.running
    || Object.values(identify.counts || {}).some((n) => n);

  const done = {
    connect: !!s.connected && !!s.stashdbConfigured,
    scan: scanDone,
    // Not `identify.finished`: a run that finished is only interesting because
    // it left nothing fingerprintable behind, and a library entirely named by
    // folder UUID reaches that state without a run at all.
    identify: scanDone && !identify.running && fingerprintPending === 0,
    review: false,                                      // filled in below
  };
  // A step is `current` when it is not done and everything it needs has
  // happened, `next` — dim — when something upstream has not.
  //
  // Review and write opens once a run has started, while it is still going: it
  // settles rows a batch at a time and never touches one again, so the rows it
  // has settled are ready to answer and write long before a whole-library run
  // ends.
  const ready = {
    connect: true,
    scan: done.connect,
    identify: scanDone,
    review: scanDone && (done.identify || identifyRan),
  };
  // Needs `ready.review`, so it cannot be worked out with the rest above: with
  // no preview fetched yet, "nothing to write" is ignorance, not success. Done
  // means nothing left to write AND nothing left to look at.
  const writeDone = ready.review && !apply.running && !!preview && toWrite === 0;
  done.review = writeDone && fingerprintPending === 0 && reviewPending === 0;

  const hints = {
    connect: done.connect
      ? `Connected to Pronoisseur &mdash; ${s.canWrite
          ? "read + write" : "read-only, so tagging stays off"}.`
      : `Add your StashDB API key, then a Pronoisseur token with
         <b>Update video metadata</b>, and test the connection.`,
    scan: scan.running
      ? `${escapeHtml(progressText(scan.label, 1))}: <b>${countText(scan.done)}</b> of
         <b>${countText(scan.total)}</b>.`
      : done.scan
        // The size of the library, and how little of it a folder-UUID scan can
        // name on its own.
        // `waiting` with `noIdentity`: a row the run has taken up is reported
        // as waiting instead of its own status, and is just as unidentified.
        ? `<b>${countText(considered)}</b> videos seen &mdash;
           ${countText(identified)} identified${identifyRan ? "" : " by UUID"},
           ${countText(mismatch)} UUID mismatches, ${countText(noIdentity
             + waiting)} still to identify.`
        : `Reads every video from Pronoisseur and resolves any that carry a
           StashDB scene id in their folder name.`,
    identify: identify.running
      ? `${escapeHtml(progressText(identify.label, 1))}: <b>${countText(identify.done)}</b> of
         <b>${countText(identify.total)}</b>. Results show up in Check matches and tag
         as each batch is looked up, and you can work on them while this runs.`
      : done.identify
        ? `Nothing left for a fingerprint run &mdash; every video the scan found
           carries an identity or an answer.`
        // The count is dropped before the first scan rather than printed as
        // three zeroes: nothing has counted anything yet, and "0 to identify"
        // over an unread library is the same false reassurance the pills gave.
        : `${fingerprintPending ? `<b>${countText(fingerprintPending)}</b> to
             identify by fingerprint &mdash; ${countText(noIdentity)} with no
             identity yet${mismatch ? `, ${countText(mismatch)} Whisparr
             disagreements` : ""}. ` : ""
           }Looks up every video without an identity on StashDB, using the
           fingerprints Pronoisseur has already worked out. Nothing is hashed
           here.`,
    // "Not found", never "waiting": waiting is what a row the run has not
    // reached yet is called, and those are not in this list.
    review: apply.running
      ? `${escapeHtml(progressText(apply.label, 1))}: <b>${countText(apply.done)}</b> of
         <b>${countText(apply.total)}</b>.`
      : done.review
        ? libraryNothingLeftHtml(apply)
        : `${reviewLine(toWrite, ambiguous, unmatched, pendingHash, true)}${waiting
             ? `${countText(waiting)} still being identified; their results join
                this list as they land. ` : ""}Grouped by model. Each matched video
           is ticked, ready to tag &mdash; untick any match that's wrong, or a
           whole model with its tick. Choose a match where StashDB is not
           sure, and search the rest by name.${(likely || unverified) ? ` ${countText(likely)} likely and
             ${countText(unverified)} unverified are held back until you include
             them.` : ""}${writeDone && toWrite === 0 && apply.finished
             ? ` ${libraryNothingLeftHtml(apply)}` : ""}`,
  };

  // The head line. `hint` above is the paragraph the strip carries; this is the
  // sentence, short enough to sit on one row beside the title — and it is all a
  // collapsed section shows, so it has to be the figure worth seeing.
  const progress = (run) =>
    `${escapeHtml(progressText(run.label, 0))} ${countText(run.done)}/${countText(run.total)}`;
  const summaries = {
    connect: done.connect ? (s.canWrite ? "read + write" : "read-only")
                          : (s.connected ? "StashDB key needed" : "not connected"),
    scan: scan.running ? progress(scan)
      : done.scan
        // The trailing clause is dropped rather than printed as a nought: a
        // library with nothing left to identify should read as finished, not as
        // finished-with-a-zero.
        ? `${countText(identified)} identified${fingerprintPending
             ? `, ${countText(fingerprintPending)} still to identify` : ""}`
        : "not scanned yet",
    identify: identify.running ? progress(identify)
      : fingerprintPending ? `${countText(fingerprintPending)} to fingerprint`
      : ready.identify ? nothingToFingerprint(scan, pendingHash) : "waiting on the scan",
    review: apply.running ? progress(apply)
      : (toWrite || reviewPending) ? reviewLine(toWrite, ambiguous, unmatched, pendingHash, false)
      // Same distinction `writeDone` draws: with no preview fetched, zero rows
      // is ignorance rather than an empty list.
      : preview ? "nothing to tag or check" : "no preview yet",
  };
  //: The number on the head's badge. Zero draws no badge at all, so a step with
  //: nothing outstanding reads as quiet rather than as a nought.
  const badges = {
    connect: 0, scan: considered, identify: fingerprintPending,
    review: toWrite + reviewPending,
  };

  return WORKFLOW_ORDER.map((key, i) => ({
    key,
    step: i + 1,
    title: WORKFLOW_TITLES[key],
    target: WORKFLOW_TARGETS[key],
    state: done[key] ? "done" : (ready[key] ? "current" : "next"),
    hint: hints[key],
    summary: summaries[key],
    badge: badges[key],
  }));
}

// How far down the viewport the first thing that is not stuck to it begins: the
// top bar plus the strip. A `scrollIntoView({block: "start"})` with no allowance
// for those parks the section head underneath them, so a jump to a step would
// land on the step above it.
function libraryStickyOffset(allowance = 12) {
  const height = (sel) => {
    const el = $(sel);
    // Guarded rather than assumed: the same render runs under a string-only stub
    // DOM in the tests, which has no box model at all.
    if (!el || !el.getBoundingClientRect) return 0;
    return el.getBoundingClientRect().height || 0;
  };
  return Math.round(height(".topbar") + height("#library-workflow") + allowance);
}

function scrollToLibrarySection(target) {
  const el = $(target);
  if (!el || !el.scrollIntoView) return;
  if (el.style) el.style.scrollMarginTop = libraryStickyOffset() + "px";
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderLibraryWorkflow() {
  const box = $("#library-workflow");
  const steps = library.steps;
  box.innerHTML = steps.map((step) => `
    <li class="step is-${step.state}" data-step="${escapeHtml(step.key)}">
      <button type="button" class="step-title" data-step-go="${escapeHtml(step.target)}"
              data-step-key="${escapeHtml(step.key)}">
        <span class="step-num">${step.state === "done" ? "&check;" : step.step}</span>
        ${escapeHtml(step.title)}
      </button>
      <span class="step-hint">${step.hint}</span>
    </li>`).join("");
  $$("[data-step-go]", box).forEach((btn) => {
    btn.onclick = () => {
      // Opens as well as scrolls. Jumping to a collapsed section and being shown
      // its one-line summary is not what "go to step 4" means, and a done step —
      // which is exactly the one you revisit — is collapsed by default.
      openLibrarySection(btn.dataset.stepKey);
      scrollToLibrarySection(btn.dataset.stepGo);
    };
  });
}

/* ---------------------------------- library: the collapsible step sections */

// One panel per step, and only the step you are on is open, so the work in front
// of you is never in the middle of four panels you are not using. A done step
// collapses to its head — number, title, the live summary line, a count badge —
// and a step nothing has unlocked yet is collapsed and dim. Opening or closing
// one by hand overrides that for the session, because a default that cannot be
// argued with is just a lock.

//: Whether a step's panel is open: the user's own click if they made one, and
//: the workflow's answer otherwise. Two steps can be `current` at once -
//: Identify, mid-run, and Review and write - and both open is right: a settled
//: row can be written while the run goes on.
function librarySectionOpen(step) {
  const manual = library.sectionOpen[step.key];
  return manual == null ? step.state === "current" : manual;
}

function libraryStepByKey(key) {
  return library.steps.filter((step) => step.key === key)[0] || null;
}

//: The strip's step titles use this: "go to step 4" has to mean the panel is
//: open when you arrive, and the step you revisit is usually a done one — which
//: is collapsed by default.
function openLibrarySection(key) {
  const step = libraryStepByKey(key);
  if (!step || librarySectionOpen(step)) return;
  library.sectionOpen[key] = true;
  renderLibrarySteps();
}

function toggleLibrarySection(key) {
  const step = libraryStepByKey(key);
  if (!step) return;
  library.sectionOpen[key] = !librarySectionOpen(step);
  renderLibrarySteps();
}

function renderLibrarySections() {
  library.steps.forEach((step) => {
    const section = $("#library-section-" + step.key);
    const head = $("#library-head-" + step.key);
    const body = $("#library-body-" + step.key);
    if (!section || !head || !body) return;
    const open = librarySectionOpen(step);
    section.className = "lib-section is-" + step.state + (open ? " is-open" : "");
    section.dataset.state = step.state;
    section.dataset.open = open ? "1" : "0";
    // `hidden`, not a display rule of its own: the stylesheet's
    // `[hidden] { display: none !important }` makes it the one way of hiding a
    // thing on this page that no other rule can quietly defeat.
    body.hidden = !open;
    if (head.setAttribute) {
      head.setAttribute("aria-expanded", open ? "true" : "false");
    }
    head.innerHTML = `
      <span class="lib-num">${step.state === "done" ? "&check;" : step.step}</span>
      <span class="lib-title">${escapeHtml(step.title)}</span>
      <span class="lib-summary">${step.summary}</span>
      ${step.badge ? `<span class="lib-badge">${countText(step.badge)}</span>` : ""}
      <span class="lib-chev" aria-hidden="true">&#9662;</span>`;
    head.onclick = () => toggleLibrarySection(step.key);
  });
}

/* ---------------------------------------------- library: the error notices */

//: The three long-running things and the order the workflow puts them in, so a
//: bar showing one error shows the earliest rather than the last to land.
const LIBRARY_ERROR_SOURCES = [
  ["scan", "Scan failed"],
  ["identify", "Identify failed"],
  ["apply", "Tagging stopped"],
];

//: An error is identified by what went wrong, not by when. A re-run that fails
//: the same way is the same error, and re-showing a dismissed one because the
//: state object is new would make Dismiss look broken.
const libraryErrorKey = (scope, message) => scope + ":" + String(message);

//: Nothing here clears a server-side error — no route does, and inventing one
//: for a cosmetic dismissal would be a write nobody asked for. The set lives in
//: the page, so a reload brings the notice back, which is the honest behaviour:
//: the run really did fail.
function libraryErrors() {
  const s = library.state || {};
  return LIBRARY_ERROR_SOURCES
    .map(([scope, label]) => ({ scope, label, message: (s[scope] || {}).error }))
    .filter((err) => err.message
            && !library.dismissed.has(libraryErrorKey(err.scope, err.message)))
    .map((err) => Object.assign(err,
                                { key: libraryErrorKey(err.scope, err.message) }));
}

//: The inline half of an error: the same words the sticky bar carries, in the
//: section that owns it, with the Dismiss that clears both at once.
function libraryErrorHtml(scope, extra = "") {
  const err = libraryErrors().filter((e) => e.scope === scope)[0];
  if (!err) return "";
  return `<div class="empty bad lib-error">
    <span class="lib-error-text">${escapeHtml(err.label)}:
      ${escapeHtml(err.message)}${extra}</span>
    <button type="button" class="ghost small"
            data-dismiss-error="${escapeHtml(err.key)}">Dismiss</button>
  </div>`;
}

function dismissLibraryError(key) {
  library.dismissed.add(key);
  // Both halves: the notice inside the section that owns it, and the line in the
  // bar. The preview is re-fetched because the apply error is drawn inside it,
  // and `renderLibrarySteps` runs either way so a failed re-fetch cannot leave
  // the bar showing an error the user has just dismissed.
  renderLibraryControls();
  renderLibraryIdentify();
  renderLibraryPreview().catch(reportError).then(() => renderLibrarySteps());
}

function wireLibraryErrorDismiss(root) {
  $$("[data-dismiss-error]", root).forEach((btn) => {
    btn.onclick = () => dismissLibraryError(btn.dataset.dismissError);
  });
}

/* ------------------------------------------ library: the sticky action bar */

// The bar carries exactly one action — the current step's — pinned to the bottom
// of the page, plus one line saying where the library stands. It appears only
// when the step's own button is out of view: scrolled off, or not drawn because
// its section is folded. While that button is on screen the bar would be a
// second copy of it, one inch below, so it stays hidden.
//
// Nothing in it is a second implementation. Every entry points at the same
// function the button inside the section calls, so the two cannot drift; the
// buttons in the sections stay where they are, and this is the copy that is
// reachable when they are not.
const LIBRARY_BAR_ACTIONS = {
  connect: () => (library.state.stashdbConfigured
    ? connectLibrary() : saveStashdbKey()),
  scan: () => startLibraryScan(false),
  identify: () => startIdentify(),
  // Check matches and tag's one action is the tag. Using the safe
  // recommendations rides beside it as an extra while there is any to use;
  // under a tab with nothing to tag, that is all the bar carries.
  review: () => applyLibrarySelection(),
};

function libraryBarHasAction(step) {
  return !!LIBRARY_BAR_ACTIONS[step.key];
}

//: The step the bar speaks for: the first current one that has an action. With
//: everything done it keeps the last one, disabled, rather than emptying out —
//: a bar that changes shape at the end is a bar that moves the thing you were
//: about to click.
function libraryBarStep() {
  const actionable = library.steps.filter(libraryBarHasAction);
  return actionable.filter((s) => s.state === "current")[0]
    || actionable[actionable.length - 1] || null;
}

//: One label, one disabled rule and any extras, per step. The disabled rules are
//: the section buttons' own: a scan overlaps nothing, and an identify and an
//: apply may not START together - though an apply may begin while a run goes on.
function libraryBarAction(step) {
  const s = library.state;
  const scan = s.scan || {}, identify = s.identify || {}, apply = s.apply || {};
  const busyNow = !!(scan.running || identify.running || apply.running);
  if (step.key === "connect") {
    return { label: s.stashdbConfigured ? "Test connection" : "Save StashDB key",
             disabled: false, extras: "" };
  }
  if (step.key === "scan") {
    return { label: scan.running ? "Scanning&hellip;" : "Scan",
             disabled: busyNow, extras: "" };
  }
  if (step.key === "identify") {
    return {
      label: identify.running ? "Identifying&hellip;" : "Identify by fingerprint",
      disabled: busyNow,
      // The one place a cancel is worth reaching without scrolling: the run this
      // cancels is the long one.
      extras: identify.running
        ? `<button type="button" class="ghost" data-bar-cancel="1">Cancel</button>`
        : "",
    };
  }
  // An identify run does not hold up the write: it settles each row once and
  // never comes back to it, and the rows it has not reached are in no list.
  const busyRows = !!(scan.running || apply.running);
  // Both follow the view: what the tabs and the filter show is what they take.
  const tagHere = reviewKindShown("ready");
  const accept = recommendedSafeCount()
    ? `<button type="button" class="${tagHere ? "secondary" : "primary"}"
         data-bar-accept="1" ${busyRows ? "disabled" : ""}
         >${acceptRecommendedLabel()}</button>` : "";
  // Choose a match, No match and Pending hashing hold nothing to tag, so no Tag at all - not a
  // dead "Tag 0" that reads as if the matched videos had gone.
  if (!tagHere) return { label: null, disabled: true, extras: accept };
  const ticked = tickedIds(library.preview).length;
  return {
    label: writeLabel(apply, ticked),
    disabled: !s.canWrite || !ticked || busyRows,
    extras: libraryOptInHtml("data-bar-include") + accept,
  };
}

//: "Tag 94 videos", or "Tagging..." while a run is going. One label for the button in
//: the section and the one in the bar, so the two numbers cannot differ.
function writeLabel(apply, ticked) {
  return (apply || {}).running ? "Tagging&hellip;"
    : `Tag ${countText(ticked)} video${ticked === 1 ? "" : "s"}`;
}

//: What the line says, in order of urgency: what is running, then what went
//: wrong, then where the library stands.
function libraryBarStatus(step, err) {
  const s = library.state;
  const running = [s.scan, s.identify, s.apply]
    .map((run) => run || {}).filter((run) => run.running)[0];
  if (running) {
    return `${escapeHtml(progressText(running.label, 1))}: <b>${countText(running.done)}</b> of
            <b>${countText(running.total)}</b>`;
  }
  if (err) return `${escapeHtml(err.label)}: ${escapeHtml(err.message)}`;
  // The preview's own summary, not a second count of the same rows: the number
  // beside the Apply button and the number above the diff have to agree.
  //
  // Except once the preview has converged, when that summary is a row of
  // noughts: "0 to write" beside a disabled button reads as a list that has lost
  // its rows, not as a job that is finished. The step's own sentence says what
  // actually happened, and it is the same sentence the section below shows.
  //
  // Nor can "done" be leaned on here: a file not found keeps the step open for
  // good - it is this step's work, looked up by name - so a finished write
  // with nothing ready says what it did and what is left instead.
  if (step.key === "review" && library.preview) {
    if (step.state === "done") return step.hint;
    if (!(library.preview.rows || []).length) {
      const apply = s.apply || {};
      return `${apply.finished ? `${libraryNothingLeftHtml(apply)} ` : ""}${step.summary}`;
    }
    return previewSummaryHtml(library.preview);
  }
  return step.summary;
}

function renderLibraryBar() {
  const box = $("#library-bar");
  const s = library.state;
  const step = s ? libraryBarStep() : null;
  if (!step) {
    box.hidden = true; box.innerHTML = "";
    setLibraryBarGap(false);
    return;
  }
  box.hidden = false;
  library.barHeight = null;   // its content changed: measure it again
  const err = libraryErrors()[0] || null;
  const action = libraryBarAction(step);
  box.innerHTML = `
    <div class="lib-bar-status${err ? " is-bad" : ""}">
      <span class="lib-bar-step">Step ${step.step} &middot;
        ${escapeHtml(step.title)}</span>
      <span class="lib-bar-line">${libraryBarStatus(step, err)}</span>
      ${err ? `<button type="button" class="ghost small"
                 data-dismiss-error="${escapeHtml(err.key)}">Dismiss</button>` : ""}
    </div>
    <div class="lib-bar-actions">
      ${action.extras}
      ${action.label ? `<button class="primary" data-bar-action="${escapeHtml(step.key)}"
              ${action.disabled ? "disabled" : ""}>${action.label}</button>` : ""}
    </div>`;
  wireLibraryErrorDismiss(box);
  $$("[data-bar-action]", box).forEach((btn) => {
    btn.onclick = LIBRARY_BAR_ACTIONS[btn.dataset.barAction];
  });
  $$("[data-bar-cancel]", box).forEach((btn) => { btn.onclick = cancelIdentify; });
  $$("[data-bar-accept]", box).forEach((btn) => {
    btn.onclick = () => acceptAllRecommended();
  });
  // The opt-in ticks are the write preview's, mirrored here because the tick is
  // what makes `Apply N` the number it is. Same setter, so the two stay level.
  $$("[data-bar-include]", box).forEach((input) => {
    input.onchange = () => setLibraryOptIn(input.dataset.barInclude, input.checked);
  });
  setLibraryBarVisibility();
}

//: Drawn at all: not inside a folded section, and with a box of its own. A
//: button the page has not drawn answers `getBoundingClientRect` with zeros, so
//: the size check covers a `display: none` that no `hidden` attribute explains.
function libraryElementDrawn(el) {
  if (el.closest && el.closest("[hidden]")) return false;
  const r = el.getBoundingClientRect();
  return !!(r.width || r.height);
}

//: Whether the step's own button is where a click can reach it. Null when this
//: page cannot say - no geometry API, as in the test stub - and the caller then
//: shows the bar, as it always did.
//:
//: The clear band runs from the bottom of the sticky header (top bar plus step
//: strip, with no breathing-room allowance: that is for scroll targets) to the
//: top of the bar, which covers the window's bottom. A button under either is as
//: good as off screen. Its vertical centre has to be inside the band, so a few
//: pixels peeking out from under an edge do not count as visible.
function libraryStepButtonInView(step, barHeight) {
  if (!window.innerHeight) return null;
  const btn = $(`[data-step-action="${step.key}"]`);
  if (!btn) return false;
  if (typeof btn.getBoundingClientRect !== "function") return null;
  if (!libraryElementDrawn(btn)) return false;
  const r = btn.getBoundingClientRect();
  const centre = (r.top + r.bottom) / 2;
  return centre > libraryStickyOffset(0)
    && centre < window.innerHeight - barHeight;
}

//: Shows or hides the bar without rebuilding it. Looked up afresh every time:
//: the buttons are re-rendered by each poll and a held element would be stale.
//:
//: Any error that has not been dismissed keeps it up. An error is drawn in its
//: section as well, but the bar is where it is always in reach, and it should
//: never be possible to scroll past one.
//:
//: Hiding takes the bar out of the flow, which would shorten the page by its
//: height and shift everything above it when the window sits at the bottom, and
//: the shift could put the button back under where the bar was. The gap holds
//: the bar's place while it is hidden, so the page is the same height either way.
//:
//: The bar's height is measured when it is drawn or the window is resized and
//: kept in `library.barHeight`; a scroll frame reads no layout of its own for it.
function setLibraryBarVisibility() {
  const box = $("#library-bar");
  const step = library.state ? libraryBarStep() : null;
  if (!step || !box) return;
  if (library.barHeight == null) {
    // A hidden bar has no height to read, so it is shown for the measurement.
    box.hidden = false;
    library.barHeight = box.offsetHeight || 0;
  }
  const height = library.barHeight;
  const inView = libraryStepButtonInView(step, height);
  const hide = inView === true && !libraryErrors().length;
  box.hidden = hide;
  setLibraryBarGap(hide, height);
}

function setLibraryBarGap(on, height) {
  const gap = $("#library-bar-gap");
  if (!gap) return;
  gap.hidden = !on;
  if (on) gap.style.height = (height || 0) + "px";
}

//: One check per animation frame, however many scroll events arrive.
let libraryBarFrame = false;
function scheduleLibraryBarCheck() {
  if (libraryBarFrame) return;
  libraryBarFrame = true;
  const run = () => { libraryBarFrame = false; setLibraryBarVisibility(); };
  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(run);
  } else {
    run();
  }
}
if (typeof window.addEventListener === "function") {
  window.addEventListener("scroll", scheduleLibraryBarCheck, { passive: true });
  window.addEventListener("resize", () => {
    library.barHeight = null;   // the line may wrap differently now
    scheduleLibraryBarCheck();
  }, { passive: true });
}

//: Where the Connect step sends someone who still needs a StashDB key, the Local
//: API switched on, or Pronoisseur itself. Each opens in its own tab, so the
//: half-filled Connect step stays where it was.
const STASHDB_SITE = "https://stashdb.org/";
const PRONOISSEUR_SITE = "https://pronoisseur.com/";
const PRONOISSEUR_CONNECTED_APPS_HELP =
  "https://pronoisseur.com/help/14-connected-apps/";

function externalLink(href, text) {
  return `<a class="ext-link" href="${href}" target="_blank" rel="noopener">${
    text} &#8599;</a>`;
}

//: What to check after a connection test fails, chosen by whether anything
//: answered: a refused token and a port nothing is listening on need different
//: fixes. Windows can reserve a block of ports for itself, and Pronoisseur then
//: reports that it couldn't listen although no other program has the port.
function connectHelpHtml(s) {
  const failure = library.connectFailure;
  if (s.connected || (!failure && !s.hasToken)) return "";
  const url = escapeHtml((failure && failure.url) || s.url || "");
  const status = failure ? failure.status : null;
  const tryPort = `<b>Try a different port.</b> Change <b>Port</b> in Pronoisseur
    under Settings &rarr; Connected apps, for example to 28869, then use the same
    number in the URL above.`;
  let help, getIt = "";
  if (status === 401 || status === 403) {
    help = `Pronoisseur answered at ${url}, but it refused this token. Tokens are
      shown once, so if you no longer have it, revoke the app under Settings
      &rarr; Connected apps and add it again.`;
  } else if (status != null) {
    help = `Something answered at ${url}, but the connection failed:
      ${escapeHtml(failure.error || "unknown error")}. ${tryPort}`;
  } else {
    help = `Can't reach Pronoisseur at ${url}. Is Pronoisseur running, with
      ${externalLink(PRONOISSEUR_CONNECTED_APPS_HELP, "Connected apps")} turned
      on? If it is, and the line under
      <b>Enable the Local API</b> shows an error, the port is the problem.
      ${tryPort}`;
    // Nothing answered, so this may be someone who has not got Pronoisseur yet.
    // Its own plain line: it is not a warning, and it must not be lost at the end
    // of the port advice.
    getIt = `<div class="hint pad">Don't have Pronoisseur yet? Get it at ${
      externalLink(PRONOISSEUR_SITE, "pronoisseur.com")}.</div>`;
  }
  return `<div class="hint pad connect-help">${help}</div>${getIt}`;
}

//: One line under the connection line about whether Pronoisseur links the StashDB
//: ids this tool writes. Nothing before a connection or before the server has
//: said (`not yet`), so a fresh page stays quiet; the error is the server's text
//: and is escaped.
function linkPatternsHtml(s) {
  if (!s.connected) return "";
  const lp = s.linkPatterns || {};
  // Each sentence is joined from pieces rather than wrapped inside a template, so
  // the markup carries it as one unbroken line.
  switch (lp.status) {
    case "registered":
      return `<div class="hint pad">${
        "StashDB links are on in Pronoisseur's Details pane."}</div>`;
    case "unsupported":
      return `<div class="hint pad">${
        "This Pronoisseur can't show StashDB links. " +
        "Update to 1.0.62 or later."}</div>`;
    case "read-only":
      return `<div class="hint pad">${
        "StashDB links need a Pronoisseur token with write access."}</div>`;
    case "failed":
      return `<div class="hint pad warn">${
        "Couldn't turn on StashDB links in Pronoisseur: " +
        escapeHtml(lp.error || "unknown error") + "."}</div>`;
    default:
      return "";
  }
}

function renderLibraryConnect() {
  const s = library.state;
  const scopes = (s.scopes || []).join(", ") || "none";
  const badge = !s.connected
    ? `<span class="pill bad">not connected</span>`
    : (s.canWrite ? `<span class="pill good">read + write</span>`
                  : `<span class="pill warn">read-only &mdash; apply disabled</span>`);
  $("#library-connect").innerHTML = `
    <div class="toolbar">
      <label>StashDB API key
        <input type="password" id="lib-stashdb-key" size="26" autocomplete="off"
               placeholder="${s.stashdbConfigured ? "saved" : "paste your API key"}">
      </label>
      <button class="${s.stashdbConfigured ? "ghost" : "primary"}"
              id="lib-stashdb-save"
              ${s.stashdbConfigured ? "" : 'data-step-action="connect"'}>Save key</button>
      <span class="pill ${s.stashdbConfigured ? "good" : "bad"}">${
        s.stashdbConfigured ? "key saved" : "no key"}</span>
      <span class="spacer"></span>
      <span class="hint">Your API key is on your profile page at ${
        externalLink(STASHDB_SITE, "stashdb.org")}.</span>
    </div>
    <div class="toolbar">
      <label>Pronoisseur URL
        <input type="text" id="lib-url" size="26" value="${escapeHtml(s.url || "")}">
      </label>
      <label>Token
        <input type="password" id="lib-token" size="26"
               placeholder="${s.hasToken ? "saved" : "pron_..."}">
      </label>
      <button class="primary" id="lib-connect"
              ${s.stashdbConfigured ? 'data-step-action="connect"' : ""}>Test connection</button>
      ${badge}
      <span class="spacer"></span>
      <span class="hint">${s.connected
        ? `Pronoisseur ${escapeHtml((s.info || {}).app_version || "?")} &middot;
           app "${escapeHtml(((s.info || {}).app || {}).name || "?")}" &middot;
           scopes: ${escapeHtml(scopes)}`
        : `Settings &rarr; Connected apps &rarr; Add app, tick
           <b>Update video metadata</b>, paste the token here.`}</span>
    </div>
    ${linkPatternsHtml(s)}
    ${connectHelpHtml(s)}`;
  $("#lib-stashdb-save").onclick = saveStashdbKey;
  $("#lib-connect").onclick = connectLibrary;
}

//: Saves the StashDB key through the server, which checks it before keeping it.
//: Like `connectLibrary`, it reads the box out of the document so the sticky
//: bar's button and the section's own do the same thing.
async function saveStashdbKey() {
  const box = $("#lib-stashdb-key");
  if (!box) return;
  if (!box.value.trim()) {
    toast("No key", "Paste your StashDB API key first.", "warn");
    return;
  }
  try {
    const out = await api("/api/library/stashdb", {
      method: "POST", body: { key: box.value },
    });
    if (out.configured) {
      toast("StashDB key accepted", "", "good");
    } else {
      toast("StashDB key not accepted", out.error || "unknown error", "bad");
    }
    if (out.configured && !out.saved) {
      toast("Not saved", out.saveError || "unknown error", "warn");
    }
    loadLibrary();
  } catch (err) { reportError(err); }
}

//: Keeps why the last connection test failed, or clears it once one succeeds.
function rememberConnectResult(out) {
  library.connectFailure = out.connected ? null
    : { url: out.url, error: out.error, status: out.status == null ? null : out.status };
}

// One function, two buttons: this one and the sticky bar's. It reads the boxes
// out of the document rather than being handed their values, so the bar's copy
// tests whatever is typed in the section without either side holding state the
// other cannot see.
async function connectLibrary() {
  const url = $("#lib-url"), token = $("#lib-token");
  if (!url || !token) return;
  try {
    const out = await api("/api/library/connect", {
      method: "POST",
      body: { url: url.value, token: token.value },
    });
    rememberConnectResult(out);
    if (out.connected) {
      toast("Connected", out.canWrite ? "Read and write." : "Read-only token.",
            out.canWrite ? "good" : "warn");
    } else {
      toast("Not connected", "The Connect step says what to check.", "bad");
    }
    if (out.saved === false && out.saveError) {
      toast("Not saved", out.saveError, "warn");
    }
    loadLibrary();
  } catch (err) {
    library.connectFailure = null;
    reportError(err);
  }
}

function renderLibraryControls() {
  const s = library.state;
  const box = $("#library-controls");
  box.hidden = !s.connected;
  if (!s.connected) return;
  const set = s.settings;
  const toggle = (key, label, hint) => `
    <label class="inline" title="${escapeHtml(hint || "")}">
      <input type="checkbox" data-setting="${key}" ${set[key] ? "checked" : ""}>
      ${escapeHtml(label)}</label>`;
  const scan = s.scan;
  box.innerHTML = `
    <div class="toolbar">
      ${toggle("write_title", "Title")}
      ${toggle("write_release_date", "Release date",
               "Written with estimated cleared. A scene with no date is left alone.")}
      ${toggle("write_studio", "Studio")}
      ${toggle("use_parent_studio", "Use parent studio",
               "Acme Originals -> Acme. Off writes the sub-studio.")}
      ${toggle("add_models", "Add models")}
      ${toggle("add_categories", "Add categories")}
      <span class="spacer"></span>
      <label class="inline" title="Also re-check videos that already carry a StashDB id">
        <input type="checkbox" id="lib-resync"> Re-sync</label>
      <button class="ghost small" id="lib-refresh-scenes"
              title="Ignore the 30-day scene cache">Refresh from StashDB</button>
      <button class="primary" id="lib-scan" data-step-action="scan"
              ${scan.running ? "disabled" : ""}>
        ${scan.running ? "Scanning..." : "Scan"}</button>
    </div>
    ${scan.running ? `<div class="estimate"><span class="figure">
        ${escapeHtml(progressText(scan.label, 1))}: <b>${countText(scan.done)}</b> of
        <b>${countText(scan.total)}</b>
      </span></div>` : scanFinishedHtml(scan)}
    ${libraryErrorHtml("scan")}`;
  wireLibraryErrorDismiss(box);
  $$("[data-setting]", box).forEach((input) => {
    input.onchange = async () => {
      try {
        await api("/api/library/settings", {
          method: "POST",
          body: { settings: { [input.dataset.setting]: input.checked } },
        });
        loadLibrary();
      } catch (err) { reportError(err); }
    };
  });
  $("#lib-scan").onclick = () => startLibraryScan(false);
  $("#lib-refresh-scenes").onclick = () => startLibraryScan(true);
}

//: What a finished scan read and what it asked StashDB: "Read N videos from
//: Pronoisseur. Checked M folder ids on StashDB (F fetched, S saved)." The saved
//: ones came from a copy under 30 days old. Nothing while it runs, and nothing
//: from a server that sent no counts, which would otherwise read as "0 of 0".
function scanFinishedHtml(scan) {
  const fin = scan.finished;
  if (scan.running || !fin || fin.folderIds == null) return "";
  const n = (count, noun) => `${countText(count)} ${noun}${count === 1 ? "" : "s"}`;
  const checked = fin.folderIds
    ? `Checked ${n(fin.folderIds, "folder id")} on StashDB (${
        countText(fin.fetched)} fetched, ${countText(fin.saved)} saved).`
    : "No folder ids to check on StashDB.";
  return `<div class="hint pad">Read ${n(fin.considered, "video")} from
    Pronoisseur. ${checked}</div>`;
}

async function startLibraryScan(refreshScenes) {
  try {
    await api("/api/library/scan", {
      method: "POST",
      body: { resync: $("#lib-resync").checked, refreshScenes },
    });
    // A tick is consent to write THIS scan's rows. The opt-in toggles add and
    // remove per group rather than replacing the set wholesale, so an id left
    // here would survive: re-scan, re-identify, the same video is `likely`
    // again, its box renders itself checked, and the row is back in the write
    // on a tick made against a listing that no longer exists. Cleared when a
    // scan starts, so consent is asked again for the rows it finds.
    library.include = new Set();
    // The same consent the other way round: an untick was about this listing.
    library.unpicked = new Set();
    // Same argument, one step stronger: a scan rebinds the row list whole, so
    // last acceptance's counts are about a library listing that is gone.
    library.accepted = null;
    loadLibrary();
  } catch (err) { reportError(err); }
}

const BUCKET_LABELS = {
  "verified": ["Verified", "oshash matches a fingerprint StashDB lists"],
  "verified-by-duration": ["Verified by duration", "no oshash hit, durations agree"],
  "unverified": ["Unverified", "tick a row to write it anyway"],
  // The order of this object is the order of the pill row, so these three sit
  // where they sit in the server's own BUCKETS tuple. A status the server
  // buckets but that has no entry here gets rows and no pill - the half-wired
  // state the shared ordering exists to prevent.
  "likely": ["Likely", "one perceptual match - tick it in the write preview to write it"],
  "ambiguous": ["Ambiguous", "more than one scene matched, or the only near match disagrees on duration - answer it below"],
  "unmatched": ["Unmatched", "no fingerprint matched - assisted search is below"],
  "pending-hash": ["Pending hashing", "Pronoisseur has no perceptual fingerprint for this video yet - search by name, or scan again later"],
  "uuid-mismatch": ["Whisparr disagreements", "the folder UUID is a different scene - never written"],
  "not-found": ["Scene not found", "StashDB no longer has this id"],
  "no-identity": ["No identity yet", "no UUID in the path - identify it by fingerprint"],
  "in-sync": ["Already in sync", "nothing to change"],
};

function renderLibraryBuckets() {
  const counts = (library.state.scan || {}).counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) { $("#library-buckets").innerHTML = ""; return; }
  const pills = Object.entries(BUCKET_LABELS)
    .filter(([key]) => counts[key])
    .map(([key, [label, hint]]) => `
      <span class="pill" title="${escapeHtml(hint)}">
        <b>${countText(counts[key])}</b> ${escapeHtml(label.toLowerCase())}</span>`).join("");
  // Not the `uuid-mismatch` bucket: a disagreement identify has since resolved
  // is no longer in that bucket, and it is still a disagreement until the folder
  // is corrected. The server keeps the claim on the row and lists both ids.
  const disagreements = library.state.disagreements || [];
  $("#library-buckets").innerHTML = `
    <div class="toolbar">${pills}</div>
    ${disagreements.length ? `<details class="pad"><summary>
        Whisparr disagreements (${escapeHtml(disagreements.length)})
        &mdash; fix these in Whisparr
      </summary>
      <p class="hint">The folder name claims one StashDB scene; the file itself is
        another. <b>Nothing here writes to Whisparr</b> &mdash; correct these
        there by hand. Until you do, every scan reads the same wrong id off the
        same folder name.</p>
      <ul class="plain">${disagreements.map((d) => `<li>
        <code>${escapeHtml(d.relPath || "(no path)")}</code><br>
        <span class="muted">Whisparr says
          <code>${escapeHtml(d.whisparrSceneId || "")}</code> &middot;
          ${d.matchedSceneId
            ? `the file matches <code>${escapeHtml(d.matchedSceneId)}</code>${
                d.matchedTitle ? ` &mdash; ${escapeHtml(d.matchedTitle)}` : ""}`
            : `nothing has named the right scene yet &mdash; run Identify by
               fingerprint`} &middot;
          ${escapeHtml(d.status)}</span></li>`).join("")}</ul></details>` : ""}`;
}

/* ------------------------------------------- library: identify by fingerprint */

// What still owes an answer, and which tally each number comes from. This gates
// the Identify button only; the sentence is the workflow strip's, split across
// steps 3 and 4 so neither step claims the other's work. The scan's counts are
// recomputed from the live rows on every read, so `no-identity` and
// `uuid-mismatch` fall away as identify settles them; the two outcomes a run
// leaves behind that can still be acted on here are counted from the run's own
// tally, which is over the rows that run touched. The two sets are disjoint - a
// row has one status - so they add up without double-counting.
//
// `likely` is deliberately NOT in this list, and the reason is about *this*
// panel only: re-running identify re-derives the same verdict from the same
// fingerprints, so counting a `likely` row as work would pin the Identify
// button open for good. It is not stranded, though - it has a bucket pill of
// its own above, an opt-in checkbox in the write preview, and a line in the
// strip's Apply step, which is where a `likely` row is actually acted on.
const IDENTIFY_TODO = [
  ["scan", "no-identity", "with no identity yet"],
  ["scan", "uuid-mismatch", "Whisparr disagreements"],
  ["identify", "ambiguous", "waiting on your answer"],
  ["identify", "unmatched", "nothing matched"],
  ["identify", "pending-hash", "pending hashing"],
];

// These count whole libraries, so they carry separators like every other figure
// on screen (the status bar, `renderEstimate`, `pagerHtml`).
const countText = (n) => escapeHtml(Number(n || 0).toLocaleString());

//: What a Pending hashing row says when Pronoisseur's perceptual fingerprints
//: are switched off, and when it is too old to share any. The server words the
//: same two reasons on each row's note (`library_common`); the banner repeats
//: them once for the whole list.
const PENDING_SWITCH_OFF = "Pronoisseur's perceptual fingerprints are switched "
  + "off. Turn on Settings → Local API → Fingerprint videos for "
  + "connected apps in Pronoisseur.";
const PENDING_OLD_PRONOISSEUR = "This version of Pronoisseur doesn't share "
  + "fingerprints. Update Pronoisseur to identify by fingerprint.";

//: Step 3's summary once nothing is left to fingerprint. The default would hide
//: the banner's news on a collapsed step, so the two states that matter say so.
function nothingToFingerprint(scan, pendingHash) {
  const fp = (scan || {}).fingerprints || {};
  if (fp.state === "older") return "Pronoisseur shares no fingerprints — update it";
  if (fp.state === "known" && fp.phashEnabled === false && pendingHash > 0) {
    return "perceptual fingerprints are off in Pronoisseur";
  }
  return "nothing left to fingerprint";
}

//: The banner above the Identify step's results, or nothing. An older
//: Pronoisseur always gets it; a switched-off one only while some video is
//: pending, since with nothing pending the switch is not in anyone's way. An
//: unreachable `/info` ("unknown") gets none: nothing is known to say.
function fingerprintBannerHtml(scanState) {
  const fp = (scanState || {}).fingerprints || {};
  const pending = ((scanState || {}).counts || {})["pending-hash"] || 0;
  const text = fp.state === "older" ? PENDING_OLD_PRONOISSEUR
    : (fp.state === "known" && fp.phashEnabled === false && pending)
      ? PENDING_SWITCH_OFF : "";
  return text ? `<div class="empty warn">${escapeHtml(text)}</div>` : "";
}

//: "All fingerprints from Pronoisseur: 40 with a perceptual hash, 3 pending
//: hashing." over the rows the run looked at, then - library-wide, from the
//: scan's `/info` and only when Pronoisseur gave a figure - how many videos it
//: could not read.
function fingerprintSourceHtml(fin, scanState) {
  const failed = ((scanState || {}).fingerprints || {}).phashFailed;
  return `All fingerprints from Pronoisseur:
    ${countText(fin.phashes)} with a perceptual hash,
    ${countText(fin["pending-hash"])} pending hashing.${failed > 0
      ? ` Pronoisseur couldn't read ${countText(failed)} video${
          failed === 1 ? "" : "s"}.` : ""}`;
}

function renderLibraryIdentify() {
  const s = library.state;
  const box = $("#library-identify");
  // Before the first scan there are no rows to identify and nothing true to say
  // about them - `start_identify` itself answers "run Scan first" in this state,
  // and claiming everything is already identified would be a lie about a library
  // nothing has looked at. `renderLibraryBuckets` handles the same pre-scan case
  // the same way: render nothing.
  if (!s.connected || !(s.scan || {}).considered) { box.innerHTML = ""; return; }
  const identify = s.identify || {};
  const counts = identify.counts || {};
  const scanCounts = (s.scan || {}).counts || {};
  const todo = IDENTIFY_TODO
    .map(([from, key, label]) =>
      [(from === "scan" ? scanCounts[key] : counts[key]) || 0, label])
    .filter(([n]) => n);
  const pending = todo.reduce((total, [n]) => total + n, 0);
  const running = !!identify.running;
  const cancelled = identify.label === "cancelled";
  const fin = identify.finished;
  // A scan landing mid-identify rebinds the row list, so that run's rows are
  // orphaned: its `finished` still reports what it considered while the counts
  // and questions, read off the new list, are empty. Saying "done" over nothing
  // would be a lie about a run that did its work on a library listing that no
  // longer exists.
  //
  // A CANCELLED run can reach the identical state with no scan anywhere near
  // it: a cancel before the first batch is settled marks nothing
  // `from_identify` and leaves both counts and questions empty, while
  // `_identify_run` still writes `finished` and cancelling is not an `error`.
  // Only the label tells the two apart, so the label is in the predicate: the
  // alternative is telling the user a scan replaced their library when they had
  // simply pressed Cancel.
  const orphaned = !!fin && fin.considered > 0 && !identify.error && !cancelled
    && Object.values(counts).reduce((a, b) => a + b, 0) === 0
    && !(identify.questions || []).length;

  box.innerHTML = `
    ${pending || running ? `<div class="toolbar">
      <span class="spacer"></span>
      <button class="primary" id="lib-identify" data-step-action="identify"
        ${(running || s.scan.running || s.apply.running) ? "disabled" : ""}>
        ${running ? "Identifying..." : "Identify by fingerprint"}</button>
      ${running ? `<button class="ghost" id="lib-identify-cancel">Cancel</button>` : ""}
    </div>` : ""}
    ${running ? `<div class="estimate"><span class="figure">
        ${escapeHtml(progressText(identify.label, 1))}: <b>${escapeHtml(identify.done)}</b> of
        <b>${escapeHtml(identify.total)}</b></span></div>
      <div class="hint pad">Results show up in Check matches and tag as each batch is
        looked up. Answer and write them while this runs; the files it has not
        reached yet stay out of the list until it does.</div>` : ""}
    ${libraryErrorHtml("identify")}
    ${cancelled && !running ? `<div class="empty warn">That run was cancelled.
      Everything it had already settled is kept, and picking it up again is
      cheap: every answer is keyed by the file's content hash, so a new run
      resumes rather than starting over.</div>` : ""}
    ${fingerprintBannerHtml(s.scan)}
    ${orphaned ? `<div class="empty warn">That run considered
      <b>${countText(fin.considered)}</b> videos, but a scan landed while it was
      running, so its rows belong to a library listing that has since been
      replaced &mdash; which is why there is nothing here to show. Running
      Identify again is safe and cheap: every answer that run recorded is keyed
      by the file's content hash and is picked straight back up. Only the run's
      own progress display was lost.</div>`
      // A cancelled run gets the plain line above instead: `considered` is the
      // rows it set out to do, not the rows it reached, so reporting it as a
      // finished tally would overstate what actually happened.
      : (fin && !cancelled) ? `<div class="empty good">Considered
        ${countText(fin.considered)} videos &mdash;
        ${countText(fin.verified)} verified, ${countText(fin.likely)} likely,
        ${countText(fin.ambiguous)} to answer, ${countText(fin.unmatched)}
        unmatched, ${countText(fin.failed)} could not be settled.
        ${fingerprintSourceHtml(fin, s.scan)}
        ${fin.failed ? `The rows that failed carry the reason &mdash; a lookup
          StashDB refused. Running Identify again retries them; the
          fingerprints come from Pronoisseur, so it is quick.` : ""}
        </div>` : ""}`;

  wireLibraryErrorDismiss(box);
  const start = $("#lib-identify");
  if (start) start.onclick = startIdentify;
  const cancel = $("#lib-identify-cancel");
  if (cancel) cancel.onclick = cancelIdentify;
}

async function startIdentify() {
  try {
    const out = await api("/api/library/match/identify", { method: "POST", body: {} });
    if (!out.started) toast("Not started", out.reason || "nothing to identify");
    // Same reason a scan clears it. An identify does not rebind the row list,
    // but it re-settles rows in place: a ticked `likely` can come back pointing
    // at a different scene, or stop being `likely` at all, and the tick would
    // survive to authorise a write against a listing the user never saw. Only
    // when the run actually started - a refused call changed nothing, and
    // throwing the ticks away would be a second thing going wrong.
    if (out.started) library.include = new Set();
    // And the acceptance line with them: a run re-decides every ambiguous row,
    // so a tally taken against the previous set of questions describes rows that
    // may no longer be ambiguous, or may no longer exist.
    if (out.started) library.accepted = null;
    loadLibrary();
  } catch (err) { reportError(err); }
}

async function cancelIdentify() {
  try {
    await api("/api/library/match/cancel", { method: "POST", body: {} });
    loadLibrary();
  } catch (err) { reportError(err); }
}

// Step 4 is grouped by model, so one model's videos are looked at - and their
// recommendations accepted - together. A row's models come from the server
// (`review_models`): the ones the video already has in Pronoisseur, plus the
// cast of the scene StashDB recommends for it. A video with two models sits in
// both groups, and answering it in one clears it from both, because every group
// is drawn from the one list of rows. Inside a group the rows with matches come
// first, then the ones StashDB found nothing for.

//: The group of rows that name no model anywhere. A model's group is `m-` plus
//: its name key, so no model's name can collide with this one.
const NO_MODEL = "nomodel";
const modelGroupId = (key) => (key ? "m-" + key : NO_MODEL);

//: The models a row is grouped under. Never undefined, never a nameless entry.
const rowModels = (row) => (Array.isArray(row.models) ? row.models : [])
  .filter((m) => m && m.key);

//: Rows into model groups: the biggest first, "No model" always last, ties by
//: name. Each group keeps the rows in the order they came in.
function reviewModelGroups(rows) {
  const groups = new Map();
  const put = (id, key, name, row) => {
    if (!groups.has(id)) groups.set(id, { id, key, name, rows: [] });
    groups.get(id).rows.push(row);
  };
  rows.forEach((row) => {
    const models = rowModels(row);
    if (!models.length) put(NO_MODEL, "", "No model", row);
    models.forEach((m) => put(modelGroupId(m.key), m.key, m.name, row));
  });
  return Array.from(groups.values()).sort((a, b) =>
    (a.id === NO_MODEL) - (b.id === NO_MODEL)
    || b.rows.length - a.rows.length
    || String(a.name).localeCompare(String(b.name)));
}

//: The ids of the model groups a row is drawn under: one per model, or "No
//: model".
const rowGroupIds = (row) => {
  const models = rowModels(row);
  return models.length ? models.map((m) => modelGroupId(m.key)) : [NO_MODEL];
};

//: Per group, how many rows its "Use N recommended" would take: the safe ones
//: among the rows to choose that the view shows, as `choicesInView` decides.
function safeByGroup() {
  const counts = new Map();
  choicesInView().forEach((q) => {
    if (!q.recommendationSafe) return;
    rowGroupIds(q).forEach((id) => counts.set(id, (counts.get(id) || 0) + 1));
  });
  return counts;
}

//: Where a scene lives for a human. Built here from the id rather than shipped
//: on the card, so the server keeps sending one scene id and this stays a
//: renderer — and so the two grids cannot end up linking differently.
const STASHDB_SCENE_URL = "https://stashdb.org/scenes/";

//: The filename filter, against the path and the title, both lower-cased —
//: a filter that misses on case is a filter nobody trusts. Plain `includes`
//: rather than a regex: these are user paths, and a stray `(` would throw.
function reviewMatches(row, needle) {
  if (!needle) return true;
  return (String(row.relPath || "") + " " + String(row.title || ""))
    .toLowerCase().includes(needle);
}

const reviewNeedle = () => library.reviewFilter.trim().toLowerCase();

//: Whether the kind chip on shows rows of `kind` ("ambiguous", "ready",
//: "unmatched" or "pending").
const reviewKindShown = (kind) =>
  library.reviewKind === "all" || library.reviewKind === kind;

//: Whether the view shows a row of `kind`: the kind chip and the filename
//: filter. What you see is what you tag - the list, Tag N and Use recommended
//: all go by this one test, so none of them can take a row the others hide.
const reviewShows = (kind, row) =>
  reviewKindShown(kind) && reviewMatches(row, reviewNeedle());

//: Both review lists, filtered, plus the unfiltered total so the filter's hint
//: can say what it is hiding. Mid-run, only the rows already on screen
//: (`library.reviewSeen`) are in either: the rest are counted by
//: `holdReviewRows` until it lets them in.
function reviewGroupRows() {
  const all = reviewRowsAll();
  const seen = library.reviewSeen;
  const onScreen = (row) => !seen || seen.has(row.videoId);
  const needle = reviewNeedle();
  const out = { total: 0, named: {} };
  REVIEW_KINDS.forEach(([kind]) => {
    const rows = all[kind].filter(onScreen);
    out.total += rows.length;
    const named = rows.filter((row) => reviewMatches(row, needle));
    // Counted before the kind chip narrows anything, so each chip can say how
    // many it would show.
    out.named[kind] = named.length;
    out[kind] = reviewKindShown(kind) ? named : [];
  });
  return out;
}

//: The four kinds of row in the one list, in the order they come inside a
//: group: the ones that need you, the ones ready to write, the ones StashDB has
//: nothing for, the ones Pronoisseur has no perceptual fingerprint for yet. Each
//: is also a chip that narrows every group to that kind.
const REVIEW_KINDS = [
  ["ambiguous", "Choose a match"],
  ["ready", "Matched"],
  ["unmatched", "No match"],
  ["pending", "Pending hashing"],
];

//: Mid-run, how many settled rows are not on screen yet - and the bookkeeping
//: that decides when they come in. A run lands a batch every few seconds, and
//: drawing each into the grid would rebuild the whole box under the user. So the
//: rows on screen when the run is first drawn stay the rows on screen, and later
//: ones are counted until this lets them in: on a poll, unless
//:  * the user is busy in the list (`reviewListBusy`): a field in it has focus,
//:    or a Search by name panel is open. The rebuild would wipe what they are
//:    typing or the results they are reading, so they wait, and the line says
//:    so (`busy`); or
//:  * the list was last redrawn under `REVIEW_AUTO_SHOW_MS` ago. Those rows wait
//:    for the next window, silently.
//: Two exceptions, both where there is nothing to disturb: an empty list takes
//: new rows straight in, and no run going means every row is shown.
//: Returns how many are waiting and whether the user is why.
function holdReviewRows(running) {
  if (!running) { library.reviewSeen = null; return { count: 0, busy: false }; }
  const all = reviewRowsAll();
  const rows = all.ambiguous.concat(all.ready, all.unmatched, all.pending);
  const now = Date.now();
  if (!library.reviewSeen) {
    library.reviewSeen = new Set(rows.map((row) => row.videoId));
    library.reviewShownAt = now;
  }
  const seen = library.reviewSeen;
  const fresh = rows.filter((row) => !seen.has(row.videoId));
  const release = () => {
    fresh.forEach((row) => seen.add(row.videoId));
    library.reviewShownAt = now;
    return { count: 0, busy: false };
  };
  if (!fresh.length) return { count: 0, busy: false };
  // Rows answered since are gone from `rows`, so this is what is on screen now.
  if (rows.length === fresh.length) return release();
  if (reviewListBusy()) return { count: fresh.length, busy: true };
  if (now - library.reviewShownAt < REVIEW_AUTO_SHOW_MS) {
    return { count: fresh.length, busy: false };
  }
  return release();
}

//: Whether the user is working inside a card: a text field, select or textarea
//: in the list has focus, or a Search by name panel (`assistedPanel`, a
//: `<details class="assisted">`) is open. Either would be lost to a rebuild.
function reviewListBusy() {
  const box = $("#library-questions");
  if (!box) return false;
  const active = document.activeElement;
  if (active && /^(INPUT|TEXTAREA|SELECT)$/i.test(active.tagName || "")
      && box.contains && box.contains(active)) {
    return true;
  }
  return $$("details.assisted[open]", box).length > 0;
}

//: "Show them": the escape hatch while the user is busy in a card, and the way
//: in for everything settled so far. Forgetting what was seen is enough - the
//: next render takes every current row as seen.
function showFreshReviewRows() {
  library.reviewSeen = null;
  // The steps, not only the list: the rows shown now join Tag N, which the
  // toolbar and the bar both count.
  renderLibrarySteps();
}

//: Both review lists, unfiltered and complete.
function reviewRowsAll() {
  const s = library.state;
  const questions = (s.identify || {}).questions || [];
  // `unmatched` rows are deliberately NOT questions - there are no candidates to
  // show a grid of - but they are exactly the rows assisted search exists for,
  // so they get the same block with the grid left out. They come from the scan's
  // buckets, which are built by the preview planner and are therefore snake_case
  // (`video_id`, `rel_path`), while the identify cards above are camelCase
  // (`videoId`, `relPath`). Crossing the two is silent, so the translation is
  // done once, here, and nothing downstream reads a bucket row again.
  // `terms` is null on purpose: a bucket row is a planned write, and the parsed
  // search terms are not on it. `assistedPanel` shows its placeholder for these
  // rather than reporting a studio and date of "none" it never actually read.
  // `pending-hash` rows are the same shape and the same block; the flag is what
  // gives them their own badge, and `note` carries the reason.
  const fromBucket = (status, extra) =>
    ((s.scan.buckets || {})[status] || []).map((row) => Object.assign({
      videoId: row.video_id, relPath: row.rel_path, path: row.path,
      title: row.title, models: row.models || [],
      durationSecs: null, note: row.note, whisparrSceneId: null, terms: null,
      noDurationMatch: false, candidates: [],
    }, extra));
  const unmatched = fromBucket("unmatched");
  const pending = fromBucket("pending-hash", { pending: true });
  // Ready to write: the write preview's rows, which the page fetches anyway.
  // snake_case off the planner, so translated here and nowhere else, like the
  // unmatched rows above.
  const ready = ((library.preview || {}).rows || []).map((row) => ({
    ready: true, videoId: row.video_id, relPath: row.rel_path, path: row.path,
    title: row.title, models: row.models || [], durationSecs: row.duration_secs,
    note: row.note, status: row.status, confidence: row.confidence,
    sceneId: row.scene_id, sceneTitle: row.scene_title, cover: row.cover,
    sceneDate: row.scene_date, sceneDateEstimated: !!row.scene_date_estimated,
    sceneDuration: row.scene_duration, durationAgrees: row.duration_agrees,
    fields: row.fields || [], candidates: [],
  }));
  return { ambiguous: questions, ready, unmatched, pending };
}

// Most ambiguous rows are the same click repeated: the grid has already put the
// right card first, so the work is repetition, not judgement. "Accept all
// recommended" does the repetition.
//
// The recommendation itself is the server's: `_candidate_rank` ranks the grid and
// `recommendationSafe` says whether the leading card is strong enough to take
// unseen. Nothing here re-decides any of it; the browser counts the safe rows and
// offers the button.

//: The rows to choose that the view shows: the kind chip, the filename filter
//: and, mid-run, only the rows already drawn. This is what Use recommended
//: sends - the unsafe ones too, so the server can count and explain the ones
//: it leaves - and the server takes the safe ones among them and nothing else.
function choicesInView() {
  const seen = library.reviewSeen;
  return (((library.state || {}).identify || {}).questions || [])
    .filter((q) => (!seen || seen.has(q.videoId)) && reviewShows("ambiguous", q));
}

//: The safe rows among them: the number beside "Use recommended matches" is
//: the number of rows that will actually move.
function recommendedSafeCount() {
  return choicesInView().filter((q) => q.recommendationSafe).length;
}

//: The label both copies of the button wear - the one above the Ambiguous list
//: and the one in the sticky bar. One function, so the two counts cannot differ.
const acceptRecommendedLabel = () =>
  `Use recommended matches (${countText(recommendedSafeCount())})`;

//: What the last acceptance did, left on screen until a scan or an identify run
//: makes it untrue. The rows it took have LEFT the list, so this is the only
//: thing that says why the ones still here stayed.
function acceptedLineHtml() {
  const done = library.accepted;
  if (!done) return "";
  const reasons = Object.keys(done.leftReasons || {})
    .filter((key) => done.leftReasons[key]);
  return `<div class="empty good">Used <b>${countText(done.accepted)}</b>
    recommended match${done.accepted === 1 ? "" : "es"}
    &middot; ${countText(done.left)} still to choose${reasons.length
      ? ` (${escapeHtml(reasons.join(", "))})` : ""}.</div>`;
}

// One handler for the button over the list, the one in the sticky bar and the
// one on each model group. It sends the rows to choose that the view shows -
// in one group, for a group's button - and the server takes the safe ones
// among them, so the button never moves a video the user cannot see.
async function acceptAllRecommended(group) {
  // Taken now, not when the button was drawn, so the dialog names exactly the
  // rows the request will send.
  const rows = choicesInView()
    .filter((q) => !group || rowGroupIds(q).includes(group.id));
  const safe = rows.filter((q) => q.recommendationSafe).length;
  if (!safe) return;
  const whose = !group ? "" : group.key ? ` for ${group.name}` : " with no model";
  const ok = await modal({
    title: `Use ${safe.toLocaleString()} recommended match${safe === 1 ? "" : "es"}${whose}?`,
    html: `<p>Each video takes its <b>Recommended</b> match &mdash; the
      strongest fingerprint, and among equals the scene StashDB lists the most
      tags for.</p>
      <p><b>Only videos this view shows, and only those whose recommended
        scene runs the same length as your file, are taken.</b> The rest stay
        for you to choose, and the line above the list says how many and
        why.</p>
      <p><b>Nothing is tagged yet.</b> These move to <b>Matched</b>, ticked,
        and Tag writes them once you have looked. Each answer is remembered
        against the file's content hash, exactly as choosing it by hand is,
        and choosing a different scene later overwrites it.</p>`,
    okLabel: `Use ${safe.toLocaleString()}`,
  });
  if (!ok) return;
  try {
    const out = await api("/api/library/match/accept-recommended",
                          { method: "POST",
                            body: { videoIds: rows.map((q) => q.videoId) } });
    library.accepted = out;
    toast(`Used ${Number(out.accepted || 0).toLocaleString()} recommended`,
          `${Number(out.left || 0).toLocaleString()} still to choose`,
          out.accepted ? "good" : "warn");
    loadLibrary();
  } catch (err) { reportError(err); }
}

//: Every model group starts shut, so a library of hundreds of models reads as a
//: list of names rather than a wall of covers - except when there is only one
//: group, or a filter is narrowing them, where shut would only mean one more
//: click to see what you asked for. A group the user opened or shut stays so.
function reviewGroupOpen(id, openByDefault) {
  const manual = library.reviewOpen[id];
  return manual == null ? openByDefault : manual;
}

//: Narrows every group at once, and resets the paging with it: "show 50 more" of
//: a list you have just replaced means nothing, and carrying a count of 150 into
//: the next filter would open it with three pages of pictures already drawn.
//: The sticky bar is redrawn with the list: its Tag N and Use recommended
//: follow the view, and it is on screen while the view is changed.
function setReviewFilter(value) {
  library.reviewFilter = String(value || "");
  library.shown = {};
  renderLibraryQuestions();
  renderLibraryBar();
}

//: The kind chips: All, Choose a match, Matched, No match, Pending hashing. Same reset as the
//: filter, and the bar with it.
function setReviewKind(kind) {
  library.reviewKind = kind;
  library.shown = {};
  renderLibraryQuestions();
  renderLibraryBar();
}

//: Expand all / Collapse all: every model group in the list, as by hand, so a
//: later poll keeps them that way. Opened groups still draw a page of cards
//: each (`QUESTION_PAGE`), and their pictures load as they near the screen.
function setAllReviewGroups(open) {
  library.reviewGroups.forEach((group, id) => { library.reviewOpen[id] = open; });
  renderLibraryQuestions();
}

//: What a row draws, for the signature: its kind, its id, and the scenes and
//: verbs on show - a match's or, for a ready row, the write's.
function rowSignature(q) {
  const verbs = (fields) => (fields || [])
    .map((f) => `${f.action}${(f.added || []).length}`).join(".");
  if (q.ready) return `r${q.videoId}:${q.sceneId}/${verbs(q.fields)}`;
  // The reason is what a pending card says, and it changes with a re-scan
  // against a different Pronoisseur without the row set changing.
  if (q.pending) return `p${q.videoId}:${q.note}`;
  return `${q.videoId}:${(q.candidates || [])
    .map((c) => `${c.sceneId}/${verbs(c.fields)}`).join(",")}`;
}

// The signature of what a render would draw: whether the section is open at all,
// the filter, and per group whether it is open, how many blocks are uncapped,
// its row ids and the scene ids each row offers. Cheap — a string join over ids
// — and that is the point. Everything that changes the markup is in it, so an
// equal signature means an equal document and the render can be skipped; a poll
// during a scan, an apply or an identify brings back the same questions over and
// over.
//
// Candidate scene ids are in it, not just a count: a new run re-decides the rows
// an earlier one left, and can do so without adding or removing one, so the
// covers on the card would otherwise be wrong for as long as the page stayed open.
//
// Whether a run is going is NOT in it. A run in flight and a finished one draw
// the same rows the same way, so the moment it ends redraws the box only if it
// actually changes it - when rows that were waiting mid-run come in - and not
// merely because the run stopped.
//
// What each match would write is in it too, as its verbs: changing a setting
// re-plans every match without changing a single scene id.
//
// Ticks are NOT in it. A tick is set in place (`setTicked`), and every redraw
// draws the ticks `library.unpicked` holds, so a redraw can never undo one.
function questionsSignature(groups, sectionOpen, filter, accepted) {
  return [sectionOpen ? "open" : "shut", filter, library.reviewKind, accepted]
    .concat(groups.map((g) => [
      g.id, g.open ? "open" : "shut", g.shown,
      g.rows.map(rowSignature).join("|"),
    ].join("~"))).join("#");
}

// Rebuilding this box is the expensive thing on the page, so it happens as rarely
// as it can:
//
//  * Nothing is drawn while step 4's section is collapsed, and nothing while a
//    nested group is. Not hidden — not drawn: `hidden` would leave every cover
//    <img> in the document and every one of them fetched, and the head's badge
//    already says how many rows are in there.
//  * While an identify run is in flight, the rows it settles after the list was
//    drawn come in on their own, but not on every poll (`holdReviewRows`): not
//    while the user is working in a card, and not more than once per
//    `REVIEW_AUTO_SHOW_MS`. A line above the box counts the ones a busy user is
//    waiting on, with a button to bring them in now. When the box is rebuilt the
//    page scrolls to keep the group the user was looking at where it was.
//  * A poll that brings back the same rows re-renders nothing (`questionsKey`).
//  * Each list is capped at `QUESTION_PAGE` blocks; the rest come in on a button.
//    `library.shown` only ever grows within a filter, so answering a question
//    does not collapse an expanded list back to the cap.
function renderLibraryQuestions() {
  const s = library.state;
  const box = $("#library-questions");
  const hint = $("#lib-review-filter-hint");
  const freshLine = $("#lib-review-fresh");
  if (!s || !s.connected) {
    box.innerHTML = "";
    if (hint) hint.innerHTML = "";
    if (freshLine) freshLine.innerHTML = "";
    library.questionsKey = null;
    library.reviewSeen = null;
    return;
  }

  const step = libraryStepByKey("review");
  const sectionOpen = step ? librarySectionOpen(step) : true;
  const held = holdReviewRows(!!(s.identify || {}).running);
  const fresh = held.count;
  const found = reviewGroupRows();
  // To pick first, then ready, then not found, so inside every group the rows
  // that need you lead.
  const rows = found.ambiguous.concat(found.ready, found.unmatched,
                                      found.pending);
  const safe = safeByGroup();
  const models = reviewModelGroups(rows);
  const openByDefault = models.length === 1 || !!library.reviewFilter.trim()
    || library.reviewKind !== "all";
  const groups = models.map((g) => Object.assign(g, {
    open: reviewGroupOpen(g.id, openByDefault),
    shown: Math.min(library.shown[g.id] || QUESTION_PAGE, g.rows.length),
    safe: safe.get(g.id) || 0,
    asks: g.rows.some((row) => (row.candidates || []).length > 0),
  }));
  // Rows, not group memberships: a video under two models is one row to answer.
  const matched = rows.length;
  library.reviewIndex = new Map(rows.map((row) => [row.videoId, row]));
  library.reviewGroups = new Map(groups.map((g) => [g.id, g]));
  // The toolbar counts what the view shows and the rows still waiting, all
  // decided just now, so it is drawn again from them. Cheap: it draws nothing
  // when nothing in it changed.
  renderLibraryWriteBar();
  // Nothing to open or shut with no group in the list.
  const expandRow = $("#lib-review-expand-row");
  if (expandRow) expandRow.hidden = !groups.length;

  // Outside the signature guard: the hint is one short string, and it has to be
  // right even on the polls where nothing else is redrawn.
  if (hint) {
    hint.innerHTML = library.reviewFilter.trim()
      ? `${countText(matched)} of ${countText(found.total)} rows match.` : "";
  }
  // The kind chips, outside it for the same reason: each says how many rows it
  // would show, and those move with every batch.
  renderReviewKinds(found);
  // Outside it too, and for the same reason: it is the one thing that changes
  // on every batch while the rows on screen stay exactly as they were. Said
  // only when the user is why they wait: rows held back by the redraw limit
  // arrive within seconds, and a line for them would only flicker.
  if (freshLine) {
    freshLine.innerHTML = fresh && held.busy && sectionOpen ? `<div class="empty fresh">
      <b>${countText(fresh)}</b> new result${fresh === 1 ? "" : "s"} will appear
      when you're done with this card.
      <button type="button" class="ghost" id="lib-review-fresh-show">Show
        them</button></div>` : "";
    const show = $("#lib-review-fresh-show");
    if (show) show.onclick = showFreshReviewRows;
  }

  // The accept and tick counts are kept out of the signature and set in place
  // instead: a tick changes them without changing a single row, and setting a
  // count is cheaper than redrawing every cover around it.
  refreshGroupHeads(box, safe);

  // The acceptance line is in the signature because it is the one thing in this
  // box that can change while every row in it stays exactly the same: an accept
  // that took nothing still has to say so.
  // Plus the one place a run in flight does draw differently: an empty list,
  // which says nothing mid-run and "nothing waiting" once the run is over.
  const emptyRun = !found.total && !!(s.identify || {}).running;
  const key = questionsSignature(groups, sectionOpen, library.reviewFilter,
                                 JSON.stringify(library.accepted || null))
    + (emptyRun ? "#empty-run" : "");
  if (key === library.questionsKey) return;
  library.questionsKey = key;

  if (!sectionOpen) { box.innerHTML = ""; return; }

  // In front of all three endings below, including the empty ones: an accept
  // that emptied the list is exactly when its tally matters most.
  const led = acceptedLineHtml();

  // Reachable only when the step has been opened by hand - it collapses itself
  // once it is done - and an open panel with nothing in it should say why.
  if (!found.total) {
    // Mid-run, an empty list is only "nothing yet": the run is still deciding
    // rows, and saying nothing waits on an answer would be a claim about rows
    // nobody has looked at. Nothing at all is drawn until it turns one up.
    box.innerHTML = (s.identify || {}).running ? led : led
      + `<div class="empty good">Nothing to tag or check.</div>`;
    return;
  }

  if (!matched) {
    const kind = REVIEW_KINDS.filter(([key]) => key === library.reviewKind)[0];
    box.innerHTML = led + `<div class="empty">${library.reviewFilter.trim()
      ? `Nothing here matches &ldquo;${escapeHtml(library.reviewFilter.trim())}&rdquo;${
          kind ? ` in &ldquo;${escapeHtml(kind[1])}&rdquo;` : ""}.`
      : `Nothing in &ldquo;${escapeHtml(kind ? kind[1] : "All")}&rdquo;.`}</div>`;
    return;
  }

  const anchor = scrollAnchor(box);
  box.innerHTML = led + acceptRecommendedToolbar()
    + groups.map(reviewGroupHtml).join("");
  keepScrollAnchor(box, anchor);
  wireQuestionBlocks(box);
  // Again, now the heads exist: a half-ticked group's dash is a DOM property,
  // `indeterminate`, which no markup can set.
  refreshGroupHeads(box, safe);
  $$("[data-group-pick]", box).forEach((input) => {
    input.onchange = () => {
      const group = library.reviewGroups.get(input.dataset.groupPick);
      if (!group) return;
      setTicked(group.rows.filter((row) => row.ready).map((row) => row.videoId),
                input.checked);
    };
  });
  $$("[data-accept-recommended]", box).forEach((btn) => {
    btn.onclick = () => acceptAllRecommended();
  });
  $$("[data-accept-model]", box).forEach((btn) => {
    btn.onclick = (event) => {
      // The button sits in the group's <summary>: without this the same click
      // would also open or shut the group under the pointer.
      event.preventDefault();
      event.stopPropagation();
      const group = groups.find((g) => g.id === btn.dataset.acceptModel);
      if (group) acceptAllRecommended({ id: group.id, key: group.key, name: group.name });
    };
  });
  $$("[data-group]", box).forEach((el) => {
    // A closed group draws no blocks, so opening one has to rebuild rather than
    // just reveal. Native <details> parsed with `open` already set fires no
    // toggle event, so this cannot loop.
    el.ontoggle = () => {
      library.reviewOpen[el.dataset.group] = !!el.open;
      renderLibraryQuestions();
    };
  });
  groups.forEach((g) => {
    // Looked up only when one was drawn. Asking for a button that is not there
    // and leaning on the null is the same code either way in a browser, but it
    // reads as "there may or may not be a button" when the group already said.
    if (!g.open || g.rows.length <= g.shown) return;
    const more = $("#lib-more-" + g.id);
    if (!more) return;
    more.onclick = () => {
      library.shown[g.id] = g.shown + QUESTION_PAGE;
      renderLibraryQuestions();
    };
  });
}

//: Where the user is in the list, before it is rebuilt: the first group head
//: whose bottom is below the top of the viewport, by group id, with its top.
//: Null when nothing can be measured (a group-less list, or no layout at all,
//: as under the test stub).
function scrollAnchor(box) {
  const heads = $$("[data-group]", box);
  for (const el of heads) {
    const head = el.querySelector ? el.querySelector("summary") || el : el;
    if (!head.getBoundingClientRect) return null;
    const rect = head.getBoundingClientRect();
    if (rect.bottom > 0) return { id: el.dataset.group, top: rect.top };
  }
  return null;
}

//: After the rebuild: scroll by however far that group's head moved, so the
//: user keeps their place while rows join above or around it. Nothing when the
//: group is gone (nothing to hold on to) or the page cannot scroll.
function keepScrollAnchor(box, anchor) {
  if (!anchor || typeof window.scrollBy !== "function") return;
  const el = $$("[data-group]", box).find((g) => g.dataset.group === anchor.id);
  if (!el) return;
  const head = el.querySelector ? el.querySelector("summary") || el : el;
  if (!head.getBoundingClientRect) return;
  const moved = head.getBoundingClientRect().top - anchor.top;
  if (moved) window.scrollBy(0, moved);
}

//: The Use-recommended button, once over every group. Drawn only when the view
//: shows a row to choose - none under Matched or No match - and disabled at
//: nought rather than hidden, so the list does not change shape under the
//: pointer as the last few rows are answered by hand.
function acceptRecommendedToolbar() {
  if (!choicesInView().length) return "";
  return `<div class="toolbar">
    <button class="secondary" data-accept-recommended="1"
      ${recommendedSafeCount() ? "" : "disabled"}>${acceptRecommendedLabel()}</button>
    <span class="hint">Uses the recommended match for every video this view
      shows whose length matches your file. Each moves to Matched, ticked; the
      rest stay for you to choose.</span>
  </div>`;
}

//: A group's rows by kind, and how many of its ready ones are ticked.
function groupCounts(g) {
  const out = { pick: 0, ready: 0, ticked: 0, none: 0, pending: 0 };
  g.rows.forEach((row) => {
    if (row.ready) {
      out.ready += 1;
      if (!library.unpicked.has(row.videoId)) out.ticked += 1;
    } else if (row.pending) {
      out.pending += 1;
    } else if ((row.candidates || []).length) {
      out.pick += 1;
    } else {
      out.none += 1;
    }
  });
  return out;
}

//: Set every group head's counts, its model tick and its accept button, and the
//: accept-all button, to what they are right now - without redrawing anything
//: around them. `safe` is `safeByGroup()`, worked out here when not passed.
function refreshGroupHeads(box, safe) {
  const counts = safe || safeByGroup();
  $$("[data-accept-model]", box).forEach((btn) => {
    const count = counts.get(btn.dataset.acceptModel) || 0;
    btn.textContent = `Use ${count.toLocaleString()} recommended`;
    btn.disabled = !count;
  });
  $$("[data-lede-for]", box).forEach((lede) => {
    const group = library.reviewGroups.get(lede.dataset.ledeFor);
    if (group) lede.textContent = `— ${reviewGroupLede(group)}`;
  });
  $$("[data-group-pick]", box).forEach((input) => {
    const group = library.reviewGroups.get(input.dataset.groupPick);
    if (!group) return;
    const { ready, ticked } = groupCounts(group);
    input.checked = ready > 0 && ticked === ready;
    input.indeterminate = ticked > 0 && ticked < ready;
  });
  $$("[data-accept-recommended]", box).forEach((btn) => {
    btn.textContent = acceptRecommendedLabel();
    btn.disabled = !recommendedSafeCount();
  });
}

//: What a group's head says after its name and count, as plain text:
//: "11 matched (10 ticked) · 1 to choose · 2 no match · 2 pending hashing".
function reviewGroupLede(g) {
  const n = (count) => Number(count || 0).toLocaleString();
  const c = groupCounts(g);
  const ticked = c.ticked === c.ready ? "all ticked"
    : c.ticked ? `${n(c.ticked)} ticked` : "none ticked";
  return [
    c.ready ? `${n(c.ready)} matched (${ticked})` : "",
    c.pick ? `${n(c.pick)} to choose` : "",
    c.none ? `${n(c.none)} no match` : "",
    c.pending ? `${n(c.pending)} pending hashing` : "",
  ].filter(Boolean).join(" · ");
}

function reviewGroupHtml(g) {
  const rest = g.rows.length - g.shown;
  const body = !g.open ? "" : (g.rows.slice(0, g.shown)
      .map((q) => questionBlock(q, g)).join("")
    + (rest ? `<div class="toolbar">
        <button class="ghost" id="lib-more-${escapeHtml(g.id)}">Show
          ${countText(Math.min(rest, QUESTION_PAGE))} more</button>
        <span class="hint">${countText(g.shown)} of
          ${countText(g.rows.length)} shown &mdash; each one carries pictures,
          so they load in batches.</span>
      </div>` : ""));
  // On the head, so a shut group can be accepted without opening it. Only on a
  // group with matches in it: a group of files StashDB found nothing for has
  // nothing to recommend.
  const accept = g.asks ? `<button type="button" class="ghost small"
      data-accept-model="${escapeHtml(g.id)}" ${g.safe ? "" : "disabled"}
      >Use ${countText(g.safe)} recommended</button>` : "";
  // The model's own tick: every ready video in the group, at once - accepting a
  // model's matches without opening it. Inside <summary> but still a checkbox:
  // a click on it ticks, it does not open or shut the group. Half-ticked is
  // drawn unticked here and given its dash by `refreshGroupHeads`.
  const c = groupCounts(g);
  const pick = c.ready ? `<input type="checkbox" class="group-pick"
      data-group-pick="${escapeHtml(g.id)}" ${c.ticked === c.ready ? "checked" : ""}
      ${c.ticked && c.ticked < c.ready ? 'data-partial="1"' : ""}
      title="Tick or untick every matched video under ${escapeHtml(g.name)}"
      aria-label="Tag every matched video under ${escapeHtml(g.name)}">`
    : `<span class="group-pick-gap"></span>`;
  return `
    <details class="lib-group" data-group="${escapeHtml(g.id)}"
             ${g.open ? "open" : ""}>
      <summary>
        ${pick}
        <span class="lib-group-title">${escapeHtml(g.name)}</span>
        <span class="lib-badge">${countText(g.rows.length)}</span>
        <span class="lib-group-lede" data-lede-for="${escapeHtml(g.id)}"
              >&mdash; ${escapeHtml(reviewGroupLede(g))}</span>
        ${accept}
      </summary>
      ${body}
    </details>`;
}

//: All / Choose a match / Matched / No match / Pending hashing, each with how many it would show. Drawn
//: into the static filter row, beside the filename box.
function renderReviewKinds(found) {
  const box = $("#lib-review-kinds");
  if (!box) return;
  const named = found.named || {};
  const all = REVIEW_KINDS.reduce((n, [kind]) => n + (named[kind] || 0), 0);
  box.innerHTML = [["all", "All", all]]
    .concat(REVIEW_KINDS.map(([kind, label]) => [kind, label, named[kind] || 0]))
    .map(([kind, label, count]) => `<button type="button"
        class="kind-chip${library.reviewKind === kind ? " is-on" : ""}"
        data-kind="${escapeHtml(kind)}" aria-pressed="${library.reviewKind === kind}"
        >${escapeHtml(label)} ${countText(count)}</button>`).join(" ");
  $$("[data-kind]", box).forEach((btn) => {
    btn.onclick = () => setReviewKind(btn.dataset.kind);
  });
}

//: The file's own stills the server can take, one at each of
//: `frames.FRAME_POINTS`. The route refuses any other number.
const FRAME_COUNT = 4;

//: The match on show for a row: the one the user switched to, else the leader.
function selectedCandidate(q) {
  const cands = q.candidates || [];
  const chosen = library.reviewTab[q.videoId];
  return cands.find((c) => c.sceneId === chosen) || cands[0] || null;
}

//: "18 min short", "40s long", or "" when the lengths agree or are unknown.
function lengthGap(c) {
  if (c.durationDelta == null || c.durationAgrees !== false) return "";
  const gap = Math.abs(Number(c.durationDelta) || 0);
  const amount = gap >= 120 ? `${Math.round(gap / 60)} min` : `${Math.round(gap)}s`;
  return `${amount} ${c.durationDelta < 0 ? "short" : "long"}`;
}

//: The leader's badge. Pink "Recommended" is kept for a match that would be
//: taken unseen - the length agrees, or the bytes match and StashDB lists no
//: length. Anything else is only the closest thing on offer, and says why in
//: amber, so a wrong cover is never dressed up as an endorsement.
function recommendationBadge(q, c) {
  if (!c.recommended) return null;
  if (q.recommendationSafe) {
    return { text: `Recommended${c.recommendedByTags ? " &middot; most tags" : ""}`,
             tone: "accent",
             title: c.recommendedByTags
               ? "the same fingerprint and length as the next match, and StashDB "
                 + "lists more tags for this one"
               : "the strongest fingerprint here, and the length agrees" };
  }
  return { text: c.durationAgrees === false ? "Closest &mdash; lengths disagree"
                                            : "Closest &mdash; length unknown",
           tone: "warn", title: "the strongest match here, but not one to take unseen" };
}

function coverHtml(url) {
  return safeImageUrl(url)
    ? `<img src="${escapeHtml(safeImageUrl(url))}" alt="" loading="lazy" decoding="async">`
    : '<span class="no-cover">no cover</span>';
}

//: One tab per match: a small cover, its place and title, and either its badge
//: or how far its length is off.
function matchTabsHtml(q) {
  const chosen = selectedCandidate(q);
  return (q.candidates || []).map((c, index) => {
    const badge = recommendationBadge(q, c);
    const gap = lengthGap(c);
    // `is-` tones: a bare `.accent` is the page's pink button style.
    const note = badge ? `<span class="tab-note is-${badge.tone}">${badge.text}</span>`
      : gap ? `<span class="tab-note is-warn">${escapeHtml(gap)}</span>` : "";
    const on = chosen && c.sceneId === chosen.sceneId;
    return `<button type="button" class="match-tab${on ? " is-on" : ""}"
        data-tab-video="${escapeHtml(q.videoId)}"
        data-tab-scene="${escapeHtml(c.sceneId)}" aria-pressed="${on ? "true" : "false"}"
        title="${escapeHtml(c.title || "")}">
      <span class="tab-thumb">${coverHtml(c.cover)}</span>
      <span class="tab-text"><span class="tab-title">${countText(index + 1)}
        &middot; ${escapeHtml(c.title || "(untitled)")}</span> ${note}</span>
    </button>`;
  }).join("");
}

function frameUrl(videoId, n) {
  return `/api/library/frame?video=${encodeURIComponent(videoId)}&n=${n}`;
}

//: What a still that failed says until the server has said why. Writing goes to
//: Pronoisseur's own catalog and never opens the file, so a drive that is
//: offline costs the pictures and nothing else - and the line says so, because
//: a page of failed pictures reads like a page that cannot write.
const NO_FRAME_TEXT = "No frame &mdash; this file can't be read right now. Writing "
  + "to Pronoisseur doesn't need the file.";

//: The file's own frame at the cover's size. ‹ › step through the four, and
//: moving across the picture scrubs them. A file the server cannot read shows a
//: line instead of a broken image. Drawn with no `src`: the address waits in
//: `data-frame-src` until the page's own frame queue (`queueFrame`) loads it.
function frameViewerHtml(q) {
  const id = escapeHtml(q.videoId);
  const n = library.reviewFrame[q.videoId] || 0;
  return `<div class="pic frame-pic">
      <img alt="" decoding="async" data-frame-img="${id}"
           data-frame-src="${escapeHtml(frameUrl(q.videoId, n))}">
      <span class="no-frame">${NO_FRAME_TEXT}</span>
    </div>
    <div class="frame-step">
      <button type="button" class="ghost small" data-frame-step="-1"
              data-frame-for="${id}" aria-label="previous frame">&lsaquo;</button>
      <span data-frame-pos="${id}">${n + 1}/${FRAME_COUNT}</span>
      <button type="button" class="ghost small" data-frame-step="1"
              data-frame-for="${id}" aria-label="next frame">&rsaquo;</button>
    </div>`;
}

//: Length first: it is where a wrong match usually gives itself away.
function lengthRowHtml(q, c) {
  const mine = q.durationSecs ? formatDuration(Math.round(q.durationSecs)) : "unknown";
  const theirs = c.duration ? formatDuration(Math.round(c.duration)) : "unknown";
  const gap = lengthGap(c);
  const verdict = c.durationAgrees === true ? `<span class="good">matches</span>`
    : gap ? `<span class="warn">${escapeHtml(gap)}</span>` : "";
  return `<div class="compare-row" data-field="length">
    <span class="field-label">Length</span>
    <span class="compare-now">${escapeHtml(mine)}</span>
    <span class="compare-next">${escapeHtml(theirs)}${verdict
      ? ` &middot; ${verdict}` : ""}</span>
  </div>`;
}

//: One field: what the file has now, and what picking this match does to it,
//: with the write preview's own verbs and chips.
function compareRowHtml(f) {
  const [word, tone] = FIELD_VERBS[f.action] || [String(f.action || ""), "same"];
  const verb = isListField(f) && f.action === "add"
    ? `${escapeHtml(word)} ${countText((f.added || []).length)}` : escapeHtml(word);
  const now = isListField(f)
    ? ((f.kept || []).map((name) =>
        `<span class="fchip kept">${escapeHtml(name)}</span>`).join(" ") || "&mdash;")
    : fieldValueHtml(f.current, f.current_estimated);
  let next;
  if (isListField(f)) {
    next = listFieldHtml(f);
  } else if (f.action === "off") {
    next = `${fieldValueHtml(f.current, f.current_estimated)} ${SWITCHED_OFF}`;
  } else if (f.action === "same") {
    next = fieldValueHtml(f.current, f.current_estimated);
  } else {
    next = fieldValueHtml(f.proposed, f.proposed_estimated)
      + (f.new ? ` ${NEW_TO_LIBRARY}` : "");
  }
  return `<div class="compare-row${tone === "same" ? " is-quiet" : ""}"
      data-field="${escapeHtml(f.field)}">
    <span class="field-label">${escapeHtml(FIELD_LABELS[f.field] || f.field)}</span>
    <span class="compare-now">${now}</span>
    <span class="compare-next"><span class="fverb ${tone}">${verb}</span> ${next}</span>
  </div>`;
}

//: Your file beside one match: two pictures the same size, then field by field
//: what picking it would write, then the two answers.
function compareHtml(q, c) {
  const id = escapeHtml(q.videoId);
  const badge = recommendationBadge(q, c);
  const facts = [
    badge ? `<span class="pill ${badge.tone === "accent" ? "rec" : badge.tone}"
      title="${escapeHtml(badge.title)}">${badge.text}</span>` : "",
    c.kind === "oshash" ? `<span class="pill good">exact file hash</span>` : "",
    (c.kind === "phash" && c.distance != null)
      ? `<span class="pill" title="perceptual hash distance - fewer is closer"
           >${escapeHtml(c.distance)} bits apart</span>` : "",
  ].filter(Boolean).join(" ");
  const counts = sceneCounts(c);
  return `
    <div class="compare-pics">
      <figure class="compare-pic">
        <figcaption class="lib-label">Your file</figcaption>
        ${frameViewerHtml(q)}
      </figure>
      <figure class="compare-pic">
        <figcaption class="lib-label">StashDB &middot;
          ${escapeHtml(c.title || "(untitled)")}</figcaption>
        <div class="pic">${coverHtml(c.cover)}${c.duration
          ? `<span class="duration">${formatDuration(Math.round(c.duration))}</span>`
          : ""}</div>
        <div class="scene-facts">
          <span class="muted">${escapeHtml(c.date || "")}${c.studio
            ? ` &middot; ${escapeHtml(c.studio)}` : ""}${counts
            ? ` &middot; ${counts}` : ""}</span>
          ${c.sceneId ? `<a class="scene-link" data-scene-link="${
              escapeHtml(c.sceneId)}" target="_blank" rel="noopener"
              href="${STASHDB_SCENE_URL}${escapeHtml(
                encodeURIComponent(c.sceneId))}">on StashDB &#8599;</a>` : ""}
        </div>
      </figure>
    </div>
    <div class="compare-rows">
      <div class="compare-row compare-head">
        <span></span><span class="lib-label">Your file now</span>
        <span class="lib-label">If you use this match</span>
      </div>
      ${lengthRowHtml(q, c)}
      ${(c.fields || []).map(compareRowHtml).join("")}
    </div>
    <div class="toolbar compare-actions">
      ${facts ? `<span class="cand-badges">${facts}</span>` : ""}
      <span class="spacer"></span>
      <button type="button" class="ghost small" data-reject="${id}">None of these match</button>
      ${c.sceneId ? `<button type="button" class="primary small"
          data-pick-scene="${escapeHtml(c.sceneId)}" data-pick-video="${id}"
          data-pick-title="${escapeHtml(c.title || "(untitled)")}"
          >Use this match</button>` : ""}
    </div>
    <div class="field-foot">Using a match doesn't tag it yet &mdash; it moves
      to Matched, ticked, for Tag to write. Nothing on this video is removed.
      &ldquo;None of these match&rdquo; is remembered against the file, so the
      next run won't ask again &mdash; search by name stays open to you.</div>`;
}

// The block reads top to bottom as one sentence: this is your file, and this
// is the scene on StashDB beside it. A library-relative path over a scene
// cover would read as one more match, so the path is absolute (`relPath` only
// as a fallback), and both pictures are labelled - "Your file" once, on its
// picture, facing "StashDB - <title>" on the match's.
//
// `g` is the model group it is drawn in. The assisted search's boxes are keyed
// by group and video together, because a video under two models is drawn twice
// when both groups are open, and a search must read the boxes it was typed in.
//: The other groups a video is also drawn in, for its "also under" line.
function alsoUnderHtml(q, group) {
  const others = rowModels(q).filter((m) => modelGroupId(m.key) !== group.id)
    .map((m) => m.name);
  return others.length
    ? ` <span class="also">also under ${escapeHtml(others.join(", "))}</span>` : "";
}

//: How a matched video was matched, in words, then whether its length agrees:
//: "by its exact file hash · the length matches". It stands where the status
//: and hash pills stood, which named the machinery rather than what it found.
//: An answer you gave is "by you", whatever status it carries.
const MATCHED_BY = new Map([
  ["verified", "by its exact file hash"],
  ["verified-by-duration", "by the StashDB id in its folder name"],
  ["likely", "by how it looks (perceptual hash), not proven"],
  ["unverified", "by the StashDB id in its folder name, not confirmed"],
]);

//: The two kinds tagged only once included: their line is amber.
const MATCHED_UNPROVEN = new Set(["likely", "unverified"]);

function matchedHow(q) {
  const by = q.confidence === "manual" ? "by you" : (MATCHED_BY.get(q.status) || "");
  const delta = q.durationSecs && q.sceneDuration
    ? Math.round((q.durationSecs - q.sceneDuration) * 10) / 10 : null;
  const gap = lengthGap({ durationDelta: delta, durationAgrees: q.durationAgrees });
  const length = q.durationAgrees === true ? "the length matches"
    : q.durationAgrees === false
      ? (gap ? `the length is ${gap}` : "the lengths differ")
      : "length unknown";
  return [by, length].filter(Boolean).map(escapeHtml).join(" &middot; ");
}

//: A matched video, ticked to tag: its tick, how it was matched, your file
//: beside the scene it was matched to, and field by field what tagging does.
function readyBlock(q, g) {
  const id = escapeHtml(q.videoId);
  const group = g || { id: NO_MODEL };
  const summary = fieldSummary(q.fields || []);
  const scene = {
    duration: q.sceneDuration, durationAgrees: q.durationAgrees,
    durationDelta: q.durationSecs && q.sceneDuration
      ? Math.round((q.durationSecs - q.sceneDuration) * 10) / 10 : null,
  };
  return `
    <article class="question is-ready" data-question="${id}">
      <div class="question-head">
        <div class="card-top">
          <label class="tick"><input type="checkbox" data-pick="${id}"
            ${library.unpicked.has(q.videoId) ? "" : "checked"}
            aria-label="Tag this video"></label>
          <span class="kind-badge ready">Matched</span>
          <span class="how-matched${MATCHED_UNPROVEN.has(q.status)
                  ? " is-warn" : ""}"
                title="${escapeHtml(q.note || "")}">${matchedHow(q)}</span>
          ${q.sceneDateEstimated ? `<span class="pill warn"
            title="StashDB dates this scene only to the year or the month - the
                   day is filled in, and the video is flagged estimated"
            >${escapeHtml(q.sceneDate || "")} (estimated)</span>` : ""}
        </div>
        <div><code>${escapeHtml(q.path || q.relPath || "(no path)")}</code>${
          alsoUnderHtml(q, group)}</div>
        ${summary ? `<div class="field-summary">${summary}</div>` : ""}
      </div>
      <div class="compare">
        <div class="compare-pics">
          <figure class="compare-pic">
            <figcaption class="lib-label">Your file</figcaption>
            ${frameViewerHtml(q)}
          </figure>
          <figure class="compare-pic">
            <figcaption class="lib-label">StashDB &middot;
              ${escapeHtml(q.sceneTitle || "(untitled)")}</figcaption>
            <div class="pic">${coverHtml(q.cover)}${q.sceneDuration
              ? `<span class="duration">${formatDuration(Math.round(q.sceneDuration))}</span>`
              : ""}</div>
            <div class="scene-facts">
              <span class="muted">${escapeHtml(q.sceneDate || "")}</span>
              ${q.sceneId ? `<a class="scene-link" data-scene-link="${
                  escapeHtml(q.sceneId)}" target="_blank" rel="noopener"
                  href="${STASHDB_SCENE_URL}${escapeHtml(
                    encodeURIComponent(q.sceneId))}">on StashDB &#8599;</a>` : ""}
            </div>
          </figure>
        </div>
        <div class="compare-rows">
          <div class="compare-row compare-head">
            <span></span><span class="lib-label">Your file now</span>
            <span class="lib-label">After tagging</span>
          </div>
          ${lengthRowHtml(q, scene)}
          ${(q.fields || []).map(compareRowHtml).join("")}
        </div>
        <div class="field-foot">Nothing on this video is removed.</div>
      </div>
    </article>`;
}

function questionBlock(q, g) {
  const id = escapeHtml(q.videoId);
  const group = g || { id: NO_MODEL };
  if (q.ready) return readyBlock(q, group);
  const cands = q.candidates || [];
  return `
    <article class="question" data-question="${id}">
      <div class="question-head">
        <div class="card-top">${q.pending
          // The reason is the server's note, said once, as the how-line.
          ? `<span class="kind-badge wait">Pending hashing</span>
             <span class="how-matched">${escapeHtml(q.note || "")}</span>`
          : cands.length
          ? `<span class="kind-badge pick">Choose a match</span>
             <span class="how-matched">${cands.length === 1
               ? "1 StashDB scene is close, but not close enough to tag unseen"
               : `${countText(cands.length)} StashDB scenes share this file's
                  fingerprint`}</span>`
          : `<span class="kind-badge none">No match</span>
             <span class="how-matched">StashDB has no scene with this file's
               fingerprint</span>`}</div>
        <div><code>${escapeHtml(q.path || q.relPath || "(no path)")}</code>${
          alsoUnderHtml(q, group)}</div>
        <div class="muted small">${escapeHtml(q.title || "(no title)")}
          ${q.durationSecs
            ? `&middot; ${formatDuration(Math.round(q.durationSecs))}` : ""}
          ${q.note && !q.pending ? `&middot; ${escapeHtml(q.note)}` : ""}</div>
        ${q.whisparrSceneId ? `<div><span class="pill warn"
          title="the id the folder name claims - fix it in Whisparr"
          >Whisparr says ${escapeHtml(q.whisparrSceneId)}</span></div>` : ""}
      </div>
      ${cands.length ? `
        <div class="lib-label">Which is your file? Use this match on the right
          one, or None of these match.</div>
        ${q.noDurationMatch ? `<div class="hint warn">None of these runs the
          same length as your file &mdash; check the pictures.</div>` : ""}
        <div class="match-tabs" data-tabs="${id}">${matchTabsHtml(q)}</div>
        <div class="compare" data-compare="${id}">${compareHtml(q,
          selectedCandidate(q))}</div>` : `
        <div class="compare-pics is-solo">
          <figure class="compare-pic">
            <figcaption class="lib-label">Your file</figcaption>
            ${frameViewerHtml(q)}
          </figure>
          <div class="compare-none muted">Search for it by name below.</div>
        </div>`}
      ${assistedPanel(q, `${group.id}~${q.videoId}`)}
    </article>`;
}

//: Switch the match on show for one video, in every group it is drawn in. Only
//: the tabs and the comparison are redrawn: the assisted search below keeps
//: whatever was typed into it.
function chooseMatch(videoId, sceneId) {
  library.reviewTab[videoId] = sceneId;
  const q = library.reviewIndex.get(videoId);
  if (!q) return;
  $$(`[data-question="${videoId}"]`).forEach((article) => {
    const tabs = $(".match-tabs", article);
    const compare = $(".compare", article);
    if (tabs) tabs.innerHTML = matchTabsHtml(q);
    if (compare) compare.innerHTML = compareHtml(q, selectedCandidate(q));
    wireQuestionBlocks(article);
  });
}

//: At most this many of the file's stills load at once. A still the server has
//: not kept yet is an ffmpeg run over the share, and the browser has only about
//: six connections to this server in all: left to `loading="lazy"`, a page of
//: cards - or one sweep of the mouse across a picture - could hold every one of
//: them, and the poll and the Pick button would wait behind the pictures.
const FRAME_LOADS = 2;

//: A load gives its slot back after this long whatever happens, so one that
//: never reports - its image dropped mid-load - cannot shrink the queue for
//: good. Longer than the server's own ffmpeg limit.
const FRAME_LOAD_GIVE_UP_MS = 75000;

const frameLoads = { active: 0, waiting: [] };

//: Ask for `url` in `img`. `urgent` - a click or a scrub - goes to the front.
//: An image already waiting just has its address replaced, so scrubbing past
//: four frames asks for the last one, not all four.
function queueFrame(img, url, urgent) {
  img.dataset.want = url;
  frameLoads.waiting = frameLoads.waiting.filter((other) => other !== img
                                                  && other.isConnected !== false);
  if (urgent) frameLoads.waiting.unshift(img); else frameLoads.waiting.push(img);
  pumpFrames();
}

//: An image mid-load is passed over, never re-pointed: changing its `src` would
//: drop the load without either event firing, and its slot with it. It is
//: taken up again the moment its own load reports.
function pumpFrames() {
  while (frameLoads.active < FRAME_LOADS) {
    const index = frameLoads.waiting.findIndex((img) => !img.frameBusy);
    if (index < 0) return;
    const [img] = frameLoads.waiting.splice(index, 1);
    const url = img.dataset.want;
    delete img.dataset.want;
    // Gone from the page, or already showing this one: nothing to load.
    if (!url || img.isConnected === false || img.getAttribute("src") === url) continue;
    frameLoads.active += 1;
    img.frameBusy = true;
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(guard);
      img.frameBusy = false;
      frameLoads.active -= 1;
      pumpFrames();
    };
    const guard = setTimeout(finish, FRAME_LOAD_GIVE_UP_MS);
    img.onload = () => {
      if (img.parentNode) img.parentNode.classList.remove("is-missing");
      finish();
    };
    img.onerror = () => {
      if (img.parentNode) img.parentNode.classList.add("is-missing");
      finish();
      explainFrame(img, url);
    };
    img.src = url;
  }
}

//: Why a still failed - "Z: is not reachable", "the file is not at ..." -
//: asked once of the server, because an <img> cannot read the answer it was
//: refused with. Costs nothing new: the server remembers a failure for a
//: minute, so this runs no ffmpeg and takes no second look at the drive.
function explainFrame(img, url) {
  const parent = img.parentNode;
  const why = parent && parent.querySelector ? $(".no-frame", parent) : null;
  if (!why) return;
  fetch(url).then((res) => (res.ok ? null : res.json())).then((payload) => {
    const message = payload && payload.error && payload.error.message;
    if (message) {
      why.textContent = `No frame — ${message}. Writing to Pronoisseur `
        + "doesn't need the file.";
    }
  }).catch(() => { /* the line already says the file can't be read */ });
}

//: Stills are asked for as their card comes near the screen, not all at once:
//: a page of fifty cards is fifty reads of the share. Made on first use, and
//: never where the browser has no IntersectionObserver - there every still is
//: simply queued.
let frameWatcher = null;
function watchFrame(img) {
  if (frameWatcher === null && typeof IntersectionObserver === "function") {
    frameWatcher = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        frameWatcher.unobserve(entry.target);
        queueFrame(entry.target, entry.target.dataset.frameSrc, false);
      });
    }, { rootMargin: "400px" });
  }
  if (frameWatcher) frameWatcher.observe(img);
  else queueFrame(img, img.dataset.frameSrc, false);
}

//: Show frame `n` of one video's file, wherever it is drawn. Wraps round.
function showFrame(videoId, n) {
  const next = ((n % FRAME_COUNT) + FRAME_COUNT) % FRAME_COUNT;
  library.reviewFrame[videoId] = next;
  $$(`[data-frame-img="${videoId}"]`).forEach((img) => {
    if (frameWatcher) frameWatcher.unobserve(img);
    img.dataset.frameSrc = frameUrl(videoId, next);
    queueFrame(img, img.dataset.frameSrc, true);
  });
  $$(`[data-frame-pos="${videoId}"]`).forEach((pos) => {
    pos.textContent = `${next + 1}/${FRAME_COUNT}`;
  });
}

function wireFrames(root) {
  $$("[data-frame-step]", root).forEach((btn) => {
    btn.onclick = () => {
      const videoId = Number(btn.dataset.frameFor);
      showFrame(videoId, (library.reviewFrame[videoId] || 0)
                         + Number(btn.dataset.frameStep));
    };
  });
  $$("[data-frame-img]", root).forEach((img) => {
    const videoId = Number(img.dataset.frameImg);
    if (!img.getAttribute("src")) watchFrame(img);
    img.onmousemove = (event) => {
      const width = img.clientWidth || 1;
      const n = Math.min(FRAME_COUNT - 1,
                         Math.max(0, Math.floor(event.offsetX / width * FRAME_COUNT)));
      if (n !== (library.reviewFrame[videoId] || 0)) showFrame(videoId, n);
    };
  });
}

//: `12 tags · 2 models`, or "" when the lookup fetched neither array. A count of
//: zero is left in: a scene that genuinely lists no tags is the thin duplicate
//: this line exists to help you avoid, and only an *absent* array — a search
//: result, whose narrower query never asked — says nothing and shows nothing.
function sceneCounts(c) {
  const parts = [];
  if (c.tagCount != null) {
    parts.push(`${escapeHtml(c.tagCount)} tag${c.tagCount === 1 ? "" : "s"}`);
  }
  if (c.performerCount != null) {
    parts.push(`${escapeHtml(c.performerCount)} model${
      c.performerCount === 1 ? "" : "s"}`);
  }
  return parts.join(" &middot; ");
}

// One card for both grids: the fingerprint candidates and the assisted-search
// results come from the same builder on the server, and the search adds exactly
// one key (`dateMatch`) that the fingerprint cards never carry. `recommended`
// runs the other way — only a question card carries it, because only the
// questions grid is ranked.
function candidateCard(c, videoId) {
  const counts = sceneCounts(c);
  const badges = [
    // Two wordings, because the reason matters: the badge says "most tags" only
    // when the tag count is genuinely what put this card in front of an equally
    // strong one. Claiming it when the bytes decided would teach the user to
    // distrust the badge the first time they checked.
    c.recommended ? `<span class="pill accent"
        title="${c.recommendedByTags
          ? "the same fingerprint and length as the card below it, and StashDB "
            + "lists more tags for this one"
          : "the strongest fingerprint here"}">Recommended${
        c.recommendedByTags ? " &middot; most tags" : ""}</span>` : "",
    c.kind === "oshash" ? `<span class="pill good">exact file hash</span>` : "",
    (c.kind === "phash" && c.distance != null)
      ? `<span class="pill" title="perceptual hash distance - fewer is closer"
           >${escapeHtml(c.distance)} bits apart</span>` : "",
    c.durationDelta == null ? ""
      : `<span class="pill ${c.durationAgrees === false ? "warn" : ""}"
              title="the file's length minus this scene's">
           ${c.durationDelta > 0 ? "+" : ""}${escapeHtml(c.durationDelta)}s
           ${c.durationAgrees === false ? "&mdash; durations disagree" : ""}</span>`,
    c.dateMatch ? `<span class="pill good">date matches</span>` : "",
  ].filter(Boolean).join("");
  return `
    <article class="scene-card${c.recommended ? " is-recommended" : ""}"
             data-pick-scene="${escapeHtml(c.sceneId)}"
             data-pick-video="${escapeHtml(videoId)}"
             title="${escapeHtml(c.title || "")}">
      <div class="cover">
        ${safeImageUrl(c.cover)
          ? `<img src="${escapeHtml(safeImageUrl(c.cover))}" alt="" loading="lazy" decoding="async">`
          : '<span class="no-cover">no cover</span>'}
        ${c.duration ? `<span class="duration">${formatDuration(c.duration)}</span>` : ""}
      </div>
      <div class="scene-body">
        <div class="scene-title">${escapeHtml(c.title || "(untitled)")}</div>
        <div class="scene-meta">
          <span>${escapeHtml(c.date || "-")}</span>
          <span class="studio">${escapeHtml(c.studio || "")}</span>
        </div>
        ${counts || c.sceneId ? `<div class="scene-facts">
          <span class="muted">${counts}</span>
          ${c.sceneId ? `<a class="scene-link" data-scene-link="${
              escapeHtml(c.sceneId)}" target="_blank" rel="noopener"
              href="${STASHDB_SCENE_URL}${escapeHtml(
                encodeURIComponent(c.sceneId))}">on StashDB &#8599;</a>` : ""}
        </div>` : ""}
        ${badges ? `<div class="cand-badges">${badges}</div>` : ""}
      </div>
    </article>`;
}

//: `slot` names this one drawing of the panel (`group~video`); it defaults to
//: the video id, for a panel drawn once.
function assistedPanel(q, slot) {
  const id = escapeHtml(q.videoId);
  const key = escapeHtml(slot == null ? q.videoId : slot);
  // A question card from the identify run carries the terms the search would
  // use, so the panel can show them and pre-fill the boxes before the first
  // search. An `unmatched` row synthesised from a scan bucket carries none -
  // bucket rows are the write planner's, not the matcher's - so it keeps the
  // placeholder rather than being told its studio and date are "none".
  const terms = q.terms || null;
  return `
    <details class="assisted" data-assisted="${key}">
      <summary>Search by name &mdash; look this file up on StashDB</summary>
      <div class="toolbar">
        <label>Title
          <input type="text" size="32" data-search-title="${key}"
                 value="${escapeHtml(q.title || "")}"
                 placeholder="blank searches on the name parsed from the path">
        </label>
        <label>Performers
          <input type="text" size="24" data-search-performers="${key}"
                 value="${escapeHtml(((terms || {}).performers || []).join(", "))}"
                 placeholder="comma-separated; blank uses the file's own">
        </label>
        <button class="primary small" data-search="${key}"
                data-search-video="${id}">Search</button>
      </div>
      <div class="hint pad" data-search-terms="${key}">${terms
        ? assistedTermsHtml(q)
        : `Search to see the studio and date parsed from this path, and the date
           window results are ranked by.`}</div>
      <div data-search-results="${key}"></div>
    </details>`;
}

function wireQuestionBlocks(root) {
  // A search result's cover is the control, and its title is on the card; a
  // match's "Use this match" button carries its title instead.
  $$("[data-pick-scene]", root).forEach((el) => {
    el.onclick = () => {
      const heading = $(".scene-title", el);
      pickScene(Number(el.dataset.pickVideo), el.dataset.pickScene,
                (el.dataset.pickTitle || (heading ? heading.textContent : "")).trim());
    };
  });
  $$("[data-reject]", root).forEach((btn) => {
    btn.onclick = () => rejectRow(Number(btn.dataset.reject));
  });
  $$("[data-search]", root).forEach((btn) => {
    btn.onclick = () => runAssistedSearch(
      Number(btn.dataset.searchVideo || btn.dataset.search), btn.dataset.search);
  });
  $$("[data-tab-scene]", root).forEach((btn) => {
    btn.onclick = () => chooseMatch(Number(btn.dataset.tabVideo), btn.dataset.tabScene);
  });
  // A ready video's own tick. Set in place, everywhere the video is drawn.
  $$("[data-pick]", root).forEach((input) => {
    input.onchange = () => setTicked([Number(input.dataset.pick)], input.checked);
  });
  wireFrames(root);
  $$("[data-scene-link]", root).forEach((link) => {
    // The whole card is a click target that offers to record the scene, and this
    // link sits inside it. Without stopping the bubble, going off to StashDB to
    // check a scene would also ask to record it - which is the opposite of what
    // the link is for.
    link.onclick = (event) => event.stopPropagation();
  });
}

async function pickScene(videoId, sceneId, title) {
  const ok = await modal({
    title: "Use this match?",
    html: `<p>This matches the file to <b>${escapeHtml(title)}</b>. It moves
      to <b>Matched</b>, ticked, ready to tag &mdash; the same standing a
      matched fingerprint gives it.</p>
      <p><b>Nothing is tagged yet.</b> Tag writes it, once you have looked.</p>
      <p>The answer is remembered against the file's content hash, so a later
        identify run won't ask about it again. Choosing a different scene
        later overwrites it.</p>`,
    okLabel: "Use this match",
  });
  if (!ok) return;
  decideRow(videoId, sceneId, "pick");
}

async function rejectRow(videoId) {
  const ok = await modal({
    title: "None of these match?",
    html: `<p>The answer is remembered against the file's content hash, so the
      next identify run won't ask about this file again.</p>
      <p>The video moves to <b>No match</b>, where you can still search for
        it by name.</p>`,
    okLabel: "None of these match",
  });
  if (!ok) return;
  decideRow(videoId, null, "reject");
}

async function decideRow(videoId, sceneId, action) {
  try {
    await api("/api/library/match/decide",
              { method: "POST", body: { videoId, sceneId, action } });
    loadLibrary();
  } catch (err) { reportError(err); }
}

// The placeholder the Title box carries once a search has run. Before then an
// empty box means "you say" and the server parses the path; after, this panel
// sends the empty string and the server searches with no title at all, so the
// two states cannot share one line of placeholder text without one of them
// being false.
const SEARCHED_TITLE_HINT = "empty searches with no title at all";

async function runAssistedSearch(videoId, slot) {
  // Every element is looked up through this rather than held, because
  // `renderLibraryQuestions` rebuilds `#library-questions` wholesale: a poll
  // landing while the request is in flight detaches anything captured before
  // the await, and `null.innerHTML` would throw inside the click handler - and
  // again inside its catch. Polls run during a scan, an apply and an identify,
  // and nothing refuses an assisted search during one, so this is reachable.
  // By `slot`, not the video id alone: the same video drawn in two open model
  // groups has two panels, and only one of them was typed into.
  const key = slot == null ? String(videoId) : String(slot);
  const find = (attr) => $(`[${attr}="${key}"]`);
  const panel = find("data-assisted");
  const titleInput = find("data-search-title");
  const namesInput = find("data-search-performers");
  // The results slot is guarded here too, because `busy()` writes to it below
  // and would throw the same way.
  if (!panel || !titleInput || !namesInput || !find("data-search-results")) return;
  const title = titleInput.value.trim();
  const names = namesInput.value.split(",").map((n) => n.trim()).filter(Boolean);
  // Before the first search the boxes hold what the row arrived with, and an
  // empty one means "nothing to say" - the server parses the path itself. After
  // a search they hold the terms the server actually used, so an empty one is
  // the user deliberately clearing it, and that has to be sent to take effect.
  const searched = panel.dataset.searched === "1";
  const body = { videoId };
  if (searched || title) body.title = title;
  if (searched || names.length) body.performers = names;

  busy(`[data-search-results="${key}"]`, "Searching StashDB...");
  let data;
  try {
    data = await api("/api/library/match/search", { method: "POST", body });
  } catch (err) {
    const slot = find("data-search-results");
    if (slot) slot.innerHTML = "";
    reportError(err);
    return;
  }
  const results = find("data-search-results");
  if (!results) return;            // rebuilt under us: the answer has nowhere to go
  const panelNow = find("data-assisted");
  const titleNow = find("data-search-title");
  const namesNow = find("data-search-performers");
  const termsNow = find("data-search-terms");
  const terms = data.terms || {};
  if (panelNow) panelNow.dataset.searched = "1";
  if (titleNow) {
    titleNow.value = terms.title || "";
    titleNow.placeholder = SEARCHED_TITLE_HINT;
  }
  if (namesNow) namesNow.value = (terms.performers || []).join(", ");
  if (termsNow) termsNow.innerHTML = assistedTermsHtml(data);
  results.innerHTML = assistedResultsHtml(data, videoId);
  wireQuestionBlocks(results);
}

function assistedTermsHtml(data) {
  const terms = data.terms || {};
  const window_ = data.dateWindow || [];
  const unresolved = data.unresolved || [];
  return `Parsed from the path: studio
    <b>${escapeHtml(terms.studio || "none")}</b> &middot; date
    <b>${escapeHtml(terms.date || "none")}</b>. ${window_.length
      ? `Results dated within a day of ${escapeHtml(terms.date)}
         (${escapeHtml(window_.join(", "))}) are shown first.`
      : `No date to rank by, so these are in StashDB's own order.`}
    ${unresolved.length ? `<br><span class="cache-flag">StashDB knows no
      performer called ${escapeHtml(unresolved.join(", "))} &mdash; the search
      ran without ${unresolved.length === 1 ? "that name" : "those names"}.
      </span>` : ""}`;
}

function assistedResultsHtml(data, videoId) {
  const cards = data.candidates || [];
  if (!cards.length) {
    return `<div class="empty">Nothing on StashDB matched those terms.</div>`;
  }
  return `
    <div class="hint pad"><b>${countText(cards.length)}</b> of
      ${countText(data.count)} results, page ${escapeHtml(data.page)}. Nothing is
      picked for you &mdash; click a cover to record it.</div>
    <div class="scene-grid">${cards
      .map((c) => candidateCard(c, videoId)).join("")}</div>`;
}

async function renderLibraryMapping() {
  const s = library.state;
  const box = $("#library-mapping");
  if (!s.connected) { box.innerHTML = ""; return; }
  const data = await api("/api/library/mapping");
  const chosen = new Set(data.allowlist || []);
  const categories = (data.categories || []).map((c) => `
    <label class="inline"><input type="checkbox" data-allow="${escapeHtml(c.id)}"
      ${chosen.has(c.id) ? "checked" : ""}> ${escapeHtml(c.name)}</label>`).join("");
  const rows = (data.tags || []).map((t) => `
    <tr>
      <td>${escapeHtml(t.name)}</td>
      <td class="muted">${escapeHtml(t.category || "")}</td>
      <td>${countText(t.videos)}</td>
      <td>${t.mode === "ignore" ? '<span class="muted">ignored</span>'
                                : escapeHtml(t.resolves_to || "")}
          <span class="muted small">${escapeHtml(t.source)}</span></td>
      <td><input type="text" class="map-input" data-tag="${escapeHtml(t.tag_id)}"
                 value="${escapeHtml(t.override || "")}" placeholder="override"></td>
      <td><label class="inline"><input type="checkbox" data-ignore="${escapeHtml(t.tag_id)}"
            ${t.mode === "ignore" ? "checked" : ""}> ignore</label></td>
    </tr>`).join("");
  box.innerHTML = `
    <details class="fold" ${chosen.size ? "" : "open"}>
      <summary>Tag categories to import (${chosen.size} of ${(data.categories || []).length})</summary>
      <p class="hint">Nothing is selected to begin with, on purpose: StashDB has thousands of
        tags and Pronoisseur's categories are shared with pictures. Pick a category and
        the table shows exactly which tags it would bring in.</p>
      <p><button type="button" class="ghost small"
          id="lib-refresh-categories">Refresh categories</button></p>
      <div class="cat-list">${categories}</div>
      ${rows ? `<table class="map-table">
        <thead><tr><th>StashDB tag</th><th>Category</th><th>Videos</th>
          <th>Becomes</th><th>Override</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`
        : `<p class="hint">No allowlisted tags in this scan.</p>`}
    </details>`;
  $("#lib-refresh-categories").onclick = async () => {
    try {
      await api("/api/library/categories/refresh", { method: "POST", body: {} });
      loadLibrary();
    } catch (err) { reportError(err); }
  };
  $$("[data-allow]", box).forEach((input) => {
    input.onchange = async () => {
      const allowlist = $$("[data-allow]", box).filter((i) => i.checked)
        .map((i) => i.dataset.allow);
      try {
        await api("/api/library/settings", { method: "POST",
                                             body: { settings: { allowlist } } });
        loadLibrary();
      } catch (err) { reportError(err); }
    };
  });
  $$(".map-input", box).forEach((input) => {
    input.onchange = () => saveTagMapping(input.dataset.tag, input.value,
                                          input.value.trim() ? "map" : "clear");
  });
  $$("[data-ignore]", box).forEach((input) => {
    input.onchange = () => saveTagMapping(input.dataset.ignore, null,
                                          input.checked ? "ignore" : "clear");
  });
}

async function saveTagMapping(tagId, categoryName, mode) {
  try {
    await api("/api/library/mapping", { method: "POST",
                                        body: { tagId, categoryName, mode } });
    loadLibrary();
  } catch (err) { reportError(err); }
}

// What the write does to each field of one video, said for every field.
//
// "old -> new" on every row read as "replace" even where the tool only ever
// adds, so a category list beside an arrow looked like the categories already on
// the video were being swapped out. They never are. Each row now carries a verb
// for what happens to that field, shows what is there now whether or not it
// changes, and every video ends by saying nothing is removed - which is always
// true: models and categories are add-only, and the tool has no remove path.

//: Field labels as a person reads them. The server sends the fields in the
//: order they are shown.
const FIELD_LABELS = {
  title: "Title", release_date: "Release date", studio: "Studio",
  models: "Models", categories: "Categories", external_ids: "StashDB link",
};

//: Each action's word, and the tone its badge wears: amber for anything that
//: overwrites what is there, blue for filling a blank, green for adding, grey
//: for leaving it alone. There is no "remove" here because there is none to show.
const FIELD_VERBS = {
  rename: ["Rename", "overwrite"], change: ["Change", "overwrite"],
  replace: ["Replace", "overwrite"], set: ["Set", "fill"], add: ["Add", "add"],
  same: ["No change", "same"], off: ["Off", "same"],
};

//: The summary line's phrase per field and action. Models and categories are
//: counted, so they are worded in `fieldSummary` itself.
const FIELD_SUMMARY = {
  title: { rename: "renames title", set: "sets title" },
  release_date: { change: "changes date", set: "sets date" },
  studio: { replace: "replaces studio", set: "sets studio" },
  external_ids: { add: "links to StashDB", replace: "re-links to StashDB" },
};

const isListField = (f) => f.field === "models" || f.field === "categories";

//: "Renames title · sets studio · adds 2 models". Empty when nothing changes.
function fieldSummary(fields) {
  const parts = fields.map((f) => {
    if (isListField(f) && f.action === "add") {
      const n = (f.added || []).length;
      return f.field === "models"
        ? `adds ${countText(n)} model${n === 1 ? "" : "s"}`
        : `adds ${countText(n)} categor${n === 1 ? "y" : "ies"}`;
    }
    return (FIELD_SUMMARY[f.field] || {})[f.action] || "";
  }).filter(Boolean);
  if (!parts.length) return "";
  const line = parts.join(" &middot; ");
  return line.charAt(0).toUpperCase() + line.slice(1);
}

function fieldValueHtml(value, estimated) {
  const text = escapeHtml(formatDiffValue(value));
  return estimated && value ? `${text} (estimated)` : text;
}

const NEW_TO_LIBRARY = `<span class="fnew">new to your library</span>`;
const SWITCHED_OFF = `<span class="fnote">(turned off in settings)</span>`;

//: Models and categories: what is on the video already as grey chips, what the
//: write adds as green "+" chips. Never a removal - there is none.
function listFieldHtml(f) {
  const kept = (f.kept || []).map((name) =>
    `<span class="fchip kept">${escapeHtml(name)}</span>`);
  const added = (f.added || []).map((item) =>
    `<span class="fchip added">+ ${escapeHtml(item.name)}${item.new
      ? ` ${NEW_TO_LIBRARY}` : ""}</span>`);
  const chips = kept.concat(added).join(" ") || "&mdash;";
  return f.action === "off" ? `${chips} ${SWITCHED_OFF}` : chips;
}

function formatDiffValue(value) {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  return String(value);
}

// The two statuses a run writes only when the row is ticked - the server's
// `OPT_IN_STATUSES`. Each gets its own checkbox and its own count, because they
// are unproven for different reasons and a single "include everything" tick
// would let one hide inside the other.
const OPT_IN_TOGGLES = [
  ["unverified", "unverified",
   "Unverified rows take their identity from the folder UUID and nothing has "
   + "confirmed it. They are shown but never written unless you include them."],
  ["likely", "likely",
   "A likely row matched one perceptual hash. A near-duplicate encode of a "
   + "different scene scores exactly the same, so these are never written "
   + "unless you include them."],
];

// Drawn twice: in the write preview's own toolbar, and in the sticky bar beside
// `Apply N`, because the tick is what makes that number the number it is. `attr`
// is what keeps the two sets of inputs apart - same markup, same setter,
// different elements - so neither copy can hold a state the other cannot see.
function libraryOptInHtml(attr) {
  const buckets = (library.state.scan || {}).buckets || {};
  return OPT_IN_TOGGLES.map(([status, label, hint]) => {
    const rows = buckets[status] || [];
    if (!rows.length) return "";
    // Checked when this group's own ids are in the set, not when the set is
    // merely non-empty: with two toggles sharing `library.include`, the second
    // would otherwise render itself ticked because the first is.
    const on = rows.some((r) => library.include.has(r.video_id));
    return `<label class="inline" title="${escapeHtml(hint)}">
      <input type="checkbox" ${attr}="${escapeHtml(status)}" ${on ? "checked" : ""}>
      Include ${countText(rows.length)} ${escapeHtml(label)}</label>`;
  }).join("");
}

function setLibraryOptIn(status, checked) {
  // Only this group's ids move, so ticking `likely` cannot silently clear an
  // `unverified` inclusion the user already made.
  (((library.state.scan || {}).buckets || {})[status] || []).forEach((r) => {
    if (checked) library.include.add(r.video_id);
    else library.include.delete(r.video_id);
  });
  // The strip's step 5, the section head's badge and the bar's `Apply N` all
  // count these rows, so they are redrawn together or they disagree.
  renderLibraryPreview().catch(reportError).then(() => renderLibrarySteps());
}

//: "120 to write - 30 models, 5 studios, 10 categories - 4 skipped". Shared
//: by the preview's own toolbar and the sticky bar, so the number beside the
//: Apply button is always the number above the diff.
function previewSummaryHtml(data) {
  // Under Choose a match, No match and Pending hashing there is nothing to tag:
  // say why, rather
  // than count matched videos the tab is not showing.
  if (!reviewKindShown("ready")) return reviewNothingToTagHtml();
  // The field diffs are only half the write. These are the entities the run
  // would create or re-stamp, straight from the planner the apply uses.
  const ent = data.entities || {};
  const inSync = (((library.state.scan || {}).buckets || {})["in-sync"] || []).length;
  const creates = [
    ent.models ? `${countText(ent.models)} model${ent.models === 1 ? "" : "s"}` : "",
    ent.studios ? `${countText(ent.studios)} studio${ent.studios === 1 ? "" : "s"}` : "",
    ent.categories
      ? `${countText(ent.categories)} categor${ent.categories === 1 ? "y" : "ies"}` : "",
  ].filter(Boolean).join(", ");
  // Ticked first, because that is what `Tag N` sends; the matched total in
  // view beside it only when some of those are unticked. Ticked videos the
  // filter hides come next, as not tagged. The entity totals are for every
  // matched row, so once fewer than all of them are going they are a ceiling
  // and say so. No matched rows at all reads as that, not as a nought beside a
  // dead button.
  const ready = (data.rows || []).length;
  const shown = shownReadyRows(data).length;
  const ticked = tickedIds(data).length;
  const held = heldBackIds(data).length;
  const outside = outsideViewIds(data).length;
  const head = !ready ? "Nothing matched to tag"
    : !shown ? "Nothing matched in this view"
    : `<b>${countText(ticked)}</b> ticked to tag${
        ticked === shown ? "" : ` of ${countText(shown)} matched`}${
        outside ? " in this view" : ""}`;
  return `${head}
    ${outside ? `&middot; ${countText(outside)} ${ticked ? "more " : ""}ticked
      outside it, not tagged` : ""}
    ${held ? `&middot; ${countText(held)} new, not in the list yet` : ""}
    ${creates ? `&middot; creates or re-stamps ${ticked < ready ? "up to " : ""}${
      creates}` : ""}
    ${ent.aliases
      ? `&middot; ${countText(ent.aliases)} alias row${ent.aliases === 1 ? "" : "s"}`
      : ""}
    ${inSync ? `&middot; ${countText(inSync)} already in sync` : ""}
    ${data.skipped ? `&middot; ${countText(data.skipped)} skipped` : ""}`;
}

//: What the write bar and the sticky bar say under a tab with nothing to tag.
function reviewNothingToTagHtml() {
  if (library.reviewKind === "pending") {
    return "Nothing here can be tagged until Pronoisseur fingerprints it, or "
      + "you find it with Search by name.";
  }
  return library.reviewKind === "unmatched"
    ? "Nothing here can be tagged until you find it with Search by name."
    : "Nothing here is tagged until you use a match &mdash; it then moves to "
      + "Matched, ticked.";
}

//: Whether the view shows a matched row: the preview's snake_case path and
//: title, read the way `reviewRowsAll` translates them for the list.
const readyShown = (row) =>
  reviewShows("ready", { relPath: row.rel_path, title: row.title });

//: Matched rows that are ticked and drawn: less the ones unticked by hand, and
//: less the ones a run settled that the list has not taken in yet
//: (`library.reviewSeen`, see `holdReviewRows`). Read off the data, never off
//: checkboxes:
//: most rows sit in groups that are shut, and a shut group draws none.
function tickedRows(preview) {
  const seen = library.reviewSeen;
  return ((preview || {}).rows || []).filter((row) =>
    !library.unpicked.has(row.video_id) && (!seen || seen.has(row.video_id)));
}

//: Matched rows the view shows, ticked or not.
function shownReadyRows(preview) {
  const seen = library.reviewSeen;
  return ((preview || {}).rows || []).filter((row) =>
    (!seen || seen.has(row.video_id)) && readyShown(row));
}

//: The video ids `Tag N` sends: the ticked rows the view shows. Nothing is
//: tagged that the list is not showing - not a row the list has not taken in
//: yet mid-run, and not one the tabs or the filename filter hide.
function tickedIds(preview) {
  return tickedRows(preview).filter((row) => readyShown(row))
    .map((row) => row.video_id);
}

//: Ticked videos the filename filter hides. Not tagged - Tag takes only what
//: the view shows - so the bar and the dialog say how many are left out.
function outsideViewIds(preview) {
  return tickedRows(preview).filter((row) => !readyShown(row))
    .map((row) => row.video_id);
}

//: Ready rows a run settled that the list has not taken in yet: ticked, but
//: not counted by Tag N until they join the list.
function heldBackIds(preview) {
  const seen = library.reviewSeen;
  if (!seen) return [];
  return ((preview || {}).rows || []).map((row) => row.video_id)
    .filter((id) => !library.unpicked.has(id) && !seen.has(id));
}

//: Fetch the write preview, then draw the write toolbar from it. The rows it
//: holds are drawn in the review list, grouped by model, not here.
//:
//: Numbered, because a poll and an opt-in tick can each have one in flight: an
//: older answer landing last would put back rows the user just switched off.
async function renderLibraryPreview() {
  const s = library.state;
  const box = $("#library-preview");
  if (!s.connected) { box.innerHTML = ""; library.previewHtml = null; return; }
  library.previewSeq = (library.previewSeq || 0) + 1;
  const seq = library.previewSeq;
  const data = await api("/api/library/preview", {
    method: "POST", body: { includeIds: Array.from(library.include) } });
  if (seq !== library.previewSeq) return;
  library.preview = data;
  renderLibraryWriteBar();
}

//: Totals, the opt-in ticks, `Write N`, and what the last run did. Drawn with no
//: fetch, so a tick can redraw it on the spot.
function renderLibraryWriteBar() {
  const s = library.state;
  const box = $("#library-preview");
  const data = library.preview;
  if (!s || !s.connected || !data) { box.innerHTML = ""; library.previewHtml = null; return; }
  const apply = s.apply;
  const ticked = tickedIds(data).length;

  // An identify run in flight does not disable the write: every row here is one
  // the run has finished with, and the rows it is still deciding are in no preview.
  // Under Choose a match, No match and Pending hashing there is nothing to tag, so neither the
  // button nor the opt-in ticks that feed it are offered there.
  const html = `
    <div class="toolbar write-bar">
      <span class="hint">${previewSummaryHtml(data)}</span>
      <span class="spacer"></span>
      ${reviewKindShown("ready") ? `${libraryOptInHtml("data-preview-include")}
      <button class="primary" id="lib-apply" data-step-action="review"
        ${(!s.canWrite || !ticked || apply.running || s.scan.running)
          ? "disabled" : ""}>${writeLabel(apply, ticked)}</button>` : ""}
    </div>
    ${s.canWrite ? "" : `<div class="empty warn">This token is read-only. Create the app
      again in Pronoisseur with <b>Update video metadata</b> ticked to tag.</div>`}
    ${apply.running ? `<div class="estimate"><span class="figure">
      ${escapeHtml(progressText(apply.label, 1))}: <b>${countText(apply.done)}</b> of
      <b>${countText(apply.total)}</b></span></div>` : ""}
    ${libraryErrorHtml("apply", `${apply.details
      ? `<pre>${escapeHtml(JSON.stringify(apply.details, null, 1))}</pre>` : ""
      }<br>Nothing in the failed batch was written.`)}
    ${apply.finished ? `<div class="empty ${apply.finished.refused ? "warn" : "good"}"
      >Tagged ${countText(apply.finished.applied)} videos,
      ${countText(apply.finished.entities)} entities,
      ${countText(apply.finished.aliases)} aliases.
      ${apply.finished.refused ? `Pronoisseur refused
        ${countText(apply.finished.refused)}
        row${apply.finished.refused === 1 ? "" : "s"} &mdash; the rest of the
        batch went through, and the reason is on each row.
        ${(apply.finished.refusals || []).slice(0, 3)
          .map((r) => `<br><code>${escapeHtml(r.videoId)}</code>
            ${escapeHtml(r.error)}`).join("")}` : ""}
      ${apply.finished.note ? `<br>${escapeHtml(apply.finished.note)}` : ""}
      </div>` : ""}
    ${data.rows.length ? "" : `<div class="empty${apply.finished ? " good" : ""}"
      >${apply.finished ? libraryNothingLeftHtml(apply)
        : "Nothing matched to tag yet. Scan and identify, choose matches below, or "
          + "include the held-back rows."}</div>`}`;

  // The page polls every two seconds while anything runs, and an identify run
  // goes on for hours with this open. A poll that would draw the same thing
  // draws nothing; the handlers wired below belong to the DOM still there.
  if (html === library.previewHtml) return;
  library.previewHtml = html;
  box.innerHTML = html;

  wireLibraryErrorDismiss(box);
  $$("[data-preview-include]", box).forEach((input) => {
    input.onchange = () =>
      setLibraryOptIn(input.dataset.previewInclude, input.checked);
  });
  const tag = $("#lib-apply");
  if (tag) tag.onclick = applyLibrarySelection;
}

//: Tick or untick videos for the write, by id, and say so everywhere it shows:
//: every drawing of each video's own tick, each model group's tick and count,
//: the write toolbar and the bar. Nothing is rebuilt. An untick has to outlive
//: every redraw, which rebuilds from the server's answer - and that answer
//: knows nothing of ticks - so it lives in `library.unpicked`.
function setTicked(ids, checked) {
  ids.forEach((id) => {
    if (checked) library.unpicked.delete(id);
    else library.unpicked.add(id);
  });
  refreshTicks();
}

function refreshTicks() {
  const box = $("#library-questions");
  $$("[data-pick]", box).forEach((input) => {
    input.checked = !library.unpicked.has(Number(input.dataset.pick));
  });
  refreshGroupHeads(box);
  renderLibraryWriteBar();
  renderLibraryBar();
}

async function applyLibrarySelection() {
  const ids = tickedIds(library.preview);
  if (!ids.length) { toast("Nothing selected", "Tick at least one video."); return; }
  // Entity totals are for the whole preview, so unticking rows can only lower
  // them: "up to" is the honest word. The opt-in counts are for the exact rows
  // ticked, because those are the numbers worth hesitating over.
  const ent = (library.preview || {}).entities || {};
  const picked = new Set(ids);
  const rows = (library.preview || {}).rows || [];
  const countPicked = (status) =>
    rows.filter((r) => picked.has(r.video_id) && r.status === status).length;
  const unverified = countPicked("unverified");
  const likely = countPicked("likely");
  const outside = outsideViewIds(library.preview).length;
  const held = heldBackIds(library.preview).length;
  const entities = (ent.models || 0) + (ent.studios || 0) + (ent.categories || 0);
  const ok = await modal({
    title: `Tag ${ids.length} video${ids.length === 1 ? "" : "s"} in Pronoisseur?`,
    html: `<p>Each video gets what its card shows under <b>After tagging</b>:
      its match's title, date, studio, models and categories, and a link to
      the StashDB scene, in Pronoisseur's catalog. Models and categories are
      only ever <b>added</b>, never removed. Your video files are never
      opened or changed.</p>
      <p>Up to <b>${escapeHtml(entities)}</b> entities will be created, or re-stamped
        with their StashDB id — ${escapeHtml(ent.models || 0)} models,
        ${escapeHtml(ent.studios || 0)} studios, ${escapeHtml(ent.categories || 0)}
        categories — plus <b>${escapeHtml(ent.aliases || 0)}</b> alias rows.</p>
      <p>Tagging title, date, studio or models hands <b>ownership of those fields</b>
        to this tool: Pronoisseur stops re-parsing them from the filename on those
        videos.</p>
      <p>A release date, once written, <b>cannot be cleared</b> through this API. It
        can only be overwritten with another date.</p>
      ${unverified ? `<p><b>${escapeHtml(unverified)}</b> of these rows are
        <b>unverified</b> — no fingerprint match, only the folder UUID.</p>` : ""}
      ${likely ? `<p><b>${escapeHtml(likely)}</b> of these rows are <b>likely</b> —
        one perceptual match, which a near-duplicate encode of a <b>different</b>
        scene produces just as readily. Nothing has proved these files are the
        scene named.</p>` : ""}
      ${outside ? `<p><b>${escapeHtml(outside)}</b> more ticked
        video${outside === 1 ? "" : "s"} outside this view
        ${outside === 1 ? "is" : "are"} <b>not</b> included. Clear the filter to
        tag ${outside === 1 ? "it" : "them"} too.</p>` : ""}
      ${held ? `<p>${escapeHtml(held)} more matched videos the run settled since
        the list was drawn are <b>not</b> included: they are not in the list yet.</p>` : ""}`,
    okLabel: "Tag",
  });
  if (!ok) return;
  try {
    await api("/api/library/apply", { method: "POST", body: { videoIds: ids } });
    loadLibrary();
  } catch (err) { reportError(err); }
}

//: "How this works" is open until it is folded, then stays folded. Kept in the
//: browser's own storage, for this viewer only: it is a convenience, so a store
//: that is missing or throws (a private window, blocked site data) leaves the
//: box open and nothing else changes.
const HOW_FOLDED_KEY = "stashdb-tagger:how-folded";

function wireHowBox(box, store) {
  if (!box) return;
  try {
    if (store && store.getItem(HOW_FOLDED_KEY) === "1") box.open = false;
  } catch (err) { /* nothing remembered, so it stays open */ }
  box.addEventListener("toggle", () => {
    try {
      if (store) store.setItem(HOW_FOLDED_KEY, box.open ? "0" : "1");
    } catch (err) { /* nowhere to keep it */ }
  });
}

function browserStore() {
  try { return window.localStorage || null; } catch (err) { return null; }
}

wireHowBox($("#lib-how"), browserStore());

// Wired once, at boot, because the box is static markup: a filter input rebuilt
// on every two-second poll loses the caret in the middle of a word. Debounced
// because each keystroke can rebuild up to a hundred cover grids.
let reviewFilterTimer = null;
const reviewFilterBox = $("#lib-review-filter");
if (reviewFilterBox) {
  reviewFilterBox.oninput = () => {
    clearTimeout(reviewFilterTimer);
    reviewFilterTimer = setTimeout(
      () => setReviewFilter(reviewFilterBox.value), 180);
  };
}

// Static too, beside the filter, so they stay put while the list redraws.
const reviewExpandButton = $("#lib-review-expand");
if (reviewExpandButton) reviewExpandButton.onclick = () => setAllReviewGroups(true);
const reviewCollapseButton = $("#lib-review-collapse");
if (reviewCollapseButton) reviewCollapseButton.onclick = () => setAllReviewGroups(false);

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (!$("#modal").hidden) { $("#modal-cancel").click(); return; }
});

// The first paint, then one attempt at a saved token: a person who set Pronoisseur
// up last time should not have to press Test connection again.
async function boot() {
  await loadLibrary();
  const s = library.state;
  if (s && s.hasToken && !s.connected) {
    try {
      rememberConnectResult(
        await api("/api/library/connect", { method: "POST", body: {} }));
    } catch (_) { /* the panel says what to check */ }
    await loadLibrary();
  }
}

boot();
