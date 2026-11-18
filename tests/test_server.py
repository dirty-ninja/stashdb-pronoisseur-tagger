import http.client
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tagger import server
from tagger.stashdb import StashDBError
from tests.test_library_app import (PHASH_B, SCENE, SCENE_B_ID, SCENE_C_ID,
                                    SCENE_ID, build, found_scene, fp_scene, legacy,
                                    parsed, performer_lookups, run_identify,
                                    run_scan, scene_query_inputs)


class TestHostGuard(unittest.TestCase):
    def test_rejects_non_loopback_host_header_when_bound_to_loopback(self):
        self.assertFalse(server.host_allowed("evil.com:8420"))
        self.assertTrue(server.host_allowed("127.0.0.1:8420"))
        self.assertTrue(server.host_allowed("localhost:8420"))
        self.assertTrue(server.host_allowed("[::1]:8420"))

    def test_missing_host_header_is_rejected(self):
        self.assertFalse(server.host_allowed(None))
        self.assertFalse(server.host_allowed(""))


class TestErrorEnvelope(unittest.TestCase):
    def test_shape(self):
        body, status = server.error_response("BAD_REQUEST", "videoId is required")
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertEqual(payload["error"]["message"], "videoId is required")

    def test_extra_detail_is_carried(self):
        body, _ = server.error_response("BAD_REQUEST", "no", detail=[{"field": "x"}])
        self.assertEqual(json.loads(body)["error"]["detail"], [{"field": "x"}])

    def test_status_mapping(self):
        self.assertEqual(server.status_for_code("BAD_REQUEST"), 400)
        self.assertEqual(server.status_for_code("FORBIDDEN"), 403)
        self.assertEqual(server.status_for_code("NOT_FOUND"), 404)
        self.assertEqual(server.status_for_code("UNSUPPORTED_MEDIA"), 415)
        self.assertEqual(server.status_for_code("UPSTREAM_ERROR"), 502)
        self.assertEqual(server.status_for_code("SOMETHING_NEW"), 500)

    def test_upstream_exceptions_map_to_502(self):
        self.assertEqual(server.code_for_exception(StashDBError("x")), "UPSTREAM_ERROR")
        self.assertEqual(server.code_for_exception(ValueError("x")), "BAD_REQUEST")


class TestStaticPaths(unittest.TestCase):
    def test_traversal_is_refused(self):
        self.assertIsNone(server.static_target("/../config.json"))
        self.assertIsNone(server.static_target("/..%2fconfig.json"))
        self.assertIsNone(server.static_target("/sub/../../secret"))

    def test_root_maps_to_index(self):
        self.assertEqual(server.static_target("/"), "index.html")

    def test_only_whitelisted_extensions(self):
        self.assertEqual(server.static_target("/app.js"), "app.js")
        self.assertEqual(server.static_target("/style.css"), "style.css")
        self.assertIsNone(server.static_target("/secrets.py"))

    def test_backslash_traversal_is_refused(self):
        # On Windows a backslash separates directories, but posixpath does not
        # see it, so these slip past the dot-dot checks unless refused outright.
        self.assertIsNone(server.static_target("/a%5c..%5c..%5cx.html"))
        self.assertIsNone(server.static_target("/a\\..\\x.html"))
        self.assertIsNone(server.static_target("/%5capp.js"))


class TestLibraryErrorCodes(unittest.TestCase):
    def test_a_pronoisseur_failure_is_an_upstream_error(self):
        from tagger.pronoisseur import PronoisseurError
        self.assertEqual(server.code_for_exception(PronoisseurError("x")),
                         "UPSTREAM_ERROR")

    def test_a_missing_connection_is_a_conflict_not_a_500(self):
        from tagger.library_app import LibraryStateError
        self.assertEqual(
            server.code_for_exception(LibraryStateError("connect first")),
            "CONFLICT")
        self.assertEqual(server.status_for_code("CONFLICT"), 409)

    def test_a_bare_runtime_error_is_still_a_500(self):
        # A thread that would not start is a fault anywhere in the tool, not a state
        # the page can render as a panel. Only the named subclass is a 409.
        self.assertEqual(server.code_for_exception(RuntimeError("no thread")),
                         "INTERNAL")


