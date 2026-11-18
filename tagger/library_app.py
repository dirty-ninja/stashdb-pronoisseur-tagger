"""Orchestration for the library page: connect, scan, identify, preview, apply.

The I/O edge. Everything that decides *what* should happen lives in
:mod:`tagger.library_sync` and :mod:`tagger.matcher` and is pure; this module
fetches, threads and writes.

Three long operations run on background threads with polled progress: a scan
(one StashDB request per new scene; no file is read),
an identify (the fingerprints Pronoisseur gave for the files no folder UUID
names, looked up in batches) and an apply (one POST per new entity, then batched video writes).
Blocking a request handler for any of them would look like a hang.

The Pronoisseur token and the StashDB key come from config or are typed into the
Connect step. A pair that works is saved to the config file through
``save_settings``; neither is ever written to the SQLite state database or put in
a response.
"""

import threading
from urllib.parse import urlsplit

from . import frames, library_sync as sync, queries
from .config import DEFAULTS, ConfigError
from .frames import FramesMixin
from .identify import IdentifyMixin
from .library_common import (BUCKETS, DECIDE_ACTIONS, IDENTIFY_BATCH, NOT_FOUND,  # noqa: F401
                             NO_IDENTITY, OPT_IN_STATUSES,  # noqa: F401
                             LibraryStateError, _idle, fingerprint_state)
from .pronoisseur import Pronoisseur, PronoisseurError, is_loopback_url
from .scenes import SceneFetcher
from .stashdb import StashDBError
from .writes import ApplyMixin


def _clean_credential(value, name):
    """Strip a typed key or token; refuse anything that cannot go in a header.

    The message names the credential and never repeats the value, because the
    HTTP layer would otherwise echo it back in its own error.
    """
    if value is not None and not isinstance(value, str):
        raise ValueError("the %s must be text" % name)
    value = (value or "").strip()
    if not value:
        raise ValueError("enter a %s" % name)
    if not all(33 <= ord(ch) <= 126 for ch in value):
        raise ValueError("the %s has a space or a character that is not "
                         "printable ASCII - paste it again" % name)
    return value


NO_OSHASH_NOTE = "Pronoisseur has no oshash for this file yet"

#: What the last link-pattern registration came to, before there has been one.
LINK_PATTERNS_NOT_YET = {"status": "not yet", "error": None}

#: Where each kind of id lives on a stash-box server. Kind names are the ones
#: Pronoisseur's Local API takes; the paths were checked against stashdb.org.
LINK_PATH_BY_KIND = (("video", "scenes"), ("model", "performers"),
                     ("studio", "studios"), ("category", "tags"))


