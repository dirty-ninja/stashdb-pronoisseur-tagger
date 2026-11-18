import os
import tempfile
import threading
import unittest
from unittest import mock

from tagger import (frames, identify as identify_module, library_common,
                    library_sync as sync, matcher, review_cards)
from tagger.library_app import (IDENTIFY_BATCH, OPT_IN_STATUSES, LibraryApp,
                               LibraryStateError)
from tagger.library_store import LibraryStore
from tagger.pronoisseur import Pronoisseur, PronoisseurError
from tagger.stashdb import StashDB
from tests.fakes import FakePronTransport, FakeStashDBTransport

#: A well-formed scene UUID. It has to be one: `extract_scene_uuid` only accepts a
#: bracketed 8-4-4-4-12 hex UUID (a malformed id is ignored, by design and by test),
#: so a readable stand-in like "scene-1" would make every video here `no-identity`.
SCENE_ID = "019f2a6c-3b71-7c48-9d02-5ae81f4b6c37"

SCENE = {
    "id": SCENE_ID, "title": "Example Scene One", "release_date": "2020-05-23",
    "duration": 1800,
    "studio": {"id": "s-1", "name": "Example Studio",
               "parent": {"id": "s-0", "name": "Example Network"}},
    "performers": [{"as": None, "performer": {"id": "p-1", "name": "Jane Example",
                                              "aliases": ["Jane E"],
                                              "gender": "FEMALE"}}],
    "tags": [{"id": "t-1", "name": "Outdoor",
              "category": {"id": "c-setting", "name": "Setting", "group": "SCENE"}}],
    "images": [{"id": "i-1", "url": "https://cdn/x.jpg"}],
    "fingerprints": [{"hash": "aaaabbbbccccdddd", "algorithm": "OSHASH",
                      "duration": 1800}],
}

VIDEO = {"id": 42, "source_id": 1,
         "rel_path": "Example Network\\x [%s]\\a.mp4" % SCENE_ID,
         "path": "D:\\library\\Example Network\\x [%s]\\a.mp4" % SCENE_ID,
         "duration_secs": 1800.0, "title": "A Mp4", "release_date": None,
         "release_date_estimated": True, "studio": None, "models": [],
         "categories": [], "external_ids": [], "rating": 0, "favourite": False,
         "availability": "available", "metadata_source": "filename"}

NO_UUID = dict(VIDEO, id=43, rel_path="Jane Example\\clip_s01.mp4",
               path="D:\\library\\New Folder\\Jane Example\\clip_s01.mp4")

#: Two more scene ids for the fingerprint tests. Hex 8-4-4-4-12, like SCENE_ID.
SCENE_B_ID = "019f2a6c-3b71-7c48-9d02-00000000000b"
SCENE_C_ID = "019f2a6c-3b71-7c48-9d02-00000000000c"

#: Two phashes 64 bits apart. They have to be **hex of equal length**: `hamming`
#: refuses a length mismatch and `classify` drops a file phash it cannot parse,
#: so a readable stand-in like "ph-1" would silently skip the phash branch
#: entirely and every perceptual test would pass for the wrong reason.
PHASH_A = "0f0f0f0f0f0f0f0f"
PHASH_B = "f0f0f0f0f0f0f0f0"


#: What a current Pronoisseur reports under `/info`'s `fingerprints` key.
DEFAULT_FINGERPRINTS = {"phash_enabled": True, "videos": 0, "oshash": 0,
                        "phash": 0, "phash_failed": 0}


def build(videos=(VIDEO,), scenes=None, hashes=None, by_hash=None, phashes=None,
          performers=None, search=None, categories=None, seed_categories=True,
          stashdb_key="k", save_settings=None, framer=None, path_exists=None,
          clock=None, fingerprints=DEFAULT_FINGERPRINTS,
          omit_fingerprint_keys=False, **kw):
    directory = tempfile.mkdtemp()
    db = os.path.join(directory, "state.db")
    # `hashes`/`phashes` are path-keyed. Pronoisseur serves each video's
    # fingerprints on the listing, so they go onto copies of the seeded rows
    # (the fixtures are shared module-level dicts).
    if not omit_fingerprint_keys:
        videos = [dict(v, oshash=(hashes or {}).get(v.get("path")),
                       phash=(phashes or {}).get(v.get("path")))
                  for v in videos]
    pron_transport = FakePronTransport(videos=videos, fingerprints=fingerprints,
                                       **kw)
    stash_transport = FakeStashDBTransport(
        scenes if scenes is not None else {SCENE_ID: SCENE}, by_hash=by_hash,
        performers=performers, search=search, categories=categories)
    extra = {key: value for key, value in (("framer", framer),
                                            ("path_exists", path_exists),
                                            ("clock", clock)) if value is not None}
    app = LibraryApp(
        cfg={"pronoisseur_url": "http://127.0.0.1:8869",
             "pronoisseur_token": "pron_test", "pronoisseur_page_size": 500},
        stashdb=StashDB("http://x", stashdb_key, interval=0,
                        transport=stash_transport),
        library_store=LibraryStore(db),
        client_factory=lambda url, token: Pronoisseur(url, token,
                                                      transport=pron_transport),
        save_settings=save_settings, **extra)
    if seed_categories:
        # Keeps the scan from making a categories request in tests that count calls.
        app.library_store.replace_tag_categories(
            [{"id": "c-seed", "name": "Seed", "group": "SCENE", "description": None}])
    app.pron_transport = pron_transport
    app.stash_transport = stash_transport
    return app


def run_scan(app, **kw):
    app.start_scan(**kw)
    app.await_scan()          # test-only: joins the worker thread
    return app.scan_state()


def run_identify(app):
    app.start_identify()
    app.await_identify()      # test-only: joins the worker thread
    return app.identify_state()


def fingerprint_calls(app):
    """How many batched fingerprint lookups a run actually made."""
    return len([call for call in app.stash_transport.calls
                if "fps" in (call.get("variables") or {})])


def fp_scene(scene_id, oshashes=(), phashes=(), duration=1800.0):
    """A StashDB scene carrying the fingerprints a test wants it found by."""
    return {"id": scene_id, "title": "Scene " + scene_id[-4:],
            "release_date": "2019-03-04", "duration": duration,
            "studio": {"id": "s-9", "name": "Studio Nine", "parent": None},
            "performers": [], "tags": [],
            "images": [{"id": "i-9", "url": "https://cdn/%s.jpg" % scene_id[-4:]}],
            "fingerprints": ([{"hash": h, "algorithm": "OSHASH"} for h in oshashes]
                             + [{"hash": h, "algorithm": "PHASH"} for h in phashes])}


def legacy(video_id):
    """A library file with no UUID in its path — no folder names its scene.

    It carries a `content_hash`, because that is what the fingerprint cache and
    the decision memory are keyed by.
    """
    rel = "Archive\\clip-%d.mp4" % video_id
    return dict(VIDEO, id=video_id, rel_path=rel, path="D:\\library\\" + rel,
                content_hash="ch-%d" % video_id, title="Clip %d" % video_id)


def parsed(video_id, **fields):
    """A legacy file carrying what Pronoisseur read out of its filename.

    The assisted search is driven entirely by that parse, so the parse has to be
    in the fixture. Every name in it is invented.
    """
    row = dict(legacy(video_id), title="Wave Set Three",
               release_date="2019-03-04",
               models=[{"id": 0, "name": "Model Alpha"}])
    row.update(fields)
    return row


def found_scene(scene_id, date):
    """A scene as `queryScenes` answers with it.

    SCENE_FIELDS, so deliberately **no** `fingerprints` and a studio with no
    parent: a card built from a search result has less to read than one built
    from a fingerprint lookup, and that difference must not crash the grid.
    """
    return {"id": scene_id, "title": "Scene " + scene_id[-4:],
            "release_date": date, "duration": 1800,
            "studio": {"id": "s-9", "name": "Studio Nine"},
            "performers": [], "tags": [],
            "images": [{"id": "i-9", "url": "https://cdn/%s.jpg" % scene_id[-4:]}]}


def scene_query_inputs(app):
    """The SceneQueryInput of every assisted search the fake transport saw."""
    return [call["variables"]["input"] for call in app.stash_transport.calls
            if "query Scenes(" in (call.get("query") or "")]


def performer_lookups(app):
    """The name each performer resolution the fake transport saw asked about.

    One entry per upstream request, so a caller that iterated a string into its
    letters shows up here as the letters it sent.
    """
    return [(call["variables"]["input"] or {}).get("names")
            for call in app.stash_transport.calls
            if "query Performers(" in (call.get("query") or "")]


CATEGORIES = [{"id": "c-1", "name": "Setting", "group": "SCENE", "description": None},
              {"id": "c-2", "name": "Hair", "group": "PEOPLE", "description": None}]


class TestCategories(unittest.TestCase):
    def test_refresh_replaces_the_local_table(self):
        app = build(categories=CATEGORIES)
        self.assertEqual(app.refresh_categories(), {"categories": 2})
        self.assertEqual([c["name"] for c in app.library_store.tag_categories()],
                         ["Hair", "Setting"])

    def test_state_and_mapping_never_touch_stashdb(self):
        app = build()
        before = len(app.stash_transport.calls)
        app.state()
        app.mapping()
        self.assertEqual(len(app.stash_transport.calls), before)

    def test_scan_fetches_categories_when_the_table_is_empty(self):
        app = build(categories=CATEGORIES, seed_categories=False)
        app.connect()
        run_scan(app)
        self.assertEqual(len(app.library_store.tag_categories()), 2)

    def test_scan_skips_the_fetch_when_categories_exist(self):
        app = build(categories=CATEGORIES)
        app.connect()
        run_scan(app)
        self.assertFalse([c for c in app.stash_transport.calls
                          if "TagCategories" in c["query"]])


class TestConnect(unittest.TestCase):
    def test_state_before_connecting_says_so(self):
        app = build()
        state = app.state()
        self.assertFalse(state["connected"])
        self.assertFalse(state["canWrite"])

    def test_connect_reports_the_app_name_and_scopes(self):
        app = build()
        out = app.connect()
        self.assertTrue(out["connected"])
        self.assertEqual(out["info"]["app"]["name"], "tagger")
        self.assertTrue(out["canWrite"])

    def test_a_read_only_token_connects_but_cannot_write(self):
        app = build(scopes=("library.read",))
        out = app.connect()
        self.assertTrue(out["connected"])
        # Every key the UI reads is camelCase, in connect() as in state().
        self.assertFalse(out["canWrite"])

    def test_a_bad_token_reports_the_api_code_and_does_not_connect(self):
        app = build()
        out = app.connect(token="pron_wrong")
        self.assertFalse(out["connected"])
        self.assertEqual(out["code"], "unauthorized")

    def test_the_token_is_never_written_to_the_state_database(self):
        app = build()
        app.connect(token="pron_test")
        with open(app.library_store.path, "rb") as fh:
            self.assertNotIn(b"pron_test", fh.read())

    def test_a_failed_connect_does_not_destroy_the_working_token(self):
        # A typo in the Token box must not cost the config token for the life of
        # the process: there is no way back from that short of a restart.
        app = build()
        self.assertTrue(app.connect()["connected"])
        failed = app.connect(token="pron_wrong")
        self.assertFalse(failed["connected"])
        self.assertEqual(failed["code"], "unauthorized")
        self.assertTrue(app.state()["hasToken"])
        self.assertTrue(app.connect()["connected"])


