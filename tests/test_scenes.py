import os
import tempfile
import unittest

from tagger.library_store import LibraryStore
from tagger.scenes import SceneFetcher
from tagger.stashdb import StashDB, StashDBError


def scene_row(scene_id="scene-1"):
    return {"id": scene_id, "title": "A Scene", "release_date": "2024-01-02",
            "duration": 1200, "studio": {"id": "s-1", "name": "Example Studio",
                                         "parent": {"id": "s-0", "name": "Example Network"}},
            "performers": [{"as": None, "performer": {"id": "p-1", "name": "Jamie Example",
                                                      "aliases": ["J. Example"],
                                                      "gender": "FEMALE"}}],
            "tags": [{"id": "t-1", "name": "Outdoor",
                      "category": {"id": "c-1", "name": "Setting", "group": "SCENE"}}],
            "images": [{"id": "i-1", "url": "https://cdn/x.jpg", "width": 1000,
                        "height": 600}],
            "fingerprints": [{"hash": "5e1f93a07b2c4d68", "algorithm": "OSHASH",
                              "duration": 1900}]}


class FakeStashDB(StashDB):
    def __init__(self, answers):
        super().__init__("http://x", "k", interval=0, transport=self._answer)
        self.answers = answers
        self.calls = []

    def _answer(self, payload):
        self.calls.append(payload)
        scene_id = payload["variables"]["id"]
        if scene_id not in self.answers:
            return {"data": {"findScene": None}}
        value = self.answers[scene_id]
        if isinstance(value, Exception):
            raise value
        return {"data": {"findScene": value}}


def fetcher(answers, **kw):
    path = os.path.join(tempfile.mkdtemp(), "state.db")
    return SceneFetcher(FakeStashDB(answers), LibraryStore(path), **kw)


class TestSceneQuery(unittest.TestCase):
    def test_only_the_queries_this_tool_uses_remain(self):
        from tagger import queries
        for name in ("QUERY_TAGS", "QUERY_STUDIOS", "ETHNICITY", "MODIFIERS"):
            self.assertFalse(hasattr(queries, name), name)

    def test_performer_lookup_input_is_a_name_search(self):
        from tagger import queries
        self.assertEqual(queries.build_performer_input("A Name"),
                         {"names": "A Name", "sort": "NAME", "direction": "ASC"})

    def test_the_document_asks_for_every_field_the_sync_needs(self):
        from tagger import queries
        for field in ("fingerprints", "algorithm", "parent", "aliases", "category",
                      "release_date", "duration", "images"):
            self.assertIn(field, queries.SCENE_DETAIL_FIELDS)
        self.assertIn("findScene", queries.FIND_SCENE)


class TestFetch(unittest.TestCase):
    def test_fetches_and_caches(self):
        f = fetcher({"scene-1": scene_row()})
        result = f.get("scene-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scene"]["title"], "A Scene")
        self.assertFalse(result["cached"])

    def test_a_second_call_is_served_from_the_cache_with_no_upstream_request(self):
        f = fetcher({"scene-1": scene_row()})
        f.get("scene-1")
        again = f.get("scene-1")
        self.assertTrue(again["cached"])
        self.assertEqual(len(f.stashdb.calls), 1)

    def test_refresh_re_queries_even_with_a_fresh_cache(self):
        f = fetcher({"scene-1": scene_row()})
        f.get("scene-1")
        f.get("scene-1", refresh=True)
        self.assertEqual(len(f.stashdb.calls), 2)

    def test_a_deleted_scene_is_recorded_as_not_found_and_not_re_fetched(self):
        f = fetcher({})
        result = f.get("gone")
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["scene"])
        f.get("gone")
        self.assertEqual(len(f.stashdb.calls), 1)

    def test_an_upstream_failure_propagates_rather_than_caching_a_lie(self):
        f = fetcher({"scene-1": StashDBError("500")})
        with self.assertRaises(StashDBError):
            f.get("scene-1")
        self.assertIsNone(f.store.cached_scene("scene-1"))

    def test_get_many_deduplicates_and_reports_progress(self):
        f = fetcher({"a": scene_row("a"), "b": scene_row("b")})
        seen = []
        out = f.get_many(["a", "b", "a"], progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(set(out), {"a", "b"})
        self.assertEqual(len(f.stashdb.calls), 2)
        self.assertEqual(seen, [(1, 2), (2, 2)])


if __name__ == "__main__":
    unittest.main()
