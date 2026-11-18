"""Review cards: the shape of a file and its candidates as the grid renders them."""

from . import library_sync as sync, matcher
from .library_common import _listed


def _candidate_card(candidate):
    """One classified candidate, flattened to what a cover grid needs.

    **Every card is a StashDB scene**, never the file on disk — the file is the
    question block's own header. The counts are here because that is the choice
    the user is actually making when one fingerprint fits more than one scene:
    the richer scene carries the categories and models a thinner duplicate does
    not, and nothing else on the card says which is which.

    The whole StashDB scene dict is deliberately **not** shipped: hundreds of rows
    times a full scene payload is a real cost, and the grid reads none of it.
    """
    scene = candidate.get("scene") or {}
    return {"sceneId": scene.get("id"), "title": scene.get("title"),
            "date": scene.get("release_date"), "duration": scene.get("duration"),
            "studio": (scene.get("studio") or {}).get("name"),
            "tagCount": _listed(scene.get("tags")),
            "performerCount": _listed(scene.get("performers")),
            # The preview's own cover comes from `library_sync`; one picker for
            # both, so a scene never shows a different image in the two grids.
            "cover": sync._cover(scene),
            "distance": candidate.get("distance"), "kind": candidate.get("kind"),
            "durationDelta": candidate.get("duration_delta"),
            "durationAgrees": candidate.get("duration_agrees")}


def _candidate_tier(card):
    """Which evidence tier a candidate sits in. Lower is stronger.

    Four tiers, out of the two facts a picker weighs first and in this order: do
    the bytes match, and does it run the same length. Identical bytes outrank a
    perceptual near-miss — a near-duplicate encode of a *different* scene is a
    real thing — and inside either, a length that agrees outranks one that does
    not.

    ``durationAgrees`` is ``None`` for a scene StashDB carries no duration for,
    and unknown is grouped with disagreeing rather than with agreeing. That is
    deliberate: the leading card is recommended to the user, and a recommendation
    has to be earned by evidence rather than by the absence of it.
    """
    exact = card.get("kind") == matcher.BY_OSHASH
    agrees = card.get("durationAgrees") is True
    return (0 if exact else 2) + (0 if agrees else 1)


def _tag_key(card):
    """Sort key for "the richer scene first". Lower is richer.

    The tie-break that matters when a fingerprint fits more than one scene:
    StashDB can carry the same film twice, once catalogued and once bare, and the
    bare listing would cost the user the categories this tool exists to import.

    ``tagCount`` is ``None`` when the lookup never asked for tags — see
    :func:`_listed` — and that sorts **below** a scene genuinely listing none.
    Unknown is not zero, and a scene nobody has counted must not out-rank one
    counted at zero on the strength of the question never having been put.
    """
    count = card.get("tagCount")
    return -(count if count is not None else -1)


def _candidate_rank(card):
    """Sort key for the review grid: the strongest fit first.

    Tier first, so evidence still beats richness: a byte-exact match listing no
    tags at all stays ahead of a perceptual near-miss carrying forty. Only
    **inside** a tier — where the fingerprints and the durations say the same
    thing about both — does the tag count decide, and there the richer scene
    wins.

    Ties after that go to the smaller duration gap, then to the closer perceptual
    distance. A missing gap sorts last inside its tier, for the same reason it
    does not win a tier: nothing is known about it.
    """
    delta = card.get("durationDelta")
    distance = card.get("distance")
    return (_candidate_tier(card), _tag_key(card),
            abs(float(delta)) if delta is not None else float("inf"),
            float(distance) if distance is not None else 0.0)


def _tags_decided(cards):
    """Whether the tag count is what put the leading card in front.

    True only when the runner-up shares the leader's tier — same fingerprint
    arm, same duration verdict, so nothing else had separated them — and lists
    fewer tags. It exists for the badge: telling the user a pick was made on tags
    when it was actually made on the bytes would teach them to distrust the badge
    the first time they checked it.
    """
    if len(cards) < 2:
        return False
    lead, runner = cards[0], cards[1]
    return (_candidate_tier(lead) == _candidate_tier(runner)
            and _tag_key(lead) < _tag_key(runner))


