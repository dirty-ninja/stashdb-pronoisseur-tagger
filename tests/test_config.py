import json
import os
import shutil
import tempfile
import unittest

from tagger import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, data):
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_defaults_are_applied(self):
        path = self._write({"stashdb_api_key": "k"})
        cfg = config.load(path, env={})
        self.assertEqual(cfg["bind_port"], 8426)
        self.assertEqual(cfg["stashdb_endpoint"], "https://stashdb.org/graphql")

    def test_env_overrides_file(self):
        path = self._write({"stashdb_api_key": "k"})
        cfg = config.load(path, env={"TAGGER_BIND_PORT": "9999"})
        self.assertEqual(cfg["bind_port"], 9999)
        self.assertIsInstance(cfg["bind_port"], int)

    def test_env_coerces_floats(self):
        path = self._write({"stashdb_api_key": "k"})
        cfg = config.load(path, env={"TAGGER_REQUEST_INTERVAL_SECONDS": "1.5"})
        self.assertEqual(cfg["request_interval_seconds"], 1.5)

    def test_unknown_file_keys_are_preserved(self):
        path = self._write({"stashdb_api_key": "k", "future_option": 1})
        self.assertEqual(config.load(path, env={})["future_option"], 1)

    def test_starts_with_no_file_and_no_keys(self):
        cfg = config.load(os.path.join(self.dir, "absent.json"), env={})
        self.assertEqual(cfg["stashdb_api_key"], "")
        self.assertEqual(cfg["bind_port"], 8426)
        self.assertEqual(cfg["state_db_path"], "tagger-state.db")

    def test_discovery_keys_are_gone(self):
        for key in ("whisparr_url", "rail_max_batch",
                    "bind_host", "catalogue_page_size"):
            self.assertNotIn(key, config.DEFAULTS)
        self.assertEqual([k for k in config.DEFAULTS if "slot" in k], [])

    def test_an_old_config_with_ffmpeg_hwaccel_still_loads(self):
        path = self._write({"stashdb_api_key": "k", "ffmpeg_hwaccel": "auto"})
        cfg = config.load(path, env={})
        self.assertEqual(cfg["ffmpeg_hwaccel"], "auto")
        self.assertNotIn("ffmpeg_hwaccel", config.DEFAULTS)

    def test_interval_below_the_floor_is_raised(self):
        cfg = config.load(None, env={"TAGGER_REQUEST_INTERVAL_SECONDS": "0.01"})
        self.assertEqual(cfg["request_interval_seconds"], 0.34)

    def test_invalid_json_names_the_file(self):
        path = self._write("{not json")
        with self.assertRaises(config.ConfigError) as caught:
            config.load(path, env={})
        self.assertIn(path, str(caught.exception))

    def test_non_object_json_is_a_config_error(self):
        path = self._write("[1, 2]")
        with self.assertRaises(config.ConfigError):
            config.load(path, env={})

    def test_save_creates_the_file(self):
        path = os.path.join(self.dir, "new.json")
        config.save(path, {"stashdb_api_key": "k"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"stashdb_api_key": "k"})

    def test_save_merges_and_keeps_other_keys(self):
        path = self._write('{"bind_port": 9000}')
        config.save(path, {"pronoisseur_token": "t"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh),
                             {"bind_port": 9000, "pronoisseur_token": "t"})

    def test_save_leaves_no_temp_file(self):
        path = os.path.join(self.dir, "c.json")
        config.save(path, {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["c.json"])

    def test_save_refuses_to_overwrite_invalid_json(self):
        path = self._write("{not json")
        with self.assertRaises(config.ConfigError):
            config.save(path, {"a": 1})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_save_failure_is_a_config_error(self):
        with self.assertRaises(config.ConfigError):
            config.save(os.path.join(self.dir, "missing", "c.json"), {"a": 1})

    def test_credentials_are_stripped_from_file_and_env(self):
        path = self._write({"stashdb_api_key": " file-key\r\n",
                            "pronoisseur_token": "\tfile-token "})
        cfg = config.load(path, env={})
        self.assertEqual(cfg["stashdb_api_key"], "file-key")
        self.assertEqual(cfg["pronoisseur_token"], "file-token")
        cfg = config.load(path, env={"TAGGER_STASHDB_API_KEY": "env-key\r\n",
                                     "TAGGER_PRONOISSEUR_TOKEN": " env-token\n"})
        self.assertEqual(cfg["stashdb_api_key"], "env-key")
        self.assertEqual(cfg["pronoisseur_token"], "env-token")

    def test_a_non_string_credential_in_the_file_is_a_config_error(self):
        path = self._write({"stashdb_api_key": 12345})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load(path, env={})
        self.assertIn("stashdb_api_key", str(ctx.exception))
        self.assertNotIn("12345", str(ctx.exception))

    def test_an_unserialisable_value_is_a_config_error_and_leaves_no_temp_file(self):
        path = os.path.join(self.dir, "c.json")
        config.save(path, {"a": 1})
        with self.assertRaises(config.ConfigError):
            config.save(path, {"bad": object()})
        self.assertEqual(os.listdir(self.dir), ["c.json"])
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"a": 1})

    def test_a_non_numeric_interval_in_the_file_names_the_key_and_file(self):
        for bad in ("fast", None, True, [1], float("nan")):
            path = self._write({"request_interval_seconds": bad})
            with self.assertRaises(config.ConfigError) as ctx:
                config.load(path, env={})
            self.assertIn("request_interval_seconds", str(ctx.exception))
            self.assertIn(path, str(ctx.exception))

    def test_a_bad_port_in_the_file_names_the_key_and_file(self):
        for bad in ("abc", "8426", 8426.5, True, None, 0, 70000, -1):
            path = self._write({"bind_port": bad})
            with self.assertRaises(config.ConfigError) as ctx:
                config.load(path, env={})
            self.assertIn("bind_port", str(ctx.exception))
            self.assertIn(path, str(ctx.exception))

    def test_a_numeric_interval_and_port_in_the_file_still_load(self):
        path = self._write({"request_interval_seconds": 2, "bind_port": 9001})
        cfg = config.load(path, env={})
        self.assertEqual(cfg["request_interval_seconds"], 2)
        self.assertEqual(cfg["bind_port"], 9001)


class TestPronoisseurKeys(unittest.TestCase):
    def test_defaults_are_present(self):
        cfg = config.load(None, env={})
        self.assertEqual(cfg["pronoisseur_url"], "http://127.0.0.1:8869")
        self.assertEqual(cfg["pronoisseur_token"], "")
        self.assertEqual(cfg["pronoisseur_page_size"], 500)

    def test_token_can_come_from_the_environment(self):
        cfg = config.load(None, env={"TAGGER_PRONOISSEUR_TOKEN": "pron_abc"})
        self.assertEqual(cfg["pronoisseur_token"], "pron_abc")