class TestReadOnlyUpstream(unittest.TestCase):
    """StashDB is only ever read. A fingerprint is never submitted upstream."""

    def test_no_graphql_mutation_document_exists_anywhere_in_the_package(self):
        # The whole package, not just `queries`: a mutation document added to
        # `stashdb.py`, or to a module that does not exist yet, breaks the same
        # rail, and reading `vars(queries)` would never see it. A hard rail
        # earns the wider pin — if this ever trips on prose, reword the prose.
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tagger")
        sources = sorted(name for name in os.listdir(root)
                         if name.endswith(".py"))
        # Without this the walk below could pass by reading nothing at all.
        self.assertIn("queries.py", sources)
        offenders = []
        for name in sources:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                for number, line in enumerate(fh, start=1):
                    if "mutation" in line.lower():
                        offenders.append("%s:%d: %s"
                                         % (name, number, line.strip()))
        self.assertEqual(offenders, [])


class TestRequestSafety(unittest.TestCase):
    def test_origin_rules(self):
        self.assertTrue(server.origin_allowed(None, "127.0.0.1:8426"))
        self.assertTrue(server.origin_allowed("http://127.0.0.1:8426", "127.0.0.1:8426"))
        self.assertFalse(server.origin_allowed("https://example.com", "127.0.0.1:8426"))
        self.assertFalse(server.origin_allowed("http://127.0.0.1:9999", "127.0.0.1:8426"))
        self.assertFalse(server.origin_allowed("null", "127.0.0.1:8426"))