def _ranked_candidates(row):
    """``(card, candidate)`` pairs, strongest first, the leader marked.

    The raw candidate rides alongside its card because two callers need different
    halves of one ranking and must never rank twice: :func:`_question_card` ships
    the cards to the browser, while :meth:`LibraryApp.accept_recommended` needs
    the **scene** off the winner to settle the row with. A second sort in the
    second caller is exactly how the button comes to accept a different scene
    from the one the grid recommended.
    """
    pairs = sorted(((_candidate_card(c), c) for c in row.get("candidates") or ()),
                   key=lambda pair: _candidate_rank(pair[0]))
    tags_decided = _tags_decided([card for card, _ in pairs])
    for position, (card, _) in enumerate(pairs):
        card["recommended"] = position == 0
        card["recommendedByTags"] = position == 0 and tags_decided
    return pairs


def _recommendation_safe(cards):
    """Whether the leading card may be accepted without a person looking at it.

    The gate on "Accept all recommended", and the reason that button is safe to
    press over a whole list at once. The length has to agree — the one check that
    catches a near-duplicate encode of a *different* scene, which is precisely
    what the perceptual arm can hand back.

    The single exception is a byte-exact match on a scene StashDB carries no
    duration for. The bytes have already settled that row; refusing it on a
    length StashDB simply never recorded would push the strongest evidence there
    is back onto the user's pile.

    A leader with no scene id is refused outright. There is nothing there to
    record, and :meth:`LibraryApp.accept_recommended` counts it as having no
    candidates rather than as a disagreement about length it never had.
    """
    if not cards or not cards[0].get("sceneId"):
        return False
    top = cards[0]
    if top["durationAgrees"] is True:
        return True
    return top["kind"] == matcher.BY_OSHASH and top["durationAgrees"] is None


def _lead_scene(pairs):
    """The scene the grid recommends, or None: the leader of the one ranking."""
    return (pairs[0][1].get("scene") or None) if pairs else None


def _question_card(row, settings=None, existing=None):
    """One row the user has to answer, camelCase, with its candidates.

    ``models`` is what the row is grouped under in Review, from
    :func:`library_sync.review_models` over the video and the **leading**
    scene. With ``settings``, every card also carries ``fields``: what picking
    that scene would write, from the same :func:`library_sync.plan_change` the
    write preview uses, so the badges on a match and on the write it turns into
    can never disagree.

    ``terms`` and ``dateWindow`` are what the assisted search *would* search on,
    read off the row by the same pure functions the search itself calls. They
    ship with the card so the panel can show the parsed studio and date, and
    pre-fill the Performers box, **before** the first search rather than only
    after it — and they are the same two keys ``assisted_search`` answers with,
    so the browser renders both with one renderer instead of two.

    ``path`` is the **absolute** file path. A library-relative path over a row of
    scene covers would read as one more candidate, and the user could not tell
    which entry is the file on their disk. It is the file; the candidates are all
    StashDB.

    The candidate order and the three flags below it are decided here rather than
    in the browser so that one rule governs the grid: ``_candidate_rank`` puts
    the strongest fit first and ``recommended`` marks it, ``recommendationSafe``
    says whether that recommendation is strong enough to be accepted in bulk, and
    ``noDurationMatch`` says that **no** candidate's length matches the file — a
    different fact from the recommendation being unsafe, and the one worth a
    warning across the whole block.
    """
    video = row["video"]
    terms = matcher.assisted_terms(video)
    pairs = _ranked_candidates(row)
    cards = [card for card, _ in pairs]
    existing = existing or {}
    if settings is not None:
        for card, candidate in pairs:
            # A match with no scene id cannot be picked, so there is no write
            # to describe: planning it would show the changes of picking nothing.
            scene = candidate.get("scene") or {}
            card["fields"] = (sync.plan_change(video, scene, settings=settings,
                                               existing=existing)["fields"]
                              if scene.get("id") else [])
    return {"videoId": video.get("id"), "relPath": video.get("rel_path"),
            "models": sync.review_models(video, _lead_scene(pairs),
                                         existing.get("models") or {}),
            "path": video.get("path"),
            "title": video.get("title"),
            "durationSecs": video.get("duration_secs"), "note": row.get("note"),
            # Present only for a row that arrived as `uuid-mismatch`, so the grid
            # can say what Whisparr claimed beside what the bytes say.
            "whisparrSceneId": row.get("whisparr_scene_id"),
            "terms": terms, "dateWindow": matcher.date_window(terms["date"]),
            "noDurationMatch": bool(cards) and not any(
                card["durationAgrees"] is True for card in cards),
            "recommendationSafe": _recommendation_safe(cards),
            "candidates": cards}
