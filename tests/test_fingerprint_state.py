import unittest

from tagger.library_common import (PENDING_NOT_YET, PENDING_OLD_PRONOISSEUR,
                                   PENDING_SWITCH_OFF, fingerprint_state,
                                   pending_reason)


class FingerprintStateTest(unittest.TestCase):
    def test_a_block_is_known(self):
        info = {"fingerprints": {"phash_enabled": False, "phash_failed": 3}}
        self.assertEqual(fingerprint_state(info, []),
                         {"state": "known", "phashEnabled": False, "phashFailed": 3})

    def test_no_block_and_no_keys_is_older(self):
        self.assertEqual(fingerprint_state({"api_version": 1}, [{"id": 1}])["state"],
                         "older")

    def test_a_phash_key_on_any_video_counts_as_support(self):
        out = fingerprint_state({"api_version": 1}, [{"id": 1}, {"id": 2, "phash": None}])
        self.assertEqual(out, {"state": "known", "phashEnabled": None,
                               "phashFailed": None})

    def test_no_info_is_unknown(self):
        self.assertEqual(fingerprint_state(None, [{"phash": "ab"}])["state"], "unknown")

    def test_a_non_numeric_failed_count_is_unknown_not_an_error(self):
        info = {"fingerprints": {"phash_enabled": "yes", "phash_failed": "lots"}}
        self.assertEqual(fingerprint_state(info, []),
                         {"state": "known", "phashEnabled": None, "phashFailed": None})
        info = {"fingerprints": {"phash_enabled": True, "phash_failed": [1]}}
        self.assertIsNone(fingerprint_state(info, [])["phashFailed"])
        info = {"fingerprints": {"phash_enabled": True, "phash_failed": 2}}
        self.assertEqual(fingerprint_state(info, [])["phashEnabled"], True)

    def test_reasons(self):
        self.assertEqual(pending_reason({"state": "older"}), PENDING_OLD_PRONOISSEUR)
        self.assertEqual(pending_reason({"state": "known", "phashEnabled": False}),
                         PENDING_SWITCH_OFF)
        self.assertEqual(pending_reason({"state": "known", "phashEnabled": True}),
                         PENDING_NOT_YET)
        self.assertEqual(pending_reason({"state": "unknown", "phashEnabled": None}),
                         PENDING_NOT_YET)


if __name__ == "__main__":
    unittest.main()
