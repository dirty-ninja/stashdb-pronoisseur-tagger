"""Persistence for the Pronoisseur library sync: scene cache, tag map, settings, log.

One SQLite file, with a connection discipline of open, commit, **close**, every
time. ``with sqlite3.connect(...)`` commits but does not close, and on Windows a
leaked handle means the file cannot be deleted.

Six tables:

* ``tag_category`` — StashDB's tag categories, the vocabulary the allowlist and
  the mapping screen choose from.
* ``stashdb_scenes`` — the raw ``findScene`` row, so a re-run of a large library
  costs no upstream requests. ``status='not_found'`` is cached too: a UUID pointing
  at a deleted scene should be asked about once a month, not once a run.
* ``tag_map`` — the explicit "this StashDB tag is that Pronoisseur category, or is
  ignored" override. The id is TEXT because StashDB tag ids are UUIDs.
* ``sync_settings`` — the run's toggles and the tag-category allowlist. The
  allowlist defaults to **empty**: importing thousands of tags into a vocabulary
  shared with pictures is the one irreversible mistake available here.
* ``sync_log`` — one row per video, last write wins, so the page can say when a
  video was last synced and from which scene.
* ``match_decisions`` — how a video's identity was settled, keyed by Pronoisseur's
  content hash.
  ``rejected`` is stored as a decision too: the user having said "none of these" is
  an answer, and asking again next run would throw it away.

A database from an earlier version may still hold a ``fingerprints`` table. It is
left alone: never read, written or dropped.

The Pronoisseur **token is never stored here**, by rule.
"""

import contextlib
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_category (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    group_name TEXT, description TEXT);

