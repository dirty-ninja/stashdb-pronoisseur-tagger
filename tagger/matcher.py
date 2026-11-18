"""Turning fingerprint candidates into an identity, or into a question.

Pure: no HTTP, no SQLite, no clock, no files, no subprocess.

The one rule that is easy to get wrong: **classify by partitioning candidates on
algorithm, not by counting the group.** A single lookup can return the correct
oshash match *and* phash-only companions — a duplicate listing of the same scene,
or a genuinely different scene with a near-identical encode. Counting the group
would send both to the cover grid and ask the user about files identified
byte-exactly.

So an oshash hit is decisive: phash companions beside it are discarded. Only when
nothing matches by oshash do the phash candidates matter at all — and then a single
one is ``likely`` (never ``verified``), because a near-duplicate encode of a
*different* scene is a real thing that happens.

For that same reason the count that decides is a count of **distinct scenes**, never
of candidate entries: one scene answering both arms of a lookup is one scene, and
reading it as two would raise the very question the partition rule exists to avoid.

**Three overlapping vocabularies name one concept, so mapping between them
matters.** Getting it wrong is silent:

* This module's *confidence* — ``BY_OSHASH`` and ``BY_PHASH`` — both persist as
  :data:`tagger.library_store.DECISION_SOURCES`' single ``"fingerprint"``; how a
  fingerprint matched is not how the identity was decided. ``BY_MANUAL`` maps to
  ``"manual"``, and a user rejecting every candidate is ``"rejected"`` with
  ``scene_id=None``.
* This module's *status* is not :mod:`tagger.library_sync`'s. ``VERIFIED`` here and
  ``library_sync.VERIFIED`` are the same string by design — that equality is the
  only reason a fingerprint-verified row is writable, and ``tests/test_matcher.py``
  pins it — but :data:`UNMATCHED` (``"unmatched"``) and
  ``library_sync.UNVERIFIED`` (``"unverified"``) are near-homographs for
  **different facts**: no scene matched at all, versus a scene identified by UUID
  whose identity nothing has confirmed. Never treat one as the other.
"""

import datetime
import re

VERIFIED = "verified"
LIKELY = "likely"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"

BY_OSHASH = "oshash"
BY_PHASH = "phash"
BY_MANUAL = "manual"      # the declared confidence for an identity nothing
                          # computed: `library_app.decide` sets it when the user
                          # picks a scene by hand, and the preview renders it as
                          # "by hand" beside the diff.

DEFAULT_DURATION_TOLERANCE = 3.0

#: stashdb.org tolerates this many differing bits on a PHASH lookup.
PHASH_MAX_DISTANCE = 4


def hamming(a_hex, b_hex):
    """Bit distance between two hex hashes of the **same length**.

    A length mismatch is a ``ValueError``, not a comparison. ``int(x, 16)``
    left-zero-pads a short hash, so a truncated listing would otherwise score a
    small distance against a file whose own phash is near-zero — and a blank
    sprite genuinely hashes to all zeros, so that file exists.
    """
    a, b = str(a_hex).strip().lower(), str(b_hex).strip().lower()
    if len(a) != len(b):
        raise ValueError("hash lengths differ (%d vs %d): %r, %r"
                         % (len(a), len(b), a_hex, b_hex))
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _fingerprints(scene, algorithm):
    for entry in (scene or {}).get("fingerprints") or ():
        if str(entry.get("algorithm") or "").upper() == algorithm:
            value = str(entry.get("hash") or "").strip().lower()
            if value:
                yield value