class TestMatchRoutes(unittest.TestCase):
    """The match flow driven over real HTTP, against the LibraryApp doubles.

    The routes are the only part of the app the browser can reach, so they are
    exercised through an actual request rather than by calling the handler's
    methods: a path typo, a mis-named body key or an exception mapped to the
    wrong status is invisible to anything less.
    """

    def serve(self, library):
        """Put one LibraryApp behind the real handler, on a loopback port."""
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(library))
        looping = threading.Event()

        def run():
            looping.set()
            httpd.serve_forever()

        threading.Thread(target=run, daemon=True).start()

        def stop():
            # `shutdown()` waits, with no timeout, on an event that only
            # `serve_forever` ever sets: called before the thread reaches its
            # loop it hangs the whole runner instead of failing one test. The
            # thread is a daemon, so closing the socket is enough on the path
            # where the loop never started.
            if looping.wait(5):
                httpd.shutdown()
            httpd.server_close()

        self.addCleanup(stop)
        self.base = "http://127.0.0.1:%d" % httpd.server_address[1]

    def request(self, method, path, body=None, headers=None):
        """One request. Returns ``(status, payload)``, errors included."""
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        sent = {"Content-Type": "application/json"}
        sent.update(headers or {})
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=sent)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:            # it is a response too, and warns if left open
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def _identified(self, video_id=91):
        """A library scanned and identified, with one row left unmatched."""
        video = legacy(video_id)
        library = build(videos=(video,),
                        hashes={video["path"]: "os-%d" % video_id},
                        phashes={video["path"]: PHASH_B},
                        scenes={SCENE_ID: SCENE,
                                SCENE_C_ID: fp_scene(SCENE_C_ID)})
        library.connect()
        run_scan(library)
        self.assertEqual(run_identify(library)["counts"]["unmatched"], 1)
        self.serve(library)
        return library

    def test_the_match_state_is_readable_and_camel_cased(self):
        video = legacy(90)
        library = build(videos=(video,), hashes={video["path"]: "os-90"},
                        by_hash={"os-90": [fp_scene(SCENE_B_ID,
                                                    oshashes=["os-90"])]})
        library.connect()
        run_scan(library)
        run_identify(library)
        self.serve(library)

        status, payload = self.request("GET", "/api/library/match")
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["verified"], 1)
        self.assertNotIn("depsMissing", payload)
        self.assertEqual([key for key in payload if "_" in key], [])

    def test_identifying_before_any_scan_is_a_conflict_not_a_500(self):
        library = build()
        library.connect()
        self.serve(library)

        status, payload = self.request("POST", "/api/library/match/identify")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "CONFLICT")
        self.assertIn("Scan", payload["error"]["message"])

    def test_a_pick_settles_the_row_and_answers_with_the_refreshed_state(self):
        library = self._identified()
        status, payload = self.request(
            "POST", "/api/library/match/decide",
            {"videoId": 91, "sceneId": SCENE_C_ID, "action": "pick"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["verified"], 1)
        self.assertEqual(payload, library.identify_state())
        self.assertEqual(library.library_store.decisions()["ch-91"]["scene_id"],
                         SCENE_C_ID)

    def test_an_unknown_action_is_refused_at_the_edge(self):
        library = self._identified()
        status, payload = self.request(
            "POST", "/api/library/match/decide",
            {"videoId": 91, "sceneId": SCENE_C_ID, "action": "maybe"})
        # 400, not the 409 `decide` itself would answer with: the typo never got
        # that far, which is the point of validating at the edge.
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertIn("action", payload["error"]["message"])
        self.assertEqual(library.identify_state()["counts"]["unmatched"], 1)
        self.assertEqual(library.library_store.decisions(), {})

    def test_a_decide_with_no_video_id_is_refused_at_the_edge(self):
        library = self._identified()
        status, payload = self.request("POST", "/api/library/match/decide",
                                       {"sceneId": SCENE_C_ID})
        self.assertEqual(status, 400)
        self.assertIn("videoId", payload["error"]["message"])
        self.assertEqual(library.identify_state()["counts"]["unmatched"], 1)

    def test_accept_recommended_takes_the_safe_rows_over_http(self):
        # The route reads no body: the server acts on the ambiguous rows it
        # already holds, so there is nothing for the browser to name and nothing
        # for the two of them to disagree about.
        video = legacy(93)
        rich = dict(fp_scene(SCENE_C_ID, oshashes=["os-93"]),
                    tags=[{"id": "t-1", "name": "Outdoor"}])
        library = build(videos=(video,), hashes={video["path"]: "os-93"},
                        by_hash={"os-93": [fp_scene(SCENE_B_ID,
                                                    oshashes=["os-93"]), rich]})
        library.connect()
        run_scan(library)
        self.assertEqual(run_identify(library)["counts"]["ambiguous"], 1)
        self.serve(library)

        status, payload = self.request("POST",
                                       "/api/library/match/accept-recommended")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"accepted": 1, "left": 0, "leftReasons": {}})
        self.assertEqual(library.identify_state()["counts"]["verified"], 1)
        self.assertEqual(library.library_store.decisions()["ch-93"]["scene_id"],
                         SCENE_C_ID)

    def _one_question(self):
        """One ambiguous row, safe to accept, whose video and scenes name nobody."""
        video = legacy(93)
        rich = dict(fp_scene(SCENE_C_ID, oshashes=["os-93"]),
                    tags=[{"id": "t-1", "name": "Outdoor"}])
        library = build(videos=(video,), hashes={video["path"]: "os-93"},
                        by_hash={"os-93": [fp_scene(SCENE_B_ID,
                                                    oshashes=["os-93"]), rich]},
                        framer=lambda path, fraction, duration=None, **kw:
                            (b"\xff\xd8 still", None),
                        # The fixture's paths are made up; the real disk must
                        # not decide whether a frame is tried.
                        path_exists=lambda path: True)
        library.connect()
        run_scan(library)
        self.assertEqual(run_identify(library)["counts"]["ambiguous"], 1)
        self.serve(library)
        return library

    def test_accept_recommended_can_take_one_models_group_only(self):
        library = self._one_question()
        status, payload = self.request(
            "POST", "/api/library/match/accept-recommended", {"model": "nobody"})
        self.assertEqual((status, payload["accepted"]), (200, 0))
        # The row names no model at all, so it is in the "No model" group.
        status, payload = self.request(
            "POST", "/api/library/match/accept-recommended", {"model": ""})
        self.assertEqual((status, payload["accepted"]), (200, 1))
        self.assertEqual(library.identify_state()["counts"]["verified"], 1)

    def test_a_model_that_is_not_text_is_refused_at_the_edge(self):
        library = self._one_question()
        status, payload = self.request(
            "POST", "/api/library/match/accept-recommended", {"model": 5})
        self.assertEqual(status, 400)
        self.assertIn("model", payload["error"]["message"])
        self.assertEqual(library.identify_state()["counts"]["ambiguous"], 1)

    def test_accept_recommended_can_take_only_the_videos_sent(self):
        library = self._one_question()
        status, payload = self.request(
            "POST", "/api/library/match/accept-recommended", {"videoIds": [999]})
        self.assertEqual((status, payload["accepted"]), (200, 0))
        status, payload = self.request(
            "POST", "/api/library/match/accept-recommended", {"videoIds": [93]})
        self.assertEqual((status, payload["accepted"]), (200, 1))
        self.assertEqual(library.identify_state()["counts"]["verified"], 1)

    def test_video_ids_that_are_not_a_list_are_refused_at_the_edge(self):
        library = self._one_question()
        status, payload = self.request(
            "POST", "/api/library/match/accept-recommended", {"videoIds": "93"})
        self.assertEqual(status, 400)
        self.assertIn("videoIds", payload["error"]["message"])
        self.assertEqual(library.identify_state()["counts"]["ambiguous"], 1)

    def fetch(self, path):
        """A GET answered with bytes: ``(status, content type, body)``."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers.get("Content-Type"), exc.read()

    def test_a_frame_of_a_scanned_file_is_served_as_a_jpeg(self):
        self._one_question()
        status, ctype, body = self.fetch("/api/library/frame?video=93&n=0")
        self.assertEqual((status, ctype, body), (200, "image/jpeg", b"\xff\xd8 still"))

    def test_a_malformed_frame_request_is_refused_at_the_edge(self):
        self._one_question()
        for query in ("video=93&n=x", "n=0", "video=93", "video=93&n=9"):
            status, _, body = self.fetch("/api/library/frame?" + query)
            self.assertEqual(status, 400, query)
            self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")

    def test_a_frame_of_a_file_not_in_the_scan_is_a_conflict(self):
        self._one_question()
        status, _, _ = self.fetch("/api/library/frame?video=999&n=0")
        self.assertEqual(status, 409)

    def test_accept_recommended_before_a_scan_is_a_conflict_not_a_500(self):
        # A scan in flight is the refusal left: an identify run no longer stops
        # an accept, it only keeps the rows it has not settled out of it.
        library = build()
        library.connect()
        library._scan["running"] = True
        self.serve(library)

        status, payload = self.request("POST",
                                       "/api/library/match/accept-recommended")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "CONFLICT")

    def test_cancelling_when_nothing_is_running_is_safe(self):
        library = build()
        library.connect()
        self.serve(library)

        status, payload = self.request("POST", "/api/library/match/cancel")
        self.assertEqual(status, 200)
        self.assertFalse(payload["running"])

    def _searchable(self, video_id=92):
        """A scanned library with one row an assisted search can run against."""
        video = parsed(video_id, models=[])
        library = build(videos=(video,),
                        hashes={video["path"]: "os-%d" % video_id},
                        search=[found_scene(SCENE_B_ID, "2019-03-04")])
        library.connect()
        run_scan(library)
        self.serve(library)
        return library

    def test_the_search_route_carries_its_overrides_upstream(self):
        self._searchable()
        status, payload = self.request(
            "POST", "/api/library/match/search",
            {"videoId": 92, "title": "Typed By Hand", "performers": [],
             "page": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["videoId"], 92)
        self.assertEqual(payload["terms"]["title"], "Typed By Hand")
        self.assertEqual([c["sceneId"] for c in payload["candidates"]],
                         [SCENE_B_ID])

    def test_a_string_of_performers_is_refused_at_the_edge(self):
        # `box.value` where `box.value.split(",")` was meant. Unguarded, the
        # string iterates its **characters**: one throttled upstream request per
        # letter, all resolving to nothing, and a search that then quietly runs
        # with no performer filter at all.
        library = self._searchable(93)
        status, payload = self.request(
            "POST", "/api/library/match/search",
            {"videoId": 93, "performers": "Model Alpha, Model Beta"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertIn("performers", payload["error"]["message"])
        self.assertEqual(performer_lookups(library), [])
        self.assertEqual(scene_query_inputs(library), [])

    def test_more_performer_names_than_the_cap_are_refused_at_the_edge(self):
        # Every name is its own paced upstream request on the handler's thread,
        # so an uncapped list is a request held open for as long as the list is.
        library = self._searchable(94)
        names = ["Model %d" % n for n in range(server.MAX_PERFORMER_NAMES + 1)]
        status, payload = self.request("POST", "/api/library/match/search",
                                       {"videoId": 94, "performers": names})
        self.assertEqual(status, 400)
        self.assertIn(str(server.MAX_PERFORMER_NAMES),
                      payload["error"]["message"])
        self.assertEqual(performer_lookups(library), [])

        # And the cap is a cap, not an off-by-one: exactly that many is fine.
        status, _ = self.request("POST", "/api/library/match/search",
                                 {"videoId": 94, "performers": names[:-1]})
        self.assertEqual(status, 200)
        self.assertEqual(len(performer_lookups(library)),
                         server.MAX_PERFORMER_NAMES)

    def test_misshapen_video_id_lists_are_refused_at_the_edge(self):
        # The same class as the `performers` guard, on the two routes that take a
        # list of ids. Both build `{int(i) for i in ...}`, so a bare string
        # iterates its **characters** and dies on one — a 500, a *server* fault,
        # reported for what is squarely a malformed request. A non-numeric entry
        # is the same 400, named, rather than an unattributed ValueError.
        library = self._identified()
        for path, key in (("/api/library/preview", "includeIds"),
                          ("/api/library/apply", "videoIds")):
            for body in ({key: "91,92"}, {key: {"91": True}}, {key: ["ninety"]}):
                with self.subTest(path=path, body=body):
                    status, payload = self.request("POST", path, body)
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
                    self.assertIn(key, payload["error"]["message"])
        # Nothing started on the way past: the guard runs before `start_apply`.
        self.assertFalse(library.apply_state()["running"])

        # And a well-formed list still gets through.
        status, _ = self.request("POST", "/api/library/preview",
                                 {"includeIds": [91]})
        self.assertEqual(status, 200)

    def test_a_mistyped_match_path_is_a_404(self):
        library = build()
        library.connect()
        self.serve(library)

        status, payload = self.request("POST", "/api/library/match/identifyy")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def raw(self, method, path, body=b"", headers=None):
        """A request with exactly the headers given, none added. (status, bytes)."""
        host, port = self.base[len("http://"):].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            sent = {"Host": "%s:%s" % (host, port)}
            sent.update(headers or {})
            if method == "POST":
                sent["Content-Length"] = str(len(body))
            for name, value in sent.items():
                conn.putheader(name, value)
            conn.endheaders(body if method == "POST" else None)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_a_foreign_origin_gets_403_on_get_and_post(self):
        library = build()
        self.serve(library)
        foreign = {"Origin": "https://example.com"}

        status, payload = self.request("GET", "/api/library/state", headers=foreign)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "FORBIDDEN")

        status, payload = self.request("POST", "/api/library/connect", {},
                                       headers=foreign)
        self.assertEqual(status, 403)
        self.assertFalse(library.connected)

    def test_the_pages_own_origin_is_accepted(self):
        self.serve(build())
        status, payload = self.request("GET", "/api/library/state",
                                       headers={"Origin": self.base})
        self.assertEqual(status, 200)
        self.assertIn("connected", payload)
        self.assertIn("fingerprints", payload["scan"])
        self.assertNotIn("depsMissing", payload["identify"])

    def test_post_without_json_content_type_is_415(self):
        library = build()
        self.serve(library)
        for headers in ({"Content-Type": "text/plain"}, {}):
            with self.subTest(headers=headers):
                status, data = self.raw("POST", "/api/library/connect", b"{}",
                                        headers)
                self.assertEqual(status, 415)
                self.assertEqual(json.loads(data)["error"]["code"],
                                 "UNSUPPORTED_MEDIA")
        self.assertFalse(library.connected)

    def test_post_with_json_and_charset_is_accepted(self):
        self.serve(build())
        status, data = self.raw(
            "POST", "/api/library/match/cancel", b"{}",
            {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)["running"])

    def test_put_and_delete_are_not_served(self):
        self.serve(build())
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                status, _ = self.raw(method, "/api/library/settings")
                self.assertEqual(status, 501)

    def test_stashdb_route_reaches_set_stashdb_key(self):
        library = build(stashdb_key="")
        self.serve(library)
        self.assertFalse(library.state()["stashdbConfigured"])

        status, payload = self.request("POST", "/api/library/stashdb",
                                       {"key": "a-new-key"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(library.stashdb.api_key, "a-new-key")

        status, payload = self.request("POST", "/api/library/stashdb",
                                       {"key": "has a space"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertEqual(library.stashdb.api_key, "a-new-key")

    def test_categories_refresh_route_reaches_refresh_categories(self):
        library = build(categories=[{"id": "c-9", "name": "Pace",
                                     "group": "SCENE", "description": None}])
        self.serve(library)
        status, payload = self.request("POST", "/api/library/categories/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"categories": 1})
        self.assertEqual([c["name"] for c in library.library_store.tag_categories()],
                         ["Pace"])


class TestPageHeaders(unittest.TestCase):
    CSP = ("default-src 'self'; img-src 'self' https:; "
           "style-src 'self' 'unsafe-inline'; object-src 'none'; "
           "base-uri 'none'; frame-ancestors 'none'")

    serve = TestMatchRoutes.serve

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers

    def test_page_carries_a_content_security_policy(self):
        self.serve(build())
        status, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Security-Policy"), self.CSP)

    def test_the_tab_icon_is_served_as_svg(self):
        self.serve(build())
        status, headers = self.get("/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/svg+xml")

    def test_only_html_carries_the_policy(self):
        self.serve(build())
        for path in ("/app.js", "/style.css", "/api/library/mapping",
                     "/api/nothing"):
            _, headers = self.get(path)
            self.assertIsNone(headers.get("Content-Security-Policy"), path)
