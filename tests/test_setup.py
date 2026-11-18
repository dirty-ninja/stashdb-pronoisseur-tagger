import json
import threading
import unittest

from tagger.config import ConfigError
from tagger.library_app import LibraryStateError
from tagger.pronoisseur import PronoisseurError, is_loopback_url
from tagger.stashdb import StashDBError
from tests.test_library_app import (CATEGORIES, NO_UUID, build, run_identify,
                                    run_scan)


def raising(exc):
    def transport(payload):
        raise exc
    return transport


class FailingClient:
    def __init__(self, error="unauthorized"):
        self.error = error

    def info(self):
        raise PronoisseurError("rejected", code=self.error, status=401)


class NothingAnswering:
    """Fails the way the transport does when no server is listening."""

    def info(self):
        raise PronoisseurError("connection refused")


class TestSetup(unittest.TestCase):
    def test_a_good_key_is_trimmed_tested_saved_and_used(self):
        saved = []
        app = build(categories=CATEGORIES, stashdb_key="", save_settings=saved.append)
        out = app.set_stashdb_key("  new-key\n")
        self.assertTrue(out["ok"])
        self.assertEqual(out["categories"], 2)
        self.assertEqual(saved, [{"stashdb_api_key": "new-key"}])
        self.assertEqual(app.stashdb.api_key, "new-key")

    def test_a_bad_key_keeps_the_working_one(self):
        saved = []
        app = build(stashdb_key="old-key", save_settings=saved.append)
        app.stashdb._transport = raising(StashDBError("StashDB HTTP 401: no"))
        out = app.set_stashdb_key("bad")
        self.assertFalse(out["ok"])
        self.assertTrue(out["configured"])
        self.assertIn("401", out["error"])
        self.assertEqual(app.stashdb.api_key, "old-key")
        self.assertEqual(saved, [])

    def test_an_empty_key_is_a_value_error(self):
        with self.assertRaises(ValueError):
            build().set_stashdb_key("   ")

    def test_a_key_change_is_refused_while_a_job_runs(self):
        app = build()
        app._scan["running"] = True
        with self.assertRaises(LibraryStateError):
            app.set_stashdb_key("new")

    def test_connect_saves_a_trimmed_token_that_works(self):
        saved = []
        app = build(save_settings=saved.append)
        out = app.connect(token=" pron_new\n")
        self.assertTrue(out["connected"])
        self.assertEqual(saved, [{"pronoisseur_token": "pron_new"}])

    def test_nothing_answering_has_no_http_status(self):
        app = build()
        app._client_factory = lambda url, token: NothingAnswering()
        self.assertIsNone(app.connect(token="pron_bad")["status"])

    def test_a_refused_token_reports_its_http_status(self):
        out = build().connect(token="pron_wrong")
        self.assertFalse(out["connected"])
        self.assertEqual(out["status"], 401)

    def test_a_good_connection_has_no_failure_status(self):
        self.assertIsNone(build().connect()["status"])

    def test_connect_saves_nothing_when_it_fails(self):
        saved = []
        app = build(save_settings=saved.append)
        app._client_factory = lambda url, token: FailingClient()
        self.assertFalse(app.connect(token="pron_bad")["connected"])
        self.assertEqual(saved, [])

    def test_connect_refuses_a_non_loopback_url(self):
        with self.assertRaises(ValueError):
            build().connect("http://192.0.2.1:8869", "t")

    def test_is_loopback_url(self):
        for good in ("http://127.0.0.1:8869", "http://localhost:8869",
                     "http://[::1]:8869"):
            self.assertTrue(is_loopback_url(good), good)
        for bad in ("http://192.0.2.1:8869", "http://example.com", "file:///x", "",
                    "http://127.0.0.1.example.com", "http://user@example.com"):
            self.assertFalse(is_loopback_url(bad), bad)

    def test_a_failed_save_keeps_the_session_working_and_says_so(self):
        def fail(updates):
            raise ConfigError("disk full")
        app = build(save_settings=fail)
        out = app.connect(token="pron_new")
        self.assertTrue(out["connected"])
        self.assertFalse(out["saved"])
        self.assertIn("disk full", out["saveError"])

    def test_no_response_carries_a_secret(self):
        app = build(categories=CATEGORIES)
        text = json.dumps([app.set_stashdb_key("sekret-key"),
                           app.connect(token="pron_sekret"), app.state()])
        self.assertNotIn("sekret", text)

    def test_scan_is_refused_until_a_stashdb_key_exists(self):
        app = build(stashdb_key="")
        app.connect()
        with self.assertRaises(LibraryStateError):
            app.start_scan()

    def test_state_reports_what_is_configured(self):
        state = build(stashdb_key="").state()
        self.assertFalse(state["stashdbConfigured"])
        self.assertTrue(state["hasToken"])
        self.assertNotIn("tokenSource", state)

    def test_a_revoked_key_during_identify_ends_the_run_and_says_why(self):
        app = build(videos=(NO_UUID,), hashes={NO_UUID["path"]: "os-80"})
        app.connect()
        run_scan(app)
        original = app.stashdb._transport
        lookups = []

        def revoked(payload):
            if "fps" in (payload.get("variables") or {}):
                lookups.append(payload)
                raise StashDBError("StashDB HTTP 401: revoked")
            return original(payload)

        app.stashdb._transport = revoked
        state = run_identify(app)
        self.assertTrue(lookups, "identify never reached the fingerprint lookup")
        self.assertFalse(state["running"])
        self.assertIn("401", json.dumps(state))

    def test_a_scan_cannot_start_while_a_key_is_on_trial(self):
        app = build(stashdb_key="old-key", categories=CATEGORIES)
        app.connect()
        original = app.stashdb._transport
        outcome = {}

        def starter():
            try:
                outcome["result"] = app.start_scan()
            except Exception as exc:                        # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=starter)

        def transport(payload):
            if "TagCategories" in payload.get("query", ""):
                thread.start()
                thread.join(0.3)
                outcome["blocked"] = thread.is_alive()
                outcome["running_during"] = app._scan["running"]
            return original(payload)

        app.stashdb._transport = transport
        out = app.set_stashdb_key("new-key")
        thread.join(10)
        self.assertTrue(out["ok"])
        self.assertTrue(outcome["blocked"])
        self.assertFalse(outcome["running_during"])
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        app.await_scan()

    def test_a_key_with_an_inner_newline_is_refused_without_echoing_it(self):
        app = build()
        with self.assertRaises(ValueError) as ctx:
            app.set_stashdb_key("sekret\nkey")
        self.assertNotIn("sekret", str(ctx.exception))
        self.assertIn("StashDB API key", str(ctx.exception))

    def test_a_token_with_an_inner_newline_is_refused_without_echoing_it(self):
        app = build()
        with self.assertRaises(ValueError) as ctx:
            app.connect(token="pron_sekret\nmore")
        self.assertNotIn("sekret", str(ctx.exception))
        self.assertIn("Pronoisseur token", str(ctx.exception))

    def test_a_key_with_a_space_or_non_ascii_is_refused(self):
        for bad in ("two words", "caf\u00e9"):
            with self.assertRaises(ValueError):
                build().set_stashdb_key(bad)


if __name__ == "__main__":
    unittest.main()


class TestNonStringCredentials(unittest.TestCase):
    def test_a_non_string_stashdb_key_is_a_value_error_without_an_echo(self):
        for bad in (12345, ["fake-key"], {"k": "fake-key"}, True):
            with self.assertRaises(ValueError) as ctx:
                build().set_stashdb_key(bad)
            self.assertNotIn("fake-key", str(ctx.exception))
            self.assertNotIn("12345", str(ctx.exception))

    def test_a_non_string_pronoisseur_token_or_url_is_a_value_error(self):
        for kwargs in ({"token": 12345}, {"token": ["fake-token"]},
                       {"url": 12345}, {"url": ["http://127.0.0.1:8869"]}):
            with self.assertRaises(ValueError) as ctx:
                build().connect(**kwargs)
            self.assertNotIn("fake-token", str(ctx.exception))
            self.assertNotIn("12345", str(ctx.exception))
