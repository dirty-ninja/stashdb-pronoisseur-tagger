import contextlib
import io
import json
import os
import socket
import tempfile
import types
import unittest

import run


class TestRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def config_with(self, values):
        """Write a config with an absolute state path; return its path."""
        values = dict({"state_db_path": os.path.join(self.dir, "state.db")}, **values)
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(values, fh)
        return path

    def args(self, path):
        return types.SimpleNamespace(config=path)

    def test_builds_with_no_keys(self):
        cfg, stashdb, library = run.build(self.args(self.config_with({})))
        self.assertEqual(stashdb.api_key, "")
        self.assertFalse(library.state()["stashdbConfigured"])

    def test_port_in_use_is_a_message_not_a_traceback(self):
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen()
        self.addCleanup(held.close)
        port = held.getsockname()[1]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run.main(["--no-open", "--config",
                             self.config_with({"bind_port": port})])
        self.assertEqual(code, 2)
        self.assertIn(str(port), err.getvalue())
        self.assertIn("bind_port", err.getvalue())

    def test_invalid_config_exits_2_and_names_the_file(self):
        path = os.path.join(self.dir, "c.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run.main(["--no-open", "--config", path])
        self.assertEqual(code, 2)
        self.assertIn(path, err.getvalue())

    def test_check_reports_not_configured_without_a_crash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run.main(["--check", "--config", self.config_with({})])
        self.assertEqual(code, 1)
        self.assertIn("not configured", out.getvalue())

    def test_check_makes_no_connection_for_an_empty_credential(self):
        class Refusing:
            api_key = ""

            def execute(self, *a, **k):
                raise AssertionError("StashDB was called without a key")

        cfg, stashdb, library = run.build(self.args(self.config_with({})))
        library.stashdb = Refusing()
        library._client_factory = lambda *a: self.fail("Pronoisseur was called")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(run.check(library.stashdb, library))
        self.assertEqual(out.getvalue().count("not configured"), 2)


if __name__ == "__main__":
    unittest.main()
