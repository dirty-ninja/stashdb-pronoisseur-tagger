"""The identify half of the library app: fingerprint, look up, decide."""

import threading

from . import library_sync as sync, matcher, queries
from .library_common import (BUCKETS, DECIDE_ACTIONS, DURATIONS_DISAGREE, IDENTIFIABLE, IDENTIFY_BATCH,
                             LOOKUP_FAILED_NOTE, LibraryStateError, NO_CANDIDATES,
                             PENDING_HASH, REJECTED_NOTE, STILL_IDENTIFYING, WAITING,
                             _note, pending_reason)
from .review_cards import (_candidate_card, _lead_scene, _question_card, _ranked_candidates,
                           _recommendation_safe)
from .stashdb import StashDBError


def _readable_phash(video):
    """The video's phash if Pronoisseur's value parses as hex, else ``None``.

    The same test ``matcher.classify`` applies. A value that fails it is treated
    as absent here, so it is never sent to StashDB and an oshash miss on the row
    is pending hashing rather than a verdict.
    """
    phash = video.get("phash")
    if not phash:
        return None
    try:
        int(str(phash), 16)
    except ValueError:
        return None
    return phash


class IdentifyMixin:
    # ---------------------------------------------------------------- identify

    def start_identify(self):
        """Identify by fingerprint the rows a folder UUID could not.

        Works on the **scan's** rows and never lists Pronoisseur again: the scan
        has already paged every video the Local API exposes, so a second listing
        would double the cost and could disagree with the preview on screen. That is also why there is no client to require here — this run
        talks to StashDB and to the local share, not to the app.
        """
        # Every check is inside the lock, because `start_scan` sets its own
        # running flag inside it: read outside, and a scan starting at the same
        # moment lets both calls pass their guards and start.
        with self._lock:
            if self._scan["running"]:
                raise LibraryStateError(
                    "a scan is running — wait for it to finish: until it lands "
                    "the rows are the previous scan's")
            if not self._rows:
                raise LibraryStateError(
                    "run Scan first — identify works on the videos the scan found")
            if self._identify["running"]:
                return {"started": False, "reason": "already running",
                        "identify": self.identify_state()}
            # The reverse of the order `start_apply` allows. An apply re-plans
            # from the rows after its own round trip, so a run taking up a
            # `likely` row part-way through would drop it from a write the user
            # had already approved.
            if self._apply["running"]:
                raise LibraryStateError(
                    "an apply is writing to Pronoisseur — wait for it to finish, "
                    "then identify")
            # Bound once, here: every index below is a position in *this* list,
            # and `_scan_run` rebinds `self._rows` to a freshly built one. See
            # `_settle`.
            rows = self._rows
            indexes = self._identifiable(rows)
            if not indexes:
                return {"started": False, "reason": "nothing to identify"}
            # Taken up under the lock, before the thread exists, so that no
            # preview or apply can plan a row in the moment between this call
            # returning and the run starting.
            self._stamp_whisparr_claims(rows, indexes)
            self._hold(rows, indexes)
            self._identify_cancel.clear()
            self._lookup_error = None
            self._identify = {"running": True, "label": "remembering decisions",
                              "done": 0, "total": 0, "error": None,
                              "details": None, "finished": None}

        def run():
            outcome = {"running": False, "label": "failed"}
            try:
                self._identify_run(rows, indexes)
                outcome["label"] = ("cancelled" if self._identify_cancel.is_set()
                                    else "done")
            except Exception as exc:                        # noqa: BLE001
                outcome["error"] = str(exc)
            finally:
                # A cancel, a crash, or a scan that orphaned the list: whatever
                # the run did not reach goes back exactly as the scan left it.
                # BEFORE the run says it has stopped: a second Identify started
                # in between would take the same rows up, and this would then
                # hand them back from under it.
                self._release(rows, indexes)
                self._identify.update(outcome)

        self._identify_thread = threading.Thread(target=run, daemon=True)
        try:
            self._identify_thread.start()
        except RuntimeError as exc:      # can't start a thread: clear the flag
            self._release(rows, indexes)
            self._identify.update({"running": False, "label": "failed",
                                   "error": str(exc)})
            raise
        return {"started": True, "identify": self.identify_state()}

    def cancel_identify(self):
        """Ask the run to stop. It checks between batches.

        No finer than that, because Python cannot kill a thread: a request
        already with StashDB is answered first.
        """
        self._identify_cancel.set()
        return self.identify_state()

    def await_identify(self, timeout=60):
        """Join the identify thread. Tests use this; the UI polls instead."""
        if self._identify_thread:
            self._identify_thread.join(timeout)

    def identify_state(self):
        """Everything the match panel renders itself from, in one read.

        camelCase throughout, as in :meth:`state`. The counts and the questions
        are over the rows an identify run actually touched, so a library that is
        mostly named by folder UUID does not drown the panel in its own successes.
        A row the run in flight has not reached yet is counted as waiting and is
        never a question: it has no answer of its own to give until it is settled.
        """
        state = dict(self._identify)
        counts = {bucket: 0 for bucket in BUCKETS}
        questions = []
        # Read once per poll, not once per row: every match on every question
        # is planned against them, so each one says what picking it would write.
        settings, existing = self._plan_settings(), self._plan_existing()
        for row in self._rows:
            if row.get("pending"):
                counts[WAITING] += 1
                continue
            if not row.get("from_identify"):
                continue
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if row["status"] == matcher.AMBIGUOUS:
                questions.append(_question_card(row, settings, existing))
        state["counts"] = counts
        state["lookupError"] = self._lookup_error
        state["questions"] = questions
        return state

    def decide(self, video_id, scene_id=None, action="pick"):
        """Record the user's answer to one question, and remember it.

        ``pick`` may name a scene the row never had as a candidate: the assisted
        search finds scenes no fingerprint pointed at. ``clear`` is the escape
        hatch — without it a mistaken pick would be permanent.
        """
        # A scan in flight is refused: `_scan_run` **rebinds** `self._rows`, so
        # the row this settles is about to be discarded whole.
        #
        # An identify run in flight is not, row by row. The run never touches a
        # row again once it has settled it, so an answer to a settled row stands;
        # a row it has not settled yet is refused, because its lookup would
        # overwrite the answer the moment it came back.
        self._refuse_while_scanning()
        rows = self._rows          # bound once: a scan may replace the list
        index = self._row_index(rows, video_id)
        if rows[index].get("pending"):
            raise LibraryStateError(STILL_IDENTIFYING)
        if action not in DECIDE_ACTIONS:
            raise LibraryStateError(
                "unknown action %r — %s" % (action, ", ".join(DECIDE_ACTIONS)))
        scene = None
        if action == "pick":
            if not scene_id:
                raise LibraryStateError("picking a scene needs a scene id")
            # Fetched before the lock: nothing may hold it across network I/O.
            result = self.scenes.get(scene_id)
            if result["status"] != "ok" or not result["scene"]:
                raise LibraryStateError(
                    "StashDB has no scene with id %s" % (scene_id,))
            scene = result["scene"]

        # Checked again, and settled, under the lock `start_identify` takes rows
        # up under. A run can start during the fetch above; without this the
        # answer would land on a row that run is about to re-decide.
        with self._lock:
            self._refuse_while_scanning()
            row = rows[index]
            if row.get("pending"):
                raise LibraryStateError(STILL_IDENTIFYING)
            content_hash = row["video"].get("content_hash")
            unremembered = self._unremembered(row)
            if action == "pick":
                self._record_pick(rows, index, scene,
                                  _note("you picked this scene by hand",
                                        unremembered))
            elif action == "reject":
                if content_hash:
                    self.library_store.record_decision(content_hash, None,
                                                       "rejected")
                self._settle(rows, index, status=matcher.UNMATCHED, scene=None,
                             scene_id=None, confidence=None, candidates=[],
                             note=_note(REJECTED_NOTE, unremembered))
            else:
                if content_hash:
                    self.library_store.clear_decision(content_hash)
                self._settle(rows, index, status=matcher.UNMATCHED, scene=None,
                             scene_id=None, confidence=None, candidates=[],
                             note="decision cleared — the next identify run will "
                                  "ask about this file again")
        return self.identify_state()

    def _refuse_while_scanning(self):
        if self._scan["running"]:
            raise LibraryStateError(
                "a scan is running — wait for it to finish: until it lands the "
                "rows are the previous scan's")

    @staticmethod
    def _unremembered(row):
        """The caveat an answer carries when the row cannot be remembered, or "".

        A row Pronoisseur gave no content hash can still be answered; it just
        cannot be written down, because the decision memory is keyed by that
        hash. Saying so on the row is the only warning the user gets that this
        one answer will be asked again after the next scan.
        """
        return ("" if row["video"].get("content_hash") else
                "Pronoisseur has no content hash for this file, so this answer "
                "lasts only until the next scan")

    def _record_pick(self, rows, index, scene, note):
        """Settle one row on a chosen scene, and remember the choice.

        The single place a pick is written down. :meth:`decide` and
        :meth:`accept_recommended` both come through here, so accepting a whole
        list of recommendations at once is remembered *exactly* as clicking one cover is
        — same table, same ``manual`` source, same settled row — and the two
        cannot drift into recording different things.
        """
        content_hash = rows[index]["video"].get("content_hash")
        if content_hash:
            self.library_store.record_decision(content_hash, scene["id"], "manual")
        self._settle(rows, index, status=matcher.VERIFIED, scene=scene,
                     scene_id=scene.get("id"), confidence=matcher.BY_MANUAL,
                     candidates=[], note=note)

    def accept_recommended(self, model=None, video_ids=None):
        """Take the recommended scene for every ambiguous row it is safe to.

        ``model`` narrows it to one of Review's model groups: a
        :func:`library_sync.name_key`, matched against the same
        :func:`library_sync.review_models` the group was drawn from, and ``""``
        for "No model". ``None`` is every row.

        ``video_ids`` narrows it to those videos: the rows to choose that the
        page's tabs and filename filter show, so the button takes what the user
        can see and nothing else. ``None`` is every row; an empty list is none.
        It only ever narrows - whether a row is safe is still decided here - and
        the page sends the unsafe rows in view too, so the ones left are counted
        with their reasons.

        Ambiguous rows are often the same click repeated: the grid has already
        ranked them, the leading card is the one the user would pick, and the work
        left is repetition rather than judgement. This does the repetition.

        What it will not do is answer the rows that need a person. A row is taken
        only when :func:`_recommendation_safe` says so — the scene runs the same
        length as the file, or the bytes match exactly and StashDB carries no
        duration to check. Everything else is left precisely as it was, `ambiguous`
        and un-remembered, with its reason counted so the list that remains can
        explain itself.

        The pass is over the same rows :meth:`identify_state` puts on screen, by
        the same two tests, so the count on the button and the rows in the list
        can never be two different populations.
        """
        # The same two rules :meth:`decide` keeps, over every row this touches: a
        # scan in flight rebinds the list whole, so it is refused; a row an
        # identify run still owns is skipped, not refused, because it is not a
        # question yet and the rows the run has finished with are. Under the
        # lock `start_identify` takes rows up under, for the reason `decide`
        # gives; nothing in here touches the network.
        rows = self._rows          # bound once: a scan may replace the list
        accepted, reasons = 0, {}
        wanted = None if video_ids is None else set(video_ids)
        # Read before the lock: on a cold cache this is four GETs, and nothing
        # may hold the lock across network I/O.
        models_index = ((self._plan_existing() if model is not None else {})
                        .get("models") or {})

        def leave(reason):
            reasons[reason] = reasons.get(reason, 0) + 1

        def in_group(row, pairs):
            keys = [m["key"] for m in sync.review_models(
                row["video"], _lead_scene(pairs), models_index)]
            return model in keys if model else not keys

        with self._lock:
            self._refuse_while_scanning()
            for index, row in enumerate(rows):
                if row.get("pending") or not row.get("from_identify") \
                        or row["status"] != matcher.AMBIGUOUS:
                    continue
                if wanted is not None and row["video"]["id"] not in wanted:
                    continue
                pairs = _ranked_candidates(row)
                if model is not None and not in_group(row, pairs):
                    continue
                cards = [card for card, _ in pairs]
                if not cards or not cards[0].get("sceneId"):
                    leave(NO_CANDIDATES)
                    continue
                if not _recommendation_safe(cards):
                    leave(DURATIONS_DISAGREE)
                    continue
                self._record_pick(
                    rows, index, pairs[0][1]["scene"],
                    _note("you accepted the recommended scene",
                          self._unremembered(row)))
                accepted += 1
        return {"accepted": accepted, "left": sum(reasons.values()),
                "leftReasons": reasons}

    def assisted_search(self, video_id, title=None, performers=None, page=1,
                        per_page=20):
        """Search StashDB on what one row's filename says, for a human to judge.

        The last resort. Every fingerprint missed, or the user rejected
        what they were shown, so the only thing left to search on is what
        Pronoisseur itself parsed out of the path. **Nothing here is ever
        auto-selected**: the results are the same cover grid the questions use,
        the user picks, and that pick lands in :meth:`decide` as a `manual`
        answer.

        ``title`` and ``performers`` override the parsed values — the "let me fix
        the search box" path, because the parse is a guess and the user can see
        the file. ``performers`` is a list of names; a name StashDB does not know
        is dropped from the filter and reported in ``unresolved``, since silently
        searching without it would explain nothing when the results are wrong.

        ``queryScenes`` answers with :data:`tagger.queries.SCENE_FIELDS`, which is
        **narrower** than ``SCENE_DETAIL_FIELDS``: no fingerprints, no parent
        studio, no performer aliases. That is everything a cover card needs, and
        it is why ``decide(action="pick")`` re-fetches the scene through
        ``SceneFetcher.get`` rather than trusting what this search returned. Keep
        that re-fetch.
        """
        rows = self._rows          # bound once: a scan may replace the list
        video = rows[self._row_index(rows, video_id)]["video"]
        # The overrides are applied to the video, not to the terms afterwards,
        # because `build_assisted_query` parses the video itself: patching its
        # output would leave the query and the terms shown beside it disagreeing.
        if isinstance(title, str) and title.strip():
            video = dict(video, title=title.strip())
        if performers is not None:
            video = dict(video, models=[{"name": name} for name in performers])
        # The names have to be ids before the query can carry them, and
        # `assisted_terms` is pure, so reading them here costs nothing.
        resolved, unresolved = self._resolve_performers(
            matcher.assisted_terms(video)["performers"])
        query = matcher.build_assisted_query(video, performer_ids=resolved)
        if isinstance(title, str) and not title.strip():
            # An emptied box searches with **no** title: what the user typed is
            # what gets searched, and an empty performers list already clears
            # performers, so the two boxes had better behave alike. This is the
            # one override that cannot ride on the video, because
            # `assisted_terms` re-derives a title from `rel_path` whenever the
            # video has none — precisely the value the user has just cleared.
            # Both halves are cleared together, so the query and the terms shown
            # beside it still agree.
            query["terms"]["title"] = None
            query["filter"].pop("title", None)
        found = self.stashdb.page(
            queries.QUERY_SCENES,
            {"input": queries.build_scene_input(
                query["filter"], performer_ids=query["performer_ids"])},
            "queryScenes", "scenes", page=page, per_page=per_page)
        ranked = matcher.rank_assisted(found["rows"], query["date_window"])
        return {"videoId": video.get("id"), "terms": query["terms"],
                "dateWindow": query["date_window"], "unresolved": unresolved,
                "count": found["count"], "page": found["page"],
                "perPage": found["perPage"],
                # The card the questions grid already renders, plus the one fact
                # only this search knows. Its fingerprint keys come out None:
                # there was no fingerprint, which is why the search exists.
                "candidates": [dict(_candidate_card(entry),
                                    dateMatch=entry["date_match"])
                               for entry in ranked]}

    def _resolve_performers(self, names):
        """StashDB ids for performer names, and the names nothing answered to.

        One ``queryPerformers`` request per name, first result wins: the picker
        is the user, and this only has to get the search close enough to judge.

        Resolved through StashDB itself, so the lookup needs no local catalogue.
        """
        resolved, unresolved = [], []
        for name in names or ():
            found = self.stashdb.page(
                queries.QUERY_PERFORMERS,
                {"input": queries.build_performer_input(name)},
                "queryPerformers", "performers", page=1, per_page=1)
            first = (found["rows"] or [{}])[0] or {}
            if first.get("id"):
                resolved.append(first["id"])
            else:
                unresolved.append(name)
        return resolved, unresolved

    # -------------------------------------------------------- identify internals

    def _identifiable(self, rows):
        """Indexes of the rows an identify run may work on, in row order."""
        return [index for index, row in enumerate(rows)
                if row["status"] in IDENTIFIABLE]

    def _row_index(self, rows, video_id):
        """Where ``video_id`` sits in ``rows``; a state error if nowhere."""
        try:
            wanted = int(video_id)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for index, row in enumerate(rows):
                if row["video"].get("id") == wanted:
                    return index
        raise LibraryStateError(
            "no video %r in this scan — re-scan and try again" % (video_id,))

    def _settle(self, rows, index, **fields):
        """Replace one row with a settled copy, in a single assignment.

        Two hazards, and ``rows`` is a parameter because of the second.

        A poll walks the rows from the request thread while a run writes to them.
        Mutating a row in place would let it see ``status='verified'`` with
        ``scene`` still ``None`` and crash inside ``plan_change``; assigning one
        list slot is atomic under the GIL, so a reader sees the whole old row or
        the whole new one and never a half of each.

        And an index is only meaningful against the list it was taken from.
        ``_scan_run`` **rebinds** ``self._rows`` to a freshly built list, so a run
        that re-read ``self._rows`` on every write could, mid-flight, settle
        position 7 of a *different* scan — attaching one video's scene to another,
        caching a fingerprint under the wrong ``content_hash``, and recording a
        wrong identity that every later run would honour. So the caller binds the
        list once and threads it through: a concurrent scan then orphans this
        run's list instead of corrupting the new one.

        Settling is also the moment a row stops waiting. Every caller settles a
        row exactly once per run, and that is the promise the page relies on to
        let Review and Apply work while the run goes on: once settled, a row is
        the user's, and the run never comes back to it.
        """
        row = dict(rows[index], from_identify=True, **fields)
        row.pop("pending", None)
        rows[index] = row
        return row

    @staticmethod
    def _hold(rows, indexes):
        """Take up every row a run is about to work on, before it touches one.

        A held row is planned as nothing and answers no question (see
        ``_changes`` and :meth:`decide`) until :meth:`_settle` hands it back.
        Its own status is left alone, so :meth:`_release` can return it exactly.
        """
        for index in indexes:
            rows[index] = dict(rows[index], pending=True)

    @staticmethod
    def _release(rows, indexes):
        """Hand back every row the run took up and never settled, unchanged."""
        for index in indexes:
            if rows[index].get("pending"):
                row = dict(rows[index])
                row.pop("pending", None)
                rows[index] = row

    def _identify_run(self, rows, indexes):
        """Settle every held row once, from the fingerprints Pronoisseur served.

        Nothing is computed here: each video arrived with its ``oshash`` and
        ``phash`` (either may be absent), and one lookup per batch carries every
        value a video has. ``rows`` and ``indexes`` are bound by
        :meth:`start_identify`, which has already taken the rows up; its thread
        hands back whatever this leaves.
        """
        settings = self.library_store.get_settings()
        tolerance = float(settings["duration_tolerance"])
        failures = set()
        remaining = self._remember(rows, indexes)
        if remaining and not self._identify_cancel.is_set():
            self._lookup_pass(rows, remaining, tolerance, failures)
        self._identify["finished"] = self._identify_summary(rows, indexes,
                                                            failures)

    def _stamp_whisparr_claims(self, rows, indexes):
        """Keep the folder's UUID before the passes overwrite ``scene_id``.

        A `uuid-mismatch` row's ``scene_id`` is what Whisparr claimed; identify
        replaces it with the scene the bytes actually are. The claim is half of
        the disagreement the user fixes in Whisparr by hand, so it is kept under
        its own key first.
        """
        for index in indexes:
            row = rows[index]
            if row["status"] == sync.UUID_MISMATCH:
                rows[index] = dict(row, whisparr_scene_id=row["scene_id"])

    def _remember(self, rows, indexes):
        """Settle every row a previous run or the user already decided.

        Returns the indexes still to look up. A remembered row costs no
        fingerprint and no lookup, which is the whole point of the memory — but a
        decision pointing at a scene StashDB has since deleted is not an answer,
        so that row goes back into the passes and says why. The decision itself is
        left alone: the user may have made it by hand.
        """
        self._identify.update({"label": "remembering decisions", "done": 0,
                               "total": len(indexes)})
        wanted = [str(rows[i]["video"]["content_hash"]) for i in indexes
                  if rows[i]["video"].get("content_hash")]
        decided = self.library_store.decisions(wanted) if wanted else {}

        remaining = []
        for done, index in enumerate(indexes, start=1):
            if self._identify_cancel.is_set():
                return []
            self._identify.update({"done": done})
            row = rows[index]
            content_hash = row["video"].get("content_hash")
            decision = decided.get(str(content_hash)) if content_hash else None
            if not decision:
                remaining.append(index)
            elif decision["source"] == "rejected":
                self._settle(rows, index, status=matcher.UNMATCHED, scene=None,
                             scene_id=None, confidence=None, candidates=[],
                             note=REJECTED_NOTE)
            elif not self._settle_remembered(rows, index, decision):
                remaining.append(index)
        return remaining

    def _settle_remembered(self, rows, index, decision):
        """Settle one remembered row. False when the row has to be looked up again."""
        row = rows[index]
        result = self.scenes.get(decision["scene_id"])
        if result["status"] != "ok" or not result["scene"]:
            rows[index] = dict(
                row, note=_note(row.get("note"),
                                "the scene remembered for this file is no longer "
                                "on StashDB — identifying it again"))
            return False
        scene = result["scene"]
        by_hand = decision["source"] == "manual"
        confidence = self._remembered_confidence(decision, row, scene)
        note = ("you picked this scene by hand on an earlier run" if by_hand
                else "identified by fingerprint on an earlier run")
        # A remembered *fingerprint* answer with no confidence is one the
        # fingerprints no longer support: either StashDB has stopped listing this
        # file's hashes on that scene, or Pronoisseur has none for it. The row is
        # still settled `verified` — the earlier run's answer stands until
        # something contradicts it, and a remembered row is never pending — but
        # settling it silently would leave the one row whose evidence has
        # evaporated looking exactly like the ones whose has not. Both causes are
        # named because this cannot tell them apart.
        if not by_hand and confidence is None:
            note = _note(note, "the fingerprints no longer corroborate it — "
                               "StashDB does not list this file's fingerprints on "
                               "that scene, or Pronoisseur has no fingerprint for "
                               "it")
        self._settle(rows, index, status=matcher.VERIFIED, scene=scene,
                     scene_id=scene.get("id"), candidates=[],
                     confidence=confidence, note=note)
        return True

    def _remembered_confidence(self, decision, row, scene):
        """Which arm settled a remembered row, for the preview's confidence column.

        ``match_decisions`` stores *why* a row was settled — fingerprint,
        manual or rejected — never *which* fingerprint hit, and that table is
        pinned by its own tests. So a manual decision is ``BY_MANUAL`` outright,
        and a fingerprint one is re-derived from the video's own fingerprints
        against the scene just fetched: pure, exact, and honest about which arm
        matched. Answering ``BY_OSHASH`` on faith would be a guess, and ``None``
        would read in the preview as "identified by its folder UUID", which it was
        not — so ``None`` is left for the one case that really is unknown, a
        video Pronoisseur has no fingerprint for.
        """
        if decision["source"] == "manual":
            return matcher.BY_MANUAL
        video = row["video"]
        try:
            match = matcher.candidate_matches(scene, video.get("oshash"),
                                              video.get("phash"))
        except ValueError:               # a corrupt cached phash: simply unknown
            return None
        return match["kind"] if match else None

    def _lookup_pass(self, rows, indexes, tolerance, failures):
        """Look every row up in one pass, a batch at a time.

        A row with neither value has nothing to look up and is settled as
        pending hashing at once. The rest go through :meth:`_match_pass` in
        chunks of :data:`IDENTIFY_BATCH`; the cancel flag is checked between
        chunks, and a cancelled run hands back what it did not reach.
        """
        self._identify.update({"label": "looking up", "done": 0,
                               "total": len(indexes)})
        reason = pending_reason(self._fingerprints)
        lookups, done = [], 0
        for index in indexes:
            video = rows[index]["video"]
            if video.get("oshash") or _readable_phash(video):
                lookups.append(index)
                continue
            self._settle_pending(rows, index, reason)
            done += 1
        self._identify.update({"done": done})
        for start in range(0, len(lookups), IDENTIFY_BATCH):
            if self._identify_cancel.is_set():
                return
            chunk = lookups[start:start + IDENTIFY_BATCH]
            self._match_pass(rows, chunk, tolerance, failures, reason)
            done += len(chunk)
            self._identify.update({"done": done})

    def _settle_pending(self, rows, index, reason):
        """Settle a row Pronoisseur has not fingerprinted enough to identify.

        Not `unmatched`: nothing has said StashDB lacks this file, only that
        there was nothing (or too little) to ask with. It stays identifiable, so
        the next run takes it up again.
        """
        video = rows[index]["video"]
        self._settle(rows, index, status=PENDING_HASH, scene=None, scene_id=None,
                     confidence=None, candidates=[], note=reason,
                     oshash=video.get("oshash") or None,
                     phash=_readable_phash(video))

    def _match_pass(self, rows, indexes, tolerance, failures, reason):
        """Look one batch up, classify each row, settle each.

        A ``StashDBError`` fails **that batch**, not the run: one bad request
        late in a big library must not throw away the rows already identified.
        The rows in the failed batch keep the status they arrived with, gain a
        note saying what the server said, and are counted as failures on the
        run's summary. They are never pending: the lookup failed, the video was
        not found missing.

        Nothing is guessed to achieve that. ``by_fingerprints`` raises rather
        than returning a short or re-ordered answer, so a caught error means no
        groups came back at all — a failed batch attaches nothing to anything.
        """
        entries = []
        for index in indexes:
            video = rows[index]["video"]
            entry = []
            # An arm whose value is missing is omitted rather than sent empty;
            # the caller only passes rows with at least one.
            if video.get("oshash"):
                entry.append({"hash": video["oshash"], "algorithm": "OSHASH"})
            if _readable_phash(video):
                entry.append({"hash": video["phash"], "algorithm": "PHASH"})
            entries.append(entry)
        try:
            groups = self.scenes.by_fingerprints(entries, batch_size=IDENTIFY_BATCH)
        except StashDBError as exc:
            self._lookup_error = self._lookup_error or str(exc)
            failed = "%s: %s" % (LOOKUP_FAILED_NOTE, exc)
            for index in indexes:
                failures.add(index)
                self._settle(rows, index,
                             note=_note(rows[index].get("note"), failed))
            return
        for index, group in zip(indexes, groups):
            video = rows[index]["video"]
            self._verdict(rows, index, group, video.get("oshash") or None,
                          _readable_phash(video), tolerance, reason)

    def _verdict(self, rows, index, candidates, oshash, phash, tolerance, reason):
        """Classify one row against its candidate group, and settle it.

        An `unmatched` verdict on a row with no phash is settled as pending
        hashing instead, with ``reason``: StashDB not knowing an oshash says
        nothing while Pronoisseur has yet to supply the other fingerprint.
        """
        verdict = matcher.classify(candidates, oshash, phash,
                                   file_duration=rows[index]["video"].get(
                                       "duration_secs"),
                                   tolerance=tolerance)
        if verdict["status"] == matcher.UNMATCHED and not phash:
            self._settle_pending(rows, index, reason)
            return
        self._settle_verdict(rows, index, verdict, oshash, phash)

    def _settle_verdict(self, rows, index, verdict, oshash, phash):
        """Settle one row on a classified verdict.

        A `verified` row is remembered, so a re-run never re-asks, and its scene
        cached. `likely`, `ambiguous` and `unmatched` record nothing: they are
        questions, not answers.
        """
        matched = verdict["candidates"]
        # `ambiguous` and `unmatched` have no scene to write against; `verified`
        # and `likely` have exactly one.
        scene = (matched[0]["scene"]
                 if matched and verdict["status"] in (matcher.VERIFIED,
                                                      matcher.LIKELY) else None)
        scene_id = (scene or {}).get("id")
        row = self._settle(rows, index, status=verdict["status"], scene=scene,
                           scene_id=scene_id, confidence=verdict["confidence"],
                           candidates=matched,
                           note=verdict["note"], oshash=oshash, phash=phash)
        if verdict["status"] != matcher.VERIFIED or not scene_id:
            return
        self.library_store.cache_scene(scene_id, scene)
        content_hash = row["video"].get("content_hash")
        if content_hash:
            self.library_store.record_decision(content_hash, scene_id,
                                               "fingerprint")

    def _identify_summary(self, rows, indexes, failures):
        """What the run did, in the numbers that matter.

        ``failed`` counts rows whose lookup StashDB refused and cuts **across**
        the status counts rather than adding to them: such a row keeps whatever
        status it arrived with, and its note says why. ``phashes`` is how many
        considered rows carried a perceptual hash from Pronoisseur, which says
        how much of the library the lookups could use. A row a cancelled run
        never reached is in none of the status counts: its status is still the
        one it arrived with, not an answer.
        """
        counts = {status: 0 for status in (matcher.VERIFIED, matcher.LIKELY,
                                           matcher.AMBIGUOUS, matcher.UNMATCHED,
                                           PENDING_HASH)}
        for index in indexes:
            if rows[index].get("pending"):
                continue
            status = rows[index]["status"]
            if status in counts:
                counts[status] += 1
        phashes = sum(1 for index in indexes
                      if _readable_phash(rows[index]["video"]))
        return dict(counts, considered=len(indexes), failed=len(failures),
                    phashes=phashes)
