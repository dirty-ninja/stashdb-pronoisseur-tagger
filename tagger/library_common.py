"""Constants and small helpers shared by the library modules."""

from . import library_sync as sync, matcher
from .scenes import MAX_FINGERPRINT_BATCH


NO_IDENTITY = "no-identity"
NOT_FOUND = "not-found"
IN_SYNC = "in-sync"
#: A row an identify run has taken up and not yet decided. It is never written to
#: a row's own ``status`` — the row keeps the status it arrived with, so a cancel
#: hands it back unchanged — and it is reported in its place while the run owns it.
WAITING = "waiting"
#: Why a row the run still owns cannot be answered yet. The run would overwrite
#: the answer as soon as its own lookup came back.
STILL_IDENTIFYING = ("this file is still being identified — its result shows up "
                     "here as soon as the run reaches it")
#: Wire status of a video Pronoisseur has not fingerprinted yet. Pronoisseur
#: computes the fingerprints; a video with nothing for StashDB to look up, or
#: whose only value found nothing, is waiting on it rather than unmatched.
PENDING_HASH = "pending-hash"
PENDING_NOT_YET = ("Pronoisseur hasn't fingerprinted this video yet, or couldn't "
                   "read it. Scan and identify again later.")
PENDING_SWITCH_OFF = ("Pronoisseur's perceptual fingerprints are switched off. "
                      "Turn on Settings → Local API → Fingerprint videos "
                      "for connected apps in Pronoisseur.")
PENDING_OLD_PRONOISSEUR = ("This version of Pronoisseur doesn't share "
                           "fingerprints. Update Pronoisseur to identify by "
                           "fingerprint.")
#: The note a row carries when its *lookup* failed.
#: The video is fine; StashDB refused the batch it was in. Re-running identify
#: retries it for the cost of a request — the fingerprints are on the row.
LOOKUP_FAILED_NOTE = "StashDB lookup failed"
REJECTED_NOTE = ("you rejected every candidate for this file — clear the decision "
                 "to be asked again")
#: What a run's ``finished`` says when the writes landed but reading them back
#: did not. The rows on screen are then the pre-write ones and the preview will
#: keep proposing them until the next scan — a fact worth stating on the run, and
#: never a reason to call a successful apply a failure.
STALE_NOTE = "written; preview may be stale until the next scan"

#: Buckets a run sorts every considered video into, in display order: strongest
#: identity first, then the questions identify asks, then the rows it cannot help.
#: :meth:`LibraryApp.scan_state` seeds its counts from this tuple, so a status
#: missing here would get rows and no count and the UI's pill list would omit it.
BUCKETS = (sync.VERIFIED, sync.VERIFIED_BY_DURATION, sync.UNVERIFIED,
           matcher.LIKELY, matcher.AMBIGUOUS, matcher.UNMATCHED, PENDING_HASH,
           WAITING, sync.UUID_MISMATCH, NOT_FOUND, NO_IDENTITY, IN_SYNC)

#: Statuses whose rows an identify run may work on: the buckets a folder UUID
#: cannot settle, plus the unsettled outcomes of a previous run, so a run
#: interrupted by an offline share can simply be run again. A row already
#: `verified` is never re-identified.
IDENTIFIABLE = (NO_IDENTITY, sync.UUID_MISMATCH, matcher.UNMATCHED,
                PENDING_HASH, matcher.LIKELY, matcher.AMBIGUOUS)

#: Rows with no scene to plan against. `likely` is absent deliberately: it HAS a
#: scene, and :data:`OPT_IN_STATUSES` makes it an opt-in write.
UNPLANNABLE = (NO_IDENTITY, NOT_FOUND, matcher.UNMATCHED, PENDING_HASH,
               matcher.AMBIGUOUS)

