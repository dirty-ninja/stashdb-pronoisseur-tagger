"""Pure planning for the Pronoisseur library sync.

Everything in this module is a pure function: no HTTP, no SQLite, no clock, no
file access. That is what makes the whole diff testable without either upstream —
`library_app.py` does the I/O and calls in here to decide what should happen.

The vocabulary of the pipeline:

* **identity** — which StashDB scene a video is, taken from the UUID Whisparr
  writes into the folder name or from a fingerprint match.
* **verification** — proof that the identity is right, because a folder can carry
  the wrong scene's UUID.
* **resolution** — turning StashDB names (studio, performer, tag) into the names
  Pronoisseur already uses, case-insensitively, because the app's own upsert is
  case-SENSITIVE and would otherwise create "Acme" beside "acme".
* **diff** — current vs proposed, per field, per the run's write toggles.
"""

import datetime
import re
import unicodedata

#: Unicode general categories that count as letters and digits in a name key: the
#: five letter categories plus **Nd only** for digits. Python's ``str.isalnum`` is wider
#: — it also accepts Nl and No (roman numerals, fractions, superscripts) — so using
#: it here would key a name the app keys differently, which is the one thing this
#: function exists to prevent.
LETTER_OR_DIGIT = frozenset(("Lu", "Ll", "Lt", "Lm", "Lo", "Nd"))

#: Whisparr's folder convention is ``YYYY-MM-DD - Title [uuid]``. The brackets are
#: required: a bare UUID anywhere in a path is not an identity claim.
SCENE_UUID_RE = re.compile(
    r"\[([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]")


def extract_scene_uuid(rel_path):
    """The StashDB scene UUID a path claims, lowercased, or None.

    The **last** match wins: a file inside an already-identified folder may itself
    carry a UUID, and the deepest one is the more specific claim.
    """
    matches = SCENE_UUID_RE.findall(rel_path or "")
    return matches[-1].lower() if matches else None


#: The three date shapes StashDB actually hands out. A legacy scene is often known
#: only to the year, or to the month, and the field is one string either way.
_PARTIAL_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def normalise_release_date(value):
    """``(date, estimated)`` for a PATCH body, or ``(None, False)`` if unusable.

    The Local API takes ``YYYY-MM-DD`` and **only** that: a partial date is
    ``422 validation``, and ``POST /videos/batch`` is all-or-nothing, so a single
    scene StashDB dates as ``"2019"`` refuses every other row in its batch.

    So a partial date is widened to the first day of the period it names and
    flagged **estimated**, which is the app's own word for "this day was filled
    in, not known" — the flag exists precisely so a client can say so. A full
    date asserts ``estimated: false``, because a date StashDB knows to the day is
    not an estimate whatever the video's flag said before.

    Anything else — a decade, a range, free text, ``"2019-02-31"`` — is
    ``(None, False)``, and the caller omits the key rather than guessing. The
    calendar is checked rather than only the shape: a day that does not exist is
    not a date this tool should invent a nearby one for.
    """
    match = _PARTIAL_DATE_RE.match((value or "").strip())
    if not match:
        return None, False
    year, month, day = match.groups()
    try:
        parsed = datetime.date(int(year), int(month or 1), int(day or 1))
    except ValueError:                   # month 13, day 32, 31 February
        return None, False
    return parsed.isoformat(), day is None


def name_key(text):
    """Pronoisseur's own name key: letters and digits only, lower-cased.

    This is the rule Pronoisseur's own name matching uses, and the app applies it
    to the stored ``raw_token`` when it builds its alias map. Match on this and two
    spellings of one name never become two entities.
    """
    return "".join(ch.lower() for ch in str(text or "")
                   if unicodedata.category(ch) in LETTER_OR_DIGIT)


