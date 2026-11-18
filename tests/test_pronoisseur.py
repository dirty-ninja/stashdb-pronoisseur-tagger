import unittest

from tagger.pronoisseur import MAX_PAGE_SIZE, Pronoisseur, PronoisseurError


class FakeTransport:
    """Records calls; answers from a routing table of (method, path) -> (status, payload).

    A list value is a script: one entry consumed per call, so retry behaviour can be
    tested without sleeping.
    """

    def __init__(self, routes):
        self.routes, self.calls = dict(routes), []

    def __call__(self, method, path, body, headers):
        self.calls.append({"method": method, "path": path, "body": body,
                           "headers": dict(headers)})
        key = (method, path)
        if key not in self.routes:
            raise AssertionError("unexpected %s %s" % (method, path))
        value = self.routes[key]
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value


def client(routes, **kw):
    kw.setdefault("sleep", lambda _s: None)
    return Pronoisseur("http://127.0.0.1:8869", "pron_test",
                       transport=FakeTransport(routes), **kw)


class TestRequestShape(unittest.TestCase):
    def test_bearer_token_and_json_accept_on_every_call(self):
        c = client({("GET", "/api/v1/info"): (200, {"api_version": 1})})
        c.info()
        headers = c._transport.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer pron_test")
        self.assertEqual(headers["Accept"], "application/json")

    def test_no_origin_header_is_ever_sent(self):
        # An Origin header is 403 forbidden_origin on every route, health included.
        c = client({("GET", "/api/v1/info"): (200, {}),
                    ("GET", "/api/v1/health"): (200, {"ok": True})})
        c.info()
        c.health()
        for call in c._transport.calls:
            self.assertNotIn("Origin", call["headers"])

    def test_health_carries_no_token(self):
        c = client({("GET", "/api/v1/health"): (200, {"ok": True})})
        self.assertTrue(c.health()["ok"])
        self.assertNotIn("Authorization", c._transport.calls[0]["headers"])

    def test_content_type_only_when_a_body_is_sent(self):
        c = client({("GET", "/api/v1/models"): (200, []),
                    ("POST", "/api/v1/models"): (201, {"id": 1, "name": "A"})})
        c.models()
        self.assertNotIn("Content-Type", c._transport.calls[0]["headers"])
        c.upsert_model("A")
        self.assertEqual(c._transport.calls[1]["headers"]["Content-Type"],
                         "application/json")


class TestErrorMapping(unittest.TestCase):
    def test_error_envelope_becomes_a_typed_error(self):
        c = client({("PATCH", "/api/v1/videos/9"): (
            422, {"error": {"code": "validation", "message": "release_date must be YYYY-MM-DD",
                            "details": [{"index": 0}]}})})
        with self.assertRaises(PronoisseurError) as ctx:
            c.patch_video(9, {"release_date": "nope"})
        self.assertEqual(ctx.exception.code, "validation")
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.details, [{"index": 0}])
        self.assertIn("YYYY-MM-DD", str(ctx.exception))

    def test_401_and_403_keep_their_codes(self):
        c = client({("GET", "/api/v1/info"): (401, {"error": {"code": "unauthorized",
                                                              "message": "no"}})})
        with self.assertRaises(PronoisseurError) as ctx:
            c.info()
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_a_body_that_is_not_the_envelope_still_raises(self):
        c = client({("GET", "/api/v1/info"): (500, "<html>boom</html>")})
        with self.assertRaises(PronoisseurError) as ctx:
            c.info()
        self.assertEqual(ctx.exception.status, 500)


class TestBusyRetry(unittest.TestCase):
    def test_503_is_retried_then_succeeds(self):
        slept = []
        c = client({("GET", "/api/v1/videos?page=1&page_size=500"): [
            (503, {"error": {"code": "busy", "message": "locked"}}),
            (200, {"items": [], "page": 1, "page_size": 500, "total": 0}),
        ]}, sleep=slept.append)
        c.videos()
        self.assertEqual(len(c._transport.calls), 2)
        self.assertEqual(slept, [2.0])

    def test_503_gives_up_after_the_retry_budget(self):
        c = client({("GET", "/api/v1/info"): (503, {"error": {"code": "busy",
                                                              "message": "locked"}})},
                   retries=3)
        with self.assertRaises(PronoisseurError) as ctx:
            c.info()
        self.assertEqual(ctx.exception.code, "busy")
        self.assertEqual(len(c._transport.calls), 3)