#: Statuses a run writes only when the user has ticked the row. `unverified` is
#: an identity from a folder UUID that nothing has confirmed; `likely` is one
#: perceptual match within PHASH_MAX_DISTANCE bits, which a different scene's
#: near-duplicate encode can also score. Neither is ever written unasked, which
#: is why `likely` stays out of ``library_sync.WRITABLE_STATUSES``: that tuple
#: is what a run writes with nothing ticked at all.
OPT_IN_STATUSES = (sync.UNVERIFIED, matcher.LIKELY)

#: The answers :meth:`LibraryApp.decide` accepts. Named so that the route can
#: refuse a typo at the edge without keeping a second copy of the list to drift
#: from this one.
DECIDE_ACTIONS = ("pick", "reject", "clear")

#: Why an ambiguous row is still waiting after :meth:`LibraryApp.accept_recommended`.
#: Two facts kept deliberately apart, because they need different work from the
#: user: a recommendation the durations do not support is a judgement call to be
#: made over the covers, while a row with nothing to pick from is not a choice at
#: all and only the assisted search can move it.
DURATIONS_DISAGREE = "durations disagree"
NO_CANDIDATES = "no candidates"

#: Videos per fingerprint request. The identify run batches the lookups itself
#: rather than handing the whole list to ``SceneFetcher.by_fingerprints``, so that
#: it can check the cancel flag between batches and settle each batch's rows as it
#: goes: a cancelled run keeps everything it had already answered.
#:
#: It is the server's own hard cap, not a tunable: over it stash-box refuses the
#: whole query with "too many scenes" and every video in the batch is lost.
IDENTIFY_BATCH = MAX_FINGERPRINT_BATCH


class LibraryStateError(RuntimeError):
    """A state the UI should render as a panel, not a server error."""


def _idle(label="idle"):
    return {"running": False, "label": label, "done": 0, "total": 0, "error": None,
            "details": None, "finished": None}


def _note(existing, addition):
    """Join a row's note to another fact, dropping whichever half is empty."""
    existing, addition = (existing or "").strip(), (addition or "").strip()
    if not existing:
        return addition
    return "%s (%s)" % (existing, addition) if addition else existing


def _listed(value):
    """How many entries a scene lists, or **None** when it lists no array at all.

    The distinction is the whole point: a scene with ``"tags": []`` genuinely has
    no tags — thin metadata, which is exactly what the picker is trying to avoid
    — while a scene fetched by a query that never asked for tags knows nothing
    either way. Rendering the second as "0 tags" would be a claim about StashDB's
    data that this code has not earned.
    """
    return len(value) if isinstance(value, (list, tuple)) else None


def fingerprint_state(info, videos):
    """What this Pronoisseur says about fingerprints, from ``/info`` and the listing.

    ``known``: ``info`` carries a ``fingerprints`` block, or has none but a
    listed video has a ``phash`` key (the keys are the support signal, and then
    the switch and failure count are not reported). ``older``: ``info`` was read
    and shows no sign of support. ``unknown``: ``info`` could not be read.
    """
    if not isinstance(info, dict):
        return {"state": "unknown", "phashEnabled": None, "phashFailed": None}
    block = info.get("fingerprints")
    if isinstance(block, dict):
        # Only a real bool is believed, and a count that is not a number is
        # unknown: a odd value from Pronoisseur must never fail the scan.
        enabled = block.get("phash_enabled")
        try:
            failed = int(block.get("phash_failed") or 0)
        except (TypeError, ValueError):
            failed = None
        return {"state": "known",
                "phashEnabled": enabled if isinstance(enabled, bool) else None,
                "phashFailed": failed}
    if any(isinstance(v, dict) and "phash" in v for v in videos):
        return {"state": "known", "phashEnabled": None, "phashFailed": None}
    return {"state": "older", "phashEnabled": None, "phashFailed": None}


def pending_reason(fingerprints):
    """The one sentence that says why a video has no fingerprint yet."""
    if fingerprints.get("state") == "older":
        return PENDING_OLD_PRONOISSEUR
    if fingerprints.get("phashEnabled") is False:
        return PENDING_SWITCH_OFF
    return PENDING_NOT_YET
