import os
import tempfile
import unittest

from tagger.library_store import LibraryStore
from tagger.scenes import MAX_FINGERPRINT_BATCH, SceneFetcher
from tagger.stashdb import StashDB, StashDBError


def scene(scene_id, oshashes=(), phashes=(), duration=1200):
    return {"id": scene_id, "title": "Scene " + scene_id, "duration": duration,
            "fingerprints": ([{"hash": h, "algorithm": "OSHASH"} for h in oshashes]
                             + [{"hash": h, "algorithm": "PHASH"} for h in phashes])}


class FakeTransport:
    """Answers the batch query from a hash -> scenes table, preserving order."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, payload):
        self.calls.append(payload)
        groups = []
        for entry in payload["variables"]["fps"]:
            found, seen = [], set()
            for fingerprint in entry:
                for row in self.table.get(fingerprint["hash"], ()):
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        found.append(row)
            groups.append(found)
        return {"data": {"findScenesBySceneFingerprints": groups}}


class CappedTransport:
    """stash-box's own behaviour over its batch limit: a GraphQL error that
    loses the **whole** query, not a truncated answer. `sizes` records how many
    videos each attempt asked about."""

    def __init__(self, cap, error="too many scenes"):
        self.cap, self.error, self.sizes = cap, error, []

    def __call__(self, payload):
        entries = payload["variables"]["fps"]
        self.sizes.append(len(entries))
        if len(entries) > self.cap:
            return {"errors": [{"message": self.error}]}
        return {"data": {"findScenesBySceneFingerprints": [[] for _ in entries]}}


def build_fetcher(transport):
    path = os.path.join(tempfile.mkdtemp(), "state.db")
    f = SceneFetcher(StashDB("http://x", "k", interval=0, transport=transport),
                     LibraryStore(path))
    f.transport = transport
    return f


def fetcher(table):
    return build_fetcher(FakeTransport(table))


def entries(count, prefix="os"):
    return [[{"hash": "%s-%d" % (prefix, i), "algorithm": "OSHASH"}]
            for i in range(count)]


class TestDocument(unittest.TestCase):
    def test_the_document_takes_a_nested_fingerprint_list(self):
        from tagger import queries
        self.assertIn("findScenesBySceneFingerprints",
                      queries.FIND_SCENES_BY_SCENE_FINGERPRINTS)
        self.assertIn("[[FingerprintQueryInput!]!]!",
                      queries.FIND_SCENES_BY_SCENE_FINGERPRINTS)
        self.assertIn("fingerprints", queries.FIND_SCENES_BY_SCENE_FINGERPRINTS)


class TestByFingerprints(unittest.TestCase):
    def test_results_are_aligned_to_the_input_by_index(self):
        # N inputs give N groups, positionally.
        f = fetcher({"os-a": [scene("a", oshashes=["os-a"])],
                     "os-b": [scene("b", oshashes=["os-b"])]})
        out = f.by_fingerprints([
            [{"hash": "os-a", "algorithm": "OSHASH"}],
            [{"hash": "os-b", "algorithm": "OSHASH"}],
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][0]["id"], "a")
        self.assertEqual(out[1][0]["id"], "b")

    def test_a_video_with_no_match_gets_an_empty_group_not_a_missing_one(self):
        f = fetcher({})
        out = f.by_fingerprints([[{"hash": "nothing", "algorithm": "OSHASH"}]])
        self.assertEqual(out, [[]])

    def test_batches_are_chunked_and_reassembled_in_order(self):
        table = {"os-%d" % i: [scene(str(i), oshashes=["os-%d" % i])]
                 for i in range(5)}
        f = fetcher(table)
        entries = [[{"hash": "os-%d" % i, "algorithm": "OSHASH"}] for i in range(5)]
        out = f.by_fingerprints(entries, batch_size=2)
        self.assertEqual(len(f.transport.calls), 3)
        self.assertEqual([g[0]["id"] for g in out], ["0", "1", "2", "3", "4"])

    def test_progress_reports_against_the_total(self):
        f = fetcher({})
        seen = []
        f.by_fingerprints([[{"hash": "x", "algorithm": "OSHASH"}]] * 5,
                          batch_size=2, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(2, 5), (4, 5), (5, 5)])

    def test_an_empty_input_makes_no_request(self):
        f = fetcher({})
        self.assertEqual(f.by_fingerprints([]), [])
        self.assertEqual(f.transport.calls, [])

    def test_a_video_with_no_fingerprints_at_all_is_refused(self):
        # An empty inner list is a client bug; the server would reject the batch.
        f = fetcher({})
        with self.assertRaises(ValueError):
            f.by_fingerprints([[]])

    def test_a_batch_size_below_one_is_refused_not_silently_empty(self):
        # range(0, n, -1) is empty, so a negative size would return [] for a
        # non-empty input: no request, no error, and the one-candidate-list-per-video
        # postcondition gone. Truncating our own answer is what the group count
        # guard below exists to prevent the server doing.
        f = fetcher({})
        entries = [[{"hash": "os-a", "algorithm": "OSHASH"}]]
        for size in (0, -1):
            with self.subTest(batch_size=size):
                with self.assertRaises(ValueError):
                    f.by_fingerprints(entries, batch_size=size)
        self.assertEqual(f.transport.calls, [])

    def test_a_short_group_list_from_the_server_is_an_error_not_silent_truncation(self):
        class ShortTransport:
            def __call__(self, payload):
                return {"data": {"findScenesBySceneFingerprints": [[]]}}
        f = build_fetcher(ShortTransport())
        with self.assertRaises(StashDBError):
            f.by_fingerprints([[{"hash": "a", "algorithm": "OSHASH"}],
                               [{"hash": "b", "algorithm": "OSHASH"}]])


class TestTheServersBatchCap(unittest.TestCase):
    """stash-box caps ``findScenesBySceneFingerprints`` at 40 scenes a call."""

    def test_the_default_batch_is_the_documented_cap(self):
        self.assertEqual(MAX_FINGERPRINT_BATCH, 40)
        f = fetcher({})
        f.by_fingerprints(entries(90))
        self.assertEqual([len(c["variables"]["fps"]) for c in f.transport.calls],
                         [40, 40, 10])

    def test_a_batch_the_server_calls_too_big_is_halved_and_retried(self):
        # The refusal loses every video in the batch, so the recovery has to be
        # a smaller ask, not a re-send. 40 -> 20 -> 10 on the left, then the same
        # on the right: two wasted requests, forty videos saved.
        transport = CappedTransport(cap=10)
        f = build_fetcher(transport)
        out = f.by_fingerprints(entries(40), batch_size=40)
        self.assertEqual(len(out), 40)
        self.assertEqual(transport.sizes, [40, 20, 10, 10, 20, 10, 10])

    def test_the_halves_keep_the_input_order(self):
        # The answer is positional. A retry that reassembled the halves out of
        # order would attach every scene to the wrong file, silently.
        table = {"os-%d" % i: [scene(str(i), oshashes=["os-%d" % i])]
                 for i in range(8)}

        class HalvingTransport(FakeTransport):
            def __call__(self, payload):
                if len(payload["variables"]["fps"]) > 2:
                    return {"errors": [{"message": "too many scenes"}]}
                return super().__call__(payload)

        f = build_fetcher(HalvingTransport(table))
        out = f.by_fingerprints(entries(8), batch_size=8)
        self.assertEqual([g[0]["id"] for g in out],
                         ["0", "1", "2", "3", "4", "5", "6", "7"])

    def test_a_single_video_the_server_still_refuses_is_not_halved_for_ever(self):
        # Nothing left to halve. A server that refuses one video is one this
        # code does not understand, and guessing further would just loop.
        transport = CappedTransport(cap=0)
        f = build_fetcher(transport)
        with self.assertRaises(StashDBError):
            f.by_fingerprints(entries(1))
        self.assertEqual(transport.sizes, [1])

    def test_any_other_failure_is_not_retried_smaller(self):
        # Asking for less does not fix a transport error, and pretending it
        # might would turn one outage into six requests.
        transport = CappedTransport(cap=0, error="upstream is on fire")
        f = build_fetcher(transport)
        with self.assertRaises(StashDBError):
            f.by_fingerprints(entries(40), batch_size=40)
        self.assertEqual(transport.sizes, [40])


if __name__ == "__main__":
    unittest.main()