def candidate_matches(scene, oshash, phash, max_distance=PHASH_MAX_DISTANCE):
    """How this candidate matches the file, or None.

    An oshash hit outranks any phash distance on the same candidate — identical
    bytes are not improved on by a perceptual near-miss.

    Raises ``ValueError`` when ``phash`` — the *file's own* fingerprint — is not
    hex. A corrupt cached value is a broken file, which is a different fact from
    "no scene matches", and swallowing it here would tell that lie. Only
    ``classify`` softens it, into a note. A malformed *listing* on the candidate
    is skipped instead: StashDB's data is not ours to be strict about.
    """
    if oshash:
        wanted = str(oshash).strip().lower()
        if wanted in set(_fingerprints(scene, "OSHASH")):
            return {"kind": BY_OSHASH, "distance": 0}

    if phash:
        int(str(phash), 16)                  # parsed once, outside the loop, so a
                                             # ValueError below can only be theirs
        best = None
        for listed in _fingerprints(scene, "PHASH"):
            try:
                distance = hamming(phash, listed)
            except ValueError:
                continue                     # a malformed listing is not fatal
            if best is None or distance < best:
                best = distance
        if best is not None and best <= int(max_distance):
            return {"kind": BY_PHASH, "distance": best}
    return None


def duration_agrees(file_duration, scene_duration,
                    tolerance=DEFAULT_DURATION_TOLERANCE):
    """True / False / **None for unknown**.

    None is not a soft False. Some legacy scenes carry no duration on StashDB, and
    treating unknown as disagreement would drop the correct candidate. A
    non-positive duration is unknown for the same reason: a scene carrying ``0``
    rather than ``null`` is a gap in their data, not a film of no length, and must
    not read as a hard disagreement.
    """
    if file_duration is None or scene_duration is None:
        return None
    file_duration, scene_duration = float(file_duration), float(scene_duration)
    if file_duration <= 0 or scene_duration <= 0:
        return None
    return abs(file_duration - scene_duration) <= float(tolerance)


def classify(candidates, oshash, phash, file_duration=None,
             tolerance=DEFAULT_DURATION_TOLERANCE,
             max_distance=PHASH_MAX_DISTANCE):
    """Classify one video against the candidates StashDB returned for it."""
    unreadable_phash = False
    if phash:
        try:
            int(str(phash), 16)
        except ValueError:
            # Drop the file's corrupt phash rather than raise: a file that oshash
            # can still identify must not be lost to it. The note below carries
            # the fact, so nothing reports it as StashDB having nothing.
            unreadable_phash, phash = True, None

    matched = []
    seen = set()
    for scene in candidates or ():
        match = candidate_matches(scene, oshash, phash, max_distance)
        if not match:
            continue
        scene_id = (scene or {}).get("id")
        if scene_id is not None and scene_id in seen:
            # One scene can answer both arms of one lookup. Counting the repeat
            # would call a byte-exactly identified file ambiguous and show the
            # user two identical covers. Keep the first: the order is the
            # server's own, and a repeat carries the same scene payload.
            continue
        seen.add(scene_id)
        scene_duration = (scene or {}).get("duration")
        agrees = duration_agrees(file_duration, scene_duration, tolerance)
        matched.append({
            "scene": scene, "kind": match["kind"], "distance": match["distance"],
            "duration_agrees": agrees,
            "duration_delta": (None if agrees is None
                               else round(float(file_duration)
                                          - float(scene_duration), 1)),
        })

    exact = [c for c in matched if c["kind"] == BY_OSHASH]
    if exact:
        # Identical bytes settle it. Phash companions are noise beside that, and a
        # duration StashDB may simply have wrong does not outrank the bytes.
        if len(exact) == 1:
            return {"status": VERIFIED, "confidence": BY_OSHASH,
                    "candidates": exact,
                    "note": "oshash matches a fingerprint StashDB lists"}
        return {"status": AMBIGUOUS, "confidence": BY_OSHASH,
                "candidates": exact,
                "note": "the same bytes are listed under %d scenes — pick one"
                        % len(exact)}

    near = [c for c in matched if c["kind"] == BY_PHASH]
    near.sort(key=lambda c: c["distance"])   # the cover grid renders these in order
    if not near:
        if unreadable_phash:
            return {"status": UNMATCHED, "confidence": None, "candidates": [],
                    "note": "oshash found nothing and Pronoisseur's phash for "
                            "this file is unreadable"}
        return {"status": UNMATCHED, "confidence": None, "candidates": [],
                "note": "no scene on StashDB carries these fingerprints"}

    dropped = [c for c in near if c["duration_agrees"] is False]
    kept = [c for c in near if c["duration_agrees"] is not False]

    if not kept:
        return {"status": AMBIGUOUS, "confidence": BY_PHASH, "candidates": near,
                "note": "every near match disagrees on duration — check the covers"}
    if len(kept) == 1 and not dropped:
        return {"status": LIKELY, "confidence": BY_PHASH, "candidates": kept,
                "note": "one perceptual match within %d bits" % max_distance}
    if len(kept) == 1:
        return {"status": LIKELY, "confidence": BY_PHASH, "candidates": kept,
                "note": "one perceptual match; %d dropped on duration"
                        % len(dropped)}
    return {"status": AMBIGUOUS, "confidence": BY_PHASH, "candidates": kept,
            "note": "%d perceptual matches — pick one" % len(kept)}