def index_by_name(entities):
    """``{name_key: entity}`` for a ``GET /models|/studios|/categories`` payload.

    The first spelling wins, so a library that already holds both "Acme" and "acme"
    gets one of them consistently instead of alternating.
    """
    index = {}
    for entity in entities or ():
        key = name_key(entity.get("name"))
        if key and key not in index:
            index[key] = entity
    return index


def index_aliases(rows):
    """``{(kind, name_key(raw_token)): canonical_name}`` for ``GET /aliases``.

    Keyed on ``name_key`` of the raw token because that is what the app itself
    keys its alias map on: "Jane A" and "jane a" are one alias to it, so they
    have to be one alias here too, or a run re-pushes rows the app already holds.

    The canonical name is kept **whole** rather than keyed. It is an entity
    *name*, and the app's name tables collate byte-for-byte, so a canonical that
    differs at all names a different target and is worth pushing again.

    The first row for a key wins, as in :func:`index_by_name`.
    """
    index = {}
    for row in rows or ():
        kind = str((row or {}).get("kind") or "").strip()
        key = name_key((row or {}).get("raw_token"))
        if not kind or not key or (kind, key) in index:
            continue
        index[(kind, key)] = str(row.get("canonical_name") or "")
    return index


# --------------------------------------------------------------- verification

VERIFIED = "verified"
VERIFIED_BY_DURATION = "verified-by-duration"
UNVERIFIED = "unverified"
UUID_MISMATCH = "uuid-mismatch"

#: Statuses that may be written without the user ticking the row. `unverified` is
#: opt-in per row; `uuid-mismatch` is never written.
WRITABLE_STATUSES = (VERIFIED, VERIFIED_BY_DURATION)

DEFAULT_DURATION_TOLERANCE = 3.0


def scene_oshashes(scene):
    """Every OSHASH listed on a scene, lower-cased. PHASH entries are excluded."""
    out = set()
    for entry in (scene or {}).get("fingerprints") or ():
        if str(entry.get("algorithm") or "").upper() == "OSHASH":
            value = str(entry.get("hash") or "").strip().lower()
            if value:
                out.add(value)
    return out


def classify_uuid_match(file_oshash, file_duration, scene,
                        tolerance=DEFAULT_DURATION_TOLERANCE):
    """Decide whether a folder's UUID really is this file's scene.

    Order matters. An OSHASH hit is proof of identical bytes. Failing that, the
    durations either agree (good enough to write), disagree (Whisparr got it wrong;
    never write it), or one of them is simply unknown, which is not evidence of
    anything and so is never a mismatch: some legacy scenes carry a null StashDB
    duration.
    """
    scene_duration = (scene or {}).get("duration")
    if file_oshash and file_oshash.strip().lower() in scene_oshashes(scene):
        return {"status": VERIFIED, "note": "oshash matches a listed fingerprint",
                "scene_duration": scene_duration}

    if scene_duration is None:
        return {"status": UNVERIFIED,
                "note": "no oshash match and StashDB lists no duration for this scene",
                "scene_duration": None}
    if file_duration is None:
        return {"status": UNVERIFIED,
                "note": "no oshash match and Pronoisseur has no duration for this file",
                "scene_duration": scene_duration}

    delta = abs(float(file_duration) - float(scene_duration))
    if delta <= float(tolerance):
        return {"status": VERIFIED_BY_DURATION,
                "note": "duration agrees within %.1fs" % delta,
                "scene_duration": scene_duration}
    return {"status": UUID_MISMATCH,
            "note": "StashDB says %.0fs, this file runs %ds — the folder UUID looks "
                    "like a different scene" % (float(scene_duration),
                                                int(file_duration)),
            "scene_duration": scene_duration}


# ----------------------------------------------------------------- resolution

def resolve_name(name, index):
    """The library's existing spelling of a name, or the name unchanged.

    The app's upsert is case-SENSITIVE — "Acme" and "acme" become two entities —
    so every name is matched against what the library already holds before it is
    sent.
    """
    entity = (index or {}).get(name_key(name))
    return entity["name"] if entity else name