class TestScan(unittest.TestCase):
    def test_a_folder_id_is_verified_from_pronoisseurs_oshash(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(fingerprint_calls(app), 0)   # Scan looks nothing up

    def test_a_folder_id_without_an_oshash_falls_to_duration(self):
        app = build()
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["counts"]["verified-by-duration"], 1)
        row = state["buckets"]["verified-by-duration"][0]
        self.assertIn("Pronoisseur has no oshash for this file yet", row["note"])
        self.assertEqual(fingerprint_calls(app), 0)

    def test_a_verified_row_carries_no_missing_oshash_note(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        row = run_scan(app)["buckets"]["verified"][0]
        self.assertNotIn("no oshash", row["note"])

    def test_the_scan_keeps_pronoisseurs_fingerprint_state(self):
        app = build(fingerprints={"phash_enabled": False, "phash_failed": 2,
                                  "videos": 1, "oshash": 0, "phash": 0})
        app.connect()
        self.assertEqual(run_scan(app)["fingerprints"],
                         {"state": "known", "phashEnabled": False, "phashFailed": 2})

    def test_a_failed_info_read_does_not_fail_the_scan(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        app.pron_transport.info_error = (
            500, {"error": {"code": "boom", "message": "x"}})
        state = run_scan(app)
        self.assertEqual(state["label"], "done")
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(state["fingerprints"]["state"], "unknown")

    def test_an_older_pronoisseur_is_reported_as_older(self):
        app = build(fingerprints=None, omit_fingerprint_keys=True)
        app.connect()
        self.assertEqual(run_scan(app)["fingerprints"]["state"], "older")
    def test_a_uuid_video_is_matched_verified_and_previewed(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["counts"]["verified"], 1)
        preview = app.preview()
        change = preview["rows"][0]
        self.assertEqual(change["patch"]["title"], "Example Scene One")
        self.assertEqual(change["cover"], "https://cdn/x.jpg")

    def test_a_video_with_no_uuid_lands_in_its_own_bucket_and_is_never_guessed(self):
        app = build(videos=(VIDEO, NO_UUID), hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["counts"]["no-identity"], 1)
        self.assertEqual([r["video_id"] for r in state["buckets"]["no-identity"]], [43])

    def test_a_duration_disagreement_is_a_mismatch_and_never_previewed(self):
        app = build(hashes={VIDEO["path"]: "0000000000000000"},
                    videos=(dict(VIDEO, duration_secs=900.0),))
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["counts"]["uuid-mismatch"], 1)
        self.assertEqual(app.preview()["rows"], [])

    def test_an_unverified_row_is_previewed_only_when_it_is_ticked(self):
        scene = dict(SCENE, duration=None, fingerprints=[])
        app = build(scenes={SCENE_ID: scene})
        app.connect()
        run_scan(app)
        self.assertEqual(app.preview()["rows"], [])
        self.assertEqual(len(app.preview(include_ids=[42])["rows"]), 1)

    def test_a_deleted_scene_is_reported_not_found_and_skipped(self):
        app = build(scenes={})
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["counts"]["not-found"], 1)

    def test_a_scan_covers_the_whole_library(self):
        # More than the 2,000 a run used to stop at: with nothing hashed here,
        # there is no cost left for a cap to bound.
        rows = [dict(NO_UUID, id=i, rel_path="Jane Example\\clip_%d.mp4" % i)
                for i in range(2101)]
        app = build(videos=rows)
        app.connect()
        state = run_scan(app)
        self.assertEqual(state["considered"], 2101)
        self.assertEqual(state["counts"]["no-identity"], 2101)
        self.assertNotIn("capped", state)
        self.assertNotIn("capped", state["finished"])

    def test_the_finished_scan_counts_folder_ids_fetched_and_saved(self):
        # Two videos name the same scene and one names none: one distinct id,
        # fetched from StashDB on the first scan and read from the saved copy
        # on the second.
        twin = dict(VIDEO, id=44,
                    rel_path="Other Folder\b [%s]\b.mp4" % SCENE_ID)
        app = build(videos=[VIDEO, twin, NO_UUID])
        app.connect()
        first = run_scan(app)["finished"]
        self.assertEqual((first["folderIds"], first["fetched"], first["saved"]),
                         (1, 1, 0))
        # A second scan inside the 30 days costs StashDB nothing.
        second = run_scan(app)["finished"]
        self.assertEqual((second["folderIds"], second["fetched"], second["saved"]),
                         (1, 0, 1))

    def test_a_scan_with_no_folder_ids_reports_zero_of_each(self):
        app = build(videos=[NO_UUID])
        app.connect()
        fin = run_scan(app)["finished"]
        self.assertEqual((fin["folderIds"], fin["fetched"], fin["saved"]),
                         (0, 0, 0))

    def test_the_scan_calls_the_folder_id_check_by_what_it_does(self):
        # The check compares each folder's scene against Pronoisseur's own
        # fingerprint; nothing opens a file, so the stage must not say so.
        seen = []
        app = build()
        app.connect()
        real = sync.classify_uuid_match

        def spy(*args, **kw):
            seen.append(app._scan["label"])
            return real(*args, **kw)

        with mock.patch.object(sync, "classify_uuid_match", spy):
            run_scan(app)
        self.assertEqual(seen, ["checking folder ids"])

    def test_a_second_scan_costs_no_stashdb_requests(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        before = len(app.stash_transport.calls)
        run_scan(app)
        self.assertEqual(len(app.stash_transport.calls), before)

    def test_the_finished_summary_never_contradicts_the_counts(self):
        # Raw row status and diffed status diverge for an already-synced video:
        # the row says `verified`, the diff says `in-sync`. Only one of them may
        # be reported, or a finished scan claims both "1 verified" and "0
        # verified / 1 in-sync" in the same payload.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app.start_apply([42])
        app.await_apply()
        state = run_scan(app, resync=True)
        self.assertEqual(state["counts"]["in-sync"], 1)
        self.assertEqual(state["counts"]["verified"], 0)
        self.assertNotIn("verified", state["finished"])
        # The folder-id counts are about StashDB lookups, not row statuses.
        self.assertEqual(state["finished"],
                         {"considered": 1, "folderIds": 1, "fetched": 0,
                          "saved": 1})

    def test_the_status_poll_survives_pronoisseur_going_away(self):
        # state() -> scan_state() -> _changes() -> _existing() is four GETs. If
        # they throw, the poll that exists to report "scan failed" fails instead
        # and the page renders nothing.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)

        def gone(*args, **kwargs):
            raise PronoisseurError("connection refused", code=None, status=None)

        app._client.models = gone
        app._client.studios = gone
        app._client.categories = gone
        app._client.aliases = gone
        app._existing_cache = None       # nothing to fall back to
        state = app.state()
        self.assertTrue(state["connected"])
        self.assertIn("counts", state["scan"])
        self.assertEqual(app.preview()["rows"][0]["video_id"], 42)

    def test_the_entity_index_survives_an_invalidation_racing_its_own_read(self):
        # `start_scan` and `_apply_run`'s `finally` both clear `_existing_cache`
        # from other threads, so an `_existing()` that stored the index on the
        # attribute and then read the attribute back for its return value can be
        # handed `None` between the two statements. `plan_change` absorbs `{}`
        # silently, so the apply path would go on to plan against an EMPTY index:
        # every model and studio proposed as new, which is precisely the
        # case-sensitive entity fork `_apply_run`'s re-plan exists to prevent.
        #
        # An attribute that is always `None` when read back is the strongest
        # possible stand-in for that race, and deterministic - no sleeps, no
        # second thread, nothing to lose. It passes only if the value is built
        # into a local and returned from the local.
        class AlwaysNone:
            def __get__(self, obj, owner=None):
                return None

            def __set__(self, obj, value):
                pass

        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        app.__class__ = type("Racy", (type(app),),
                             {"_existing_cache": AlwaysNone()})
        existing = app._existing(refresh=True)
        self.assertEqual(sorted(existing),
                         ["aliases", "categories", "models", "studios"])

    def test_scanning_without_a_connection_is_refused(self):
        app = build()
        with self.assertRaises(RuntimeError):
            app.start_scan()

    def test_the_preview_reports_the_entities_the_run_would_create(self):
        # The per-video field diffs are only half of what an apply writes: it also
        # creates models, studios and categories and pushes alias rows, and none of
        # that appeared anywhere before the click.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        preview = app.preview()
        self.assertEqual(preview["entities"]["models"], 1)
        self.assertEqual(preview["entities"]["studios"], 1)
        self.assertEqual(preview["entities"]["aliases"], 1)
        self.assertEqual(preview["unverified"], 0)

    def test_polling_the_scan_state_does_not_re_read_the_entity_lists(self):
        # The UI polls about once a second; each GET costs the app a video count for
        # every entry, so the names are read once per run, not once per poll.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app.scan_state()
        before = len([c for c in app.pron_transport.calls
                      if c["path"] == "/api/v1/models"])
        for _ in range(5):
            app.scan_state()
            app.preview()
        after = len([c for c in app.pron_transport.calls
                     if c["path"] == "/api/v1/models"])
        self.assertEqual(before, after)


class TestApply(unittest.TestCase):
    def _applied(self, **kw):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"}, **kw)
        app.connect()
        app.library_store.update_settings({"allowlist": ["c-setting"]})
        run_scan(app)
        app.start_apply([42])
        app.await_apply()
        return app

    def test_applies_the_diff_to_the_api(self):
        app = self._applied()
        video = app.pron_transport.videos[42]
        self.assertEqual(video["title"], "Example Scene One")
        self.assertEqual(video["release_date"], "2020-05-23")
        self.assertFalse(video["release_date_estimated"])
        self.assertEqual(video["studio"]["name"], "Example Network")
        self.assertEqual([m["name"] for m in video["models"]], ["Jane Example"])
        self.assertEqual([c["name"] for c in video["categories"]], ["Outdoor"])
        self.assertEqual(video["external_ids"],
                         [{"provider": "stashdb", "id": SCENE_ID}])

    def test_entities_are_stamped_with_their_stashdb_ids_before_the_batch(self):
        app = self._applied()
        studio = next(s for s in app.pron_transport.studios if s["name"] == "Example Network")
        self.assertEqual(studio["external_ids"],
                         [{"provider": "stashdb", "id": "s-0"}])
        order = [c["path"] for c in app.pron_transport.calls
                 if c["method"] == "POST"]
        self.assertLess(order.index("/api/v1/studios"),
                        order.index("/api/v1/videos/batch"))

    def test_aliases_are_pushed_once_as_actor_kind(self):
        app = self._applied()
        self.assertIn({"kind": "actor", "raw_token": "Jane E",
                       "canonical_name": "Jane Example"}, app.pron_transport.aliases)

    def test_the_run_is_logged_per_video(self):
        app = self._applied()
        entry = app.library_store.sync_log([42])[42]
        self.assertEqual(entry["scene_id"], SCENE_ID)
        self.assertIn("title", entry["fields"])

    def test_a_second_run_proposes_nothing(self):
        app = self._applied()
        run_scan(app, resync=True)
        self.assertEqual(app.preview()["rows"], [])
        self.assertEqual(app.scan_state()["counts"]["in-sync"], 1)

    def test_the_preview_converges_with_no_second_scan_at_all(self):
        # After an apply the preview must not keep offering the rows just written:
        # every diff has to be recomputed against the video as written, not against
        # the row the SCAN read.
        app = self._applied()
        preview = app.preview()
        self.assertEqual(preview["rows"], [])
        self.assertEqual(preview["entities"],
                         {"models": 0, "studios": 0, "categories": 0, "aliases": 0})
        self.assertEqual(app.scan_state()["counts"]["in-sync"], 1)

    def test_the_alias_rows_converge_too(self):
        # The videos alone are not enough: `plan_entity_upserts` has to skip aliases
        # the app already holds, or a run that wrote N alias rows re-proposes N.
        app = self._applied()
        self.assertEqual(app.preview()["entities"]["aliases"], 0)
        run_scan(app, resync=True)
        self.assertEqual(app.preview()["entities"]["aliases"], 0)

    def test_the_finished_summary_reads_as_before(self):
        finished = self._applied().apply_state()["finished"]
        self.assertEqual(finished["applied"], 1)
        self.assertEqual(finished["entities"], 3)      # model, studio, category
        self.assertEqual(finished["aliases"], 1)
        self.assertEqual(finished["refused"], 0)
        self.assertEqual(finished["refusals"], [])
        self.assertIsNone(finished["note"])

    def test_the_rows_are_re_read_with_one_bounded_listing(self):
        # Not one `GET /videos/{id}` per row: the contract has no by-ids read, so this is
        # the paged listing, filtered here - and `page_size` over 500 is a 422,
        # not a clamp.
        app = self._applied()
        listings = [c for c in app.pron_transport.calls
                    if c["method"] == "GET" and c["path"].startswith("/api/v1/videos?")
                    and "batch" not in c["path"]]
        after_write = listings[-1]["path"]
        self.assertIn("page_size=500", after_write)
        # Unfiltered: the scan's own filter hides exactly the rows just stamped.
        self.assertNotIn("missing_external_id", after_write)
        self.assertEqual(len([c for c in app.pron_transport.calls
                              if c["method"] == "GET"
                              and c["path"].startswith("/api/v1/videos?")]), 2)

    def test_a_re_read_that_fails_keeps_the_rows_and_says_so(self):
        # The writes landed. Only the page's picture of them is behind, and a
        # successful apply must not be reported as an error because of it.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)

        def gone(*args, **kwargs):
            raise PronoisseurError("connection refused")

        app._client.all_videos = gone
        app.start_apply([42])
        app.await_apply()
        state = app.apply_state()
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["applied"], 1)
        self.assertEqual(state["finished"]["note"], library_common.STALE_NOTE)
        # The write itself is real, whatever the page now shows.
        self.assertEqual(app.pron_transport.videos[42]["title"],
                         "Example Scene One")

    def test_a_run_that_wrote_nothing_re_reads_nothing(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        del app.pron_transport.videos[42]        # the app forgot this video
        before = len([c for c in app.pron_transport.calls if c["method"] == "GET"])
        app.start_apply([42])
        app.await_apply()
        self.assertEqual(app.apply_state()["finished"]["refused"], 1)
        self.assertEqual(app.apply_state()["finished"]["note"], None)
        after = [c for c in app.pron_transport.calls if c["method"] == "GET"]
        self.assertEqual(len([c for c in after[before:]
                              if c["path"].startswith("/api/v1/videos?")]), 0)

    def test_a_read_only_token_refuses_to_apply(self):
        app = build(scopes=("library.read",))
        app.connect()
        run_scan(app)
        with self.assertRaises(RuntimeError):
            app.start_apply([42])

    def test_applying_while_a_scan_is_running_is_refused(self):
        # Apply would write the *previous* scan's rows: a plan nobody approved.
        # The flag is set directly so the refusal is tested, not a thread race.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app._scan["running"] = True
        with self.assertRaises(LibraryStateError):
            app.start_apply([42])
        self.assertFalse(app.apply_state()["running"])

    def test_a_row_the_app_refuses_is_reported_and_does_not_fail_the_run(self):
        # `POST /videos/batch` is all-or-nothing, so one refused row would stop the
        # whole run. The refusal names the item it will not take, and that is a
        # statement about that row and nothing else.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        del app.pron_transport.videos[42]        # the app forgot this video
        app.start_apply([42])
        app.await_apply()
        state = app.apply_state()
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["applied"], 0)
        self.assertEqual(state["finished"]["refused"], 1)
        self.assertEqual(state["finished"]["refusals"][0]["videoId"], 42)
        self.assertIn("not found", state["finished"]["refusals"][0]["error"])

    def test_a_refusal_the_app_will_not_itemise_still_stops_the_run(self):
        # No `details` list means the app is talking about the whole request, and
        # there is no subset of it to retry.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app.pron_transport.batch_error = (
            422, {"error": {"code": "validation", "message": "batch too large",
                            "details": None}})
        app.start_apply([42])
        app.await_apply()
        state = app.apply_state()
        self.assertIn("batch too large", state["error"])
        self.assertIsNone(state["finished"])

    def test_a_failure_that_is_not_a_refusal_stops_the_run_as_before(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app.pron_transport.batch_error = (
            500, {"error": {"code": "server", "message": "the app fell over"}})
        app.start_apply([42])
        app.await_apply()
        self.assertIn("the app fell over", app.apply_state()["error"])


class TestApplyRefusals(unittest.TestCase):
    """One row the app will not take must not cost the batch it rides in.

    ``POST /videos/batch`` is all-or-nothing, so a single scene StashDB dates as
    ``"2019"`` would stop the whole apply. The refusal names the item's index, and
    an index is a statement about that row and nothing else.
    """

    IDS = (42, 43, 44)

    def refusal(self, *entries):
        return (422, {"error": {"code": "validation",
                                "message": "one or more items are invalid; "
                                           "nothing was written",
                                "details": list(entries)}})

    def batches(self, app):
        """The video ids of every `POST /videos/batch`, in order."""
        return [[item["id"] for item in call["body"]["items"]]
                for call in app.pron_transport.calls
                if call["path"] == "/api/v1/videos/batch"]

    def _three(self):
        rows = tuple(dict(VIDEO, id=video_id,
                          rel_path="Example Network\\v%d [%s]\\a.mp4"
                                   % (video_id, SCENE_ID),
                          path="D:\\library\\v%d.mp4" % video_id)
                     for video_id in self.IDS)
        app = build(videos=rows,
                    hashes={row["path"]: "aaaabbbbccccdddd" for row in rows})
        app.connect()
        run_scan(app)
        return app

    def test_one_refused_row_costs_only_itself_and_the_batch_still_lands(self):
        app = self._three()
        del app.pron_transport.videos[43]        # the app forgot this one video
        app.start_apply(list(self.IDS))
        app.await_apply()

        state = app.apply_state()
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["applied"], 2)
        self.assertEqual(state["finished"]["refused"], 1)
        # The whole batch first, then the same batch without the row it named.
        self.assertEqual(self.batches(app), [[42, 43, 44], [42, 44]])
        for video_id in (42, 44):
            self.assertEqual(app.pron_transport.videos[video_id]["title"],
                             "Example Scene One")

    def test_the_refused_row_says_why_on_the_row_and_in_the_finished_line(self):
        app = self._three()
        del app.pron_transport.videos[43]
        app.start_apply(list(self.IDS))
        app.await_apply()

        refusal, = app.apply_state()["finished"]["refusals"]
        self.assertEqual(refusal["videoId"], 43)
        self.assertIn("not found", refusal["error"])
        row = next(r for r in app.preview()["rows"] if r["video_id"] == 43)
        self.assertIn("Pronoisseur refused this write", row["note"])
        self.assertIn("not found", row["note"])

    def test_only_the_rows_that_landed_are_logged(self):
        app = self._three()
        del app.pron_transport.videos[43]
        app.start_apply(list(self.IDS))
        app.await_apply()
        self.assertEqual(sorted(app.library_store.sync_log()), [42, 44])

    def test_a_batch_of_nothing_but_refused_rows_sends_no_retry(self):
        app = self._three()
        for video_id in self.IDS:
            del app.pron_transport.videos[video_id]
        app.start_apply(list(self.IDS))
        app.await_apply()
        state = app.apply_state()
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["applied"], 0)
        self.assertEqual(state["finished"]["refused"], 3)
        self.assertEqual(len(self.batches(app)), 1)

    def test_a_retry_refused_in_its_turn_stops_the_run_with_the_apps_message(self):
        # Once, never in a loop: a second refusal is the app disagreeing with
        # something this code has already tried to accommodate, and peeling a
        # 500-row batch apart one row per request is not a recovery.
        app = self._three()
        app.pron_transport.batch_errors = [
            self.refusal({"index": 1, "id": 43,
                          "error": "release_date must be YYYY-MM-DD"}),
            self.refusal({"index": 0, "id": 42, "error": "title must be a string"}),
        ]
        app.start_apply(list(self.IDS))
        app.await_apply()

        state = app.apply_state()
        self.assertIn("one or more items are invalid", state["error"])
        self.assertEqual(state["details"][0]["error"], "title must be a string")
        self.assertIsNone(state["finished"])
        self.assertEqual(self.batches(app), [[42, 43, 44], [42, 44]])
        # All-or-nothing on both attempts, so nothing was written at all.
        self.assertEqual(app.pron_transport.videos[42]["title"], "A Mp4")
        # The first refusal is still on its row: it happened, whatever came next.
        row = next(r for r in app.preview()["rows"] if r["video_id"] == 43)
        self.assertIn("release_date must be YYYY-MM-DD", row["note"])

    def test_a_refusal_naming_no_index_stops_the_run(self):
        app = self._three()
        app.pron_transport.batch_error = (
            422, {"error": {"code": "validation", "message": "items must hold "
                                                             "1-500 entries",
                            "details": None}})
        app.start_apply(list(self.IDS))
        app.await_apply()
        self.assertIn("items must hold 1-500 entries",
                      app.apply_state()["error"])
        self.assertEqual(len(self.batches(app)), 1)

    def test_an_index_outside_the_batch_is_not_acted_on(self):
        # Dropping position 9 of a three-item batch would drop the wrong row, or
        # none, and call the rest a success either way.
        app = self._three()
        app.pron_transport.batch_error = self.refusal(
            {"index": 9, "id": 99, "error": "video 99 not found"})
        app.start_apply(list(self.IDS))
        app.await_apply()
        self.assertIn("one or more items are invalid",
                      app.apply_state()["error"])
        self.assertEqual(len(self.batches(app)), 1)

    def test_a_detail_list_with_no_index_at_all_stops_the_run(self):
        app = self._three()
        app.pron_transport.batch_error = self.refusal({"error": "something"})
        app.start_apply(list(self.IDS))
        app.await_apply()
        self.assertTrue(app.apply_state()["error"])
        self.assertEqual(len(self.batches(app)), 1)

    def test_a_scene_stashdb_dates_only_to_the_year_no_longer_refuses_at_all(self):
        # The same case, end to end: the partial date is widened
        # before it is sent, so there is nothing for the app to refuse.
        year_only = dict(SCENE, release_date="2019")
        app = build(videos=(VIDEO,), scenes={SCENE_ID: year_only},
                    hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app.start_apply([42])
        app.await_apply()
        state = app.apply_state()
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["refused"], 0)
        self.assertEqual(app.pron_transport.videos[42]["release_date"],
                         "2019-01-01")
        self.assertTrue(app.pron_transport.videos[42]["release_date_estimated"])

    def test_a_name_added_between_scan_and_apply_is_not_forked(self):
        # The scan plans against an empty model index, so it resolves the StashDB
        # spelling "Jane Example". The user then adds "jane example" inside
        # Pronoisseur. The app's upsert is case-SENSITIVE, so an apply that still
        # trusts the scan-time index forks the performer into two entities — via
        # the POST, via the batch's models.add, or both.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        self.assertEqual(app.preview()["rows"][0]["patch"]["models"],
                         {"add": ["Jane Example"]})

        app.pron_transport.models.append(
            {"id": 7, "name": "jane example", "external_ids": [], "video_count": 0})
        app.start_apply([42])
        app.await_apply()

        self.assertIsNone(app.apply_state()["error"])
        self.assertEqual([m["name"] for m in app.pron_transport.videos[42]["models"]],
                         ["jane example"])
        self.assertEqual([m["name"] for m in app.pron_transport.models],
                         ["jane example"])

    def test_apply_only_touches_the_videos_asked_for(self):
        rows = (VIDEO, dict(VIDEO, id=44, rel_path="b [%s]\\c.mp4" % SCENE_ID))
        app = build(videos=rows, hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        app.start_apply([42])
        app.await_apply()
        self.assertEqual(app.pron_transport.videos[44]["title"], "A Mp4")


class TestMapping(unittest.TestCase):
    def test_mapping_lists_allowlisted_tags_with_their_resolution(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        app.library_store.update_settings({"allowlist": ["c-setting"]})
        run_scan(app)
        rows = app.mapping()["tags"]
        self.assertEqual(rows[0]["name"], "Outdoor")
        self.assertEqual(rows[0]["resolves_to"], "Outdoor")
        self.assertEqual(rows[0]["videos"], 1)

    def test_an_override_changes_the_resolution(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        app.library_store.update_settings({"allowlist": ["c-setting"]})
        run_scan(app)
        app.set_tag_mapping("t-1", "Outside", "map")
        self.assertEqual(app.mapping()["tags"][0]["resolves_to"], "Outside")
        self.assertEqual(app.preview()["rows"][0]["patch"]["categories"],
                         {"add": ["Outside"]})


class TestIdentify(unittest.TestCase):
    def test_an_oshash_hit_settles_the_row_in_one_lookup(self):
        video = legacy(50)
        scene = fp_scene(SCENE_B_ID, oshashes=["os-50"])
        app = build(videos=(video,), hashes={video["path"]: "os-50"},
                    by_hash={"os-50": [scene]})
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(state["finished"]["phashes"], 0)
        self.assertEqual(fingerprint_calls(app), 1)
        self.assertEqual(app.scan_state()["buckets"]["verified"][0]["scene_id"],
                         SCENE_B_ID)

    def test_a_row_with_both_values_whose_oshash_misses_ends_likely(self):
        # One perceptual match inside the tolerance, durations agreeing: never
        # `verified`, because a near-duplicate encode of a different scene scores
        # exactly this.
        video = legacy(51)
        scene = fp_scene(SCENE_B_ID, phashes=[PHASH_A], duration=1800.0)
        app = build(videos=(video,), hashes={video["path"]: "os-51"},
                    phashes={video["path"]: PHASH_A},
                    by_hash={PHASH_A: [scene]})
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["likely"], 1)
        self.assertEqual(state["finished"]["phashes"], 1)
        self.assertEqual(fingerprint_calls(app), 1)
        self.assertEqual(app.scan_state()["buckets"]["likely"][0]["scene_id"],
                         SCENE_B_ID)

    def test_two_scenes_listing_the_same_bytes_become_a_question(self):
        video = legacy(52)
        app = build(videos=(video,), hashes={video["path"]: "os-52"},
                    by_hash={"os-52": [fp_scene(SCENE_B_ID, oshashes=["os-52"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-52"])]})
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["ambiguous"], 1)
        card = state["questions"][0]
        self.assertEqual(card["videoId"], 52)
        self.assertEqual([c["sceneId"] for c in card["candidates"]],
                         [SCENE_B_ID, SCENE_C_ID])
        # A card carries what a cover grid needs, never the whole scene payload.
        self.assertEqual(card["candidates"][0]["cover"],
                         "https://cdn/%s.jpg" % SCENE_B_ID[-4:])
        self.assertNotIn("scene", card["candidates"][0])

    def test_a_whisparr_disagreement_keeps_the_uuid_it_arrived_with(self):
        # The folder UUID is half of the disagreement a user fixes by hand, so
        # it has to survive identify overwriting `scene_id` with the real scene.
        mismatch = dict(VIDEO, id=53, duration_secs=900.0, content_hash="ch-53")
        app = build(videos=(mismatch,), hashes={VIDEO["path"]: "os-53"},
                    by_hash={"os-53": [fp_scene(SCENE_B_ID, oshashes=["os-53"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-53"])]})
        app.connect()
        self.assertEqual(run_scan(app)["counts"]["uuid-mismatch"], 1)
        state = run_identify(app)
        self.assertEqual(state["questions"][0]["whisparrSceneId"], SCENE_ID)

    def test_a_video_pronoisseur_has_not_hashed_is_pending_and_the_run_finishes(self):
        # A video Pronoisseur could not read costs its own row and nothing more.
        good, bad = legacy(54), legacy(55)
        app = build(videos=(good, bad), hashes={good["path"]: "os-54"},
                    by_hash={"os-54": [fp_scene(SCENE_B_ID, oshashes=["os-54"])]})
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(state["finished"]["pending-hash"], 1)
        self.assertEqual(state["finished"]["failed"], 0)
        self.assertIsNone(state["error"])
        note = app.scan_state()["buckets"]["pending-hash"][0]["note"]
        self.assertEqual(note, library_common.PENDING_NOT_YET)

    def test_a_remembered_fingerprint_costs_no_lookup_on_a_re_run(self):
        video = legacy(56)
        app = build(videos=(video,), hashes={video["path"]: "os-56"},
                    by_hash={"os-56": [fp_scene(SCENE_B_ID, oshashes=["os-56"])]})
        app.connect()
        run_scan(app)
        run_identify(app)
        self.assertEqual(app.library_store.decisions()["ch-56"]["source"],
                         "fingerprint")
        run_scan(app)             # the rows come back `no-identity` from scratch
        calls = len(app.stash_transport.calls)
        state = run_identify(app)
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(len(app.stash_transport.calls), calls)

    def test_a_remembered_rejection_is_an_answer_and_is_not_looked_up(self):
        # The scene below WOULD verify this row. The rejection has to stop it.
        video = legacy(57)
        app = build(videos=(video,), hashes={video["path"]: "os-57"},
                    by_hash={"os-57": [fp_scene(SCENE_B_ID, oshashes=["os-57"])]})
        app.connect()
        app.library_store.record_decision("ch-57", None, "rejected")
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["unmatched"], 1)
        self.assertEqual(fingerprint_calls(app), 0)
        self.assertIn("rejected",
                      app.scan_state()["buckets"]["unmatched"][0]["note"])

    def test_a_cancelled_run_stops_between_batches_and_keeps_what_it_settled(self):
        videos = [legacy(400 + i) for i in range(IDENTIFY_BATCH + 10)]
        app = build(videos=tuple(videos),
                    hashes={v["path"]: "os-%d" % v["id"] for v in videos},
                    by_hash={"os-%d" % v["id"]: [fp_scene(
                        "019f2a6c-3b71-7c48-9d02-%012d" % v["id"],
                        oshashes=["os-%d" % v["id"]])] for v in videos})
        app.connect()
        run_scan(app)
        # Cancel from inside the first lookup: deterministic, no sleeps.
        app.stash_transport.on_fingerprint_call = (
            lambda n: n == 1 and app.cancel_identify())
        state = run_identify(app)
        self.assertFalse(state["running"])
        self.assertEqual(state["label"], "cancelled")
        self.assertEqual(state["counts"]["verified"], IDENTIFY_BATCH)  # kept
        self.assertEqual(fingerprint_calls(app), 1)       # the second never ran
        self.assertEqual(app.scan_state()["counts"]["no-identity"], 10)

    def test_the_preview_survives_ambiguous_and_unmatched_rows(self):
        # `ambiguous` and `unmatched` rows have no scene. Without the UNPLANNABLE
        # guard `plan_change` gets None and the whole page dies on every poll.
        ambiguous, unmatched = legacy(65), legacy(66)
        app = build(videos=(VIDEO, ambiguous, unmatched),
                    hashes={VIDEO["path"]: "aaaabbbbccccdddd",
                            ambiguous["path"]: "os-65", unmatched["path"]: "os-66"},
                    phashes={unmatched["path"]: PHASH_B},
                    by_hash={"os-65": [fp_scene(SCENE_B_ID, oshashes=["os-65"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-65"])]})
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["ambiguous"], 1)
        self.assertEqual(state["counts"]["unmatched"], 1)
        self.assertEqual([r["video_id"] for r in app.preview()["rows"]], [42])
        self.assertEqual(app.scan_state()["counts"]["ambiguous"], 1)

    def test_sixty_rows_take_two_requests_and_stay_aligned(self):
        # The answer is index-aligned, so a batching bug attaches the wrong scene
        # to a file rather than failing: pin the pairing, not just the count.
        videos, hashes, by_hash, expected = [], {}, {}, {}
        for index in range(60):
            video = legacy(100 + index)
            digest = "os-%03d" % index
            scene_id = "019f2a6c-3b71-7c48-9d02-%012d" % index
            videos.append(video)
            hashes[video["path"]] = digest
            by_hash[digest] = [fp_scene(scene_id, oshashes=[digest])]
            expected[video["id"]] = scene_id
        app = build(videos=tuple(videos), hashes=hashes, by_hash=by_hash)
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["verified"], 60)
        self.assertEqual(fingerprint_calls(app), 2)
        self.assertEqual({c["video_id"]: c["scene_id"]
                          for c in app.scan_state()["buckets"]["verified"]},
                         expected)

    def test_the_batch_size_is_the_servers_own_cap(self):
        # stash-box refuses more than 40 scenes per findScenesBySceneFingerprints
        # call; a larger batch is refused and loses the whole lookup.
        from tagger.scenes import MAX_FINGERPRINT_BATCH
        self.assertEqual(IDENTIFY_BATCH, 40)
        self.assertEqual(IDENTIFY_BATCH, MAX_FINGERPRINT_BATCH)

    def test_one_failed_batch_does_not_throw_away_the_rest_of_the_run(self):
        # An identify run over a large library is long. A single bad request
        # partway through must cost that batch, not the run: the rows it covered keep
        # their status and gain a note, everything else is
        # identified as normal, and the summary says how many were lost.
        videos, hashes, by_hash = [], {}, {}
        for index in range(IDENTIFY_BATCH + 10):
            video = legacy(200 + index)
            digest = "os-f%03d" % index
            videos.append(video)
            hashes[video["path"]] = digest
            by_hash[digest] = [fp_scene("019f2a6c-3b71-7c48-9d02-%012d" % index,
                                        oshashes=[digest])]
        app = build(videos=tuple(videos), hashes=hashes, by_hash=by_hash)
        app.connect()
        run_scan(app)
        # The first fingerprint lookup fails; every later one is answered.
        real, spent = app.stashdb._transport, []

        def flaky(payload):
            if "fps" in (payload.get("variables") or {}) and not spent:
                spent.append(True)
                return {"errors": [{"message": "server exploded"}]}
            return real(payload)

        app.stashdb._transport = flaky
        state = run_identify(app)
        self.assertEqual(state["label"], "done")
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["failed"], IDENTIFY_BATCH)
        self.assertEqual(state["counts"]["verified"], 10)
        rows = app.scan_state()["buckets"]["no-identity"]
        self.assertEqual(len(rows), IDENTIFY_BATCH)
        self.assertIn("StashDB lookup failed", rows[0]["note"])
        self.assertIn("server exploded", rows[0]["note"])

    def test_a_short_answer_settles_nothing_and_says_so_on_the_row(self):
        # The answer is index-aligned, so a group count that disagrees would
        # attach the wrong scene to a file. `by_fingerprints` refuses to hand one
        # back, so the batch settles nothing — and because it settles *nothing*,
        # the run can safely carry on and report the batch as failed instead of
        # throwing away every row it had already identified.
        video = legacy(68)
        app = build(videos=(video,), hashes={video["path"]: "os-68"})
        app.connect()
        run_scan(app)
        app.stashdb._transport = lambda payload: {
            "data": {"findScenesBySceneFingerprints": []}}
        state = run_identify(app)
        self.assertEqual(state["label"], "done")
        self.assertIsNone(state["error"])
        self.assertEqual(state["finished"]["failed"], 1)
        # The row is untouched apart from a note saying why.
        self.assertEqual(app.scan_state()["counts"]["no-identity"], 1)
        row = app.scan_state()["buckets"]["no-identity"][0]
        self.assertIn("index-aligned", row["note"])

    def test_the_statuses_identify_produces_are_always_counted(self):
        # The UI seeds its pill list from these counts, so a status
        # missing from BUCKETS would get rows and no count and the pill would
        # silently vanish. They must be present at zero, before any run.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        counts = run_scan(app)["counts"]
        for status in ("likely", "ambiguous", "unmatched"):
            with self.subTest(status=status):
                self.assertEqual(counts[status], 0)

    def test_a_scan_replacing_the_rows_mid_run_cannot_redirect_the_writes(self):
        # `_scan_run` rebinds `self._rows` to a freshly built list. A run that
        # re-read the attribute per write would settle position N of a *different*
        # scan: one video's scene attached to another, a fingerprint cached under
        # the wrong `content_hash`, and a `record_decision` under it that every
        # later run would honour. The run owns its list, so a scan orphans it.
        target, intruder = legacy(80), legacy(99)
        app = build(videos=(target,), hashes={target["path"]: "os-80"},
                    by_hash={"os-80": [fp_scene(SCENE_B_ID, oshashes=["os-80"])]})
        app.connect()
        run_scan(app)
        own = app._rows                     # the list the run is about to bind

        # A real second scan's rows, so the replacement can never drift in shape.
        other = build(videos=(intruder,), hashes={intruder["path"]: "os-99"})
        other.connect()
        run_scan(other)

        # Swap the list out mid-run, exactly as a scan landing would.
        app.stash_transport.on_fingerprint_call = (
            lambda n: setattr(app, "_rows", other._rows))
        state = run_identify(app)

        self.assertIsNone(state["error"])
        self.assertEqual(own[0]["status"], "verified")    # settled in its own list
        self.assertEqual(own[0]["scene_id"], SCENE_B_ID)
        self.assertEqual(list(app.library_store.decisions()), ["ch-80"])
        self.assertEqual(app._rows[0]["status"], "no-identity")   # untouched
        self.assertIsNone(app._rows[0]["scene"])
        self.assertNotIn("from_identify", app._rows[0])

    def test_a_remembered_scene_stashdb_deleted_goes_back_through_the_passes(self):
        # The only path that puts a row *back* into the lookups. The decision is
        # left alone - the user may have made it by hand - but a scene that no
        # longer exists is not an answer, so the file is identified again.
        video = legacy(83)
        app = build(videos=(video,), hashes={video["path"]: "os-83"},
                    by_hash={"os-83": [fp_scene(SCENE_B_ID, oshashes=["os-83"])]})
        app.connect()
        app.library_store.record_decision("ch-83", SCENE_C_ID, "fingerprint")
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(fingerprint_calls(app), 1)       # it WAS looked up again
        self.assertEqual(app.scan_state()["buckets"]["verified"][0]["scene_id"],
                         SCENE_B_ID)                      # the live scene
        self.assertEqual(app.library_store.decisions()["ch-83"]["scene_id"],
                         SCENE_B_ID)                      # re-remembered correctly

    def test_a_remembered_fingerprint_the_hashes_no_longer_support_says_so(self):
        # The scene still exists, so unlike the test above the row is settled
        # straight from memory — but StashDB no longer lists this file's oshash on
        # it, so `candidate_matches` finds nothing and there is no honest
        # confidence to show. The earlier run's answer still stands; what must not
        # happen is settling it `verified` in silence, which would leave the one
        # row whose evidence has evaporated looking exactly like the rows whose
        # has not. The video carries its oshash, so this is the "no longer
        # listed" arm and not merely "Pronoisseur has no fingerprint".
        video = legacy(84)
        app = build(videos=(video,), hashes={video["path"]: "os-84"},
                    scenes={SCENE_C_ID: fp_scene(SCENE_C_ID,
                                                 oshashes=["os-a-different-file"])})
        app.connect()
        app.library_store.record_decision("ch-84", SCENE_C_ID, "fingerprint")
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["verified"], 1)
        row = app.scan_state()["buckets"]["verified"][0]
        self.assertEqual(row["scene_id"], SCENE_C_ID)
        self.assertIn("no longer corroborate", row["note"])
        self.assertIsNone(app.preview()["rows"][0]["confidence"])

        # And a remembered decision the fingerprints DO still support says nothing
        # of the sort - the note is only for the row that lost its evidence.
        other = legacy(85)
        app2 = build(videos=(other,), hashes={other["path"]: "os-85"},
                     scenes={SCENE_C_ID: fp_scene(SCENE_C_ID,
                                                  oshashes=["os-85"])})
        app2.connect()
        app2.library_store.record_decision("ch-85", SCENE_C_ID, "fingerprint")
        run_scan(app2)
        run_identify(app2)
        kept = app2.scan_state()["buckets"]["verified"][0]
        self.assertNotIn("no longer corroborate", kept["note"])
        self.assertEqual(app2.preview()["rows"][0]["confidence"],
                         matcher.BY_OSHASH)

    def test_the_state_carries_the_identify_panel(self):
        video = legacy(82)
        app = build(videos=(video,), hashes={video["path"]: "os-82"},
                    by_hash={"os-82": [fp_scene(SCENE_B_ID, oshashes=["os-82"])]})
        app.connect()
        run_scan(app)
        run_identify(app)
        identify = app.state()["identify"]
        self.assertEqual(identify["counts"]["verified"], 1)
        self.assertEqual(identify["label"], "done")
        self.assertEqual(identify, app.identify_state())

    def test_identifying_a_fully_identified_library_reports_nothing_to_do(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)          # the one row is `verified` by its folder UUID
        self.assertEqual(app.start_identify(),
                         {"started": False, "reason": "nothing to identify"})

    def test_a_second_identify_while_one_runs_is_reported_not_started(self):
        # The flag is set directly so the branch is tested, not a thread race.
        video = legacy(84)
        app = build(videos=(video,), hashes={video["path"]: "os-84"})
        app.connect()
        run_scan(app)
        app._identify["running"] = True
        out = app.start_identify()
        self.assertFalse(out["started"])
        self.assertEqual(out["reason"], "already running")
        self.assertIn("counts", out["identify"])

    def test_identifying_before_any_scan_is_refused(self):
        app = build()
        app.connect()
        with self.assertRaises(LibraryStateError):
            app.start_identify()

    def test_identifying_while_a_scan_is_running_is_refused(self):
        # The rows are the *previous* scan's until the running one lands. The flag
        # is set directly so the refusal is tested, not a thread race.
        video = legacy(67)
        app = build(videos=(video,), hashes={video["path"]: "os-67"})
        app.connect()
        run_scan(app)
        app._scan["running"] = True
        with self.assertRaises(LibraryStateError):
            app.start_identify()
        self.assertFalse(app.identify_state()["running"])


def hex_phash(n):
    """A distinct, well-formed perceptual hash that no scene in these tests lists."""
    return "%016x" % (0x1000 + n)


class TestProgressiveIdentify(unittest.TestCase):
    """Results land while the lookups are still running.

    A whole-library run is many requests, and nothing should wait for the last.
    These pin what makes the run usable before then: each batch is settled as its
    answer comes back, a row is decided exactly once, and the rows already
    decided can be answered and written while the rest are still waiting.

    The mid-run tests shrink the batch to one video, so the second lookup is a
    moment when the first row is settled and the second still waiting.
    """

    def _misses(self, count, start=300, matched=None):
        """`count` legacy files whose oshash finds nothing but who carry a phash.

        ``matched`` is the one index whose perceptual hash StashDB does list.
        """
        videos = [legacy(start + i) for i in range(count)]
        hashes = {v["path"]: "os-%d" % v["id"] for v in videos}
        phashes = {v["path"]: hex_phash(i) for i, v in enumerate(videos)}
        by_hash = {}
        if matched is not None:
            phashes[videos[matched]["path"]] = PHASH_A
            by_hash[PHASH_A] = [fp_scene(SCENE_B_ID, phashes=[PHASH_A])]
        return build(videos=tuple(videos), hashes=hashes, phashes=phashes,
                     by_hash=by_hash)

    def test_each_batch_is_settled_before_the_next_is_looked_up(self):
        n = IDENTIFY_BATCH
        app = self._misses(n * 2 + 5, matched=0)
        app.connect()
        run_scan(app)
        seen = {}

        def at(call):
            if call == 2:                 # the second batch, not yet answered
                seen["lookups"] = fingerprint_calls(app)
                seen["counts"] = app.identify_state()["counts"]
                seen["likely"] = app.scan_state()["buckets"]["likely"]

        app.stash_transport.on_fingerprint_call = at
        state = run_identify(app)
        self.assertEqual(seen.get("lookups"), 2)
        self.assertEqual(seen["counts"]["likely"], 1)
        self.assertEqual(seen["counts"]["unmatched"], n - 1)
        self.assertEqual(seen["counts"]["waiting"], n + 5)
        self.assertEqual([r["scene_id"] for r in seen["likely"]], [SCENE_B_ID])
        # And the run as a whole still settles everything, a batch at a time.
        self.assertEqual(fingerprint_calls(app), 3)
        self.assertEqual(state["counts"]["waiting"], 0)
        self.assertEqual(state["counts"]["unmatched"], n * 2 + 4)
        self.assertEqual(state["finished"]["phashes"], n * 2 + 5)

    def test_nothing_is_waiting_before_a_run_and_the_count_is_still_there(self):
        # The page seeds its figures from these counts, so the key has to be
        # present at nought rather than missing.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        self.assertEqual(run_scan(app)["counts"]["waiting"], 0)
        self.assertEqual(app.identify_state()["counts"]["waiting"], 0)

    def test_a_row_the_run_has_taken_back_up_is_never_offered_for_writing(self):
        # A `likely` row from an earlier run is an opt-in write. Once a new run
        # takes it up it belongs to that run until the run settles it again:
        # ticked or not it is out of the preview, and it is counted as waiting
        # rather than as the verdict it may be about to lose.
        video = legacy(51)
        app = build(videos=(video,), hashes={video["path"]: "os-51"},
                    phashes={video["path"]: PHASH_A},
                    by_hash={PHASH_A: [fp_scene(SCENE_B_ID, phashes=[PHASH_A])]})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["likely"], 1)
        self.assertEqual(len(app.preview(include_ids=[51])["rows"]), 1)

        seen, real = {}, app.stashdb._transport

        def watching(payload):
            if "fps" in (payload.get("variables") or {}) and not seen:
                seen["preview"] = app.preview(include_ids=[51])["rows"]
                seen["counts"] = app.scan_state()["counts"]
            return real(payload)

        app.stashdb._transport = watching
        run_identify(app)
        self.assertEqual(seen["preview"], [])
        self.assertEqual(seen["counts"]["likely"], 0)
        self.assertEqual(seen["counts"]["waiting"], 1)
        # Settled again, it is offered again.
        self.assertEqual(len(app.preview(include_ids=[51])["rows"]), 1)

    def test_a_settled_question_can_be_answered_while_the_run_is_looking_up(self):
        asked, slow = legacy(70), legacy(71)
        app = build(videos=(asked, slow),
                    scenes={SCENE_B_ID: fp_scene(SCENE_B_ID, oshashes=["os-70"]),
                            SCENE_C_ID: fp_scene(SCENE_C_ID, oshashes=["os-70"])},
                    hashes={asked["path"]: "os-70", slow["path"]: "os-71"},
                    phashes={slow["path"]: hex_phash(1)},
                    by_hash={"os-70": [fp_scene(SCENE_B_ID, oshashes=["os-70"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-70"])]})
        app.connect()
        run_scan(app)
        outcome = {}

        def mid_run(call):
            if call != 2:
                return
            outcome["questions"] = [q["videoId"]
                                    for q in app.identify_state()["questions"]]
            outcome["verified"] = app.decide(70, scene_id=SCENE_B_ID)[
                "counts"]["verified"]
            try:
                app.decide(71, scene_id=SCENE_C_ID)
            except LibraryStateError as exc:
                outcome["refusal"] = str(exc)

        app.stash_transport.on_fingerprint_call = mid_run
        with mock.patch.object(identify_module, "IDENTIFY_BATCH", 1):
            state = run_identify(app)
        self.assertIsNone(state["error"])
        self.assertEqual(outcome.get("questions"), [70])
        self.assertEqual(outcome.get("verified"), 1)
        # The file still waiting for its lookup has no answer to give yet.
        self.assertIn("still being identified", outcome.get("refusal", ""))
        # And the run did not undo the answer given while it was going.
        verified = app.scan_state()["buckets"]["verified"]
        self.assertEqual([(r["video_id"], r["scene_id"]) for r in verified],
                         [(70, SCENE_B_ID)])
        self.assertEqual(app.library_store.decisions()["ch-70"]["source"], "manual")

    def test_accepting_recommendations_mid_run_takes_the_rows_already_settled(self):
        agree, slow = legacy(90), legacy(92)
        rich = dict(fp_scene(SCENE_C_ID, oshashes=["os-90"]),
                    tags=[{"id": "t-1", "name": "Outdoor"}])
        app = build(videos=(agree, slow),
                    hashes={agree["path"]: "os-90", slow["path"]: "os-92"},
                    phashes={slow["path"]: hex_phash(2)},
                    by_hash={"os-90": [fp_scene(SCENE_B_ID, oshashes=["os-90"]),
                                       rich]})
        app.connect()
        run_scan(app)
        outcome = {}

        def mid_run(call):
            if call == 2:
                outcome["accepted"] = app.accept_recommended()

        app.stash_transport.on_fingerprint_call = mid_run
        with mock.patch.object(identify_module, "IDENTIFY_BATCH", 1):
            state = run_identify(app)
        self.assertIsNone(state["error"])
        self.assertEqual((outcome.get("accepted") or {}).get("accepted"), 1)
        self.assertEqual(app.library_store.decisions()["ch-90"],
                         {"scene_id": SCENE_C_ID, "source": "manual",
                          "decided_at": mock.ANY})

    def test_a_question_the_run_has_taken_back_up_cannot_be_answered(self):
        # An ambiguous row from an earlier run is re-decided by the next one, so
        # neither a click nor Accept-all may settle it while that is happening:
        # the run would overwrite the answer the moment its lookup came back.
        agree = legacy(90)
        app = build(videos=(agree,), hashes={agree["path"]: "os-90"},
                    scenes={SCENE_B_ID: fp_scene(SCENE_B_ID, oshashes=["os-90"])},
                    by_hash={"os-90": [fp_scene(SCENE_B_ID, oshashes=["os-90"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-90"])]})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["ambiguous"], 1)

        outcome, real = {}, app.stashdb._transport

        def watching(payload):
            if "fps" in (payload.get("variables") or {}) and not outcome:
                outcome["accepted"] = app.accept_recommended()
                try:
                    app.decide(90, scene_id=SCENE_B_ID)
                except LibraryStateError as exc:
                    outcome["refusal"] = str(exc)
            return real(payload)

        app.stashdb._transport = watching
        state = run_identify(app)
        self.assertEqual(outcome["accepted"],
                         {"accepted": 0, "left": 0, "leftReasons": {}})
        self.assertIn("still being identified", outcome.get("refusal", ""))
        self.assertEqual(state["counts"]["ambiguous"], 1)
        self.assertNotIn("ch-90", app.library_store.decisions())

    def test_a_settled_row_can_be_written_while_the_run_is_looking_up(self):
        hit, slow = legacy(72), legacy(73)
        app = build(videos=(hit, slow),
                    hashes={hit["path"]: "os-72", slow["path"]: "os-73"},
                    phashes={slow["path"]: hex_phash(3)},
                    by_hash={"os-72": [fp_scene(SCENE_B_ID, oshashes=["os-72"])]})
        app.connect()
        run_scan(app)
        outcome = {}

        def mid_run(call):
            if call != 2:
                return
            outcome["started"] = app.start_apply([72]).get("started")
            app.await_apply()
            outcome["apply"] = app.apply_state()

        app.stash_transport.on_fingerprint_call = mid_run
        with mock.patch.object(identify_module, "IDENTIFY_BATCH", 1):
            state = run_identify(app)
        self.assertIsNone(state["error"])
        self.assertTrue(outcome.get("started"))
        self.assertEqual(outcome["apply"]["finished"]["applied"], 1)

    def test_identify_does_not_start_while_an_apply_is_writing(self):
        # The reverse order is the hazard: a run taking up a `likely` row the
        # apply is part-way through writing. The flag is set directly so the
        # refusal is tested, not a thread race.
        video = legacy(74)
        app = build(videos=(video,), hashes={video["path"]: "os-74"})
        app.connect()
        run_scan(app)
        app._apply["running"] = True
        with self.assertRaises(LibraryStateError):
            app.start_identify()
        self.assertFalse(app.identify_state()["running"])

    def test_a_cancel_hands_the_waiting_rows_back_as_the_scan_left_them(self):
        app = self._misses(3, start=310)
        app.connect()
        run_scan(app)
        app.stash_transport.on_fingerprint_call = (
            lambda n: n == 1 and app.cancel_identify())
        with mock.patch.object(identify_module, "IDENTIFY_BATCH", 1):
            state = run_identify(app)
        self.assertEqual(state["label"], "cancelled")
        self.assertEqual(state["counts"]["waiting"], 0)
        counts = app.scan_state()["counts"]
        self.assertEqual(counts["waiting"], 0)
        # The first batch was answered; the two it never reached are as the scan
        # left them.
        self.assertEqual(counts["unmatched"], 1)
        self.assertEqual(counts["no-identity"], 2)

    def test_the_rows_are_handed_back_before_the_run_says_it_has_stopped(self):
        # Otherwise a second Identify clicked in that gap takes the same rows up,
        # and the first run's hand-back then releases them under the second -
        # where an answer would be accepted and the second run would overwrite it.
        app = self._misses(2, start=330)
        app.connect()
        run_scan(app)
        seen, real = [], app._release

        def watching(rows, indexes):
            seen.append(app._identify["running"])
            return real(rows, indexes)

        app._release = watching
        run_identify(app)
        self.assertEqual(seen, [True])

    def test_an_answer_racing_a_new_run_for_the_same_row_is_refused(self):
        # A pick checks the row, fetches the scene over the network, then settles.
        # A run that starts during that fetch takes the row up, so the settle has
        # to check again, under the lock the run takes rows up under - or the
        # answer lands on a row the run is about to re-decide.
        asked = legacy(87)
        app = build(videos=(asked,), hashes={asked["path"]: "os-87"},
                    scenes={SCENE_B_ID: fp_scene(SCENE_B_ID, oshashes=["os-87"])},
                    by_hash={"os-87": [fp_scene(SCENE_B_ID, oshashes=["os-87"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-87"])]})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["ambiguous"], 1)

        gate, real_get = threading.Event(), app.scenes.get
        app._identify_run = lambda rows, indexes: gate.wait(5)

        def racing(scene_id, *args, **kwargs):
            app.start_identify()
            return real_get(scene_id, *args, **kwargs)

        app.scenes.get = racing
        try:
            with self.assertRaises(LibraryStateError) as caught:
                app.decide(87, scene_id=SCENE_B_ID)
            self.assertIn("still being identified", str(caught.exception))
            self.assertNotIn("ch-87", app.library_store.decisions())
        finally:
            gate.set()
            app.await_identify()

    def test_a_run_that_fails_outright_hands_its_waiting_rows_back_too(self):
        app = self._misses(2, start=320)
        app.connect()
        run_scan(app)

        def explode(_call):
            raise RuntimeError("the share went away")

        app.stash_transport.on_fingerprint_call = explode
        state = run_identify(app)
        self.assertEqual(state["label"], "failed")
        counts = app.scan_state()["counts"]
        self.assertEqual(counts["waiting"], 0)
        self.assertEqual(counts["no-identity"], 2)


class TestReviewGrid(unittest.TestCase):
    """What the cover grid says about a row, and in what order.

    A library-relative path over a row of scene covers reads as one more candidate,
    so the card has to make clear which is the file on disk and which are StashDB
    scenes. These tests pin the three things that answer that: the absolute path
    on the card, an order that puts the strongest fit first, and enough metadata
    per card to prefer the richer scene when more than one genuinely fits.

    `_question_card` is exercised directly because the ranking is pure and the
    four tiers cannot all be reached through one live run: `matcher.classify`
    discards phash companions the moment an oshash hits, by design.
    """

    def rated(self, scene_id, kind, agrees, delta, distance=0, **scene):
        """One classified candidate, as `matcher.classify` hands it over."""
        return {"scene": dict(fp_scene(scene_id), **scene), "kind": kind,
                "distance": distance, "duration_agrees": agrees,
                "duration_delta": delta}

    def tags(self, count):
        """`count` tags, as StashDB lists them on a scene."""
        return [{"id": "t-%d" % n, "name": "Tag %d" % n} for n in range(count)]

    def card(self, *candidates, video=None):
        return review_cards._question_card(
            {"video": video if video is not None else legacy(300),
             "note": "two scenes list these bytes", "candidates": list(candidates)})

    def order(self, *candidates):
        return [c["sceneId"] for c in self.card(*candidates)["candidates"]]

    # ------------------------------------------------------------- the order

    def test_the_four_tiers_run_exact_and_agreeing_first_and_perceptual_last(self):
        # Handed over in exactly the wrong order, so a renderer that kept the
        # server's order would fail every assertion below.
        self.assertEqual(
            self.order(self.rated("phash-off", matcher.BY_PHASH, False, 220.0),
                       self.rated("phash-ok", matcher.BY_PHASH, True, 0.9),
                       self.rated("exact-off", matcher.BY_OSHASH, False, 219.9),
                       self.rated("exact-ok", matcher.BY_OSHASH, True, 0.9)),
            ["exact-ok", "exact-off", "phash-ok", "phash-off"])

    def test_a_scene_stashdb_lists_no_duration_for_ranks_below_one_that_agrees(self):
        # `duration_agrees` is None there, not False, and unknown must not win a
        # badge: it is the absence of evidence, not evidence.
        self.assertEqual(
            self.order(self.rated("unknown", matcher.BY_OSHASH, None, None),
                       self.rated("agrees", matcher.BY_OSHASH, True, 2.0)),
            ["agrees", "unknown"])

    def test_inside_a_tier_the_richer_scene_wins_before_the_duration_gap(self):
        # The same bytes are listed under two scenes - one catalogued, one bare -
        # and the catalogued one is the better pick, because the bare listing costs
        # every category the write exists to import. Two seconds of duration must not
        # outweigh fourteen tags.
        self.assertEqual(
            self.order(self.rated("bare", matcher.BY_OSHASH, True, 0.0),
                       self.rated("rich", matcher.BY_OSHASH, True, 2.0,
                                  tags=self.tags(14))),
            ["rich", "bare"])

    def test_a_tier_still_beats_a_tag_count(self):
        # Evidence outranks richness, and it has to: a perceptual near-miss
        # carrying forty tags is still a near-miss, and identical bytes are still
        # identical bytes.
        self.assertEqual(
            self.order(self.rated("near", matcher.BY_PHASH, True, 0.0,
                                  tags=self.tags(40)),
                       self.rated("exact", matcher.BY_OSHASH, True, 0.0)),
            ["exact", "near"])

    def test_a_scene_nobody_counted_ranks_below_one_counted_at_zero(self):
        # `tagCount` is None when the lookup never asked for tags, and unknown is
        # not the same fact as "lists none". It must not out-rank the scene whose
        # emptiness has actually been checked, on the strength of the question
        # never having been put.
        unknown = self.rated("unknown", matcher.BY_OSHASH, True, 0.0)
        unknown["scene"].pop("tags")
        self.assertEqual(
            self.order(unknown, self.rated("counted", matcher.BY_OSHASH, True, 0.0)),
            ["counted", "unknown"])

    def test_inside_a_tier_the_smaller_duration_gap_wins(self):
        self.assertEqual(
            self.order(self.rated("far", matcher.BY_OSHASH, True, -2.4),
                       self.rated("near", matcher.BY_OSHASH, True, 0.9),
                       self.rated("middle", matcher.BY_OSHASH, True, 1.5)),
            ["near", "middle", "far"])

    def test_a_dead_heat_on_duration_goes_to_the_closer_perceptual_distance(self):
        self.assertEqual(
            self.order(self.rated("loose", matcher.BY_PHASH, True, 0.0, distance=4),
                       self.rated("tight", matcher.BY_PHASH, True, 0.0, distance=1)),
            ["tight", "loose"])

    # ----------------------------------------------------- the recommendation

    def test_the_leading_card_is_the_recommendation_and_the_rest_are_not(self):
        card = self.card(self.rated("b", matcher.BY_OSHASH, True, 4.0),
                         self.rated("a", matcher.BY_OSHASH, True, 0.9))
        self.assertEqual([c["recommended"] for c in card["candidates"]],
                         [True, False])
        # And the length agrees, so it may be accepted without a person looking.
        self.assertTrue(card["recommendationSafe"])
        self.assertFalse(card["noDurationMatch"])

    def test_the_badge_claims_the_tags_only_when_the_tags_decided_it(self):
        decided = self.card(self.rated("bare", matcher.BY_OSHASH, True, 0.0),
                            self.rated("rich", matcher.BY_OSHASH, True, 2.0,
                                       tags=self.tags(14)))
        self.assertEqual(decided["candidates"][0]["sceneId"], "rich")
        self.assertTrue(decided["candidates"][0]["recommendedByTags"])

    def test_a_pick_the_bytes_decided_does_not_claim_the_tags_decided_it(self):
        # The runner-up is a whole tier below and carries far more tags. Saying
        # "most tags" here is a badge a reader could disprove at a glance, and
        # one disproved badge is every badge distrusted.
        card = self.card(self.rated("exact", matcher.BY_OSHASH, True, 0.0),
                         self.rated("near", matcher.BY_PHASH, True, 0.0,
                                    tags=self.tags(14)))
        self.assertEqual(card["candidates"][0]["sceneId"], "exact")
        self.assertFalse(card["candidates"][0]["recommendedByTags"])

    def test_a_recommendation_no_length_supports_is_never_safe(self):
        card = self.card(self.rated("a", matcher.BY_OSHASH, False, 219.9),
                         self.rated("b", matcher.BY_PHASH, False, -300.0))
        # Still the recommendation — it is the strongest thing on offer — but
        # never taken for the user: a near-duplicate encode of a *different*
        # scene is exactly what the length check catches.
        self.assertEqual([c["recommended"] for c in card["candidates"]],
                         [True, False])
        self.assertFalse(card["recommendationSafe"])
        # The warning the block carries: not "the top card missed out", but "none
        # of these runs the same length as your file".
        self.assertTrue(card["noDurationMatch"])

    def test_exact_bytes_on_a_scene_with_no_duration_are_safe_anyway(self):
        # Some scenes carry no duration on StashDB. The bytes have already settled
        # the row; refusing it on a length nobody ever recorded would send the
        # strongest evidence there is back to manual review.
        card = self.card(self.rated("unknown", matcher.BY_OSHASH, None, None))
        self.assertTrue(card["recommendationSafe"])

    def test_a_perceptual_match_with_no_duration_is_not_safe(self):
        # Same missing duration, half the evidence: nothing here has ruled out
        # the near-duplicate encode, so this one is a person's to judge.
        card = self.card(self.rated("unknown", matcher.BY_PHASH, None, None))
        self.assertTrue(card["candidates"][0]["recommended"])
        self.assertFalse(card["recommendationSafe"])

    def test_a_top_card_that_misses_out_is_not_the_same_as_nothing_agreeing(self):
        # Identical bytes still outrank a perceptual match that happens to agree
        # on length, so the leader here is recommended but unsafe — and the block
        # must not then claim no candidate matches the file's length, because one
        # does.
        card = self.card(self.rated("exact", matcher.BY_OSHASH, False, 219.9),
                         self.rated("near", matcher.BY_PHASH, True, 0.9))
        self.assertEqual([c["sceneId"] for c in card["candidates"]],
                         ["exact", "near"])
        self.assertFalse(card["recommendationSafe"])
        self.assertFalse(card["noDurationMatch"])

    def test_a_row_with_no_candidates_at_all_warns_about_nothing(self):
        self.assertFalse(self.card()["noDurationMatch"])
        self.assertFalse(self.card()["recommendationSafe"])

    # ------------------------------------------------------------- the file

    def test_the_card_carries_the_absolute_path_of_the_file(self):
        video = legacy(301)
        card = self.card(video=video)
        self.assertEqual(card["path"], video["path"])
        self.assertTrue(card["path"].startswith("D:\\library\\"))
        # Kept beside it: the filter and every existing reader still use it.
        self.assertEqual(card["relPath"], video["rel_path"])

    def test_a_video_with_no_absolute_path_still_carries_its_relative_one(self):
        video = dict(legacy(302))
        video.pop("path")
        card = self.card(video=video)
        self.assertIsNone(card["path"])
        self.assertEqual(card["relPath"], video["rel_path"])

    # --------------------------------------------------------- the metadata

    def test_each_card_counts_the_tags_and_models_stashdb_lists(self):
        rich = self.rated(
            "rich", matcher.BY_OSHASH, True, 0.0,
            tags=[{"id": "t-%d" % n, "name": "Tag %d" % n} for n in range(12)],
            performers=[{"performer": {"id": "p-1", "name": "A"}},
                        {"performer": {"id": "p-2", "name": "B"}}])
        card, = self.card(rich)["candidates"]
        self.assertEqual(card["tagCount"], 12)
        self.assertEqual(card["performerCount"], 2)

    def test_a_scene_that_genuinely_lists_nothing_counts_zero(self):
        # `fp_scene` carries empty arrays, which is a real fact about the scene —
        # the thin duplicate this line exists to help you avoid — and not the
        # same as a lookup that never asked.
        card, = self.card(self.rated("bare", matcher.BY_OSHASH, True, 0.0))[
            "candidates"]
        self.assertEqual((card["tagCount"], card["performerCount"]), (0, 0))

    def test_a_scene_whose_lookup_never_fetched_tags_reports_no_count(self):
        scene = fp_scene("thin")
        scene.pop("tags")
        scene.pop("performers")
        card, = self.card({"scene": scene, "kind": matcher.BY_OSHASH,
                           "distance": 0, "duration_agrees": True,
                           "duration_delta": 0.0})["candidates"]
        self.assertIsNone(card["tagCount"])
        self.assertIsNone(card["performerCount"])

    # ------------------------------------------------------- through a run

    def test_a_live_run_ships_the_path_the_counts_and_the_badge(self):
        video = legacy(303)
        app = build(videos=(video,), hashes={video["path"]: "os-303"},
                    by_hash={"os-303": [
                        fp_scene(SCENE_B_ID, oshashes=["os-303"], duration=1500.0),
                        fp_scene(SCENE_C_ID, oshashes=["os-303"])]})
        app.connect()
        run_scan(app)
        card = run_identify(app)["questions"][0]
        self.assertEqual(card["path"], video["path"])
        # SCENE_C runs 1800s like the file; SCENE_B is five minutes short, so the
        # order the lookup returned is reversed on the way out.
        self.assertEqual([c["sceneId"] for c in card["candidates"]],
                         [SCENE_C_ID, SCENE_B_ID])
        self.assertEqual([c["recommended"] for c in card["candidates"]],
                         [True, False])
        self.assertTrue(card["recommendationSafe"])
        self.assertEqual(card["candidates"][0]["tagCount"], 0)
        self.assertFalse(card["noDurationMatch"])

    def test_a_live_run_recommends_the_richest_of_two_equally_good_scenes(self):
        # Both scenes list the file's bytes and both run its length, so nothing
        # the fingerprints or the durations know separates them. The tags do.
        video = legacy(304)
        rich = dict(fp_scene(SCENE_C_ID, oshashes=["os-304"]),
                    tags=[{"id": "t-%d" % n, "name": "Tag %d" % n}
                          for n in range(9)])
        app = build(videos=(video,), hashes={video["path"]: "os-304"},
                    by_hash={"os-304": [fp_scene(SCENE_B_ID, oshashes=["os-304"]),
                                        rich]})
        app.connect()
        run_scan(app)
        card = run_identify(app)["questions"][0]
        self.assertEqual([c["sceneId"] for c in card["candidates"]],
                         [SCENE_C_ID, SCENE_B_ID])
        self.assertEqual([c["tagCount"] for c in card["candidates"]], [9, 0])
        self.assertTrue(card["candidates"][0]["recommendedByTags"])
        self.assertTrue(card["recommendationSafe"])


class TestDecide(unittest.TestCase):
    def _asked(self, video_id=70):
        """A run that left one row unmatched, waiting for the user's answer."""
        video = legacy(video_id)
        # Both values, so StashDB not knowing them is `unmatched`; an oshash
        # alone would be pending hashing.
        app = build(videos=(video,), hashes={video["path"]: "os-%d" % video_id},
                    phashes={video["path"]: PHASH_B},
                    scenes={SCENE_ID: SCENE,
                            SCENE_C_ID: fp_scene(SCENE_C_ID, oshashes=["other"])})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["unmatched"], 1)
        return app

    def test_a_pick_may_name_a_scene_that_was_never_a_candidate(self):
        # The assisted search finds scenes no fingerprint pointed at, so `pick`
        # fetches the scene rather than looking it up among the candidates.
        app = self._asked()
        state = app.decide(70, scene_id=SCENE_C_ID, action="pick")
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(app.library_store.decisions()["ch-70"],
                         {"scene_id": SCENE_C_ID, "source": "manual",
                          "decided_at": mock.ANY})
        self.assertEqual(app.scan_state()["buckets"]["verified"][0]["scene_id"],
                         SCENE_C_ID)

    def test_picking_a_scene_stashdb_does_not_have_is_refused(self):
        app = self._asked()
        with self.assertRaises(LibraryStateError):
            app.decide(70, scene_id=SCENE_B_ID, action="pick")

    def test_a_rejection_is_remembered_as_an_answer(self):
        app = self._asked()
        state = app.decide(70, action="reject")
        self.assertEqual(state["counts"]["unmatched"], 1)
        self.assertEqual(app.library_store.decisions()["ch-70"]["source"],
                         "rejected")

    def test_clearing_forgets_the_decision_so_a_later_run_re_asks(self):
        # Without this a mistaken pick would be permanent.
        app = self._asked()
        app.decide(70, scene_id=SCENE_C_ID, action="pick")
        state = app.decide(70, action="clear")
        self.assertEqual(app.library_store.decisions(), {})
        self.assertEqual(state["counts"]["unmatched"], 1)

    def test_a_file_with_no_content_hash_is_answered_but_cannot_be_remembered(self):
        # The decision memory is keyed by the content hash. Without one the answer
        # still lands on the row; it just says it will not survive the next scan.
        video = dict(legacy(71))
        video.pop("content_hash")
        app = build(videos=(video,), hashes={video["path"]: "os-71"},
                    scenes={SCENE_ID: SCENE,
                            SCENE_C_ID: fp_scene(SCENE_C_ID, oshashes=["other"])})
        app.connect()
        run_scan(app)
        run_identify(app)
        state = app.decide(71, scene_id=SCENE_C_ID, action="pick")
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(app.library_store.decisions(), {})
        self.assertIn("no content hash",
                      app.scan_state()["buckets"]["verified"][0]["note"])

    def test_an_unknown_decision_action_is_refused(self):
        # A typo in the route must not silently settle nothing and report success.
        app = self._asked()
        with self.assertRaises(LibraryStateError):
            app.decide(70, scene_id=SCENE_C_ID, action="maybe")

    def test_deciding_an_unknown_video_is_refused(self):
        app = self._asked()
        with self.assertRaises(LibraryStateError):
            app.decide(999, scene_id=SCENE_C_ID, action="pick")

    def test_a_run_in_flight_does_not_by_itself_stop_an_answer(self):
        # The rule is per row, not per run: a row the run has settled is never
        # touched by it again, so its answer stands. Only a row the run still
        # owns is refused - see TestProgressiveIdentify. The flag is set directly
        # so what is tested is that the flag alone refuses nothing.
        app = self._asked(video_id=79)
        app._identify["running"] = True
        state = app.decide(79, scene_id=SCENE_C_ID, action="pick")
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(app.library_store.decisions()["ch-79"]["scene_id"],
                         SCENE_C_ID)

    def test_answering_while_a_scan_is_running_is_refused(self):
        # Worse than answering during an identify: `_scan_run` rebinds
        # `self._rows`, so the row this settles is about to be discarded whole.
        # `start_apply` refuses on the same grounds. The flag is set directly so
        # the refusal is what is tested, not a thread race.
        app = self._asked(video_id=81)
        app._scan["running"] = True
        with self.assertRaises(LibraryStateError):
            app.decide(81, scene_id=SCENE_C_ID, action="pick")
        self.assertEqual(app.library_store.decisions(), {})

        app._scan["running"] = False
        state = app.decide(81, scene_id=SCENE_C_ID, action="pick")
        self.assertEqual(state["counts"]["verified"], 1)


class TestAcceptRecommended(unittest.TestCase):
    """One button to accept every row it safely can, and the rows it refuses.

    The grid ranks each row's candidates; this lets the user say yes to all of the
    top picks at once.

    The rule these pin is the one that keeps that safe. Only a row whose
    recommended scene runs the same length as the file is taken; everything else
    is left exactly as it was, counted, and told apart by *why*. A button that
    quietly answered the doubtful rows too would be worse than no button, because
    the answers are remembered and a later run would never ask again.
    """

    def _asked(self):
        """Two ambiguous rows: one the lengths agree on, one they do not."""
        agree, disagree = legacy(90), legacy(91)
        rich = dict(fp_scene(SCENE_C_ID, oshashes=["os-90"]),
                    tags=[{"id": "t-1", "name": "Outdoor"}])
        app = build(
            videos=(agree, disagree),
            hashes={agree["path"]: "os-90", disagree["path"]: "os-91"},
            by_hash={
                # The same bytes under a bare listing and a catalogued one, both
                # running the file's own length.
                "os-90": [fp_scene(SCENE_B_ID, oshashes=["os-90"]), rich],
                # The same bytes under two scenes that both run five minutes
                # short of the file. Nothing here is safe to take unseen.
                "os-91": [fp_scene(SCENE_B_ID, oshashes=["os-91"], duration=1500.0),
                          fp_scene(SCENE_C_ID, oshashes=["os-91"],
                                   duration=1500.0)]})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["ambiguous"], 2)
        return app

    def test_only_the_safe_rows_are_taken_and_the_rest_are_explained(self):
        app = self._asked()
        self.assertEqual(
            app.accept_recommended(),
            {"accepted": 1, "left": 1,
             "leftReasons": {library_common.DURATIONS_DISAGREE: 1}})
        state = app.identify_state()
        self.assertEqual(state["counts"]["verified"], 1)
        self.assertEqual(state["counts"]["ambiguous"], 1)
        # And the one still on screen is the one that needs a person.
        self.assertEqual([q["videoId"] for q in state["questions"]], [91])

    def test_the_scene_recorded_is_the_one_the_grid_recommended(self):
        # Not merely "a candidate": the richer of the two, which is the whole
        # point of ranking on tags. A second sort inside the accept would be how
        # this silently became the other one.
        app = self._asked()
        app.accept_recommended()
        self.assertEqual(app.scan_state()["buckets"]["verified"][0]["scene_id"],
                         SCENE_C_ID)

    def test_the_answer_is_remembered_exactly_as_a_manual_pick_is(self):
        app = self._asked()
        app.accept_recommended()
        self.assertEqual(app.library_store.decisions()["ch-90"],
                         {"scene_id": SCENE_C_ID, "source": "manual",
                          "decided_at": mock.ANY})
        # The row the durations refused wrote nothing down at all — the next run
        # must still ask about it.
        self.assertNotIn("ch-91", app.library_store.decisions())

    def test_an_accepted_row_is_writable_and_reaches_the_preview(self):
        # The number beside Apply is the preview's row count, so an acceptance
        # that never reached the preview would look like a button that did
        # nothing.
        app = self._asked()
        self.assertEqual(app.preview()["rows"], [])
        app.accept_recommended()
        self.assertEqual([r["video_id"] for r in app.preview()["rows"]], [90])

    def test_a_second_call_takes_nothing_and_reports_the_same_rows_left(self):
        # The accepted rows are `verified` now, not ambiguous, so they are simply
        # not seen again. Pressing the button twice must not double anything.
        app = self._asked()
        app.accept_recommended()
        self.assertEqual(
            app.accept_recommended(),
            {"accepted": 0, "left": 1,
             "leftReasons": {library_common.DURATIONS_DISAGREE: 1}})
        self.assertEqual(app.identify_state()["counts"]["verified"], 1)

    def test_byte_exact_rows_stashdb_gives_no_duration_for_are_taken(self):
        # The one exception to the length rule, live: the bytes have settled it,
        # and StashDB simply never recorded a runtime to check against.
        video = legacy(92)
        app = build(videos=(video,), hashes={video["path"]: "os-92"},
                    by_hash={"os-92": [
                        fp_scene(SCENE_B_ID, oshashes=["os-92"], duration=None),
                        fp_scene(SCENE_C_ID, oshashes=["os-92"], duration=None)]})
        app.connect()
        run_scan(app)
        card = run_identify(app)["questions"][0]
        self.assertTrue(card["recommendationSafe"])
        self.assertEqual(app.accept_recommended(),
                         {"accepted": 1, "left": 0, "leftReasons": {}})

    def test_a_row_with_nothing_pickable_is_counted_apart_from_a_disagreement(self):
        # The row is built by hand because `matcher.classify` cannot produce it:
        # an ambiguous verdict always carries the candidates it could not choose
        # between. The branch exists for StashDB data the classifier will take
        # but the picker cannot — a candidate scene carrying no id. There is a
        # grid, nothing on it can be recorded, and calling that a disagreement
        # about length would send the user looking for one that is not there.
        app = self._asked()
        rows = app._rows
        index = [i for i, row in enumerate(rows)
                 if row["video"]["id"] == 91][0]
        headless = dict(rows[index]["candidates"][0])
        headless["scene"] = {key: value for key, value
                             in headless["scene"].items() if key != "id"}
        rows[index] = dict(rows[index], candidates=[headless])
        self.assertFalse(app.identify_state()["questions"][1]["recommendationSafe"])
        self.assertEqual(app.accept_recommended(),
                         {"accepted": 1, "left": 1,
                          "leftReasons": {library_common.NO_CANDIDATES: 1}})

    def test_accepting_while_a_scan_is_in_flight_is_refused(self):
        # A scan rebinds the list whole, so every row this would settle is about
        # to be discarded. An identify run is not refused here: it keeps the rows
        # it still owns out of the accept instead (TestProgressiveIdentify). The
        # flag is set directly so the refusal is what is tested, not a race.
        app = self._asked()
        app._scan["running"] = True
        with self.assertRaises(LibraryStateError):
            app.accept_recommended()
        app._scan["running"] = False
        self.assertEqual(app.library_store.decisions(), {})
        self.assertEqual(app.accept_recommended()["accepted"], 1)

    def test_a_file_with_no_content_hash_is_taken_but_says_it_is_not_remembered(self):
        # The decision memory is keyed by the content hash. Without one the
        # acceptance still lands on the row; it just says it will not survive the
        # next scan — the same caveat a click on a cover carries, from the same
        # helper.
        video = dict(legacy(94))
        video.pop("content_hash")
        rich = dict(fp_scene(SCENE_C_ID, oshashes=["os-94"]),
                    tags=[{"id": "t-1", "name": "Outdoor"}])
        app = build(videos=(video,), hashes={video["path"]: "os-94"},
                    by_hash={"os-94": [fp_scene(SCENE_B_ID, oshashes=["os-94"]),
                                       rich]})
        app.connect()
        run_scan(app)
        run_identify(app)
        self.assertEqual(app.accept_recommended()["accepted"], 1)
        self.assertEqual(app.library_store.decisions(), {})
        note = app.scan_state()["buckets"]["verified"][0]["note"]
        self.assertIn("you accepted the recommended scene", note)
        self.assertIn("no content hash", note)


def cast(scene, *names):
    """A scene with these performers on it. Invented names only."""
    return dict(scene, performers=[
        {"as": None, "performer": {"id": "p-" + sync.name_key(name), "name": name,
                                   "aliases": []}} for name in names])


class TestReviewByModel(unittest.TestCase):
    """Review is grouped by model, and a model's group can be accepted at once.

    A row's models are the ones the video already has in Pronoisseur plus the
    performers on the scene StashDB recommends for it - and only that scene: a
    runner-up's cast would put one video in groups it has nothing to do with.
    """

    def _grouped(self):
        jane = dict(legacy(100), models=[{"id": 1, "name": "Jane Example"}])
        bare, unsafe = legacy(101), legacy(102)
        kay = dict(legacy(103), models=[{"id": 2, "name": "Kay Example"}])
        outdoor = [{"id": "t-1", "name": "Outdoor"}]
        app = build(
            videos=(jane, bare, unsafe, kay),
            models=[{"id": 1, "name": "Jane Example", "external_ids": [],
                     "video_count": 1}],
            hashes={jane["path"]: "os-100", bare["path"]: "os-101",
                    unsafe["path"]: "os-102", kay["path"]: "os-103"},
            # Kay is the one nothing lists; the phash makes that `unmatched`
            # rather than pending hashing.
            phashes={kay["path"]: PHASH_B},
            by_hash={
                # The richer listing leads, so it is the recommendation.
                "os-100": [cast(dict(fp_scene(SCENE_B_ID, oshashes=["os-100"]),
                                     tags=outdoor), "Sam Sample"),
                           cast(fp_scene(SCENE_C_ID, oshashes=["os-100"]),
                                "Lee Other")],
                "os-101": [cast(dict(fp_scene(SCENE_B_ID, oshashes=["os-101"]),
                                     tags=outdoor), "jane example"),
                           cast(fp_scene(SCENE_C_ID, oshashes=["os-101"]),
                                "Lee Other")],
                # No cast anywhere, and five minutes short of the file.
                "os-102": [fp_scene(SCENE_B_ID, oshashes=["os-102"], duration=1500.0),
                           fp_scene(SCENE_C_ID, oshashes=["os-102"],
                                    duration=1500.0)]})
        app.connect()
        run_scan(app)
        counts = run_identify(app)["counts"]
        self.assertEqual((counts["ambiguous"], counts["unmatched"]), (3, 1))
        return app

    def questions(self, app):
        return {q["videoId"]: q for q in app.identify_state()["questions"]}

    @staticmethod
    def field(card, name):
        return [f for f in card["fields"] if f["field"] == name][0]

    def test_a_question_names_the_models_it_is_grouped_under(self):
        asked = self.questions(self._grouped())
        self.assertEqual(asked[100]["models"],
                         [{"name": "Jane Example", "key": "janeexample"},
                          {"name": "Sam Sample", "key": "samsample"}])
        # Spelled as the library already spells her, not as StashDB credits her.
        self.assertEqual(asked[101]["models"],
                         [{"name": "Jane Example", "key": "janeexample"}])
        self.assertEqual(asked[102]["models"], [])

    def test_only_the_recommended_scene_decides_the_group(self):
        for card in self.questions(self._grouped()).values():
            self.assertNotIn("Lee Other", [m["name"] for m in card["models"]])

    def test_every_match_carries_what_picking_it_would_write(self):
        lead, runner = self.questions(self._grouped())[100]["candidates"]
        self.assertEqual(self.field(lead, "models")["kept"], ["Jane Example"])
        self.assertEqual([m["name"] for m in self.field(lead, "models")["added"]],
                         ["Sam Sample"])
        self.assertEqual([m["name"] for m in self.field(runner, "models")["added"]],
                         ["Lee Other"])
        self.assertEqual(self.field(lead, "title")["action"], "rename")
        self.assertEqual(self.field(lead, "title")["proposed"], "Scene 000b")

    def test_a_match_with_no_scene_id_describes_no_write(self):
        # It cannot be picked, so a plan for it would be the plan of picking
        # nothing - a rename to a blank title, a link to no scene.
        app = self._grouped()
        rows = app._rows
        index = [i for i, row in enumerate(rows) if row["video"]["id"] == 100][0]
        headless = dict(rows[index]["candidates"][1])
        headless["scene"] = {key: value for key, value
                             in headless["scene"].items() if key != "id"}
        rows[index] = dict(rows[index], candidates=[rows[index]["candidates"][0],
                                                    headless])
        lead, runner = self.questions(app)[100]["candidates"]
        self.assertTrue(lead["fields"])
        self.assertEqual(runner["fields"], [])

    def test_what_a_match_would_write_follows_the_settings(self):
        app = self._grouped()
        app.library_store.update_settings({"add_models": False})
        lead = self.questions(app)[100]["candidates"][0]
        self.assertEqual(self.field(lead, "models")["action"], "off")
        # The grouping does not follow it: what a scene's cast IS does not
        # change because the write was told to leave models alone.
        self.assertEqual([m["key"] for m in self.questions(app)[100]["models"]],
                         ["janeexample", "samsample"])

    def test_accepting_one_models_group_takes_only_its_safe_rows(self):
        app = self._grouped()
        self.assertEqual(app.accept_recommended(model="samsample"),
                         {"accepted": 1, "left": 0, "leftReasons": {}})
        self.assertEqual(sorted(self.questions(app)), [101, 102])

    def test_a_video_under_two_models_is_taken_from_either_group(self):
        app = self._grouped()
        self.assertEqual(app.accept_recommended(model="janeexample")["accepted"], 2)
        self.assertEqual(sorted(self.questions(app)), [102])

    def test_the_no_model_group_is_the_empty_key_and_keeps_the_length_rule(self):
        app = self._grouped()
        self.assertEqual(app.accept_recommended(model=""),
                         {"accepted": 0, "left": 1,
                          "leftReasons": {library_common.DURATIONS_DISAGREE: 1}})
        self.assertEqual(sorted(self.questions(app)), [100, 101, 102])

    def test_a_group_nobody_is_in_takes_nothing(self):
        app = self._grouped()
        self.assertEqual(app.accept_recommended(model="nobody"),
                         {"accepted": 0, "left": 0, "leftReasons": {}})

    def test_no_model_at_all_is_still_every_group(self):
        app = self._grouped()
        self.assertEqual(app.accept_recommended()["accepted"], 2)

    def test_accepting_the_videos_in_view_takes_only_their_safe_rows(self):
        # The page sends every row to choose that its tabs and filter show,
        # safe or not, so the ones it leaves can still be counted and explained.
        app = self._grouped()
        self.assertEqual(app.accept_recommended(video_ids=[101, 102]),
                         {"accepted": 1, "left": 1,
                          "leftReasons": {library_common.DURATIONS_DISAGREE: 1}})
        self.assertEqual(sorted(self.questions(app)), [100, 102])

    def test_an_empty_view_takes_nothing_not_everything(self):
        app = self._grouped()
        self.assertEqual(app.accept_recommended(video_ids=[]),
                         {"accepted": 0, "left": 0, "leftReasons": {}})
        self.assertEqual(sorted(self.questions(app)), [100, 101, 102])

    def test_a_row_ready_to_write_carries_its_models_and_both_lengths(self):
        # Review and write is one list: a verified row is grouped like a
        # question, and set beside the file with the scene's length.
        app = self._grouped()
        app.accept_recommended(model="samsample")
        row = [r for r in app.preview()["rows"] if r["video_id"] == 100][0]
        self.assertEqual(row["models"],
                         [{"name": "Jane Example", "key": "janeexample"},
                          {"name": "Sam Sample", "key": "samsample"}])
        self.assertEqual((row["duration_secs"], row["scene_duration"],
                          row["duration_agrees"]), (1800.0, 1800.0, True))

    def test_a_ready_row_whose_length_is_unknown_says_unknown_not_no(self):
        app = self._grouped()
        app.accept_recommended(model="samsample")
        index = [i for i, r in enumerate(app._rows) if r["video"]["id"] == 100][0]
        app._rows[index] = dict(app._rows[index],
                                scene=dict(app._rows[index]["scene"], duration=None))
        row = [r for r in app.preview()["rows"] if r["video_id"] == 100][0]
        self.assertIsNone(row["duration_agrees"])

    def test_an_unmatched_row_carries_the_models_it_already_has(self):
        rows = self._grouped().scan_state()["buckets"]["unmatched"]
        self.assertEqual([(r["video_id"], r["models"]) for r in rows],
                         [(103, [{"name": "Kay Example", "key": "kayexample"}])])


class FakeDisk:
    """Stands in for `os.path.exists`: everything exists except `missing`.

    An entry in `missing` is a path, or a drive (`Z:`, `\\\\nas\\media`) that
    takes every path under it. Records every path it is asked about.
    """

    def __init__(self, missing=()):
        self.missing, self.asked = tuple(missing), []

    def __call__(self, path):
        self.asked.append(path)
        text = str(path).rstrip("\\").lower()
        return not any(text == gone.lower() or text.startswith(gone.lower() + "\\")
                       for gone in self.missing)


class FakeClock:
    """A monotonic clock a test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeFramer:
    """Stands in for `frames.safe_frame`: answers from memory, counts calls."""

    def __init__(self, data=b"\xff\xd8 a jpeg", error=None):
        self.data, self.error, self.calls = data, error, []

    def __call__(self, path, fraction, duration=None, **kwargs):
        self.calls.append((path, fraction, duration))
        return (None, self.error) if self.error else (self.data, None)


class TestFrames(unittest.TestCase):
    """Stills from the user's own file, for Review to set beside a StashDB cover.

    Pronoisseur's API has no poster to hand out, so the tool takes its own. Each
    one costs an ffmpeg run over the share, so it is taken once and kept on disk
    beside the state database, keyed by the file's content hash so a re-scan
    does not throw it away.
    """

    def _app(self, framer, video=None, videos=None, exists=None, clock=None):
        """A scanned library. Every file exists unless ``exists`` says otherwise:
        the fixtures' paths are made up, and the real disk must not decide."""
        self.exists = exists or FakeDisk()
        self.clock = clock or FakeClock()
        app = build(videos=videos or (video or legacy(110),), framer=framer,
                    path_exists=self.exists, clock=self.clock)
        app.connect()
        run_scan(app)
        return app

    def frames_dir(self, app):
        return os.path.join(os.path.dirname(app.library_store.path), "tagger-frames")

    def test_a_frame_comes_from_its_point_through_the_file(self):
        framer = FakeFramer()
        app = self._app(framer)
        self.assertEqual(app.frame(110, 1), framer.data)
        self.assertEqual(framer.calls,
                         [("D:\\library\\Archive\\clip-110.mp4", 0.35, 1800.0)])

    def test_a_frame_is_taken_once_and_then_read_back(self):
        framer = FakeFramer()
        app = self._app(framer)
        app.frame(110, 2)
        self.assertEqual(app.frame(110, 2), framer.data)
        self.assertEqual(len(framer.calls), 1)

    def test_a_kept_frame_outlives_a_new_scan(self):
        framer = FakeFramer()
        app = self._app(framer)
        app.frame(110, 0)
        run_scan(app)
        app.frame(110, 0)
        self.assertEqual(len(framer.calls), 1)

    def test_each_point_is_its_own_frame(self):
        framer = FakeFramer()
        app = self._app(framer)
        app.frame(110, 0)
        app.frame(110, 3)
        self.assertEqual([call[1] for call in framer.calls], [0.10, 0.85])

    def test_a_point_out_of_range_is_refused_before_ffmpeg_runs(self):
        framer = FakeFramer()
        app = self._app(framer)
        for n in (-1, len(frames.FRAME_POINTS)):
            with self.assertRaises(ValueError):
                app.frame(110, n)
        self.assertEqual(framer.calls, [])

    def test_a_file_the_scan_did_not_list_is_refused(self):
        # Only files in the scan, by id: the route can never be pointed at a
        # path of the caller's choosing.
        app = self._app(FakeFramer())
        with self.assertRaises(LibraryStateError):
            app.frame(999, 0)

    def test_a_frame_that_cannot_be_read_is_reported_and_not_kept(self):
        framer = FakeFramer(error="ffmpeg could not read the file")
        app = self._app(framer)
        with self.assertRaises(LibraryStateError) as caught:
            app.frame(110, 0)
        self.assertIn("ffmpeg could not read the file", str(caught.exception))
        self.assertFalse(os.listdir(self.frames_dir(app))
                         if os.path.isdir(self.frames_dir(app)) else [])

    def test_a_failed_frame_is_not_tried_again_for_a_minute(self):
        # Stepping through frames, or the page asking why one failed, must not
        # run ffmpeg against the same unreadable file over and over. A share that
        # was asleep often is not a minute later, so it is tried again then.
        framer = FakeFramer(error="ffmpeg could not read the file")
        app = self._app(framer)
        for _ in range(3):
            with self.assertRaises(LibraryStateError) as caught:
                app.frame(110, 0)
        self.assertIn("ffmpeg could not read the file", str(caught.exception))
        self.assertEqual(len(framer.calls), 1)
        self.clock.now += 61
        with self.assertRaises(LibraryStateError):
            app.frame(110, 0)
        self.assertEqual(len(framer.calls), 2)

    # ------------------------------------------- a drive that is not there

    # Writing never needs the file - the Local API updates Pronoisseur's own
    # catalog - but a still does. A network drive that is offline must cost one
    # look, not an ffmpeg run per frame per card.

    def test_a_file_pronoisseur_lists_as_unavailable_is_not_tried(self):
        video = dict(legacy(113), availability="unavailable")
        framer = FakeFramer()
        app = self._app(framer, video=video)
        with self.assertRaises(LibraryStateError) as caught:
            app.frame(113, 0)
        self.assertIn("unavailable", str(caught.exception))
        self.assertEqual(framer.calls, [])
        self.assertEqual(self.exists.asked, [])

    def test_a_drive_that_is_not_there_is_looked_at_once_for_every_file_on_it(self):
        first = dict(legacy(114), path="Z:\\offline\\a.mp4")
        second = dict(legacy(115), path="Z:\\offline\\b.mp4")
        framer = FakeFramer()
        app = self._app(framer, videos=(first, second), exists=FakeDisk(missing=("Z:",)))
        for video_id in (114, 115):
            with self.assertRaises(LibraryStateError) as caught:
                app.frame(video_id, 0)
            self.assertIn("Z: is not reachable", str(caught.exception))
        self.assertEqual(framer.calls, [])
        # The first file and its drive, and nothing after: the second file on
        # the same drive is answered from what the first one found.
        self.assertEqual(self.exists.asked, ["Z:\\offline\\a.mp4", "Z:\\"])

    def test_the_drive_is_looked_at_again_after_a_minute(self):
        video = dict(legacy(116), path="Z:\\offline\\a.mp4")
        disk = FakeDisk(missing=("Z:",))
        app = self._app(FakeFramer(), video=video, exists=disk)
        with self.assertRaises(LibraryStateError):
            app.frame(116, 0)
        disk.missing = ()
        self.clock.now += 61
        self.assertEqual(app.frame(116, 0), FakeFramer().data)

    def test_a_network_share_is_a_drive_too(self):
        first = dict(legacy(117), path="\\\\nas\\media\\a.mp4")
        second = dict(legacy(118), path="\\\\nas\\media\\b.mp4")
        app = self._app(FakeFramer(), videos=(first, second),
                        exists=FakeDisk(missing=("\\\\nas\\media",)))
        for video_id in (117, 118):
            with self.assertRaises(LibraryStateError) as caught:
                app.frame(video_id, 0)
        self.assertIn("\\\\nas\\media is not reachable", str(caught.exception))
        self.assertEqual(len(self.exists.asked), 2)

    def test_a_missing_file_on_a_drive_that_is_there_says_so(self):
        video = dict(legacy(119), path="D:\\moved\\a.mp4")
        app = self._app(FakeFramer(), video=video,
                        exists=FakeDisk(missing=("D:\\moved\\a.mp4",)))
        with self.assertRaises(LibraryStateError) as caught:
            app.frame(119, 0)
        self.assertIn("is not at D:\\moved\\a.mp4", str(caught.exception))

    def test_a_kept_frame_still_shows_while_its_drive_is_offline(self):
        video = dict(legacy(120), path="Z:\\offline\\a.mp4")
        disk = FakeDisk()
        framer = FakeFramer()
        app = self._app(framer, video=video, exists=disk)
        app.frame(120, 0)
        disk.missing = ("Z:",)
        self.assertEqual(app.frame(120, 0), framer.data)
        self.assertEqual(len(framer.calls), 1)

    def test_frames_are_kept_beside_the_state_database(self):
        app = self._app(FakeFramer())
        app.frame(110, 0)
        self.assertEqual(os.listdir(self.frames_dir(app)), ["ch-110-0.jpg"])

    def test_a_file_with_no_content_hash_is_kept_by_its_video_id(self):
        video = dict(legacy(111))
        video.pop("content_hash")
        app = self._app(FakeFramer(), video=video)
        app.frame(111, 0)
        self.assertEqual(os.listdir(self.frames_dir(app)), ["v111-0.jpg"])

    def test_a_content_hash_cannot_write_outside_the_frames_folder(self):
        video = dict(legacy(112), content_hash="..\\..\\evil")
        app = self._app(FakeFramer(), video=video)
        app.frame(112, 0)
        self.assertEqual(os.listdir(self.frames_dir(app)), ["evil-0.jpg"])


class TestAssistedSearch(unittest.TestCase):
    """The last resort: search on the filename parse, and let the user judge."""

    def _ready(self, video, **kw):
        """A scanned library with one row no folder UUID could name."""
        app = build(videos=(video,), hashes={video["path"]: "os-x"}, **kw)
        app.connect()
        run_scan(app)
        return app

    def test_a_performer_name_is_resolved_and_filters_the_search(self):
        app = self._ready(
            parsed(72),
            performers={"Model Alpha": [{"id": "p-1", "name": "Model Alpha"}]},
            search=[found_scene(SCENE_B_ID, "2019-03-04")])
        out = app.assisted_search(72)
        self.assertEqual(out["unresolved"], [])
        # The id has to reach the SceneQueryInput itself: `build_scene_input`
        # reads performers only from its own argument, so an id parked inside
        # `filter` would be inert while reading as though it filtered.
        self.assertEqual(scene_query_inputs(app)[0]["performers"],
                         {"value": ["p-1"], "modifier": "INCLUDES"})
        self.assertEqual([c["sceneId"] for c in out["candidates"]], [SCENE_B_ID])

    def test_a_name_stashdb_does_not_know_is_reported_and_never_sent(self):
        # Silently searching without it would explain nothing when the results
        # are wrong.
        app = self._ready(
            parsed(73, models=[{"id": 0, "name": "Model Alpha"},
                               {"id": 0, "name": "Model Beta"}]),
            performers={"Model Alpha": [{"id": "p-1", "name": "Model Alpha"}]},
            search=[found_scene(SCENE_B_ID, "2019-03-04")])
        out = app.assisted_search(73)
        self.assertEqual(out["unresolved"], ["Model Beta"])
        sent = scene_query_inputs(app)[0]
        self.assertEqual(sent["performers"]["value"], ["p-1"])
        self.assertNotIn("Model Beta", str(sent))

    def test_a_title_override_replaces_the_parsed_title_upstream(self):
        app = self._ready(parsed(74, models=[]),
                          search=[found_scene(SCENE_B_ID, "2019-03-04")])
        out = app.assisted_search(74, title="Different Words Entirely")
        self.assertEqual(scene_query_inputs(app)[0]["title"],
                         "Different Words Entirely")
        # The terms the panel shows beside the box say the same thing the query
        # said, or the user is judging results against a search nobody ran.
        self.assertEqual(out["terms"]["title"], "Different Words Entirely")

    def test_an_emptied_title_box_searches_with_no_title_at_all(self):
        # What the user typed is what gets searched. Falling back to the
        # filename here would search for the very words they just deleted —
        # and an empty performers list already clears performers, so the two
        # boxes have to behave alike.
        app = self._ready(parsed(80, models=[]),
                          search=[found_scene(SCENE_B_ID, "2019-03-04")])
        out = app.assisted_search(80, title="")
        self.assertNotIn("title", scene_query_inputs(app)[0])
        self.assertIsNone(out["terms"]["title"])
        # The results still come back; a title-less search is a real search.
        self.assertEqual([c["sceneId"] for c in out["candidates"]], [SCENE_B_ID])

    def test_a_performer_override_replaces_the_parsed_names(self):
        app = self._ready(
            parsed(75),
            performers={"Model Gamma": [{"id": "p-3", "name": "Model Gamma"}]},
            search=[found_scene(SCENE_B_ID, "2019-03-04")])
        out = app.assisted_search(75, performers=["Model Gamma"])
        self.assertEqual(out["terms"]["performers"], ["Model Gamma"])
        self.assertEqual(out["unresolved"], [])
        self.assertEqual(scene_query_inputs(app)[0]["performers"]["value"],
                         ["p-3"])

    def test_the_candidates_come_back_date_matches_first(self):
        # A rank, not a filter: the window came from a filename, so a scene
        # outside it is still shown, just lower.
        app = self._ready(parsed(76, models=[]),
                          search=[found_scene(SCENE_C_ID, "2021-11-11"),
                                  found_scene(SCENE_B_ID, "2019-03-05")])
        out = app.assisted_search(76)
        self.assertEqual([c["sceneId"] for c in out["candidates"]],
                         [SCENE_B_ID, SCENE_C_ID])
        self.assertTrue(out["candidates"][0]["dateMatch"])
        self.assertFalse(out["candidates"][1]["dateMatch"])
        self.assertEqual(out["dateWindow"],
                         ["2019-03-03", "2019-03-04", "2019-03-05"])
        self.assertEqual(out["count"], 2)

    def test_a_search_card_and_a_question_card_differ_by_one_key_each(self):
        # This is what lets the UI render one cover grid for both. A second,
        # subtly different card builder would need a second grid. Each grid adds
        # only what it alone knows: a search result can match the filename's
        # date, and only the questions grid is ranked, so only it recommends
        # anything or says why. Only a question's matches are set side by side
        # with the file, so only they say what picking them would write.
        ambiguous, searched = legacy(77), parsed(78, models=[])
        app = build(videos=(ambiguous, searched),
                    hashes={ambiguous["path"]: "os-77",
                            searched["path"]: "os-78"},
                    by_hash={"os-77": [fp_scene(SCENE_B_ID, oshashes=["os-77"]),
                                       fp_scene(SCENE_C_ID, oshashes=["os-77"])]},
                    search=[found_scene(SCENE_B_ID, "2019-03-04")])
        app.connect()
        run_scan(app)
        question = run_identify(app)["questions"][0]["candidates"][0]
        card = app.assisted_search(78)["candidates"][0]
        self.assertEqual(set(card) - set(question), {"dateMatch"})
        self.assertEqual(set(question) - set(card),
                         {"recommended", "recommendedByTags", "fields"})
        self.assertEqual(card["cover"], "https://cdn/%s.jpg" % SCENE_B_ID[-4:])
        self.assertEqual(card["studio"], "Studio Nine")
        # No fingerprint pointed here — that is why this search exists at all.
        self.assertIsNone(card["kind"])
        self.assertIsNone(card["distance"])

    def test_searching_for_a_video_this_scan_never_saw_is_refused(self):
        app = self._ready(parsed(79, models=[]))
        with self.assertRaises(LibraryStateError):
            app.assisted_search(999)

    def test_a_question_card_carries_the_terms_the_search_would_use(self):
        # The panel shows the parsed studio and date, and pre-fills the
        # Performers box, BEFORE the first search. It can only do that if the
        # card ships the terms, so this is what stops that panel going back to
        # promising something it has not been given.
        video = parsed(209, studio={"id": 3, "name": "Studio Nine"})
        app = build(videos=(video,), hashes={video["path"]: "os-209"},
                    by_hash={"os-209": [fp_scene(SCENE_B_ID, oshashes=["os-209"]),
                                        fp_scene(SCENE_C_ID, oshashes=["os-209"])]})
        app.connect()
        run_scan(app)
        card = run_identify(app)["questions"][0]
        self.assertEqual(card["terms"]["studio"], "Studio Nine")
        self.assertEqual(card["terms"]["date"], "2019-03-04")
        self.assertEqual(card["terms"]["performers"], ["Model Alpha"])
        self.assertEqual(card["terms"]["title"], "Wave Set Three")
        # The window the results are ranked by, so the panel can name it before
        # a search rather than only after one.
        self.assertEqual(card["dateWindow"],
                         ["2019-03-03", "2019-03-04", "2019-03-05"])
        # The same shape the search itself answers with, so the browser renders
        # both with one renderer instead of two that can drift.
        self.assertEqual(set(card["terms"]), set(app.assisted_search(209)["terms"]))


class TestWriteBack(unittest.TestCase):
    """Which identified rows reach a write, and what the write carries."""

    def test_a_fingerprint_verified_row_is_writable_without_being_ticked(self):
        # The point of `matcher.VERIFIED` and `library_sync.VERIFIED` being one
        # string: a row identified by its bytes flows through the write with nothing
        # ticked, exactly as a row identified by a folder UUID does.
        video = legacy(200)
        app = build(videos=(video,), hashes={video["path"]: "os-200"},
                    by_hash={"os-200": [fp_scene(SCENE_B_ID, oshashes=["os-200"])]})
        app.connect()
        run_scan(app)
        run_identify(app)
        preview = app.preview()                      # nothing ticked
        self.assertEqual([r["video_id"] for r in preview["rows"]], [200])
        self.assertEqual(preview["rows"][0]["scene_id"], SCENE_B_ID)
        self.assertEqual(preview["rows"][0]["confidence"], matcher.BY_OSHASH)

    def test_a_likely_row_reaches_the_preview_only_when_it_is_ticked(self):
        video = legacy(201)
        app = build(videos=(video,), hashes={video["path"]: "os-201"},
                    phashes={video["path"]: PHASH_A},
                    by_hash={PHASH_A: [fp_scene(SCENE_B_ID, phashes=[PHASH_A],
                                                duration=1800.0)]})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["likely"], 1)

        untouched = app.preview()
        self.assertEqual(untouched["rows"], [])
        self.assertEqual(untouched["likely"], 0)
        # `skipped`, not merely an empty `rows`: the row HAS a patch and was held
        # back by `_writable`. Without this, the negative half would read the
        # same whether the row was refused or simply had nothing to write.
        self.assertEqual(untouched["skipped"], 1)

        ticked = app.preview(include_ids=[201])
        self.assertEqual([r["video_id"] for r in ticked["rows"]], [201])
        self.assertEqual(ticked["likely"], 1)
        # Counted apart from `unverified`, which the apply confirmation hesitates
        # over for a different reason.
        self.assertEqual(ticked["unverified"], 0)
        self.assertEqual(ticked["rows"][0]["confidence"], matcher.BY_PHASH)

    def test_an_ambiguous_or_unmatched_row_is_writable_by_no_path(self):
        # Ticked or not: neither has a scene, so neither has anything to write.
        ambiguous, unmatched = legacy(202), legacy(203)
        app = build(videos=(ambiguous, unmatched),
                    hashes={ambiguous["path"]: "os-202",
                            unmatched["path"]: "os-203"},
                    phashes={unmatched["path"]: PHASH_B},
                    by_hash={"os-202": [fp_scene(SCENE_B_ID, oshashes=["os-202"]),
                                        fp_scene(SCENE_C_ID, oshashes=["os-202"])]})
        app.connect()
        run_scan(app)
        state = run_identify(app)
        self.assertEqual(state["counts"]["ambiguous"], 1)
        self.assertEqual(state["counts"]["unmatched"], 1)
        # The mechanism, not just the outcome: both rows are planned, and both
        # are planned to nothing, because neither has a scene to write against.
        buckets = app.scan_state()["buckets"]
        self.assertEqual([c["video_id"] for c in buckets["ambiguous"]], [202])
        self.assertEqual([c["video_id"] for c in buckets["unmatched"]], [203])
        self.assertEqual([c["patch"] for c in
                          buckets["ambiguous"] + buckets["unmatched"]], [{}, {}])
        for include in ([], [202, 203]):
            with self.subTest(include=include):
                self.assertEqual(app.preview(include_ids=include)["rows"], [])

    def test_likely_is_opt_in_and_never_writable_without_a_tick(self):
        # `LIKELY` is kept out of WRITABLE_STATUSES because a DIFFERENT scene's
        # near-duplicate encode scores `likely`. It is writable by a tick and by
        # nothing else, so both facts have to hold at once: widening
        # WRITABLE_STATUSES is the "fix" these assertions refuse.
        self.assertNotIn(matcher.LIKELY, sync.WRITABLE_STATUSES)
        self.assertEqual(set(OPT_IN_STATUSES), {sync.UNVERIFIED, matcher.LIKELY})

    def test_a_resolved_disagreement_replaces_the_wrong_external_id(self):
        # The row arrives claiming one scene and carrying that scene's id; the
        # bytes say another. `plan_change` emits `external_ids: {"set": [...]}`,
        # so the correction is free, provided the wrong id is REPLACED rather than
        # added beside it. Driven through the real apply path, because the
        # per-provider upsert is what makes that true.
        video = dict(VIDEO, id=204, duration_secs=900.0, content_hash="ch-204",
                     external_ids=[{"provider": "stashdb", "id": SCENE_ID},
                                   {"provider": "other", "id": "keep-me"}])
        app = build(videos=(video,), hashes={VIDEO["path"]: "os-204"},
                    by_hash={"os-204": [fp_scene(SCENE_B_ID, oshashes=["os-204"])]})
        app.connect()
        # A resync: the default scan filter skips a video that already carries a
        # stashdb id, and a wrong id is exactly such a video.
        self.assertEqual(run_scan(app, resync=True)["counts"]["uuid-mismatch"], 1)
        self.assertEqual(run_identify(app)["counts"]["verified"], 1)
        # The precondition, stated rather than assumed: without a WRONG id here
        # this would only prove an id can be added, which nothing doubted.
        self.assertEqual(app.pron_transport.videos[204]["external_ids"],
                         [{"provider": "stashdb", "id": SCENE_ID},
                          {"provider": "other", "id": "keep-me"}])

        app.start_apply([204])
        app.await_apply()
        self.assertIsNone(app.apply_state()["error"])
        self.assertEqual(app.pron_transport.videos[204]["external_ids"],
                         [{"provider": "stashdb", "id": SCENE_B_ID},
                          {"provider": "other", "id": "keep-me"}])

    def test_every_planned_change_carries_a_confidence_key(self):
        # A UI that has to test for the key's existence is a UI that will get it
        # wrong, so it is always present — including on the rows that never went
        # through a fingerprint arm at all.
        picked, missed = legacy(205), legacy(206)
        app = build(videos=(VIDEO, picked, missed),
                    hashes={VIDEO["path"]: "aaaabbbbccccdddd",
                            picked["path"]: "os-205", missed["path"]: "os-206"},
                    phashes={missed["path"]: PHASH_B},
                    by_hash={"os-205": [fp_scene(SCENE_B_ID, oshashes=["os-205"])]})
        app.connect()
        run_scan(app)
        run_identify(app)
        changes = [change for rows in app.scan_state()["buckets"].values()
                   for change in rows]
        by_id = {change["video_id"]: change for change in changes}
        self.assertEqual(sorted(by_id), [42, 205, 206])
        for video_id, change in sorted(by_id.items()):
            with self.subTest(video=video_id):
                self.assertIn("confidence", change)
        self.assertIsNone(by_id[42]["confidence"])       # named by its folder UUID
        self.assertEqual(by_id[205]["confidence"], matcher.BY_OSHASH)
        # Unplannable: no scene, so no identity for a confidence to be about.
        self.assertEqual(by_id[206]["status"], matcher.UNMATCHED)
        self.assertIsNone(by_id[206]["confidence"])

    def test_a_manual_pick_is_previewed_as_decided_by_hand(self):
        video = legacy(210)
        app = build(videos=(video,), hashes={video["path"]: "os-210"},
                    scenes={SCENE_B_ID: fp_scene(SCENE_B_ID)},
                    by_hash={"os-210": [fp_scene(SCENE_C_ID, oshashes=["os-210"]),
                                        fp_scene(SCENE_B_ID, oshashes=["os-210"])]})
        app.connect()
        run_scan(app)
        run_identify(app)
        app.decide(210, scene_id=SCENE_B_ID, action="pick")
        row, = app.preview()["rows"]
        self.assertEqual(row["scene_id"], SCENE_B_ID)
        self.assertEqual(row["confidence"], matcher.BY_MANUAL)


class TestDisagreements(unittest.TestCase):
    """Whisparr folders whose UUID disagrees with the identified scene; only reported."""

    def test_an_unresolved_disagreement_names_the_folder_uuid_and_no_match(self):
        mismatch = dict(VIDEO, id=207, duration_secs=900.0, content_hash="ch-207")
        app = build(videos=(mismatch,), hashes={VIDEO["path"]: "os-207"})
        app.connect()
        run_scan(app)
        entry, = app.disagreements()
        self.assertEqual(entry["videoId"], 207)
        self.assertEqual(entry["relPath"], VIDEO["rel_path"])
        self.assertEqual(entry["whisparrSceneId"], SCENE_ID)
        self.assertIsNone(entry["matchedSceneId"])
        self.assertIsNone(entry["matchedTitle"])
        self.assertEqual(entry["status"], sync.UUID_MISMATCH)
        # Shipped in the one state read, so the page needs no second round trip.
        self.assertEqual(app.state()["disagreements"], [entry])

    def test_a_resolved_disagreement_shows_both_ids_side_by_side(self):
        # Still a disagreement: identify found the right scene, but the folder
        # name in Whisparr still claims the wrong one until it is fixed.
        mismatch = dict(VIDEO, id=208, duration_secs=900.0, content_hash="ch-208")
        scene = fp_scene(SCENE_B_ID, oshashes=["os-208"])
        app = build(videos=(mismatch,), hashes={VIDEO["path"]: "os-208"},
                    by_hash={"os-208": [scene]})
        app.connect()
        run_scan(app)
        run_identify(app)
        entry, = app.disagreements()
        self.assertEqual(entry["whisparrSceneId"], SCENE_ID)
        self.assertEqual(entry["matchedSceneId"], SCENE_B_ID)
        self.assertEqual(entry["matchedTitle"], scene["title"])
        self.assertEqual(entry["status"], matcher.VERIFIED)

    def test_a_row_whose_folder_uuid_never_disagreed_is_not_listed(self):
        app = build(videos=(VIDEO, legacy(211)),
                    hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        self.assertEqual(app.disagreements(), [])


class FlippingLock:
    """A lock that flips a flag the instant it is acquired.

    The point of reading a run's ``["running"]`` flag *inside* ``_lock`` is that
    the read sees the state as of acquisition. A check made outside reads the flag
    before this fires and starts the run anyway; a check inside sees it set and
    refuses. Both ``start_scan`` and ``start_identify`` set their flag inside the
    lock, so the same instrument proves both halves of ``start_apply``'s guard.
    Deterministic — no sleeps, no second thread, no race to lose.
    """

    def __init__(self, on_enter):
        self._real, self._on_enter = threading.Lock(), on_enter

    def __enter__(self):
        self._real.acquire()
        self._on_enter()
        return self

    def __exit__(self, *exc):
        self._real.release()
        return False


class TestApplyLocking(unittest.TestCase):
    def test_apply_reads_the_scan_flag_inside_the_lock(self):
        # `start_scan` sets its running flag inside `_lock`, so an apply that
        # reads it outside can pass its guard while a scan is starting — and
        # then write the previous scan's rows, a plan nobody approved.
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        run_scan(app)
        # Mirrors the real sequence: the user looks at the preview, then applies.
        app.preview()
        app._lock = FlippingLock(
            lambda: app._scan.update({"running": True}))
        with self.assertRaises(LibraryStateError):
            app.start_apply([42])
        self.assertFalse(app.apply_state()["running"])
        self.assertEqual(app.pron_transport.videos[42]["title"], "A Mp4")

    def test_identify_reads_the_apply_flag_inside_the_lock(self):
        # The order that is refused now runs the other way. An apply re-plans
        # from `self._rows` after its own round trip, so a run that took up a
        # ticked `likely` row part-way through would drop it from a write the
        # user had already approved. `start_apply` sets its flag inside `_lock`,
        # so `start_identify` has to read it there too.
        video = legacy(86)
        app = build(videos=(video,), hashes={video["path"]: "os-86"})
        app.connect()
        run_scan(app)
        app._lock = FlippingLock(lambda: app._apply.update({"running": True}))
        with self.assertRaises(LibraryStateError):
            app.start_identify()
        self.assertFalse(app.identify_state()["running"])
        self.assertEqual(app.scan_state()["counts"]["waiting"], 0)

    def test_a_run_takes_its_rows_up_before_start_identify_returns(self):
        # Not later, in the thread: between `start_identify` returning and the
        # thread starting, an apply could otherwise plan a row the run is about
        # to re-decide. The run is replaced by a gate so that window is held open.
        video = legacy(51)
        app = build(videos=(video,), hashes={video["path"]: "os-51"},
                    phashes={video["path"]: PHASH_A},
                    by_hash={PHASH_A: [fp_scene(SCENE_B_ID, phashes=[PHASH_A])]})
        app.connect()
        run_scan(app)
        self.assertEqual(run_identify(app)["counts"]["likely"], 1)

        gate = threading.Event()
        app._identify_run = lambda rows, indexes: gate.wait(5)
        app.start_identify()
        try:
            self.assertEqual(app.preview(include_ids=[51])["rows"], [])
            self.assertEqual(app.scan_state()["counts"]["waiting"], 1)
        finally:
            gate.set()
            app.await_identify()
        # And the gate's "run" settled nothing, so the row comes back as it was.
        self.assertEqual(app.scan_state()["counts"]["likely"], 1)
        self.assertEqual(app.scan_state()["counts"]["waiting"], 0)


class PronoisseurFingerprintsTest(unittest.TestCase):
    """Identify reads the fingerprints Pronoisseur serves and never computes any.

    One lookup per batch carries every value a video has. A video with no phash
    whose oshash found nothing is *pending hashing*, never `unmatched`: the
    absence is Pronoisseur's, not StashDB's.
    """

    def run_one(self, video, hashes=None, phashes=None, by_hash=None, **kw):
        app = build(videos=(video,), hashes=hashes, phashes=phashes,
                    by_hash=by_hash, **kw)
        app.connect()
        run_scan(app)
        state = run_identify(app)
        return app, state

    def test_both_values_go_in_one_lookup(self):
        app, _ = self.run_one(NO_UUID, hashes={NO_UUID["path"]: "os-43"},
                              phashes={NO_UUID["path"]: PHASH_A})
        fps = [call["variables"]["fps"] for call in app.stash_transport.calls
               if "fps" in (call.get("variables") or {})]
        self.assertEqual(len(fps), 1)
        self.assertEqual(len(fps[0]), 1)
        self.assertEqual(sorted(f["algorithm"] for f in fps[0][0]),
                         ["OSHASH", "PHASH"])

    def test_an_exact_oshash_still_wins_over_phash_companions(self):
        scene = fp_scene(SCENE_ID, oshashes=["os-43"])
        scene_b = fp_scene(SCENE_B_ID, phashes=[PHASH_A])
        app, state = self.run_one(
            NO_UUID, hashes={NO_UUID["path"]: "os-43"},
            phashes={NO_UUID["path"]: PHASH_A},
            by_hash={"os-43": [scene], PHASH_A: [scene_b]})
        row = app.scan_state()["buckets"]["verified"][0]
        self.assertEqual(row["confidence"], matcher.BY_OSHASH)
        self.assertEqual(row["scene_id"], SCENE_ID)
        self.assertEqual(len(app._rows[0]["candidates"]), 1)

    def test_an_oshash_miss_without_a_phash_is_pending(self):
        app, state = self.run_one(NO_UUID, hashes={NO_UUID["path"]: "os-43"})
        row = app.scan_state()["buckets"][library_common.PENDING_HASH][0]
        self.assertEqual(row["note"], library_common.PENDING_NOT_YET)
        self.assertEqual(app._rows[0]["oshash"], "os-43")
        self.assertIsNone(row["scene_id"])

    def test_no_values_is_pending_with_no_lookup(self):
        app, state = self.run_one(NO_UUID)
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 1)
        self.assertEqual(fingerprint_calls(app), 0)

    def test_both_values_missing_on_stashdb_is_no_match(self):
        app, state = self.run_one(NO_UUID, hashes={NO_UUID["path"]: "os-43"},
                                  phashes={NO_UUID["path"]: PHASH_A})
        self.assertEqual(app.scan_state()["counts"]["unmatched"], 1)
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 0)

    def test_the_switch_off_reason(self):
        app, _ = self.run_one(
            NO_UUID, hashes={NO_UUID["path"]: "os-43"},
            fingerprints=dict(DEFAULT_FINGERPRINTS, phash_enabled=False))
        row = app.scan_state()["buckets"]["pending-hash"][0]
        self.assertEqual(row["note"], library_common.PENDING_SWITCH_OFF)

    def test_an_older_pronoisseur_sends_no_lookup_and_says_update(self):
        app, state = self.run_one(NO_UUID, fingerprints=None,
                                  omit_fingerprint_keys=True)
        row = app.scan_state()["buckets"]["pending-hash"][0]
        self.assertEqual(row["note"], library_common.PENDING_OLD_PRONOISSEUR)
        self.assertEqual(fingerprint_calls(app), 0)
        self.assertEqual(state["label"], "done")

    def test_a_remembered_row_is_never_pending(self):
        video = legacy(300)
        app = build(videos=(video,),
                    scenes={SCENE_B_ID: fp_scene(SCENE_B_ID, oshashes=["x"])})
        app.connect()
        app.library_store.record_decision("ch-300", SCENE_B_ID, "fingerprint")
        run_scan(app)
        run_identify(app)
        row = app.scan_state()["buckets"]["verified"][0]
        self.assertIsNone(row["confidence"])
        self.assertIn("Pronoisseur has no fingerprint", row["note"])
        self.assertEqual(fingerprint_calls(app), 0)

    def test_a_malformed_phash_counts_as_none(self):
        video = legacy(303)
        app = build(videos=(video,), hashes={video["path"]: "os-303"},
                    phashes={video["path"]: "zz-not-hex"})
        app.connect()
        run_scan(app)
        run_identify(app)
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 1)
        sent = [fp for call in app.stash_transport.calls
                for group in (call.get("variables") or {}).get("fps", [])
                for fp in group]
        self.assertTrue(sent)
        self.assertNotIn("PHASH", [fp["algorithm"] for fp in sent])

    def test_remembered_confidence_comes_from_the_video(self):
        video = legacy(301)
        app = build(videos=(video,), hashes={video["path"]: "os-301"},
                    scenes={SCENE_B_ID: fp_scene(SCENE_B_ID,
                                                 oshashes=["os-301"])})
        app.connect()
        app.library_store.record_decision("ch-301", SCENE_B_ID, "fingerprint")
        run_scan(app)
        run_identify(app)
        row = app.scan_state()["buckets"]["verified"][0]
        self.assertEqual(row["confidence"], matcher.BY_OSHASH)

    def test_a_failed_batch_is_not_pending(self):
        video = legacy(302)
        app = build(videos=(video,), hashes={video["path"]: "os-302"})
        app.connect()
        run_scan(app)
        app.stashdb._transport = lambda payload: {
            "errors": [{"message": "server exploded"}]}
        state = run_identify(app)
        row = app.scan_state()["buckets"]["no-identity"][0]
        self.assertIn(library_common.LOOKUP_FAILED_NOTE, row["note"])
        self.assertEqual(state["finished"]["failed"], 1)
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 0)

    def test_pending_is_identifiable_and_unplannable(self):
        app, _ = self.run_one(NO_UUID, hashes={NO_UUID["path"]: "os-43"})
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 1)
        self.assertEqual(app.preview()["rows"], [])
        self.assertEqual(app.preview(include_ids=[43])["rows"], [])
        # Taken up again by the next run.
        started = app.start_identify()
        app.await_identify()
        self.assertTrue(started["started"])
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 1)
        self.assertEqual(app.identify_state()["finished"]["considered"], 1)

    def test_a_pending_hash_row_takes_a_hand_pick(self):
        app, _ = self.run_one(NO_UUID)
        app.decide(43, scene_id=SCENE_ID)
        row = app.scan_state()["buckets"]["verified"][0]
        self.assertEqual(row["confidence"], matcher.BY_MANUAL)

    def test_a_whisparr_disagreement_that_finds_nothing_is_pending_and_still_listed(self):
        mismatch = dict(VIDEO, id=303, duration_secs=2400.0,
                        content_hash="ch-303")
        app = build(videos=(mismatch,), hashes={VIDEO["path"]: "os-303"})
        app.connect()
        self.assertEqual(run_scan(app)["counts"]["uuid-mismatch"], 1)
        run_identify(app)
        self.assertEqual(app.scan_state()["counts"]["pending-hash"], 1)
        entry, = app.disagreements()
        self.assertEqual(entry["whisparrSceneId"], SCENE_ID)
        self.assertEqual(entry["status"], library_common.PENDING_HASH)

    def test_the_finished_counts(self):
        hit, wait, miss = legacy(310), legacy(311), legacy(312)
        app = build(videos=(hit, wait, miss),
                    hashes={hit["path"]: "os-310", wait["path"]: "os-311",
                            miss["path"]: "os-312"},
                    phashes={miss["path"]: PHASH_B},
                    by_hash={"os-310": [fp_scene(SCENE_B_ID,
                                                 oshashes=["os-310"])]})
        app.connect()
        run_scan(app)
        finished = run_identify(app)["finished"]
        self.assertEqual(finished["verified"], 1)
        self.assertEqual(finished["pending-hash"], 1)
        self.assertEqual(finished["unmatched"], 1)
        self.assertEqual(finished["phashes"], 1)
        self.assertEqual(finished["considered"], 3)
        self.assertNotIn("phashed", finished)


DEFAULT_LINK_BODY = {
    "label": "StashDB",
    "patterns": {"video": "https://stashdb.org/scenes/{id}",
                 "model": "https://stashdb.org/performers/{id}",
                 "studio": "https://stashdb.org/studios/{id}",
                 "category": "https://stashdb.org/tags/{id}"}}


def link_puts(app):
    return [c for c in app.pron_transport.calls
            if c["method"] == "PUT" and c["path"].startswith("/api/v1/link-patterns")]


class TestLinkPatterns(unittest.TestCase):
    """The tool registers its StashDB id patterns so Pronoisseur can link them."""

    def tag_one(self, app):
        run_scan(app)
        app.start_apply([42])
        app.await_apply()

    def test_connect_registers_the_default_patterns_once(self):
        app = build()
        out = app.connect()
        puts = link_puts(app)
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["path"], "/api/v1/link-patterns/stashdb")
        self.assertEqual(puts[0]["body"], DEFAULT_LINK_BODY)
        self.assertEqual(out["linkPatterns"], {"status": "registered", "error": None})
        self.assertEqual(app.pron_transport.link_patterns["stashdb"]["patterns"],
                         DEFAULT_LINK_BODY["patterns"])

    def test_the_provider_is_the_one_the_tool_writes_into_external_ids(self):
        from tagger import library_sync
        app = build()
        app.connect()
        self.assertEqual(link_puts(app)[0]["path"].rsplit("/", 1)[-1],
                         library_sync.PROVIDER)

    def test_a_custom_endpoint_gives_that_servers_base(self):
        app = build()
        app.cfg["stashdb_endpoint"] = "http://box.example:9998/graphql"
        app.connect()
        patterns = link_puts(app)[0]["body"]["patterns"]
        self.assertEqual(patterns["video"], "http://box.example:9998/scenes/{id}")
        self.assertEqual(patterns["category"], "http://box.example:9998/tags/{id}")

    def test_an_endpoint_that_is_not_http_is_not_registered(self):
        for bad in ("ftp://box.example/graphql", "not a url", "https:///graphql"):
            app = build()
            app.cfg["stashdb_endpoint"] = bad
            out = app.connect()
            self.assertTrue(out["connected"], bad)
            self.assertEqual(link_puts(app), [], bad)
            self.assertEqual(out["linkPatterns"]["status"], "failed", bad)
            self.assertTrue(out["linkPatterns"]["error"], bad)

    def test_a_tag_run_registers_again_before_its_writes(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.connect()
        self.tag_one(app)
        calls = app.pron_transport.calls
        puts = link_puts(app)
        self.assertEqual(len(puts), 2)
        last_put = max(i for i, c in enumerate(calls) if c in puts)
        first_write = min(i for i, c in enumerate(calls)
                          if c["method"] in ("POST", "PATCH")
                          and not c["path"].startswith("/api/v1/link-patterns"))
        self.assertLess(last_put, first_write)

    def test_a_read_only_token_makes_no_request_and_still_connects(self):
        app = build(scopes=("library.read",))
        out = app.connect()
        self.assertTrue(out["connected"])
        self.assertEqual(link_puts(app), [])
        self.assertEqual(out["linkPatterns"]["status"], "read-only")
        self.assertEqual(app.state()["linkPatterns"]["status"], "read-only")

    def test_an_older_pronoisseur_is_unsupported_and_nothing_fails(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.pron_transport.supports_link_patterns = False
        out = app.connect()
        self.assertTrue(out["connected"])
        self.assertEqual(out["linkPatterns"], {"status": "unsupported", "error": None})
        self.tag_one(app)
        self.assertEqual(app.apply_state()["finished"]["applied"], 1)
        self.assertEqual(app.state()["linkPatterns"]["status"], "unsupported")

    def test_a_server_error_fails_quietly_with_its_reason(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.pron_transport.link_patterns_error = (
            500, {"error": {"code": "internal", "message": "boom"}})
        out = app.connect()
        self.assertTrue(out["connected"])
        self.assertEqual(out["linkPatterns"]["status"], "failed")
        self.assertIn("boom", out["linkPatterns"]["error"])
        self.tag_one(app)
        self.assertEqual(app.apply_state()["finished"]["applied"], 1)
        self.assertEqual(app.state()["linkPatterns"]["status"], "failed")

    def test_a_transport_error_fails_quietly_with_its_reason(self):
        app = build(hashes={VIDEO["path"]: "aaaabbbbccccdddd"})
        app.pron_transport.link_patterns_error = PronoisseurError("link down")
        out = app.connect()
        self.assertTrue(out["connected"])
        self.assertEqual(out["linkPatterns"]["status"], "failed")
        self.assertIn("link down", out["linkPatterns"]["error"])
        self.tag_one(app)
        self.assertEqual(app.apply_state()["finished"]["applied"], 1)

    def test_a_later_success_replaces_a_failure(self):
        app = build()
        app.pron_transport.link_patterns_error = (500, None)
        self.assertEqual(app.connect()["linkPatterns"]["status"], "failed")
        app.pron_transport.link_patterns_error = None
        self.assertEqual(app.connect()["linkPatterns"]["status"], "registered")

    def test_before_any_connect_the_status_is_not_yet(self):
        app = build()
        self.assertEqual(app.state()["linkPatterns"],
                         {"status": "not yet", "error": None})

    def test_a_failed_connect_makes_no_registration(self):
        app = build()
        app.connect(token="pron_wrong")
        self.assertEqual(link_puts(app), [])


class TestFakeValidatesLinkPatterns(unittest.TestCase):
    """The fake must refuse what the real route refuses, or the above prove nothing."""

    def client(self, **kw):
        fake = FakePronTransport(**kw)
        return fake, Pronoisseur("http://127.0.0.1:8869", "pron_test", transport=fake)

    def refused(self, patterns, label="L", provider="stashdb"):
        _, client = self.client()
        with self.assertRaises(PronoisseurError) as caught:
            client.put_link_patterns(provider, label, patterns)
        self.assertEqual(caught.exception.status, 422)

    def test_id_twice_is_refused(self):
        self.refused({"video": "https://h.example/{id}/x/{id}"})

    def test_id_missing_is_refused(self):
        self.refused({"video": "https://h.example/scenes/"})

    def test_id_in_the_host_is_refused(self):
        self.refused({"video": "https://{id}.example/scenes"})

    def test_not_http_is_refused(self):
        self.refused({"video": "ftp://h.example/{id}"})

    def test_unknown_kind_is_refused(self):
        self.refused({"scene": "https://h.example/{id}"})

    def test_no_kinds_is_refused(self):
        self.refused({})

    def test_a_long_label_is_refused(self):
        self.refused({"video": "https://h.example/{id}"}, label="x" * 65)

    def test_a_bad_provider_is_refused(self):
        self.refused({"video": "https://h.example/{id}"}, provider="bad provider")

    def test_an_unknown_body_field_is_refused(self):
        fake, _ = self.client()
        status, _ = fake("PUT", "/api/v1/link-patterns/stashdb",
                         {"label": "L", "extra": 1,
                          "patterns": {"video": "https://h.example/{id}"}},
                         {"Authorization": "Bearer pron_test"})
        self.assertEqual(status, 422)

    def test_the_thirty_third_provider_is_refused(self):
        _, client = self.client()
        for n in range(32):
            client.put_link_patterns("p%d" % n, "L",
                                     {"video": "https://h.example/{id}"})
        with self.assertRaises(PronoisseurError):
            client.put_link_patterns("one-more", "L",
                                     {"video": "https://h.example/{id}"})
        # An existing provider can still be replaced at the cap.
        client.put_link_patterns("p0", "L2", {"video": "https://h.example/v/{id}"})

    def test_a_put_replaces_the_whole_entry(self):
        fake, client = self.client()
        client.put_link_patterns("s", "A", {"video": "https://h.example/{id}",
                                            "model": "https://h.example/m/{id}"})
        client.put_link_patterns("s", "B", {"video": "https://h.example/{id}"})
        self.assertEqual(list(fake.link_patterns["s"]["patterns"]), ["video"])

    def test_a_read_only_token_is_refused(self):
        _, client = self.client(scopes=("library.read",))
        with self.assertRaises(PronoisseurError) as caught:
            client.put_link_patterns("s", "A", {"video": "https://h.example/{id}"})
        self.assertEqual(caught.exception.status, 403)

    def test_the_client_quotes_the_provider(self):
        fake, client = self.client()
        client.put_link_patterns("a.b-c_1", "L", {"video": "https://h.example/{id}"})
        self.assertEqual(fake.calls[-1]["path"], "/api/v1/link-patterns/a.b-c_1")
        with self.assertRaises(PronoisseurError):
            client.put_link_patterns("a/b", "L", {"video": "https://h.example/{id}"})
        self.assertEqual(fake.calls[-1]["path"], "/api/v1/link-patterns/a%2Fb")


if __name__ == "__main__":
    unittest.main()
