"""The tool never computes a fingerprint: Pronoisseur serves them.

A source scan, so that a hasher cannot creep back in under a new name without a
test noticing. Stdlib only.
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAGGER = os.path.join(ROOT, "tagger")

FORBIDDEN = ("def oshash", "def phash", "PerceptionHash",
             "import numpy", "from PIL")


class TestNoHashing(unittest.TestCase):
    def test_no_module_computes_a_fingerprint(self):
        found = []
        for name in sorted(os.listdir(TAGGER)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(TAGGER, name), encoding="utf-8") as fh:
                text = fh.read()
            found += ["%s: %s" % (name, bad) for bad in FORBIDDEN if bad in text]
        self.assertEqual(found, [])

    def test_the_hash_module_and_its_requirements_file_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(TAGGER, "fingerprints.py")))
        self.assertFalse(os.path.exists(os.path.join(ROOT,
                                                     "requirements-phash.txt")))


if __name__ == "__main__":
    unittest.main()