def scene_studio(scene, use_parent=True):
    """``{"id", "name"}`` for the studio to write, or None.

    Defaults to the **parent** studio: Whisparr files land under sub-studios whose
    parent is the network you actually think in (a sub-label "Acme Originals"
    under "Acme"). Pronoisseur's studios are flat, so which to use is a taste
    call and the toggle exists for it.
    """
    studio = (scene or {}).get("studio")
    if not studio:
        return None
    parent = studio.get("parent") if use_parent else None
    chosen = parent if (parent and parent.get("name")) else studio
    name = (chosen.get("name") or "").strip()
    return {"id": chosen.get("id"), "name": name} if name else None


def scene_performers(scene, credited_as_aliases=True):
    """Canonical performers with their aliases.

    The **canonical** StashDB name is the model name; a credited-as name is an
    alias, not an identity ("Jane D." credited on a scene for "Jane Doe"). An alias
    that normalises to the canonical name is not an alias at all.
    """
    out = []
    for row in (scene or {}).get("performers") or ():
        performer = row.get("performer") or {}
        name = (performer.get("name") or "").strip()
        if not name:
            continue
        canonical_key = name_key(name)
        aliases, seen = [], {canonical_key}
        raw_aliases = list(performer.get("aliases") or ())
        credited = (row.get("as") or "").strip()
        if credited and credited_as_aliases:
            raw_aliases.append(credited)
        for alias in raw_aliases:
            alias = (alias or "").strip()
            key = name_key(alias)
            if alias and key and key not in seen:
                seen.add(key)
                aliases.append(alias)
        out.append({"id": performer.get("id"), "name": name, "aliases": aliases})
    return out


def review_models(video, scene, models_index):
    """The models a Review row is grouped under: ``[{"name", "key"}]``.

    The video's own models first, as Pronoisseur spells them, then the
    canonical performers on ``scene`` — the one StashDB recommends, never a
    runner-up — in the library's own spelling where it already has one, which is
    the spelling the write would use. Listed once each by :func:`name_key`, so
    "JANE EXAMPLE" off a filename and "Jane Example" off StashDB are one group.
    A credited-as name is an alias, not a second person, and is left out.
    """
    out, seen = [], set()
    names = [m.get("name") for m in (video or {}).get("models") or ()]
    names += [resolve_name(p["name"], models_index)
              for p in scene_performers(scene, credited_as_aliases=False)]
    for name in names:
        name = (name or "").strip()
        key = name_key(name)
        if key and key not in seen:
            seen.add(key)
            out.append({"name": name, "key": key})
    return out


def allowlisted_tags(scene, allowlist):
    """The scene's tags whose StashDB tag category is allowlisted.

    A tag with no category is never allowlisted — there is no box to tick for it.
    """
    allowed = {str(c) for c in (allowlist or ())}
    out = []
    for tag in (scene or {}).get("tags") or ():
        category = tag.get("category") or {}
        if category.get("id") and str(category["id"]) in allowed:
            out.append(tag)
    return out


def resolve_scene_tags(scene, allowlist, tag_map, existing_categories):
    """Allowlisted tags resolved to Pronoisseur category names.

    Resolution order, highest first: an explicit ``tag_map`` row (which may say
    "ignore"), a case-insensitive match against a category the library already has,
    then the StashDB tag name verbatim — which the apply step will create.
    """
    tag_map = tag_map or {}
    out, seen = [], set()
    for tag in allowlisted_tags(scene, allowlist):
        if not tag.get("id"):
            # No id is no key for an override and nothing to stamp as an external
            # id — str(None) would POST the literal string "None" as one.
            continue
        tag_id = str(tag["id"])
        override = tag_map.get(tag_id)
        if override and override.get("mode") == "ignore":
            continue
        if override and override.get("mode") == "map" and override.get("category_name"):
            name, source = override["category_name"], "tag_map"
        else:
            raw = (tag.get("name") or "").strip()
            if not raw:
                continue
            existing = (existing_categories or {}).get(name_key(raw))
            name, source = (existing["name"], "existing") if existing else (raw, "stashdb")
        key = name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"tag_id": tag_id, "name": name, "source": source})
    return out