# ------------------------------------------------------- assisted search
#
# The last resort. Every fingerprint missed (or the user rejected what they
# were shown), so the only thing left to search on is what Pronoisseur itself
# parsed out of the filename — a title, some model names, a studio, a date.
# All of it is a guess, none of it is authoritative, and so nothing below ever
# decides anything: it builds the *input* to a search a human then judges.
# Building that input is pure, which is why it lives here rather than beside
# the transport that will send it.

#: Release noise dropped from a filename-derived title. Compared
#: case-insensitively and only as whole tokens: a substring test would take the
#: "hd" out of "Hdream" and the "4k" out of "4kids".
_TITLE_NOISE = frozenset((
    "1080p", "720p", "480p", "2160p", "4k", "8k", "xxx", "x264", "x265",
    "h264", "h265", "hevc", "avc", "aac", "mp4", "mkv", "wmv", "webrip",
    "web-dl", "hdrip", "sd", "hd",
))

_BRACKETED = re.compile(r"\[[^\]]*\]")
_LEADING_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*-?\s*")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _stem_of(rel_path):
    """The last segment of a library-relative path, final extension dropped.

    Split on both separators by hand rather than through ``os.path`` or
    ``pathlib``: the rows come from a Windows library, but this tool also runs
    on POSIX, where ``os.path`` reads ``"Studio\\Scene.mp4"`` as a single
    segment and would carry the backslash into the search term.
    """
    if not isinstance(rel_path, str):
        return ""
    segment = re.split(r"[\\/]", rel_path)[-1]
    return segment.rsplit(".", 1)[0] if "." in segment else segment


def _clean_stem(stem):
    """A filename stem read as a human title, or None if nothing human is left.

    Order matters: the bracketed group goes first (Whisparr parks the scene UUID
    there, and it can sit either side of the date), then the leading date while
    its hyphens are still hyphens, and only then are ``.`` and ``_`` turned into
    spaces — which is what makes tokens out of a dotted release name so the
    noise list can see them.
    """
    text = _BRACKETED.sub(" ", stem)
    text = _LEADING_DATE.sub("", text)
    text = text.replace(".", " ").replace("_", " ")
    return " ".join(t for t in text.split()
                    if t.lower() not in _TITLE_NOISE) or None


def _performer_names(models):
    """Names in row order. Exact repeats collapse; nothing else is judged.

    Two spellings of one performer stay two entries — deciding they are the same
    person is the picker's job, and guessing it here would quietly drop a name
    the user needs to resolve.
    """
    names = []
    for entry in models or ():
        name = (entry or {}).get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def _iso_date(value):
    """The value when it is literally ``YYYY-MM-DD``, else None.

    Shape only, and deliberately unrepaired: ``"2024-1-2"`` is the parser
    telling us it is unsure, and reading a day out of it would rank a guessed
    date above scenes that carry a real one.
    """
    return value if isinstance(value, str) and _ISO_DATE.fullmatch(value) else None


