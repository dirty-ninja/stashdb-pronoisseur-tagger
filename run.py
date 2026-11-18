#!/usr/bin/env python3
"""Match a Pronoisseur library against StashDB and write the tags back.

    python run.py              serve and open a browser tab
    python run.py --no-open    serve only
    python run.py --check      verify both connections answer, then exit

Standard library only. The page is served from this process on the loopback
address; keys are entered there and kept in config.json.
"""

import argparse
import os
import sys
import threading
import webbrowser

from tagger import config
from tagger.library_app import LibraryApp
from tagger.library_store import LibraryStore
from tagger.server import make_server
from tagger.stashdb import StashDB, StashDBError


def build(args):
    cfg = config.load(args.config)
    stashdb = StashDB(cfg["stashdb_endpoint"], cfg["stashdb_api_key"],
                      interval=cfg["request_interval_seconds"])
    db_path = cfg["state_db_path"]
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
    library = LibraryApp(
        cfg, stashdb, LibraryStore(db_path),
        save_settings=lambda updates: config.save(args.config, updates))
    return cfg, stashdb, library


def check(stashdb, library):
    """Report each connection; a credential that is not set is not attempted."""
    ok = True
    if not stashdb.api_key:
        print("  StashDB      not configured")
        ok = False
    else:
        try:
            count = library.refresh_categories()["categories"]
            print("  StashDB      OK - %d tag categories" % count)
        except StashDBError as exc:
            print("  StashDB      FAILED - %s" % exc)
            ok = False
    if not library.state()["hasToken"]:
        print("  Pronoisseur  not configured")
        ok = False
    else:
        try:
            result = library.connect()
        except ValueError as exc:
            result = {"connected": False, "error": str(exc)}
        if result["connected"]:
            print("  Pronoisseur  OK - %s"
                  % ("can write" if result["canWrite"] else "read-only token"))
        else:
            print("  Pronoisseur  FAILED - %s" % result["error"])
            ok = False
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=config.DEFAULT_PATH,
                        help="path to config.json (default: beside this script)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser tab")
    parser.add_argument("--check", action="store_true",
                        help="verify both connections answer, then exit")
    args = parser.parse_args(argv)

    try:
        cfg, stashdb, library = build(args)
    except config.ConfigError as exc:
        print("Configuration error: %s" % exc, file=sys.stderr)
        return 2

    if args.check:
        print("Checking connections:")
        return 0 if check(stashdb, library) else 1

    try:
        httpd = make_server(cfg, library)
    except OSError:
        print("Port %d is already in use. Stop whatever holds it, or set "
              "bind_port in config.json." % int(cfg["bind_port"]), file=sys.stderr)
        return 2

    url = "http://127.0.0.1:%d/" % int(cfg["bind_port"])
    print("serving %s" % url)
    print("Ctrl-C to stop.")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