# --------------------------------------------------------------------- diff

#: Every external id this tool writes carries this provider name. The app stores
#: it as free text and keys on it, so it must never drift.
PROVIDER = "stashdb"


def _external_id(entity, provider=PROVIDER):
    for entry in (entity or {}).get("external_ids") or ():
        if str(entry.get("provider") or "").lower() == provider:
            return str(entry.get("id") or "")
    return None


def _cover(scene):
    for image in (scene or {}).get("images") or ():
        url = (image.get("url") or "").strip()
        if url:
            return url
    return None


def plan_change(video, scene, settings, existing, status=VERIFIED, note=""):
    """Current vs proposed for one video, and the PATCH that would get it there.

    Pure. Everything it needs about the library — the existing model, studio and
    category names — arrives in ``existing`` as ``{kind: {name_key: entity}}``.

    Three contract rules shape the body it builds. ``release_date`` **cannot be
    cleared** and is `422` as ``null``, so a scene with no date simply omits the
    key. It must also be a full ``YYYY-MM-DD``, which StashDB's own dates are not
    always — see :func:`normalise_release_date`, which widens a partial one and
    says so. And ``release_date_estimated`` is **preserved unless named**, so it
    is sent explicitly whenever a date is being asserted — including when the
    date already matches but the flag does not, which is a real change worth
    making in either direction.
    """
    existing = existing or {}
    models_index = existing.get("models") or {}
    studios_index = existing.get("studios") or {}
    categories_index = existing.get("categories") or {}

    diff, patch, fields = [], {}, []
    entities = {"models": [], "studios": [], "categories": [], "aliases": []}

    def record(field, current, proposed):
        diff.append({"field": field, "current": current, "proposed": proposed})

    # `fields` is what the preview shows: every field, changed or not, with the
    # verb for what the write does to it. `diff` lists only the changes as
    # old -> new, which reads as "replace" even where the tool only ever adds.
    def scalar(field, action, current, proposed=None, **extra):
        fields.append(dict({"field": field, "action": action, "current": current,
                            "proposed": current if proposed is None else proposed},
                           **extra))

    def overwrite(current, verb):
        """`set` into a blank field, otherwise ``verb``: rename, change, replace."""
        return "set" if not (current or "").strip() else verb

    # --- title
    scene_title = (scene.get("title") or "").strip()
    current_title = video.get("title")
    if not settings["write_title"]:
        scalar("title", "off", current_title)
    elif scene_title and (current_title or "").strip() != scene_title:
        patch["title"] = scene_title
        record("title", current_title, scene_title)
        scalar("title", overwrite(current_title, "rename"), current_title, scene_title)
    else:
        scalar("title", "same", current_title)

    # --- release date (+ the estimated flag, which is preserved unless named)
    raw_date = (scene.get("release_date") or "").strip()
    scene_date, date_estimated = normalise_release_date(raw_date)
    if settings["write_release_date"] and raw_date and not scene_date:
        # Said on the row rather than swallowed: the date is the one field the
        # API cannot take back once written, so a shape this tool refuses to
        # guess at is shown beside the write it is missing from, and never fails
        # the batch it rides in.
        note = ("%s (%s)" % (note, "StashDB date %r not usable" % raw_date)
                if note else "StashDB date %r not usable" % raw_date)
    current_date = video.get("release_date")
    estimated = bool(video.get("release_date_estimated"))
    if not settings["write_release_date"]:
        scalar("release_date", "off", current_date, current_estimated=estimated,
               proposed_estimated=estimated)
    elif scene_date and (current_date or "").strip() != scene_date:
        patch["release_date"] = scene_date
        patch["release_date_estimated"] = date_estimated
        record("release_date", current_date, scene_date)
        scalar("release_date", overwrite(current_date, "change"), current_date,
               scene_date, current_estimated=estimated,
               proposed_estimated=date_estimated)
    elif scene_date and estimated != date_estimated:
        # The same day, but whether it is a guess changes: a real write, and the
        # row has to say which way.
        patch["release_date_estimated"] = date_estimated
        record("release_date_estimated", estimated, date_estimated)
        scalar("release_date", "change", current_date, scene_date,
               current_estimated=estimated, proposed_estimated=date_estimated)
    else:
        scalar("release_date", "same", current_date, current_estimated=estimated,
               proposed_estimated=estimated)

    # --- studio
    studio = scene_studio(scene, use_parent=settings["use_parent_studio"])
    current_studio = ((video.get("studio") or {}) or {}).get("name")
    studio_action = "off" if not settings["write_studio"] else "same"
    if settings["write_studio"] and studio:
        name = resolve_name(studio["name"], studios_index)
        if name_key(current_studio) != name_key(name):
            patch["studio"] = name
            record("studio", current_studio, name)
            studio_action = overwrite(current_studio, "replace")
            # "New to your library" is whether the write creates the studio, which
            # is exactly whether the index has no entry for its name.
            scalar("studio", studio_action, current_studio, name,
                   new=name_key(name) not in studios_index)
        if studio.get("id"):
            entities["studios"].append({"name": name, "stashdb_id": studio["id"]})
    if studio_action in ("off", "same"):
        scalar("studio", studio_action, current_studio, new=False)

    # --- models (add only — removal is never offered)
    kept_models = [m.get("name") for m in video.get("models") or ()]
    added_models = []
    if settings["add_models"]:
        have = {name_key(m.get("name")) for m in video.get("models") or ()}
        additions = []
        for performer in scene_performers(
                scene, credited_as_aliases=settings["credited_as_aliases"]):
            name = resolve_name(performer["name"], models_index)
            if name_key(name) in have:
                continue
            have.add(name_key(name))
            additions.append(name)
            added_models.append({"name": name,
                                 "new": name_key(name) not in models_index})
            if performer.get("id"):
                entities["models"].append({"name": name, "stashdb_id": performer["id"]})
            for alias in performer["aliases"]:
                entities["aliases"].append({"kind": "actor", "raw_token": alias,
                                            "canonical_name": name})
        if additions:
            patch["models"] = {"add": additions}
            record("models", kept_models, additions)
    fields.append({"field": "models", "kept": kept_models, "added": added_models,
                   "action": ("off" if not settings["add_models"]
                              else "add" if added_models else "same")})

    # --- categories (add only)
    kept_categories = [c.get("name") for c in video.get("categories") or ()]
    added_categories = []
    if settings["add_categories"]:
        have = {name_key(c.get("name")) for c in video.get("categories") or ()}
        additions = []
        for tag in resolve_scene_tags(scene, settings["allowlist"],
                                      settings.get("tag_map") or {}, categories_index):
            if name_key(tag["name"]) in have:
                continue
            have.add(name_key(tag["name"]))
            additions.append(tag["name"])
            added_categories.append({"name": tag["name"],
                                     "new": name_key(tag["name"])
                                     not in categories_index})
            # A `tag_map` override says "file this tag under *my* category", not
            # "my category IS that StashDB tag". Stamping the tag id on it would
            # assert an equivalence the user never made, and the contract offers no
            # way to take an external id back. The category still goes on the
            # video; it just claims nothing.
            if tag.get("source") != "tag_map":
                entities["categories"].append({"name": tag["name"],
                                               "stashdb_id": tag["tag_id"]})
        if additions:
            patch["categories"] = {"add": additions}
            record("categories", kept_categories, additions)
    fields.append({"field": "categories", "kept": kept_categories,
                   "added": added_categories,
                   "action": ("off" if not settings["add_categories"]
                              else "add" if added_categories else "same")})

    # --- external id (per-provider upsert; leaves other providers alone)
    scene_id = str(scene.get("id") or "")
    current_link = _external_id(video)
    if scene_id and current_link != scene_id:
        patch["external_ids"] = {"set": [{"provider": PROVIDER, "id": scene_id}]}
        record("external_ids", current_link, scene_id)
        scalar("external_ids", "replace" if current_link else "add", current_link,
               scene_id)
    else:
        scalar("external_ids", "same", current_link)

    if not patch:
        entities = {"models": [], "studios": [], "categories": [], "aliases": []}

    return {
        "video_id": video.get("id"), "rel_path": video.get("rel_path"),
        # The absolute path as well as the library-relative one: an `unmatched`
        # row is rendered as a review block whose header has to say which of the
        # things on screen is the file on disk.
        "path": video.get("path"),
        "title": video.get("title"), "scene_id": scene_id, "status": status,
        "note": note, "diff": diff, "fields": fields, "patch": patch,
        "entities": entities,
        "cover": _cover(scene), "scene_title": scene_title or None,
        # The date as it would be **written**, not as StashDB spells it, plus
        # whether the day in it was filled in by this tool. A widened date is a
        # real write and should be approved knowing which part StashDB vouched for.
        "scene_date": scene_date or None,
        "scene_date_estimated": bool(scene_date) and date_estimated,
        "duration_secs": video.get("duration_secs"),
        "availability": video.get("availability"),
        "metadata_source": video.get("metadata_source"),
    }


