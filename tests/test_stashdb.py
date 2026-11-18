import unittest

from tagger.stashdb import MAX_PER_PAGE, StashDB, StashDBError


def fake_transport(pages, root="queryPerformers", list_key="performers"):
    """Return a transport yielding the given (count, rows) pages in order."""
    calls = []

    def _t(payload):
        count, rows = pages[len(calls)]
        calls.append(payload)
        return {"data": {root: {"count": count, list_key: rows}}}

    _t.calls = calls
    return _t


def rows(start, stop):
    return [{"id": "id-%d" % i} for i in range(start, stop)]


class TestPaginate(unittest.TestCase):
    def _client(self, transport):
        return StashDB("http://x/graphql", "key", interval=0, transport=transport)

    def test_pages_until_count_satisfied(self):
        t = fake_transport([(250, rows(0, 100)), (250, rows(100, 200)),
                            (250, rows(200, 250))])
        got = self._client(t).paginate("q", {}, "queryPerformers", "performers")
        self.assertEqual(len(got), 250)
        self.assertEqual(len(t.calls), 3)

    def test_page_numbers_increment_from_one(self):
        t = fake_transport([(150, rows(0, 100)), (150, rows(100, 150))])
        self._client(t).paginate("q", {}, "queryPerformers", "performers")
        self.assertEqual([c["variables"]["input"]["page"] for c in t.calls], [1, 2])

    def test_short_page_before_count_raises(self):
        t = fake_transport([(250, rows(0, 40))])
        with self.assertRaises(StashDBError) as ctx:
            self._client(t).paginate("q", {}, "queryPerformers", "performers")
        self.assertIn("truncated", str(ctx.exception).lower())

    def test_final_short_page_is_fine(self):
        t = fake_transport([(120, rows(0, 100)), (120, rows(100, 120))])
        got = self._client(t).paginate("q", {}, "queryPerformers", "performers")
        self.assertEqual(len(got), 120)

    def test_zero_count_yields_nothing(self):
        t = fake_transport([(0, [])])
        self.assertEqual(
            self._client(t).paginate("q", {}, "queryPerformers", "performers"), [])

    def test_per_page_is_clamped_to_max(self):
        t = fake_transport([(5, rows(0, 5))])
        self._client(t).paginate("q", {}, "queryPerformers", "performers",
                                 per_page=500)
        self.assertEqual(t.calls[0]["variables"]["input"]["per_page"], MAX_PER_PAGE)

    def test_max_rows_stops_early_without_raising(self):
        t = fake_transport([(1000, rows(0, 100))])
        got = self._client(t).paginate("q", {}, "queryPerformers", "performers",
                                       max_rows=100)
        self.assertEqual(len(got), 100)
        self.assertEqual(len(t.calls), 1)

    def test_empty_page_before_count_raises(self):
        t = fake_transport([(250, rows(0, 100)), (250, [])])
        with self.assertRaises(StashDBError):
            self._client(t).paginate("q", {}, "queryPerformers", "performers")


class TestExecute(unittest.TestCase):
    def _client(self, transport):
        return StashDB("http://x/graphql", "key", interval=0, transport=transport)

    def test_graphql_errors_raise_not_return_empty(self):
        def t(_payload):
            return {"errors": [{"message": "unknown field ethnicity"}]}
        with self.assertRaises(StashDBError) as ctx:
            self._client(t).execute("q", {})
        self.assertIn("ethnicity", str(ctx.exception))

    def test_missing_data_key_raises(self):
        def t(_payload):
            return {}
        with self.assertRaises(StashDBError):
            self._client(t).execute("q", {})

    def test_count_uses_a_single_row_page(self):
        t = fake_transport([(2400, rows(0, 1))])
        self.assertEqual(
            self._client(t).count("q", {}, "queryPerformers"), 2400)
        self.assertEqual(t.calls[0]["variables"]["input"]["per_page"], 1)


class TestPacing(unittest.TestCase):
    def test_two_threads_asking_at_once_are_still_spaced_apart(self):
        # The identify run and a click on the page (a pick, an assisted search)
        # share one client while a run is going. Both waiting out the same gap and
        # then firing together would be two requests inside the pacing floor.
        import threading
        import time

        stamps = []

        def t(_payload):
            stamps.append(time.monotonic())
            return {"data": {}}

        client = StashDB("http://x/graphql", "key", interval=0.2, transport=t)
        client._last_call = time.monotonic()      # both callers have to wait
        gate = threading.Barrier(2)

        def ask():
            gate.wait()
            client.execute("q", {})

        threads = [threading.Thread(target=ask) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(stamps), 2)
        self.assertGreaterEqual(abs(stamps[1] - stamps[0]), 0.18)


class TestUserAgent(unittest.TestCase):
    def test_user_agent_names_the_tool(self):
        from tagger import stashdb
        self.assertTrue(stashdb.USER_AGENT.startswith("stashdb-pronoisseur-tagger/"))
        self.assertNotIn("Mozilla", stashdb.USER_AGENT)


class TestUnsendableKey(unittest.TestCase):
    def test_an_unsendable_key_is_not_echoed(self):
        # A newline inside a key makes http.client refuse the header with a
        # ValueError that quotes it. The real transport must not repeat it.
        import http.server
        import threading

        server = http.server.HTTPServer(("127.0.0.1", 0),
                                        http.server.BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        key = "fake-key-line-one\nfake-key-line-two"
        client = StashDB("http://127.0.0.1:%d/graphql" % server.server_port, key,
                         interval=0)
        with self.assertRaises(StashDBError) as ctx:
            client.execute("query { x }", {})
        self.assertNotIn("fake-key", str(ctx.exception))
        self.assertIn("StashDB request failed", str(ctx.exception))