class LibraryApp(IdentifyMixin, ApplyMixin, FramesMixin):
    def __init__(self, cfg, stashdb, library_store,
                 client_factory=Pronoisseur, save_settings=None,
                 framer=frames.safe_frame, frames_dir=None,
                 path_exists=None, clock=None):
        """``framer`` takes Review's stills, ``(jpeg, error)``-shaped, and is a
        parameter so that the tests never touch a media file; ``path_exists`` and
        ``clock`` say what is on disk and what time it is, so a test can take a
        drive offline and wait out a minute.
        """
        self._init_frames(framer, frames_dir, path_exists, clock)
        self.cfg = cfg
        self.stashdb = stashdb
        self.library_store = library_store
        self._client_factory = client_factory
        self._save_settings = save_settings
        self.scenes = SceneFetcher(stashdb, library_store)

        self._client = None
        self._info = None
        self._url = cfg.get("pronoisseur_url") or ""
        self._token = cfg.get("pronoisseur_token") or ""
        self._link_patterns = dict(LINK_PATTERNS_NOT_YET)

        self._rows = []           # every considered video, with its scene + status
        # What Pronoisseur said about fingerprints at the last scan.
        self._fingerprints = {"state": "unknown", "phashEnabled": None,
                              "phashFailed": None}
        self._existing_cache = None
        self._scan = _idle("never run")
        self._apply = _idle()
        self._identify = _idle("never run")
        self._scan_thread = None
        self._apply_thread = None
        self._identify_thread = None
        self._identify_cancel = threading.Event()
        # The first StashDB error that failed a lookup batch in the last identify,
        # kept apart from the progress dict for the same reason.
        self._lookup_error = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ connect

    @property
    def connected(self):
        return self._client is not None and self._info is not None

    def _scopes(self):
        return list((self._info or {}).get("app", {}).get("scopes") or ())

    def can_write(self):
        return "library.write" in self._scopes()

    def connect(self, url=None, token=None):
        """Test a URL + token pair and, if it answers, keep the client.

        A read-only token connects: the page then shows every diff and disables
        Apply, which is more useful than refusing to talk at all.

        The candidate url and token are held as locals until ``info()`` answers.
        Committing them first would mean a single typo'd token overwrote a working
        one from ``config.json`` for the life of the process, with no way back
        short of a restart.
        """
        if url is not None and not isinstance(url, str):
            raise ValueError("the Pronoisseur URL must be text")
        if token is not None and not isinstance(token, str):
            raise ValueError("the Pronoisseur token must be text")
        candidate_url = (url or self._url or "").strip()
        if not is_loopback_url(candidate_url):
            raise ValueError("the Pronoisseur URL must point at this computer "
                             "(127.0.0.1 or localhost)")
        candidate_token, typed = self._token, False
        if token is not None and token.strip():
            candidate_token = _clean_credential(token, "Pronoisseur token")
            typed = True
        client = self._client_factory(candidate_url, candidate_token)
        try:
            info = client.info()
        except PronoisseurError as exc:
            self._client, self._info = None, None
            self._link_patterns = dict(LINK_PATTERNS_NOT_YET)
            return {"connected": False, "url": candidate_url, "error": str(exc),
                    "code": exc.code, "status": exc.status, "canWrite": False, "scopes": [],
                    "saved": False, "saveError": None}
        updates = {}
        if candidate_url != self._url:
            updates["pronoisseur_url"] = candidate_url
        if typed:
            updates["pronoisseur_token"] = candidate_token
        self._url, self._token = candidate_url, candidate_token
        self._client, self._info = client, info
        saved, save_error = self._persist(updates)
        # After the connection is kept, so nothing registration does can undo it.
        self._register_link_patterns(client)
        return {"connected": True, "url": self._url, "info": info,
                "scopes": self._scopes(), "canWrite": self.can_write(),
                "error": None, "code": None, "status": None,
                "saved": saved, "saveError": save_error,
                "linkPatterns": dict(self._link_patterns)}

    def _link_base(self):
        """The scheme and host (and port) of the configured stash-box server.

        Links go to the server the tool is actually pointed at, so a user on
        another stash-box gets links there rather than to stashdb.org. None when
        the endpoint is not an http(s) address with a host.
        """
        endpoint = self.cfg.get("stashdb_endpoint") or DEFAULTS["stashdb_endpoint"]
        try:
            parts = urlsplit(endpoint)
            host = parts.hostname
            parts.port              # raises ValueError on a bad port
        except (ValueError, AttributeError):
            return None
        if parts.scheme not in ("http", "https") or not host:
            return None
        netloc = parts.netloc.rsplit("@", 1)[-1]      # never carry credentials
        return "%s://%s" % (parts.scheme, netloc)

    def _register_link_patterns(self, client):
        """Tell Pronoisseur how to turn this tool's StashDB ids into links.

        Idempotent: the PUT replaces the provider's entry, and removing the app
        in Pronoisseur's Settings removes its patterns, so registering at every
        connect and every Tag run heals that and covers videos tagged before the
        feature existed. It never raises: a link nicety must not fail a connect
        or a Tag run, so the outcome is kept for the page instead.
        """
        outcome = {"status": "failed", "error": None}
        try:
            if not self.can_write():
                outcome["status"] = "read-only"
            else:
                base = self._link_base()
                if base is None:
                    outcome["error"] = ("the StashDB address in the config is "
                                        "not an http or https address")
                else:
                    client.put_link_patterns(
                        sync.PROVIDER, "StashDB",
                        {kind: "%s/%s/{id}" % (base, path)
                         for kind, path in LINK_PATH_BY_KIND})
                    outcome["status"] = "registered"
        except PronoisseurError as exc:
            if exc.status == 404 and exc.code == "not_found":
                outcome["status"] = "unsupported"   # a Pronoisseur before 1.0.62
            else:
                outcome["error"] = str(exc)[:200] or "unknown error"
        except Exception as exc:                    # noqa: BLE001 - see above
            outcome["error"] = (str(exc) or type(exc).__name__)[:200]
        self._link_patterns = outcome

    def set_stashdb_key(self, key):
        """Test a StashDB key against the live API and, if it works, keep it.

        The category fetch is the test. A key that fails leaves the working one in
        place, so a typo never costs the session its access.
        """
        candidate = _clean_credential(key, "StashDB API key")
        # The whole swap is under the lock: the run starters set their running
        # flags under it, so none can begin on a key that is still on trial, and
        # a failed trial cannot flip the key back beneath a run.
        with self._lock:
            if (self._scan["running"] or self._identify["running"]
                    or self._apply["running"]):
                raise LibraryStateError(
                    "a run is in progress - wait for it to finish before "
                    "changing the StashDB key")
            previous = self.stashdb.api_key
            self.stashdb.api_key = candidate
            try:
                count = self.refresh_categories()["categories"]
            except StashDBError as exc:
                self.stashdb.api_key = previous
                return {"ok": False, "configured": bool(previous),
                        "categories": len(self.library_store.tag_categories()),
                        "error": str(exc), "saved": False, "saveError": None}
            saved, save_error = self._persist({"stashdb_api_key": candidate})
        return {"ok": True, "configured": True, "categories": count,
                "error": None, "saved": saved, "saveError": save_error}

    def _persist(self, updates):
        """Write settings through ``save_settings``: (saved, error message).

        A failed write never undoes the connection that just worked; the caller
        reports it so the user knows the setting will not survive a restart.
        """
        if not updates:
            return True, None
        if self._save_settings is None:
            return False, None
        try:
            self._save_settings(updates)
        except ConfigError as exc:
            return False, str(exc)
        return True, None

    def _require_client(self):
        if not self.stashdb.api_key:
            raise LibraryStateError(
                "add your StashDB API key first, in the Connect step")
        if not self.connected:
            raise LibraryStateError(
                "connect to Pronoisseur first — Settings > Connected apps in the "
                "app, then Connect here")
        return self._client

    # ------------------------------------------------------------------- state

    def state(self):
        """Everything the page renders itself from, in one read.

        Every key is camelCase, as in :meth:`connect` — this payload is consumed by
        the browser one step later and nothing in-process reads it.
        """
        return {
            "connected": self.connected,
            "url": self._url,
            "hasToken": bool(self._token),
            "stashdbConfigured": bool(self.stashdb.api_key),
            "info": self._info,
            "scopes": self._scopes(),
            "canWrite": self.can_write(),
            "settings": self.library_store.get_settings(),
            "categories": self.library_store.tag_categories(),
            "scan": self.scan_state(),
            "identify": self.identify_state(),
            "apply": self.apply_state(),
            "linkPatterns": dict(self._link_patterns),
            # Shipped here rather than behind its own route: the page renders the
            # list beside the bucket pills, and a second round trip per poll to
            # fetch a list that is usually empty would be pure cost.
            "disagreements": self.disagreements(),
            "logCount": self.library_store.sync_log_count(),
        }

    def disagreements(self):
        """The rows whose folder UUID names a scene the file is not.

        The "Whisparr disagreements" list: video, the UUID Whisparr wrote into the
        folder name, and the scene the bytes actually matched. **Nothing in this
        app writes to Whisparr or contacts it** — the list exists so the user can
        fix those folders there by hand.

        One entry per row that *arrived* as `uuid-mismatch`, resolved or not. A
        row identify has since settled no longer carries that status, but the
        claim it arrived with is stamped on it by
        :meth:`_stamp_whisparr_claims`, and a resolved disagreement is still a
        disagreement until Whisparr is corrected. ``matchedSceneId`` is None
        while nothing has named the right scene, which includes a row whose
        stamped claim is still its only id — a run cancelled before it settled.
        """
        rows = self._rows          # bound once: a scan may replace the list
        out = []
        for row in rows:
            claimed = row.get("whisparr_scene_id") or (
                row["scene_id"] if row["status"] == sync.UUID_MISMATCH else None)
            if not claimed:
                continue
            matched = row["scene_id"] if row["scene_id"] != claimed else None
            out.append({
                "videoId": row["video"].get("id"),
                "relPath": row["video"].get("rel_path"),
                "whisparrSceneId": claimed,
                "matchedSceneId": matched,
                "matchedTitle": ((row.get("scene") or {}).get("title")
                                 if matched else None),
                "status": row["status"]})
        return out

    def update_settings(self, patch):
        return {"settings": self.library_store.update_settings(patch or {})}

    def set_tag_mapping(self, tag_id, category_name, mode):
        if mode == "clear":
            self.library_store.clear_tag_map(tag_id)
        else:
            self.library_store.set_tag_map(tag_id, category_name, mode)
        return self.mapping()

    # -------------------------------------------------------------------- scan

    def start_scan(self, resync=False, refresh_scenes=False):
        client = self._require_client()
        with self._lock:
            if self._scan["running"]:
                return {"started": False, "reason": "already running",
                        "scan": dict(self._scan)}
            self._scan = {"running": True, "label": "listing videos", "done": 0,
                          "total": 0, "error": None, "details": None,
                          "finished": None}
            self._existing_cache = None

        def run():
            try:
                self._scan_run(client, resync=resync, refresh_scenes=refresh_scenes)
                self._scan.update({"running": False, "label": "done"})
            except Exception as exc:                        # noqa: BLE001
                self._scan.update({"running": False, "label": "failed",
                                   "error": str(exc)})

        self._scan_thread = threading.Thread(target=run, daemon=True)
        try:
            self._scan_thread.start()
        except RuntimeError as exc:      # can't start a thread: clear the flag
            self._scan.update({"running": False, "label": "failed",
                               "error": str(exc)})
            raise
        return {"started": True, "scan": dict(self._scan)}

    def await_scan(self, timeout=60):
        """Join the scan thread. Tests use this; the UI polls instead."""
        if self._scan_thread:
            self._scan_thread.join(timeout)

    def refresh_categories(self):
        data = self.stashdb.execute(queries.QUERY_TAG_CATEGORIES, {})
        rows = data["queryTagCategories"]["tag_categories"]
        return {"categories": self.library_store.replace_tag_categories(rows)}

    def _scan_run(self, client, resync, refresh_scenes):
        if not self.library_store.tag_categories():
            self.refresh_categories()
        settings = self.library_store.get_settings()
        page_size = int(self.cfg.get("pronoisseur_page_size", 500))

        def progress(done, total):
            self._scan.update({"done": done, "total": total})

        # A failed info read must not fail the scan: fingerprint state is then
        # simply unknown.
        try:
            info = client.info()
        except PronoisseurError:
            info = None
        # The whole library, every time: nothing here reads or hashes a file, so
        # a large run costs one listing and a few StashDB lookups per 40 videos.
        filters = {} if resync else {"missing_external_id": sync.PROVIDER}
        videos = client.all_videos(page_size=page_size, progress=progress,
                                   **filters)
        self._fingerprints = fingerprint_state(info, videos)

        self._scan.update({"label": "reading identities", "done": 0,
                           "total": len(videos)})
        identified, rows = [], []
        for video in videos:
            scene_id = sync.extract_scene_uuid(video.get("rel_path"))
            if not scene_id:
                rows.append({"video": video, "scene_id": None, "scene": None,
                             "status": NO_IDENTITY,
                             "note": "no StashDB UUID in the path — identify "
                                     "matches these by fingerprint"})
            else:
                identified.append((video, scene_id))

        self._scan.update({"label": "fetching scenes", "done": 0,
                           "total": len({s for _, s in identified})})
        folder_stats = {}
        fetched = self.scenes.get_many([s for _, s in identified],
                                       refresh=refresh_scenes, progress=progress,
                                       stats=folder_stats)

        # Nothing opens a file here: each folder's scene is compared with the
        # fingerprint Pronoisseur already holds for the video.
        self._scan.update({"label": "checking folder ids", "done": 0,
                           "total": len(identified)})
        tolerance = float(settings["duration_tolerance"])
        for index, (video, scene_id) in enumerate(identified, start=1):
            self._scan.update({"done": index})
            result = fetched.get(scene_id) or {"status": NOT_FOUND, "scene": None}
            if result["status"] != "ok" or not result["scene"]:
                rows.append({"video": video, "scene_id": scene_id, "scene": None,
                             "status": NOT_FOUND,
                             "note": "StashDB has no scene with this id any more"})
                continue
            scene = result["scene"]
            file_hash = video.get("oshash")
            verdict = sync.classify_uuid_match(file_hash, video.get("duration_secs"),
                                               scene, tolerance=tolerance)
            note = verdict["note"]
            if not file_hash and verdict["status"] != sync.VERIFIED:
                note = "%s (%s)" % (note, NO_OSHASH_NOTE)
            rows.append({"video": video, "scene_id": scene_id, "scene": scene,
                         "status": verdict["status"], "note": note,
                         "oshash": file_hash})

        self._rows = rows
        # `finished` deliberately carries no per-status counts. Raw row statuses
        # and the ones `scan_state` reports diverge the moment a video is already
        # in sync — the row says `verified`, the diff says `in-sync` — and two
        # contradictory tallies in one payload is worse than one. `counts` is the
        # single source of truth, recomputed from the same `_changes()` pass the
        # buckets come from.
        self._scan.update({"label": "done", "considered": len(videos),
                           "finished": {"considered": len(videos),
                                        # What the page says the scan asked
                                        # StashDB: distinct scene ids named in
                                        # folders, split by where each came from.
                                        "folderIds": len(fetched),
                                        "fetched": folder_stats.get("fetched", 0),
                                        "saved": folder_stats.get("saved", 0)}})

    def scan_state(self):
        state = dict(self._scan)
        changes = self._changes()
        by_status = {bucket: [] for bucket in BUCKETS}
        counts = {bucket: 0 for bucket in BUCKETS}
        for change in changes:
            status = change["status"]
            by_status.setdefault(status, []).append(change)
            counts[status] = counts.get(status, 0) + 1
        state["counts"] = counts
        state["fingerprints"] = dict(self._fingerprints)
        state["buckets"] = by_status
        state.setdefault("considered", len(self._rows))
        return state

    def mapping(self):
        """Every allowlisted tag seen in this scan, with where it would land."""
        settings = self.library_store.get_settings()
        tag_map = self.library_store.tag_map()
        existing = (self._existing(tolerant=True).get("categories") or {}
                    if self.connected else {})
        tags, counts = {}, {}
        for row in self._rows:
            if not row.get("scene"):
                continue
            for tag in sync.allowlisted_tags(row["scene"], settings["allowlist"]):
                tag_id = str(tag["id"])
                tags[tag_id] = tag
                counts[tag_id] = counts.get(tag_id, 0) + 1
        out = []
        for tag_id, tag in sorted(tags.items(), key=lambda kv: kv[1].get("name") or ""):
            override = tag_map.get(tag_id) or {}
            resolved = sync.resolve_scene_tags({"tags": [tag]}, settings["allowlist"],
                                               tag_map, existing)
            out.append({
                "tag_id": tag_id, "name": tag.get("name"),
                "category": (tag.get("category") or {}).get("name"),
                "mode": override.get("mode") or "map",
                "override": override.get("category_name"),
                "resolves_to": resolved[0]["name"] if resolved else None,
                "source": resolved[0]["source"] if resolved else "ignored",
                "videos": counts[tag_id]})
        return {"tags": out, "allowlist": settings["allowlist"],
                "categories": self.library_store.tag_categories()}