def batch_item(change):
    """``{"id": …, **patch}`` for ``POST /videos/batch``, or None when in sync."""
    if not change.get("patch"):
        return None
    item = {"id": change["video_id"]}
    item.update(change["patch"])
    return item


def plan_entity_upserts(changes, existing):
    """Which models/studios/categories must be POSTed, and which aliases pushed.

    Only entities that are **new**, or that exist without this provider's external
    id (or with a different one), are upserted. Every POST is a write on the app
    side and refreshes its library view even when it changes nothing, so a no-op
    POST per entity per run is a real cost worth not paying.

    Aliases are held to the same rule. ``existing["aliases"]`` is
    :func:`index_aliases` of ``GET /aliases``; an alias the app already folds onto
    the same canonical name is skipped. Without that index every alias on every
    row would be proposed on every run, and the preview would keep offering rows
    the app already holds.
    """
    existing = existing or {}
    alias_index = existing.get("aliases") or {}
    out = {"models": [], "studios": [], "categories": [], "aliases": []}
    seen = {"models": set(), "studios": set(), "categories": set()}
    alias_seen = set()

    for change in changes or ():
        if not change.get("patch"):
            continue
        for kind in ("models", "studios", "categories"):
            index = existing.get(kind) or {}
            for entity in change["entities"][kind]:
                key = name_key(entity["name"])
                if not key or key in seen[kind]:
                    continue
                seen[kind].add(key)
                current = index.get(key)
                if current and _external_id(current) == str(entity["stashdb_id"]):
                    continue
                out[kind].append({"name": entity["name"],
                                  "stashdb_id": str(entity["stashdb_id"])})
        for alias in change["entities"]["aliases"]:
            key = (alias["kind"], name_key(alias["raw_token"]),
                   name_key(alias["canonical_name"]))
            if key in alias_seen:
                continue
            alias_seen.add(key)
            current = alias_index.get((alias["kind"],
                                       name_key(alias["raw_token"])))
            if current is not None and current == alias["canonical_name"]:
                continue
            out["aliases"].append(alias)
    return out


def chunked(items, size):
    """Split a list into fixed-size chunks. ``POST /videos/batch`` caps at 500."""
    size = int(size)
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    items = list(items or ())
    return [items[start:start + size] for start in range(0, len(items), size)]
