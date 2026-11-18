#!/usr/bin/env python3
"""Live smoke check: does StashDB still have the shape this code assumes?

The unit suite runs offline against fixtures, so it cannot notice StashDB
renaming a field or changing a limit. Those changes do not raise; they present
as an empty result set, which reads like "nothing matched". This script exists
to tell the difference.

    python scripts/smoke.py

It only reads, and needs a StashDB key saved in config.json (or set in
TAGGER_STASHDB_API_KEY). Exit code 0 means every assumption held, 1 means a
check failed, 2 means no key is configured.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagger import config, queries  # noqa: E402
from tagger.stashdb import MAX_PER_PAGE, StashDB  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((PASS, name, detail))
    except Exception as exc:  # noqa: BLE001 - a failed check is the point
        results.append((FAIL, name, str(exc)))


def main():
    try:
        cfg = config.load(config.DEFAULT_PATH)
    except config.ConfigError as exc:
        print("Configuration error: %s" % exc, file=sys.stderr)
        return 2
    if not cfg["stashdb_api_key"]:
        print("No StashDB API key is configured. Save one on the Connect step "
              "of the page, or set TAGGER_STASHDB_API_KEY, then run this again.",
              file=sys.stderr)
        return 2

    sdb = StashDB(cfg["stashdb_endpoint"], cfg["stashdb_api_key"],
                  interval=cfg["request_interval_seconds"])

    def auth():
        data = sdb.execute(queries.QUERY_TAG_CATEGORIES, {})
        rows = data["queryTagCategories"]["tag_categories"]
        if not rows:
            raise AssertionError("queryTagCategories returned no rows")
        return "ApiKey header accepted; %d tag categories" % len(rows)

    def per_page_cap():
        data = sdb.execute(queries.QUERY_SCENES,
                           {"input": dict(queries.build_scene_input({}),
                                          page=1, per_page=500)})
        rows = len(data["queryScenes"]["scenes"])
        if rows > MAX_PER_PAGE:
            raise AssertionError(
                "per_page cap has changed: asked 500, got %d. Paging assumptions "
                "in tagger/stashdb.py need revisiting." % rows)
        return "asked for 500 rows, received %d - cap intact" % rows

    def scene_payload():
        data = sdb.execute(queries.QUERY_SCENES,
                           {"input": dict(queries.build_scene_input({}),
                                          page=1, per_page=1)})
        scenes = data["queryScenes"]["scenes"]
        if not scenes:
            raise AssertionError("queryScenes returned nothing at all")
        missing = [f for f in ("id", "title", "release_date", "studio", "performers")
                   if f not in scenes[0]]
        if missing:
            raise AssertionError("scene payload missing %s" % ", ".join(missing))
        return "scene payload carries id, title, release_date, studio, performers"

    check("StashDB auth", auth)
    check("per_page cap = %d" % MAX_PER_PAGE, per_page_cap)
    check("scene payload", scene_payload)

    width = max(len(name) for _, name, _ in results)
    print()
    for status, name, detail in results:
        print("  %-4s  %-*s  %s" % (status, width, name, detail))
    failed = [r for r in results if r[0] == FAIL]
    print()
    if failed:
        print("%d of %d checks FAILED. The code's assumptions no longer hold - "
              "fix before trusting a result." % (len(failed), len(results)))
        return 1
    print("All %d checks passed." % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