def assisted_terms(video):
    """What Pronoisseur already believes about a file, as search terms.

    Nothing here comes from StashDB — that is the point. Fingerprints are what
    StashDB knows about the bytes, and they have just failed; the filename is
    all that is left. Every field is independently optional, so a bare path with
    no models, studio or date is a legitimate row and yields a title-only
    search.
    """
    video = video or {}

    title = video.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        # The path is the only other place a name lives, and the only place the
        # release noise lives, so it is cleaned and the row's own title is not.
        title = _clean_stem(_stem_of(video.get("rel_path")))

    studio = video.get("studio")
    studio_name = studio.get("name") if isinstance(studio, dict) else None
    studio_name = studio_name.strip() if isinstance(studio_name, str) else ""

    return {"title": title or None,
            "performers": _performer_names(video.get("models")),
            "studio": studio_name or None,
            "date": _iso_date(video.get("release_date"))}


def date_window(value, days=1):
    """The dates within ``days`` of ``value``, ascending, as ``YYYY-MM-DD``.

    Empty for a missing date and for a well-shaped impossible one
    (``2024-02-31``): a window has to be real days or it cannot be compared to
    a release date. This is arithmetic on a given value, never a clock — the
    module reads no ``today``, so the same row always produces the same window.
    """
    text = _iso_date(value)
    if text is None:
        return []
    try:
        anchor = datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return []
    days = int(days)
    return [(anchor + datetime.timedelta(days=offset)).isoformat()
            for offset in range(-days, days + 1)]


def build_assisted_query(video, performer_ids=None, studio_ids=None, days=1):
    """The whole input to a manual search, as one value.

    **No ``date`` key goes into the filter, ever.** A scene input has no
    BETWEEN criterion, so a three-day window cannot be expressed as one criterion, and an
    ``EQUALS`` on the filename's date would drop the correct scene in exactly
    the case the ±1 day exists for — a date that is a day out. The window is
    returned instead, for the caller to rank and label on; :func:`rank_assisted`
    is that ranking. A ``GREATER_THAN`` is not an improvement on this, it is the
    same mistake pointing one way.

    ``performer_ids`` and ``studio_ids`` are ids resolved elsewhere — this
    module never turns a name into an id — and they are carried differently
    because ``queries.build_scene_input`` reads them differently: ``studios`` is
    a filter key it understands, while performers reach it only as its own
    argument. So the performer ids ride at the top level rather than inside
    ``filter``. A caller handing ``filter`` straight through then gets the
    studio filtering it can see, and cannot come to believe performers were
    applied when ``build_scene_input`` had silently ignored them.
    """
    terms = assisted_terms(video)
    filt = {"scene_sort": "DATE", "scene_direction": "DESC"}
    if terms["title"]:
        filt["title"] = terms["title"]
    if studio_ids:
        filt["studios"] = list(studio_ids)
    return {"terms": terms,
            "filter": filt,
            "performer_ids": list(performer_ids or ()),
            "date_window": date_window(terms["date"], days)}


def rank_assisted(scenes, window):
    """Order manual-search results for the cover grid: date matches first.

    A rank, not a filter. The window came from a filename, so a scene outside it
    is still shown — just lower — and a row with no date at all is not evidence
    against itself. The sort is stable and deliberately has no secondary key:
    inside each group StashDB's own ordering is better than anything invented
    from data we already know to be a guess.
    """
    wanted = set(window or ())
    ranked = []
    for scene in scenes or ():
        released = (scene or {}).get("release_date")
        ranked.append({"scene": scene,
                       "date_match": bool(released) and released in wanted})
    ranked.sort(key=lambda row: not row["date_match"])
    return ranked