CREATE TABLE IF NOT EXISTS stashdb_scenes (
    id TEXT PRIMARY KEY,
    json TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tag_map (
    stashdb_tag_id TEXT PRIMARY KEY,
    category_name TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('map', 'ignore')),
    updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sync_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sync_log (
    video_id INTEGER PRIMARY KEY,
    scene_id TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    fields_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS match_decisions (
    content_hash TEXT PRIMARY KEY,
    scene_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('fingerprint', 'manual', 'rejected')),
    decided_at TEXT NOT NULL);
"""

#: One run's write policy. The allowlist being empty is deliberate and is surfaced
#: in the UI as "pick some categories".
DEFAULT_SETTINGS = {
    "allowlist": [],              # StashDB tag-category ids whose tags may import
    "use_parent_studio": True,    # networks over sub-studios (the useful unit here)
    "write_title": True,
    "write_release_date": True,
    "write_studio": True,
    "add_models": True,
    "add_categories": True,
    "credited_as_aliases": True,  # push a performer's "as" name as an alias too
    "duration_tolerance": 3.0,    # seconds, for verify-by-duration
    "chunk_size": 100,            # videos per POST /videos/batch
}

_SETTING_TYPES = {key: type(value) for key, value in DEFAULT_SETTINGS.items()}
_SQLITE_VARIABLE_CHUNK = 500

#: How a video's identity was settled. ``rejected`` means the user looked at the
#: candidates and said none of them — which is a decision, and must be remembered.
DECISION_SOURCES = ("fingerprint", "manual", "rejected")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LibraryStore:
    def __init__(self, path, now=None):
        self.path = path
        self._now = now or _now
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    # ----------------------------------------------------------- tag categories

    def replace_tag_categories(self, rows):
        with self._connect() as conn:
            conn.execute("DELETE FROM tag_category")
            conn.executemany(
                "INSERT INTO tag_category (id, name, group_name, description) "
                "VALUES (?,?,?,?)",
                [(r["id"], r.get("name"), r.get("group"), r.get("description"))
                 for r in rows])
        return len(rows)

    def tag_categories(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, group_name, description FROM tag_category "
                "ORDER BY group_name, name")]

    # ------------------------------------------------------------------ settings

    def get_settings(self):
        settings = dict(DEFAULT_SETTINGS)
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM sync_settings").fetchall()
        for row in rows:
            if row["key"] in settings:
                try:
                    settings[row["key"]] = json.loads(row["value"])
                except ValueError:
                    pass
        return settings

    def update_settings(self, patch):
        patch = dict(patch or {})
        unknown = sorted(set(patch) - set(DEFAULT_SETTINGS))
        if unknown:
            raise ValueError("unknown setting(s): %s" % ", ".join(unknown))
        for key, value in patch.items():
            expected = _SETTING_TYPES[key]
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
                patch[key] = value
            if expected in (int, float) and isinstance(value, bool):
                raise ValueError("%s must be %s" % (key, expected.__name__))
            if not isinstance(value, expected):
                raise ValueError("%s must be %s" % (key, expected.__name__))
        # `POST /videos/batch` is 1-500 by contract and refuses on the way out,
        # after every entity of the run has already been POSTed. An impossible
        # chunk size has to be refused here, where nothing has been written yet.
        if "chunk_size" in patch and not 1 <= patch["chunk_size"] <= 500:
            raise ValueError("chunk_size must be between 1 and 500")
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO sync_settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, json.dumps(value)) for key, value in patch.items()])
        return self.get_settings()

    # --------------------------------------------------------------- scene cache

    def cache_scene(self, scene_id, scene, status="ok"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stashdb_scenes (id, json, status, fetched_at) "
                "VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "json=excluded.json, status=excluded.status, "
                "fetched_at=excluded.fetched_at",
                (str(scene_id), json.dumps(scene) if scene is not None else None,
                 status, self._now()))

    def cached_scene(self, scene_id, max_age_days=30):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, json, status, fetched_at FROM stashdb_scenes WHERE id = ?",
                (str(scene_id),)).fetchone()
        if not row:
            return None
        if max_age_days is not None and \
                _age_days(row["fetched_at"], self._now()) > max_age_days:
            return None
        return {"id": row["id"], "status": row["status"],
                "fetched_at": row["fetched_at"],
                "scene": json.loads(row["json"]) if row["json"] else None}

    def forget_scenes(self, scene_ids=None):
        with self._connect() as conn:
            if scene_ids is None:
                conn.execute("DELETE FROM stashdb_scenes")
            else:
                conn.executemany("DELETE FROM stashdb_scenes WHERE id = ?",
                                 [(str(i),) for i in scene_ids])

    # ------------------------------------------------------------------- tag map

    def tag_map(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT stashdb_tag_id, category_name, mode FROM tag_map").fetchall()
        return {r["stashdb_tag_id"]: {"category_name": r["category_name"],
                                      "mode": r["mode"]} for r in rows}

    def set_tag_map(self, tag_id, category_name, mode):
        if mode not in ("map", "ignore"):
            raise ValueError("mode must be 'map' or 'ignore'")
        name = (category_name or "").strip() or None
        if mode == "map" and not name:
            raise ValueError("a mapped tag needs a category name")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tag_map (stashdb_tag_id, category_name, mode, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(stashdb_tag_id) DO UPDATE SET "
                "category_name=excluded.category_name, mode=excluded.mode, "
                "updated_at=excluded.updated_at",
                (str(tag_id), name if mode == "map" else None, mode, self._now()))

    def clear_tag_map(self, tag_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM tag_map WHERE stashdb_tag_id = ?", (str(tag_id),))

    # ------------------------------------------------------------------ sync log

    def log_sync(self, video_id, scene_id, fields):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_log (video_id, scene_id, applied_at, fields_json) "
                "VALUES (?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
                "scene_id=excluded.scene_id, applied_at=excluded.applied_at, "
                "fields_json=excluded.fields_json",
                (int(video_id), str(scene_id), self._now(),
                 json.dumps(fields or {}, sort_keys=True)))

    def sync_log(self, video_ids=None):
        out = {}
        with self._connect() as conn:
            if video_ids is None:
                rows = conn.execute(
                    "SELECT video_id, scene_id, applied_at, fields_json "
                    "FROM sync_log").fetchall()
            else:
                ids = [int(i) for i in video_ids]
                rows = []
                for start in range(0, len(ids), _SQLITE_VARIABLE_CHUNK):
                    chunk = ids[start:start + _SQLITE_VARIABLE_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    rows.extend(conn.execute(
                        "SELECT video_id, scene_id, applied_at, fields_json "
                        "FROM sync_log WHERE video_id IN (%s)" % marks,
                        chunk).fetchall())
        for row in rows:
            out[row["video_id"]] = {
                "scene_id": row["scene_id"], "applied_at": row["applied_at"],
                "fields": json.loads(row["fields_json"])}
        return out

    def sync_log_count(self):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0]

    # ----------------------------------------------------------- decisions

    def record_decision(self, content_hash, scene_id, source):
        if source not in DECISION_SOURCES:
            raise ValueError("source must be one of %s" % (DECISION_SOURCES,))
        if source != "rejected" and not scene_id:
            raise ValueError("a %s decision needs a scene id" % source)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO match_decisions "
                "(content_hash, scene_id, source, decided_at) VALUES (?,?,?,?) "
                "ON CONFLICT(content_hash) DO UPDATE SET "
                "scene_id=excluded.scene_id, source=excluded.source, "
                "decided_at=excluded.decided_at",
                (str(content_hash), str(scene_id) if scene_id else None,
                 source, self._now()))

    def decisions(self, content_hashes=None):
        out = {}
        with self._connect() as conn:
            if content_hashes is None:
                rows = conn.execute(
                    "SELECT content_hash, scene_id, source, decided_at "
                    "FROM match_decisions").fetchall()
            else:
                ids = [str(h) for h in content_hashes]
                rows = []
                for start in range(0, len(ids), _SQLITE_VARIABLE_CHUNK):
                    chunk = ids[start:start + _SQLITE_VARIABLE_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    rows.extend(conn.execute(
                        "SELECT content_hash, scene_id, source, decided_at "
                        "FROM match_decisions WHERE content_hash IN (%s)"
                        % marks, chunk).fetchall())
        for row in rows:
            out[row["content_hash"]] = {"scene_id": row["scene_id"],
                                        "source": row["source"],
                                        "decided_at": row["decided_at"]}
        return out

    def clear_decision(self, content_hash):
        with self._connect() as conn:
            conn.execute("DELETE FROM match_decisions WHERE content_hash = ?",
                         (str(content_hash),))


def _age_days(then, now):
    try:
        a = datetime.fromisoformat(then)
        b = datetime.fromisoformat(now)
    except ValueError:
        return float("inf")
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return abs((b - a).total_seconds()) / 86400.0
