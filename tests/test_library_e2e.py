"""End-to-end: the real client, over real HTTP, against a fake Local API.

Everything else in this suite injects a transport. This test does not: it proves
the urllib path sends the right headers (bearer, no Origin), reads the contract's
status codes correctly, and survives a full scan -> identify -> decide -> preview
-> apply -> re-scan cycle with the second run proposing nothing.

Two videos, on purpose: one named by the UUID in its folder, one named by nothing
at all and identified by its fingerprint. Both have to reach the same write and
both have to be silent on the second run, because "proposes nothing" is the
strongest single check in this suite and it is worth nothing if it only ever
covers the easy half.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tagger.library_app import LibraryApp
from tagger.library_store import LibraryStore
from tagger.pronoisseur import Pronoisseur
from tagger.stashdb import StashDB
from tests.fakes import FakePronTransport, FakeStashDBTransport
from tests.test_library_app import (SCENE, SCENE_B_ID, SCENE_C_ID, SCENE_ID,
                                    VIDEO, fingerprint_calls, fp_scene, legacy)

#: The oshash of the one file no folder UUID names. **Two** scenes list it, so
#: the run has to ask rather than guess — which is what puts `decide` in the
#: cycle below instead of leaving the fingerprint path a straight line.
LEGACY_OSHASH = "0123456789abcdef"


def serve(fake):
    """Run FakePronTransport behind a real HTTP server. Returns (base_url, stop)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            body = json.loads(raw) if raw else None
            headers = {k: v for k, v in self.headers.items()}
            status, payload = fake(method, self.path, body, headers)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_PUT(self):
            self._handle("PUT")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return "http://127.0.0.1:%d" % port, lambda: (httpd.shutdown(), httpd.server_close())


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.legacy = legacy(90)
        self.scene_b = fp_scene(SCENE_B_ID, oshashes=[LEGACY_OSHASH])
        self.scene_c = fp_scene(SCENE_C_ID, oshashes=[LEGACY_OSHASH])
        # Pronoisseur serves each file's oshash on the listing; Scan reads it there.
        self.fake = FakePronTransport(
            videos=[dict(VIDEO, oshash="aaaabbbbccccdddd", phash=None),
                    dict(self.legacy, oshash=LEGACY_OSHASH, phash=None)],
            fingerprints={"phash_enabled": True})
        self.base_url, self.stop = serve(self.fake)
        self.addCleanup(self.stop)
        db = os.path.join(tempfile.mkdtemp(), "state.db")
        stash = FakeStashDBTransport(
            {SCENE_ID: SCENE, SCENE_B_ID: self.scene_b, SCENE_C_ID: self.scene_c},
            by_hash={LEGACY_OSHASH: [self.scene_b, self.scene_c]})
        self.app = LibraryApp(
            cfg={"pronoisseur_url": self.base_url, "pronoisseur_token": "pron_test",
                 "pronoisseur_page_size": 500},
            stashdb=StashDB("http://x", "k", interval=0, transport=stash),
            library_store=LibraryStore(db),
            client_factory=Pronoisseur)
        # Named the way `tests.test_library_app.build` names it, so the shared
        # `fingerprint_calls` helper counts this run's lookups too.
        self.app.stash_transport = stash

    def test_health_needs_no_token_and_info_does(self):
        client = Pronoisseur(self.base_url, "")
        self.assertTrue(client.health()["ok"])
        from tagger.pronoisseur import PronoisseurError
        with self.assertRaises(PronoisseurError) as ctx:
            client.info()
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_full_cycle_writes_then_proposes_nothing_on_a_re_run(self):
        self.app.connect()
        self.app.library_store.update_settings({"allowlist": ["c-setting"]})

        self.app.start_scan()
        self.app.await_scan()
        counts = self.app.scan_state()["counts"]
        self.assertEqual(counts["verified"], 1)
        self.assertEqual(counts["no-identity"], 1)

        # Only the UUID-named row can be written yet: nothing has said what the
        # other file is.
        preview = self.app.preview()
        self.assertEqual([r["video_id"] for r in preview["rows"]], [42])

        # Two scenes list this file's bytes, so the run asks rather than guesses.
        self.app.start_identify()
        self.app.await_identify()
        identify = self.app.identify_state()
        self.assertEqual(identify["counts"]["ambiguous"], 1)
        self.assertEqual([q["videoId"] for q in identify["questions"]], [90])

        self.app.decide(90, scene_id=SCENE_B_ID, action="pick")
        preview = self.app.preview()
        self.assertEqual(sorted(r["video_id"] for r in preview["rows"]), [42, 90])
        # A fingerprint-decided row is writable with nothing ticked, because
        # `verified` is one status whichever route reached it.
        self.assertEqual(
            {r["video_id"]: r["confidence"] for r in preview["rows"]},
            {42: None, 90: "manual"})

        self.app.start_apply([42, 90])
        self.app.await_apply()
        self.assertIsNone(self.app.apply_state()["error"])

        # Over real HTTP, with nothing re-scanned and nothing re-ticked: the run
        # re-read what it wrote, so the preview has nothing left to propose and
        # the aliases it pushed are not offered a second time. Without this the
        # preview said "2 to write" for ever with both writes plainly landed.
        settled = self.app.preview()
        self.assertEqual(settled["rows"], [])
        self.assertEqual(settled["entities"]["aliases"], 0)
        self.assertIsNone(self.app.apply_state()["finished"]["note"])
        self.assertEqual(self.app.scan_state()["counts"]["in-sync"], 2)

        identified = self.fake.videos[90]
        self.assertEqual(identified["title"], self.scene_b["title"])
        self.assertEqual(identified["external_ids"],
                         [{"provider": "stashdb", "id": SCENE_B_ID}])

        written = self.fake.videos[42]
        self.assertEqual(written["title"], "Example Scene One")
        self.assertEqual(written["release_date"], "2020-05-23")
        self.assertFalse(written["release_date_estimated"])
        self.assertEqual(written["studio"]["name"], "Example Network")
        self.assertEqual([m["name"] for m in written["models"]], ["Jane Example"])
        self.assertEqual([c["name"] for c in written["categories"]], ["Outdoor"])
        self.assertEqual(written["external_ids"],
                         [{"provider": "stashdb", "id": SCENE_ID}])
        # Writing curated fields hands ownership to this app, which is what stops
        # Pronoisseur's own filename re-parser overwriting them later.
        self.assertEqual(written["metadata_source"], "tagger")

        # Second run: both external ids are now present, so the default filter
        # finds nothing at all, and a forced re-sync finds them already in sync.
        self.app.start_scan()
        self.app.await_scan()
        self.assertEqual(self.app.preview()["rows"], [])

        self.app.start_scan(resync=True)
        self.app.await_scan()
        # The re-scan rebuilds every row from the folder names, so the file no
        # UUID names is back to `no-identity` — identifying it again is what
        # proves the answer survived, and it costs no fingerprint lookup at all
        # because the decision is remembered against the file's content hash.
        lookups = fingerprint_calls(self.app)
        self.assertEqual(lookups, 1)      # the first run's, and only that one
        self.app.start_identify()
        self.app.await_identify()
        self.assertEqual(fingerprint_calls(self.app), lookups)
        self.assertEqual(self.app.scan_state()["counts"]["in-sync"], 2)
        self.assertEqual(self.app.preview()["rows"], [])

    def test_no_origin_header_reaches_the_server(self):
        self.app.connect()
        for call in self.fake.calls:
            self.assertTrue(call["path"].startswith("/api/v1/"))
        # The fake 403s on Origin; a successful connect proves none was sent.
        self.assertTrue(self.app.connected)


if __name__ == "__main__":
    unittest.main()
