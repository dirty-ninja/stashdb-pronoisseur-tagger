"""The write half of the library app: preview, plan and apply."""

import threading

from . import library_sync as sync, matcher
from .library_common import (IN_SYNC, LibraryStateError, OPT_IN_STATUSES, STALE_NOTE, UNPLANNABLE,
                             WAITING, _note)
from .pronoisseur import MAX_PAGE_SIZE, PronoisseurError


class ApplyMixin:
    # ----------------------------------------------------------------- preview

    def _existing(self, refresh=False, tolerant=False):
        """The library's model, studio and category names and its aliases, memoised.

        ``scan_state`` and ``preview`` both recompute every diff on every read, and
        the UI polls them about once a second while a run is going. Without this
        cache that is four ``GET`` requests per poll, each of which the app answers
        with a video count for every entry. Invalidated when a scan starts and after an
        apply — the only two moments the answer can change.

        That last sentence is only true because :meth:`_refresh_written` re-reads
        the videos a run wrote. This index says what the library's *entities* are;
        the videos themselves live on ``self._rows``, and leaving those stale after
        an apply is what stopped the preview converging.

        ``aliases`` is :func:`library_sync.index_aliases`, not an entity index —
        ``plan_entity_upserts`` reads it to skip alias rows the app already holds,
        and nothing else looks at it.

        ``tolerant`` is for the read paths only. A poll that exists to report
        "scan running" or "scan failed" must not itself throw because Pronoisseur
        was closed mid-run — four GETs behind a status read would take the whole
        page down. It falls back to the last-known index, or ``{}``, which
        ``plan_change`` already handles. The apply path never passes it: there,
        resolving names against a stale or empty index is exactly the bug that
        forks an entity, so the error must surface.
        """
        # Built into a local and returned from the local, never re-read off the
        # attribute: `start_scan` and `_apply_run`'s `finally` both set
        # `_existing_cache = None` from other threads, so a caller that passes
        # the `if` below can otherwise be handed `None` by the return. The apply
        # path would then plan against an EMPTY index — every model and studio
        # proposed as new, which is the case-sensitive entity fork `_apply_run`'s
        # docstring exists to prevent, and `plan_change` absorbs `{}` silently.
        cache = self._existing_cache
        if refresh or cache is None:
            client = self._require_client()
            try:
                cache = {
                    "models": sync.index_by_name(client.models()),
                    "studios": sync.index_by_name(client.studios()),
                    "categories": sync.index_by_name(client.categories()),
                    "aliases": sync.index_aliases(client.aliases())}
            except PronoisseurError:
                if not tolerant:
                    raise
                # Same reason: the local snapshot first, so the fallback cannot
                # be turned into `{}` by an invalidation racing this read.
                return cache or self._existing_cache or {}
            self._existing_cache = cache
        return cache

    def _plan_settings(self):
        """The settings every plan is made under, the tag map folded in.

        One reader for the write preview and for Review's matches, so a match
        says exactly what the write it turns into will do.
        """
        settings = dict(self.library_store.get_settings())
        settings["tag_map"] = self.library_store.tag_map()
        return settings

    def _plan_existing(self):
        """The library index plans resolve names against; ``{}`` when offline."""
        return self._existing(tolerant=True) if self.connected else {}

    def _changes(self, existing=None):
        """Recompute every row's diff from the current settings. Cheap and pure."""
        if not self._rows:
            return []
        settings = self._plan_settings()
        existing = existing if existing is not None else self._plan_existing()
        out = []
        for row in self._rows:
            # A row an identify run has taken up is the run's until it settles
            # it: planned as nothing, so no preview, tick or apply can reach it,
            # and reported as waiting rather than as the verdict it may be about
            # to lose. That, and the run never touching a row again once it has
            # settled it, is what lets Review and Apply work while a run goes on.
            waiting = bool(row.get("pending"))
            if waiting or row["status"] in UNPLANNABLE:
                out.append({"video_id": row["video"].get("id"),
                            "rel_path": row["video"].get("rel_path"),
                            "title": row["video"].get("title"),
                            "scene_id": row["scene_id"],
                            "status": WAITING if waiting else row["status"],
                            "note": row["note"], "diff": [], "fields": [],
                            "patch": {},
                            "entities": {"models": [], "studios": [],
                                         "categories": [], "aliases": []},
                            "cover": None, "scene_title": None, "scene_date": None,
                            "scene_date_estimated": False,
                            # `path` alongside `rel_path`: an `unmatched` row is
                            # rendered as a review block, whose header has to be
                            # unmistakably the file on disk.
                            "path": row["video"].get("path"),
                            # And grouped there by model, like the questions.
                            # No scene to add a cast, so the video's own only.
                            "models": sync.review_models(row["video"], None, {}),
                            # Always present, never guessed. `confidence` on a
                            # change answers "how sure is the identity this write
                            # would assert", and a row with no scene asserts
                            # none — an `ambiguous` row's own confidence names
                            # which arm produced the candidates it could not
                            # choose between, which is a different question.
                            "confidence": None})
                continue
            change = sync.plan_change(row["video"], row["scene"], settings=settings,
                                      existing=existing, status=row["status"],
                                      note=row["note"])
            # Attached here, not inside `plan_change`: that function is pure and
            # reads nothing of this, and widening its signature for a field the
            # planner never consults is a worse trade than one key set at the
            # call site. `None` for a row that never went through identify: its
            # identity came from the folder UUID, which is not a fingerprint arm.
            change["confidence"] = row.get("confidence")
            # Review and write is one list grouped by model, with the file set
            # beside its scene: the same grouping the questions use, and the
            # scene's length with the same verdict the matcher reaches. Unknown
            # stays None, never a soft False.
            change["models"] = sync.review_models(row["video"], row["scene"],
                                                  existing.get("models") or {})
            change["scene_duration"] = (row["scene"] or {}).get("duration")
            change["duration_agrees"] = matcher.duration_agrees(
                row["video"].get("duration_secs"), change["scene_duration"],
                settings.get("duration_tolerance", matcher.DEFAULT_DURATION_TOLERANCE))
            if not change["patch"] and change["status"] in sync.WRITABLE_STATUSES:
                change["status"] = IN_SYNC
            out.append(change)
        return out

    def preview(self, include_ids=None):
        """Rows that would be written: everything verified, plus ticked opt-ins.

        The opt-ins are :data:`OPT_IN_STATUSES` — `unverified` and `likely`.

        A ``uuid-mismatch`` is never previewed, whatever is ticked: its identity
        is known to be wrong. The fix is an identify run, which rewrites the
        row's status to whatever the fingerprints actually say.
        """
        include = {int(i) for i in (include_ids or ())}
        rows, skipped = [], []
        for change in self._changes():
            if not change["patch"]:
                continue
            (rows if self._writable(change, include) else skipped).append(change)
        # A run writes more than the per-video field diffs: it creates or re-stamps
        # entities and pushes alias rows. Approving a write you cannot see is not
        # approval, so the planner the apply itself uses is run here and its totals
        # reported. The index is memoised, so this costs the polling UI no GETs.
        existing = self._existing(tolerant=True) if self.connected else {}
        upserts = sync.plan_entity_upserts(rows, existing)
        return {"rows": rows, "skipped": len(skipped),
                "canWrite": self.can_write(),
                "entities": {kind: len(upserts[kind]) for kind in
                             ("models", "studios", "categories", "aliases")},
                # Counted separately, not summed into one "opt-in" figure: the
                # apply confirmation hesitates over them for different reasons,
                # and one number would let a `likely` row hide inside an
                # `unverified` tally the user has already learned to accept.
                "unverified": sum(1 for change in rows
                                  if change["status"] == sync.UNVERIFIED),
                "likely": sum(1 for change in rows
                              if change["status"] == matcher.LIKELY),
                "chunkSize": int(self.library_store.get_settings()["chunk_size"])}

    @staticmethod
    def _writable(change, include):
        """Whether one planned change may be written in a run that ticked ``include``.

        The single predicate behind both the preview and the apply, so the two
        can never drift. ``uuid-mismatch`` satisfies neither branch and so cannot
        reach a write by any path, ticked or not; ``ambiguous`` and ``unmatched``
        are refused a line earlier still, by the empty patch an
        :data:`UNPLANNABLE` row is given.
        """
        if not change["patch"]:
            return False
        if change["status"] in sync.WRITABLE_STATUSES:
            return True
        return (change["status"] in OPT_IN_STATUSES
                and change["video_id"] in include)

    def _selected(self, wanted, existing=None):
        """The rows a run over ``wanted`` would write, planned against ``existing``.

        ``start_apply`` calls this to decide there is work at all; ``_apply_run``
        calls it a second time against the freshly-read index, so that the names it
        actually sends are the library's current spellings.
        """
        return [change for change in self._changes(existing=existing)
                if change["video_id"] in wanted and self._writable(change, wanted)]

    # ------------------------------------------------------------------- apply

    def start_apply(self, video_ids):
        client = self._require_client()
        # Outside the lock, and first: the scopes come from
        # `connect()`, which no background thread touches, so there is nothing
        # here for a lock to protect — and refusing a read-only token before the
        # index read below keeps that path costing no requests at all.
        if not self.can_write():
            raise LibraryStateError(
                "this token is read-only — tick 'Update video metadata' when "
                "creating the app in Pronoisseur")
        wanted = {int(i) for i in (video_ids or ())}
        # Read the entity index *before* the lock. On a cold cache this is three
        # GETs, and nothing may hold a lock across network I/O: a Pronoisseur
        # that has gone away would otherwise stall every other button on this
        # page for as long as the retries take. Planning against it here is only
        # the "is there work at all" gate — `_apply_run` re-reads the index with
        # ``refresh=True`` and re-plans, so what is actually written is still
        # planned against the library as it is at write time. That guarantee
        # lives there, and this does not touch it.
        existing = self._existing(tolerant=True)
        # The remaining checks are inside the lock, because `start_scan` sets its
        # own running flag inside it: read outside, and a scan starting at the
        # same moment lets both calls pass their guards and start — the same race
        # `start_identify` closes. Nothing under the lock does network I/O.
        with self._lock:
            # The rows are the *previous* scan's until the running one lands, so
            # an apply now writes a plan nobody approved.
            if self._scan["running"]:
                raise LibraryStateError(
                    "a scan is running — wait for it to finish, then re-check the "
                    "preview before applying")
            # An identify in flight is NOT refused, though `_apply_run` re-plans
            # from `self._rows` after its own network round trip. The run takes
            # every row it will work on out of the plan before it touches one
            # (`_changes` plans a waiting row as nothing), and settles each row
            # exactly once, never revisiting it. So the rows a write can reach are
            # the ones the run has finished with, and they stay as the preview
            # showed them; a row still waiting is in no preview and no write.
            # `start_identify` refuses the reverse order, a run starting while
            # this writes, for the same reason.
            changes = self._selected(wanted, existing=existing)
            if not changes:
                return {"started": False, "reason": "nothing selected"}
            if self._apply["running"]:
                return {"started": False, "reason": "already running",
                        "apply": dict(self._apply)}
            self._apply = {"running": True, "label": "upserting entities", "done": 0,
                           "total": len(changes), "error": None, "details": None,
                           "finished": None}

        def run():
            try:
                self._apply_run(client, wanted)
                self._apply.update({"running": False, "label": "done"})
            except PronoisseurError as exc:
                self._apply.update({"running": False, "label": "failed",
                                    "error": str(exc), "details": exc.details})
            except Exception as exc:                        # noqa: BLE001
                self._apply.update({"running": False, "label": "failed",
                                    "error": str(exc)})

        self._apply_thread = threading.Thread(target=run, daemon=True)
        try:
            self._apply_thread.start()
        except RuntimeError as exc:      # can't start a thread: clear the flag
            self._apply.update({"running": False, "label": "failed",
                                "error": str(exc)})
            raise
        return {"started": True, "apply": dict(self._apply)}

    def await_apply(self, timeout=60):
        if self._apply_thread:
            self._apply_thread.join(timeout)

    def _apply_run(self, client, wanted):
        """Write the run, planned afresh against the library as it is right now.

        The changes are re-planned here rather than reused from the preview. Both
        halves of a write resolve names — ``plan_entity_upserts`` decides what to
        POST, and the batch sends the names in ``models.add`` — and the app's own
        upsert is case-SENSITIVE. Planning against an index read at scan time would
        mean that a performer added inside Pronoisseur since then gets its
        spelling forked: either a duplicate entity is POSTed, or the POST is
        correctly skipped and the batch forks it anyway. One fresh index, one
        planning pass, both halves consistent.

        A batch the app refuses per-row does **not** stop the run — see
        :meth:`_write_batch`. Only a refusal it will not itemise, or a failure
        that is not a refusal at all, still does.
        """
        # First, so the ids this run writes are links in Pronoisseur's Details
        # pane from the moment they land. Never raises; the outcome is kept.
        self._register_link_patterns(client)
        existing = self._existing(refresh=True)
        changes = self._selected(wanted, existing=existing)
        if not changes:                  # someone else wrote these in the meantime
            self._apply.update({"label": "writing videos", "done": 0, "total": 0,
                                "finished": {"applied": 0, "entities": 0,
                                             "aliases": 0, "refused": 0,
                                             "refusals": [], "note": None}})
            return
        upserts = sync.plan_entity_upserts(changes, existing)
        refused = []             # filled by `_write_batch`; reported either way
        # Bound before the try, because the `finally` re-reads what the run wrote
        # — including on the path where a batch the app will not itemise stops
        # the run partway through and the earlier chunks have already landed.
        written_ids, applied = [], 0

        try:
            for kind, method in (("studios", client.upsert_studio),
                                 ("models", client.upsert_model),
                                 ("categories", client.upsert_category)):
                for entity in upserts[kind]:
                    method(entity["name"],
                           external_ids=[{"provider": sync.PROVIDER,
                                          "id": entity["stashdb_id"]}])
            if upserts["aliases"]:
                for chunk in sync.chunked(upserts["aliases"], 500):
                    client.put_aliases(chunk)

            settings = self.library_store.get_settings()
            items = [sync.batch_item(change) for change in changes]
            items = [item for item in items if item]
            self._apply.update({"label": "writing videos", "done": 0,
                                "total": len(items)})
            by_id = {change["video_id"]: change for change in changes}
            for chunk in sync.chunked(items, int(settings["chunk_size"])):
                written = self._write_batch(client, chunk, refused)
                applied += len(written)
                self._apply.update({"done": applied})
                for item in written:
                    written_ids.append(item["id"])
                    change = by_id[item["id"]]
                    self.library_store.log_sync(
                        change["video_id"], change["scene_id"],
                        {d["field"]: d["proposed"] for d in change["diff"]})
        finally:
            # Names and stamps have changed. A run that failed mid-batch has still
            # created every entity the upsert loop got through, so the stale cache
            # has to go on that path too.
            self._existing_cache = None
            # And so does the page's picture of the videos themselves, one level
            # down. This is the last thing the run does, so the next `preview()`
            # reads the library as it is now.
            stale = not self._refresh_written(client, written_ids)
        self._apply.update({
            "finished": {"applied": applied, "entities": sum(
                len(upserts[k]) for k in ("models", "studios", "categories")),
                "aliases": len(upserts["aliases"]),
                # Rows the app itself refused. Counted, never silent: a run that
                # wrote 98 of 100 has to say which two it did not.
                "refused": len(refused), "refusals": refused[:50],
                # Normally None. Set only when the writes landed but reading them
                # back did not — see `_refresh_written`.
                "note": STALE_NOTE if stale else None}})

    def _refresh_written(self, client, video_ids):
        """Re-read the videos this run wrote onto ``self._rows``. True if it did.

        Without this the preview never converges. :meth:`preview` diffs every row
        against ``row["video"]`` — the video as the **scan** read it — so a row a
        run has just written is diffed against its own pre-write self and
        proposed again, for ever, with a live Apply button under it.

        One paged listing, not one ``GET /videos/{id}`` per row: the contract has
        no by-ids read, ``page_size`` is capped at 500 and rejected above it
        rather than clamped, and 474 single reads to answer one question is a
        cost the app pays in full-row assembly. The listing is unfiltered on
        purpose — the scan's own ``missing_external_id`` filter would hide
        exactly the rows this run has just stamped.

        A row the listing no longer carries is left as it was: the write
        happened, the app has since moved the video, and the next scan is what
        settles that.

        **A failed re-read is not a failed run.** The videos are written either
        way; only the page's picture of them is behind, so the run reports
        :data:`STALE_NOTE` and keeps its summary rather than turning a successful
        apply into an error.
        """
        wanted = {int(i) for i in (video_ids or ()) if i is not None}
        if not wanted:
            return True
        page_size = min(int(self.cfg.get("pronoisseur_page_size") or MAX_PAGE_SIZE),
                        MAX_PAGE_SIZE)
        fresh = {}
        try:
            for video in client.all_videos(page_size=page_size):
                try:
                    video_id = int(video.get("id"))
                except (TypeError, ValueError):
                    continue
                if video_id in wanted:
                    fresh[video_id] = video
        except Exception:                               # noqa: BLE001
            # Broad on purpose, and this is the one place in this module where
            # that is right: this runs in `_apply_run`'s `finally`, so anything
            # raised here would *replace* the exception a failed batch is already
            # carrying — the run would report a listing problem instead of the
            # refusal that actually stopped it.
            return False
        # Whole-row assignment, as `_note_refusal` and `_settle` do it: a poll
        # walking the list from the request thread sees the old row or the new
        # one, never a half of each. The slot is re-read under the lock at write
        # time, because Review stays open while this runs: an answer given to a
        # row between reading it and writing it back would otherwise be undone.
        rows = self._rows
        for index, row in enumerate(rows):
            try:
                video_id = int(row["video"].get("id"))
            except (TypeError, ValueError):
                continue
            video = fresh.get(video_id)
            if video is not None:
                with self._lock:
                    rows[index] = dict(rows[index], video=video)
        return True

    @staticmethod
    def _refused_indexes(exc, size):
        """``{index: message}`` for the items the app named as invalid, or None.

        Only two fields of a ``422 validation`` detail entry are trusted: the
        ``index`` into the items just sent, and the app's own ``error`` text.
        Everything else in there is the app's business.

        **None means "not a per-item refusal", and the run must stop.** A 422
        with no index list is a statement about the whole request, and every
        other status is a transport or auth problem that asking for less would
        only repeat. So is a detail list this cannot read end to end: a partial
        reading would drop some rows and call the rest a success. An index
        outside the chunk is the one thing dropped quietly rather than obeyed —
        acting on it would remove the wrong row — and a list with nothing left
        after that falls back to None like any other unreadable refusal.
        """
        if getattr(exc, "status", None) != 422:
            return None
        details = exc.details
        if not isinstance(details, (list, tuple)) or not details:
            return None
        out = {}
        for entry in details:
            if not isinstance(entry, dict):
                return None
            index = entry.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                return None
            if 0 <= index < size:
                out[index] = str(entry.get("error") or "refused")
        return out or None

    def _write_batch(self, client, chunk, refused):
        """Send one batch, and return the items the app actually wrote.

        ``POST /videos/batch`` is **all-or-nothing**: one row the app will not
        validate and nothing in the batch is written. Letting that stop the whole
        run would mean a single unusable scene date costs every other row its
        write.

        So an indexed refusal is treated as what it is: a statement about those
        rows and nothing else. They are dropped, noted against their videos, and
        the rest of the batch is sent **once** more. Once, deliberately: a second
        refusal is the app disagreeing with something this code has already tried
        to accommodate, and looping on it would either write nothing slowly or
        peel a large batch apart one row at a time. That stops the run, with the
        app's own message, like every non-indexed failure.
        """
        try:
            client.batch(chunk)
            return chunk
        except PronoisseurError as exc:
            indexes = self._refused_indexes(exc, len(chunk))
            if indexes is None:
                raise
        for index, message in sorted(indexes.items()):
            item = chunk[index]
            refused.append({"videoId": item.get("id"), "error": message})
            self._note_refusal(item.get("id"), message)
        rest = [item for index, item in enumerate(chunk) if index not in indexes]
        if not rest:
            return []
        client.batch(rest)          # a second refusal stops the run
        return rest

    def _note_refusal(self, video_id, message):
        """Say on the row that Pronoisseur would not take this write.

        Written into ``self._rows`` the same way an identify run settles a row —
        one whole-row assignment, never edited in place — so a poll walking the
        list from the request thread sees the old row or the new one and never a
        half of each. A row the current scan no longer holds is simply skipped:
        the write was planned against it, but the page has moved on.
        """
        rows = self._rows
        try:
            index = self._row_index(rows, video_id)
        except LibraryStateError:
            return
        # Read and written under the lock, as `_refresh_written` does, so an
        # answer given to this row in the same moment is not undone.
        with self._lock:
            row = rows[index]
            rows[index] = dict(row, note=_note(
                row.get("note"), "Pronoisseur refused this write: %s" % message))

    def apply_state(self):
        return dict(self._apply)

    def log(self):
        entries = self.library_store.sync_log()
        rows = [dict(entry, video_id=video_id) for video_id, entry in entries.items()]
        rows.sort(key=lambda r: r["applied_at"], reverse=True)
        return {"entries": rows[:200], "total": len(rows)}
