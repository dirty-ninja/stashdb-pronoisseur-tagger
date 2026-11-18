"""StashDB scene lookups, by id or by fingerprint, with a local cache.

``findScene(id)`` fetches the scene whose UUID a folder name carries, so there is
nothing to search for. :meth:`SceneFetcher.by_fingerprints` is the batched
``findScenesBySceneFingerprints`` lookup for files no folder UUID names.

One request per scene, paced by the client's interval, and cached for 30 days so
a re-run costs nothing upstream. A scene that no longer exists is cached as
``not_found`` for the same period: a dead UUID should be asked about once a
month, not once a run.

Nothing here interprets a scene. Reading a scene's studio, performers or tags is
:mod:`tagger.library_sync`'s job, and it is pure.
"""

from . import queries
from .stashdb import StashDBError

DEFAULT_MAX_AGE_DAYS = 30

#: stash-box's **hard cap** on ``findScenesBySceneFingerprints``: more than this
#: many videos in one call and the server refuses the whole query with
#: ``too many scenes``. It is a refusal, not a truncation: every video in an
#: over-large batch is lost.
MAX_FINGERPRINT_BATCH = 40

#: The substring that marks that refusal. Matched rather than parsed because the
#: server sends prose, and the recovery — halve and retry — is only correct for
#: this one error: any other failure is not fixed by asking for less.
TOO_MANY = "too many"


class SceneFetcher:
    def __init__(self, stashdb, library_store, max_age_days=DEFAULT_MAX_AGE_DAYS):
        self.stashdb = stashdb
        self.store = library_store
        self.max_age_days = max_age_days

    def get(self, scene_id, refresh=False):
        scene_id = str(scene_id)
        if not refresh:
            hit = self.store.cached_scene(scene_id, max_age_days=self.max_age_days)
            if hit:
                return {"status": hit["status"], "scene": hit["scene"], "cached": True}

        data = self.stashdb.execute(queries.FIND_SCENE, {"id": scene_id})
        scene = data.get("findScene")
        status = "ok" if scene else "not_found"
        self.store.cache_scene(scene_id, scene, status=status)
        return {"status": status, "scene": scene, "cached": False}

    def get_many(self, scene_ids, refresh=False, progress=None, stats=None):
        """Fetch each distinct id once, in the order first seen.

        ``stats``, when given, is filled with how many of them were asked of
        StashDB (``fetched``) and how many came from a saved copy under
        ``max_age_days`` old (``saved``), so a caller can say which it was.
        """
        unique = list(dict.fromkeys(str(i) for i in scene_ids if i))
        out = {}
        if stats is not None:
            stats.update({"fetched": 0, "saved": 0})
        for index, scene_id in enumerate(unique, start=1):
            out[scene_id] = self.get(scene_id, refresh=refresh)
            if stats is not None:
                stats["saved" if out[scene_id]["cached"] else "fetched"] += 1
            if progress:
                progress(index, len(unique))
        return out

    def _lookup(self, chunk):
        """One fingerprint request, halved and retried when it is refused as
        too big.

        The cap is a server-side refusal, so an over-large batch loses every
        video in it rather than some of them. Halving costs one wasted request
        and recovers the rest, and the halves are concatenated in order, so the
        one-group-per-video alignment the caller relies on survives the retry.

        Below two videos there is nothing left to halve: a "too many" on a
        single video is a server this code does not understand, and guessing
        further would just loop. Every other failure propagates untouched —
        asking for less does not fix a transport error, and pretending it might
        would turn one outage into six requests.
        """
        try:
            data = self.stashdb.execute(queries.FIND_SCENES_BY_SCENE_FINGERPRINTS,
                                        {"fps": chunk})
        except StashDBError as exc:
            if len(chunk) < 2 or TOO_MANY not in str(exc).lower():
                raise
            half = len(chunk) // 2
            return self._lookup(chunk[:half]) + self._lookup(chunk[half:])
        groups = data.get("findScenesBySceneFingerprints") or []
        if len(groups) != len(chunk):
            raise StashDBError(
                "fingerprint lookup returned %d groups for %d videos; the "
                "result is index-aligned, so a mismatch would attach the wrong "
                "scene to a file" % (len(groups), len(chunk)))
        return groups

    def by_fingerprints(self, entries, batch_size=MAX_FINGERPRINT_BATCH,
                        progress=None):
        """Look many videos up at once. Returns one candidate list per input.

        ``entries`` is one inner list of ``{hash, algorithm}`` per video. The
        server answers with one group per entry, in the same order, so the result
        is zipped back onto the caller's videos positionally — a short answer is
        truncation and raises rather than silently mis-aligning identities.

        ``batch_size`` defaults to the server's own cap. A caller may ask for
        less; asking for more is not clamped, because :meth:`_lookup` recovers
        from the refusal anyway and silently rewriting a caller's explicit number
        would hide the fact that it was wrong.
        """
        entries = list(entries or ())
        if not entries:
            return []
        for entry in entries:
            if not entry:
                raise ValueError("every video needs at least one fingerprint")
        if int(batch_size) < 1:
            # range(0, n, 0) raises, but range(0, n, -1) is simply EMPTY: a
            # negative size would return [] for a non-empty input and break the
            # one-list-per-video postcondition the group-count guard below exists
            # to defend. A method that refuses to let the server truncate its
            # answer must not truncate it itself.
            raise ValueError("batch_size must be at least 1; got %r"
                             % (batch_size,))

        out = []
        for start in range(0, len(entries), int(batch_size)):
            chunk = entries[start:start + int(batch_size)]
            out.extend(self._lookup(chunk))
            if progress:
                progress(len(out), len(entries))
        return out