class TestVideos(unittest.TestCase):
    def test_filters_become_query_parameters(self):
        c = client({("GET", "/api/v1/videos?missing_external_id=stashdb&page=1&page_size=500"):
                    (200, {"items": [], "page": 1, "page_size": 500, "total": 0})})
        c.videos(missing_external_id="stashdb")
        self.assertEqual(c._transport.calls[0]["path"],
                         "/api/v1/videos?missing_external_id=stashdb&page=1&page_size=500")

    def test_page_size_above_the_server_maximum_is_refused_locally(self):
        # The server rejects >500 with 422 rather than clamping; fail before the call.
        c = client({})
        with self.assertRaises(ValueError):
            c.videos(page_size=MAX_PAGE_SIZE + 1)

    def test_all_videos_pages_to_completion_using_total(self):
        page1 = {"items": [{"id": 1}, {"id": 2}], "page": 1, "page_size": 2, "total": 3}
        page2 = {"items": [{"id": 3}], "page": 2, "page_size": 2, "total": 3}
        c = client({("GET", "/api/v1/videos?page=1&page_size=2"): (200, page1),
                    ("GET", "/api/v1/videos?page=2&page_size=2"): (200, page2)})
        seen = []
        rows = c.all_videos(page_size=2, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])
        self.assertEqual(seen, [(2, 3), (3, 3)])

    def test_all_videos_stops_at_max_rows(self):
        page1 = {"items": [{"id": 1}, {"id": 2}], "page": 1, "page_size": 2, "total": 9}
        c = client({("GET", "/api/v1/videos?page=1&page_size=2"): (200, page1)})
        self.assertEqual(len(c.all_videos(page_size=2, max_rows=2)), 2)

    def test_a_short_page_before_total_is_reached_is_an_error_not_a_silent_stop(self):
        page1 = {"items": [{"id": 1}], "page": 1, "page_size": 2, "total": 9}
        c = client({("GET", "/api/v1/videos?page=1&page_size=2"): (200, page1)})
        with self.assertRaises(PronoisseurError):
            c.all_videos(page_size=2)


class TestWrites(unittest.TestCase):
    def test_batch_rejects_an_oversized_chunk_before_sending(self):
        c = client({})
        with self.assertRaises(ValueError):
            c.batch([{"id": i} for i in range(501)])

    def test_batch_rejects_an_empty_list(self):
        c = client({})
        with self.assertRaises(ValueError):
            c.batch([])

    def test_upsert_model_sends_name_external_ids_and_aliases(self):
        c = client({("POST", "/api/v1/models"): (201, {"id": 5, "name": "Jamie Example"})})
        c.upsert_model("Jamie Example",
                       external_ids=[{"provider": "stashdb", "id": "p-1"}],
                       aliases=["J. Example"])
        body = c._transport.calls[0]["body"]
        self.assertEqual(body, {"name": "Jamie Example",
                                "external_ids": [{"provider": "stashdb", "id": "p-1"}],
                                "aliases": ["J. Example"]})

    def test_upsert_studio_never_sends_aliases(self):
        # aliases on /studios or /categories is 422 validation.
        c = client({("POST", "/api/v1/studios"): (200, {"id": 3, "name": "Example Network"})})
        c.upsert_studio("Example Network", external_ids=[{"provider": "stashdb", "id": "s-1"}])
        self.assertNotIn("aliases", c._transport.calls[0]["body"])

    def test_aliases_reads_the_list_and_sends_no_body(self):
        rows = [{"kind": "actor", "raw_token": "J Example",
                 "canonical_name": "Jamie Example"}]
        c = client({("GET", "/api/v1/aliases"): (200, rows)})
        self.assertEqual(c.aliases(), rows)
        self.assertIsNone(c._transport.calls[0]["body"])
        self.assertNotIn("Content-Type", c._transport.calls[0]["headers"])

    def test_aliases_answers_a_list_even_when_the_app_sends_nothing(self):
        c = client({("GET", "/api/v1/aliases"): (200, None)})
        self.assertEqual(c.aliases(), [])

    def test_put_aliases_sends_a_bare_array(self):
        c = client({("PUT", "/api/v1/aliases"): (200, {"count": 1})})
        entries = [{"kind": "actor", "raw_token": "J Example", "canonical_name": "Jamie Example"}]
        c.put_aliases(entries)
        self.assertEqual(c._transport.calls[0]["body"], entries)

    def test_put_aliases_refuses_an_empty_array(self):
        c = client({})
        with self.assertRaises(ValueError):
            c.put_aliases([])


class TestScopes(unittest.TestCase):
    def test_can_write_reads_the_granted_scopes(self):
        c = client({("GET", "/api/v1/info"): (200, {
            "app_version": "1.0.49", "api_version": 1,
            "app": {"name": "tagger", "scopes": ["library.read"]}})})
        self.assertEqual(c.scopes(), ["library.read"])
        self.assertFalse(c.can_write())

    def test_can_write_is_true_when_the_scope_is_granted(self):
        c = client({("GET", "/api/v1/info"): (200, {
            "app": {"name": "tagger", "scopes": ["library.read", "library.write"]}})})
        self.assertTrue(c.can_write())


if __name__ == "__main__":
    unittest.main()


class TestUnsendableToken(unittest.TestCase):
    def test_an_unsendable_token_is_not_echoed(self):
        import http.server
        import threading

        server = http.server.HTTPServer(("127.0.0.1", 0),
                                        http.server.BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        token = "fake-token-line-one\nfake-token-line-two"
        api = Pronoisseur("http://127.0.0.1:%d" % server.server_port, token,
                          retries=1)
        with self.assertRaises(PronoisseurError) as ctx:
            api.info()
        self.assertNotIn("fake-token", str(ctx.exception))
        self.assertIn("Pronoisseur request failed", str(ctx.exception))
