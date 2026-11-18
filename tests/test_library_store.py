import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tagger.library_store import DEFAULT_SETTINGS, LibraryStore


def store(now=None):
    path = os.path.join(tempfile.mkdtemp(), "state.db")
    return LibraryStore(path, now=now)


class TestSettings(unittest.TestCase):
    def test_defaults_come_back_when_nothing_is_stored(self):
        settings = store().get_settings()
        self.assertEqual(settings, DEFAULT_SETTINGS)
        self.assertEqual(settings["allowlist"], [])
        self.assertTrue(settings["use_parent_studio"])

    def test_the_default_allowlist_is_empty_so_no_tags_import_by_accident(self):
        self.assertEqual(DEFAULT_SETTINGS["allowlist"], [])

    def test_a_patch_merges_and_survives_a_reopen(self):
        s = store()
        s.update_settings({"use_parent_studio": False, "allowlist": ["cat-1"]})
        reopened = LibraryStore(s.path)
        settings = reopened.get_settings()
        self.assertFalse(settings["use_parent_studio"])
        self.assertEqual(settings["allowlist"], ["cat-1"])
        self.assertTrue(settings["write_title"])       # untouched default

    def test_an_unknown_key_is_refused_rather_than_silently_stored(self):
        with self.assertRaises(ValueError):
            store().update_settings({"wat": 1})

    def test_a_bool_is_not_a_sneaky_int_or_float(self):
        # isinstance(True, int) is True in Python, so a naive type check lets a
        # bool through as chunk_size/duration_tolerance - a later
        # int(settings["chunk_size"]) would silently become a batch size of 1.
        s = store()
        s.update_settings({"use_parent_studio": False})  # bool setting, bool value: fine
        with self.assertRaises(ValueError):
            s.update_settings({"chunk_size": True})
        with self.assertRaises(ValueError):
            s.update_settings({"duration_tolerance": True})
        s.update_settings({"duration_tolerance": 5})  # still coerces to 5.0
        self.assertEqual(s.get_settings()["duration_tolerance"], 5.0)
        s.update_settings({"chunk_size": 250})
        self.assertEqual(s.get_settings()["chunk_size"], 250)

    def test_a_known_key_with_the_wrong_type_is_refused(self):
        with self.assertRaises(ValueError):
            store().update_settings({"allowlist": "not-a-list"})


class TestSettingRanges(unittest.TestCase):
    """The batch contract is 1-500, and it refuses on the way out.

    By the time `POST /videos/batch` rejects an oversized chunk, every entity of
    the run has already been POSTed. An impossible chunk size has to be refused
    here, where nothing has been written yet.
    """

    def test_a_chunk_size_outside_the_contract_is_refused(self):
        s = store()
        with self.assertRaises(ValueError):
            s.update_settings({"chunk_size": 501})
        with self.assertRaises(ValueError):
            s.update_settings({"chunk_size": 0})
        self.assertEqual(s.get_settings()["chunk_size"],
                         DEFAULT_SETTINGS["chunk_size"])

    def test_both_ends_of_the_range_are_accepted(self):
        s = store()
        self.assertEqual(s.update_settings({"chunk_size": 1})["chunk_size"], 1)
        self.assertEqual(s.update_settings({"chunk_size": 500})["chunk_size"], 500)


class TestNoRunCap(unittest.TestCase):
    """A scan covers the whole library, so there is no per-run cap to set."""

    def test_there_is_no_run_cap_setting(self):
        s = store()
        self.assertNotIn("max_run", DEFAULT_SETTINGS)
        self.assertNotIn("max_run", s.get_settings())
        with self.assertRaises(ValueError):
            s.update_settings({"max_run": 5000})

    def test_a_cap_saved_by_an_older_version_is_ignored(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        LibraryStore(path)                    # creates the schema
        conn = sqlite3.connect(path)
        with conn:
            conn.execute("INSERT INTO sync_settings (key, value) VALUES (?, ?)",
                         ("max_run", json.dumps(50)))
        conn.close()
        s = LibraryStore(path)
        self.assertNotIn("max_run", s.get_settings())
        self.assertEqual(s.update_settings({"chunk_size": 250})["chunk_size"], 250)


class TestSceneCache(unittest.TestCase):
    def test_round_trips_a_scene(self):
        s = store()
        s.cache_scene("scene-1", {"id": "scene-1", "title": "A"})
        hit = s.cached_scene("scene-1")
        self.assertEqual(hit["scene"]["title"], "A")
        self.assertEqual(hit["status"], "ok")

    def test_a_miss_is_none(self):
        self.assertIsNone(store().cached_scene("nope"))

    def test_a_stale_row_is_a_miss(self):
        old = (datetime.now(timezone.utc) - timedelta(days=31)).replace(
            microsecond=0).isoformat()
        s = store(now=lambda: old)
        s.cache_scene("scene-1", {"id": "scene-1"})
        self.assertIsNone(LibraryStore(s.path).cached_scene("scene-1",
                                                            max_age_days=30))

    def test_a_stale_row_is_still_returned_when_age_is_not_capped(self):
        old = (datetime.now(timezone.utc) - timedelta(days=400)).replace(
            microsecond=0).isoformat()
        s = store(now=lambda: old)
        s.cache_scene("scene-1", {"id": "scene-1"})
        self.assertIsNotNone(LibraryStore(s.path).cached_scene("scene-1",
                                                               max_age_days=None))

    def test_not_found_is_cached_so_a_dead_uuid_is_not_re_fetched_every_run(self):
        s = store()
        s.cache_scene("gone", None, status="not_found")
        hit = s.cached_scene("gone")
        self.assertEqual(hit["status"], "not_found")
        self.assertIsNone(hit["scene"])

    def test_forget_drops_named_scenes_and_forget_all_empties_it(self):
        s = store()
        s.cache_scene("a", {"id": "a"})
        s.cache_scene("b", {"id": "b"})
        s.forget_scenes(["a"])
        self.assertIsNone(s.cached_scene("a"))
        self.assertIsNotNone(s.cached_scene("b"))
        s.forget_scenes()
        self.assertIsNone(s.cached_scene("b"))


class TestTagMap(unittest.TestCase):
    def test_upsert_and_read_back(self):
        s = store()
        s.set_tag_map("tag-1", "Outdoor", "map")
        s.set_tag_map("tag-2", None, "ignore")
        mapping = s.tag_map()
        self.assertEqual(mapping["tag-1"], {"category_name": "Outdoor", "mode": "map"})
        self.assertEqual(mapping["tag-2"]["mode"], "ignore")

    def test_re_setting_a_tag_replaces_it(self):
        s = store()
        s.set_tag_map("tag-1", "Outdoor", "map")
        s.set_tag_map("tag-1", "Indoor", "map")
        self.assertEqual(s.tag_map()["tag-1"]["category_name"], "Indoor")

    def test_mode_is_constrained(self):
        with self.assertRaises(ValueError):
            store().set_tag_map("tag-1", "Outdoor", "maybe")

    def test_map_mode_needs_a_category_name(self):
        with self.assertRaises(ValueError):
            store().set_tag_map("tag-1", "  ", "map")

    def test_clear_removes_the_override(self):
        s = store()
        s.set_tag_map("tag-1", "Outdoor", "map")
        s.clear_tag_map("tag-1")
        self.assertEqual(s.tag_map(), {})


class TestTagCategories(unittest.TestCase):
    def test_replace_tag_categories_is_wholesale(self):
        s = store()
        s.replace_tag_categories([{"id": "a", "name": "A", "group": "SCENE", "description": None},
                                  {"id": "b", "name": "B", "group": "SCENE", "description": None}])
        self.assertEqual(s.replace_tag_categories(
            [{"id": "a", "name": "A", "group": "SCENE", "description": None}]), 1)
        self.assertEqual([c["id"] for c in s.tag_categories()], ["a"])

    def test_tag_categories_are_ordered_by_group_then_name(self):
        s = store()
        s.replace_tag_categories([{"id": "1", "name": "Z", "group": "PEOPLE", "description": None},
                                  {"id": "2", "name": "B", "group": "SCENE", "description": None},
                                  {"id": "3", "name": "A", "group": "SCENE", "description": None}])
        self.assertEqual([c["name"] for c in s.tag_categories()], ["Z", "A", "B"])


class TestSyncLog(unittest.TestCase):
    def test_records_and_reads_back_the_fields_written(self):
        s = store()
        s.log_sync(42, "scene-1", {"title": "Better", "models": ["A", "B"]})
        entry = s.sync_log([42])[42]
        self.assertEqual(entry["scene_id"], "scene-1")
        self.assertEqual(entry["fields"]["models"], ["A", "B"])
        self.assertTrue(entry["applied_at"])

    def test_one_row_per_video_last_write_wins(self):
        s = store()
        s.log_sync(42, "scene-1", {"title": "One"})
        s.log_sync(42, "scene-2", {"title": "Two"})
        self.assertEqual(s.sync_log_count(), 1)
        self.assertEqual(s.sync_log([42])[42]["scene_id"], "scene-2")

    def test_reading_without_ids_returns_everything(self):
        s = store()
        s.log_sync(1, "a", {})
        s.log_sync(2, "b", {})
        self.assertEqual(set(s.sync_log()), {1, 2})

    def test_reading_more_ids_than_sqlite_takes_variables_is_chunked(self):
        s = store()
        for video_id in range(1200):
            s.log_sync(video_id, "s", {})
        self.assertEqual(len(s.sync_log(list(range(1200)))), 1200)


OLD_FINGERPRINTS_DDL = """
CREATE TABLE IF NOT EXISTS fingerprints (
    content_hash TEXT PRIMARY KEY,
    oshash TEXT,
    phash TEXT,
    duration REAL,
    computed_at TEXT NOT NULL);
"""


class TestFingerprintTableGone(unittest.TestCase):
    def _tables(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()

    def test_an_old_database_with_a_fingerprint_cache_still_opens(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(OLD_FINGERPRINTS_DDL)
        conn.execute("INSERT INTO fingerprints VALUES (?,?,?,?,?)",
                     ("ch-1", "aaaa", "bbbb", 12.5, "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()
        s = LibraryStore(path)
        self.assertEqual(s.get_settings(), DEFAULT_SETTINGS)
        self.assertIn("fingerprints", self._tables(path))
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("SELECT * FROM fingerprints").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("ch-1", "aaaa", "bbbb", 12.5,
                                 "2026-01-01T00:00:00")])

    def test_a_new_database_has_no_fingerprint_table(self):
        self.assertNotIn("fingerprints", self._tables(store().path))

    def test_the_store_has_no_cache_methods_left(self):
        self.assertFalse(hasattr(LibraryStore, "cache_fingerprint"))
        self.assertFalse(hasattr(LibraryStore, "cached_fingerprint"))


class TestDecisionMemory(unittest.TestCase):
    def test_records_and_reads_back(self):
        s = store()
        s.record_decision("ch-1", "scene-a", "fingerprint")
        entry = s.decisions(["ch-1"])["ch-1"]
        self.assertEqual(entry["scene_id"], "scene-a")
        self.assertEqual(entry["source"], "fingerprint")
        self.assertTrue(entry["decided_at"])

    def test_a_rejection_is_a_decision_with_no_scene(self):
        # "None of these" must be durable, or a re-run asks again forever.
        s = store()
        s.record_decision("ch-1", None, "rejected")
        entry = s.decisions(["ch-1"])["ch-1"]
        self.assertIsNone(entry["scene_id"])
        self.assertEqual(entry["source"], "rejected")

    def test_source_is_constrained(self):
        with self.assertRaises(ValueError):
            store().record_decision("ch-1", "scene-a", "guessed")

    def test_a_non_rejected_decision_needs_a_scene(self):
        with self.assertRaises(ValueError):
            store().record_decision("ch-1", None, "manual")

    def test_one_row_per_content_hash_last_write_wins(self):
        s = store()
        s.record_decision("ch-1", "scene-a", "fingerprint")
        s.record_decision("ch-1", "scene-b", "manual")
        entries = s.decisions()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries["ch-1"]["scene_id"], "scene-b")

    def test_reading_without_ids_returns_everything(self):
        s = store()
        s.record_decision("ch-1", "a", "manual")
        s.record_decision("ch-2", "b", "manual")
        self.assertEqual(set(s.decisions()), {"ch-1", "ch-2"})

    def test_reading_more_ids_than_sqlite_takes_is_chunked(self):
        s = store()
        for i in range(1200):
            s.record_decision("ch-%d" % i, "scene", "manual")
        self.assertEqual(len(s.decisions(["ch-%d" % i for i in range(1200)])), 1200)

    def test_clear_forgets_one_decision(self):
        s = store()
        s.record_decision("ch-1", "scene-a", "manual")
        s.clear_decision("ch-1")
        self.assertEqual(s.decisions(), {})


if __name__ == "__main__":
    unittest.main()
