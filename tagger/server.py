"""Local JSON API and static server.

The browser talks only to this process, which holds the StashDB key and the
Pronoisseur token; neither is ever sent to the page.

The listener is loopback-only, and every request is checked twice. A Host header
that is not loopback is refused, which closes DNS rebinding (a page on the open
internet resolving its own name to 127.0.0.1). A request that carries an Origin
other than this server's own is refused, which closes cross-site form posts and
fetches from any page the user happens to have open. A POST must also declare a
JSON body, so a cross-site form, which cannot, never reaches a handler.
"""

import json
import os
import posixpath
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .library_app import DECIDE_ACTIONS, LibraryStateError
from .pronoisseur import PronoisseurError
from .stashdb import StashDBError

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web")

STATUS_CODES = {
    "BAD_REQUEST": 400,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "UNSUPPORTED_MEDIA": 415,
    "UPSTREAM_ERROR": 502,
}

CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".ico": "image/x-icon"}

MAX_BODY_BYTES = 1_000_000

#: Sent with the page only. Scripts and styles come from this server and nothing
#: else; covers may load from any https host; nothing may embed the page or
#: change its base address.
PAGE_POLICY = ("default-src 'self'; img-src 'self' https:; "
               "style-src 'self' 'unsafe-inline'; object-src 'none'; "
               "base-uri 'none'; frame-ancestors 'none'")


# ------------------------------------------------------------------- helpers

def status_for_code(code):
    return STATUS_CODES.get(code, 500)


def code_for_exception(exc):
    if isinstance(exc, (StashDBError, PronoisseurError)):
        return "UPSTREAM_ERROR"
    if isinstance(exc, ValueError):
        return "BAD_REQUEST"
    # "connect to Pronoisseur first" and "this token is read-only" are states, not
    # bugs: 409 tells the UI to show the Connect panel instead of a red toast. Only
    # that named subclass: a bare RuntimeError (a thread that would not start, say)
    # is a 500, and calling it a conflict would hide a real fault behind a panel.
    if isinstance(exc, LibraryStateError):
        return "CONFLICT"
    return "INTERNAL"


def error_response(code, message, **extra):
    payload = {"error": dict({"code": code, "message": str(message)}, **extra)}
    return json.dumps(payload).encode("utf-8"), status_for_code(code)


def host_allowed(host_header):
    """True when the Host header names this computer."""
    if not host_header:
        return False
    if host_header.startswith("["):
        host = host_header.split("]")[0] + "]"
    else:
        host = host_header.rsplit(":", 1)[0]
    return host.lower() in ("127.0.0.1", "localhost", "[::1]", "::1")


def origin_allowed(origin, host_header):
    """True when there is no Origin, or it is this server's own address."""
    if origin is None:
        return True
    return origin.lower() == ("http://" + (host_header or "")).lower()


def static_target(path):
    """Map a request path to a file under web/, or None if it is not allowed."""
    decoded = urllib.parse.unquote(path or "/")
    # posixpath does not treat a backslash as a separator, but Windows does, so
    # `a\..\..\x` would otherwise climb out of web/ after the checks below.
    if "\\" in decoded:
        return None
    if decoded in ("/", ""):
        return "index.html"
    clean = posixpath.normpath(decoded).lstrip("/")
    if not clean or clean.startswith("..") or ".." in clean.split("/"):
        return None
    if os.path.splitext(clean)[1].lower() not in CONTENT_TYPES:
        return None
    return clean


def _video_id(body):
    """The ``videoId`` a match route was asked about, as an int.

    Validated at the edge rather than inside ``LibraryApp``: a missing or
    non-numeric id is a malformed request, and letting it reach ``_row_index``
    would answer "re-scan and try again", sending the user off to fix a scan
    that is perfectly fine.
    """
    try:
        return int(body.get("videoId"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "videoId is required and must be a whole number") from exc


def _decide_action(body):
    """The answer a decide route carries, refused here if it is not one."""
    action = body.get("action", "pick")
    if action not in DECIDE_ACTIONS:
        raise ValueError("action must be one of %s; got %r"
                         % (", ".join(DECIDE_ACTIONS), action))
    return action


#: Performer names one assisted search may carry. Each one costs its own
#: ``queryPerformers`` request, paced by ``StashDB.interval`` on the handler's
#: own thread — twenty is already about seven seconds for a single click. A
#: filename that parsed out more than twenty performers is a bad parse, not a
#: search worth running.
MAX_PERFORMER_NAMES = 20


def _performer_names(body):
    """The ``performers`` an assisted search was asked for, or None.

    Shape-checked at the edge with ``videoId`` and ``action``, because the two
    plausible malformed bodies both *succeed* quietly. A string —
    ``"a name, another"`` where a list was meant — iterates its **characters**:
    one throttled upstream request per letter, every one resolving to nothing,
    a search that then runs with no performer filter at all, and a screenful of
    single letters in ``unresolved`` that reads exactly like "StashDB has never
    heard of them". A dict iterates its keys just as silently.
    """
    names = body.get("performers")
    if names is None:
        return None
    if not isinstance(names, list):
        raise ValueError("performers must be a list of names, got %s"
                         % type(names).__name__)
    if len(names) > MAX_PERFORMER_NAMES:
        raise ValueError("at most %d performer names per search; got %d"
                         % (MAX_PERFORMER_NAMES, len(names)))
    return names


def _model_key(body):
    """The model group an accept is narrowed to, or None for every row.

    A string, checked at the edge: ``""`` is the "No model" group, and anything
    else that is not text would reach the comparison as a key that matches no
    group, quietly accepting nothing.
    """
    model = body.get("model")
    if model is None or isinstance(model, str):
        return model
    raise ValueError("model must be a model's name key, as text; got %s"
                     % type(model).__name__)


def _frame_query(query):
    """``(video id, frame number)`` from a frame request's query string."""
    params = urllib.parse.parse_qs(query or "")
    try:
        return int(params["video"][0]), int(params["n"][0])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("a frame needs a whole-number video and n") from exc


def _video_ids(body, key):
    """The list of video ids under ``key``, or None if the body omits it.

    Shape-checked at the edge for the same reason as ``performers``: the two
    routes that take one build a set with ``{int(i) for i in ...}``, so a bare
    string iterates its **characters** and a dict its keys. Every plausible
    malformed body then dies inside ``LibraryApp`` on a ``ValueError`` nobody
    wrote a message for, and the browser is told 500 — a server fault — for what
    is squarely a malformed request. Contents are checked here too: one
    non-numeric entry is the same 400, named.
    """
    ids = body.get(key)
    if ids is None:
        return None
    if not isinstance(ids, list):
        raise ValueError("%s must be a list of video ids, got %s"
                         % (key, type(ids).__name__))
    try:
        return [int(i) for i in ids]
    except (TypeError, ValueError) as exc:
        raise ValueError("every %s entry must be a whole number" % (key,)) from exc


# -------------------------------------------------------------------- routing

def make_handler(library):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stashdb-pronoisseur-tagger"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # Request paths can carry a video's file name; keep them out of the console.
            return

        def _send(self, body, status=200, content_type="application/json",
                  close=False, cache=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if cache:
                self.send_header("Cache-Control", cache)
            if content_type.startswith("text/html"):
                self.send_header("Content-Security-Policy", PAGE_POLICY)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status=200):
            self._send(json.dumps(payload), status)

        def _fail(self, code, message, close=False, **extra):
            body, status = error_response(code, message, **extra)
            self._send(body, status, close=close)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            if not length:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError as exc:
                raise ValueError("request body is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _guard(self):
            host = self.headers.get("Host")
            # A refused request's body is never read, so the connection is closed
            # rather than left to parse that body as the next request.
            if not host_allowed(host):
                self._fail("FORBIDDEN", "Host header not permitted", close=True)
                return False
            if not origin_allowed(self.headers.get("Origin"), host):
                self._fail("FORBIDDEN", "Origin not permitted", close=True)
                return False
            return True

        # verbs -----------------------------------------------------------

        def do_GET(self):
            if not self._guard():
                return
            url = urllib.parse.urlparse(self.path)
            path = url.path
            try:
                if path == "/api/library/state":
                    return self._json(library.state())
                # A still of the user's own file, by video id: never a path the
                # browser names. Kept by the browser for an hour, because a
                # Review page asks for the same few again on every redraw.
                if path == "/api/library/frame":
                    video_id, n = _frame_query(url.query)
                    return self._send(library.frame(video_id, n), 200,
                                      "image/jpeg", cache="private, max-age=3600")
                if path == "/api/library/scan":
                    return self._json(library.scan_state())
                if path == "/api/library/match":
                    return self._json(library.identify_state())
                if path == "/api/library/apply":
                    return self._json(library.apply_state())
                if path == "/api/library/mapping":
                    return self._json(library.mapping())
                if path == "/api/library/log":
                    return self._json(library.log())
                if path.startswith("/api/"):
                    return self._fail("NOT_FOUND", "no such endpoint: " + path)
                return self._static(path)
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                return self._fail(code_for_exception(exc), exc)

        def do_POST(self):
            if not self._guard():
                return
            path = urllib.parse.urlparse(self.path).path
            media_type = (self.headers.get("Content-Type") or "").split(";")[0]
            if media_type.strip().lower() != "application/json":
                return self._fail("UNSUPPORTED_MEDIA",
                                  "send the body as application/json", close=True)
            try:
                body = self._body()
                if path == "/api/library/connect":
                    return self._json(library.connect(body.get("url"),
                                                      body.get("token")))
                if path == "/api/library/stashdb":
                    return self._json(library.set_stashdb_key(body.get("key")))
                if path == "/api/library/categories/refresh":
                    return self._json(library.refresh_categories())
                if path == "/api/library/scan":
                    return self._json(library.start_scan(
                        resync=bool(body.get("resync")),
                        refresh_scenes=bool(body.get("refreshScenes"))))
                # Exact comparisons, so `/api/library/match` and its children stay
                # distinct whatever order they sit in and a mistyped one falls
                # through to the 404 below. Anything reintroduced here as a
                # `startswith` would swallow all five.
                if path == "/api/library/match/identify":
                    return self._json(library.start_identify())
                if path == "/api/library/match/cancel":
                    return self._json(library.cancel_identify())
                if path == "/api/library/match/decide":
                    return self._json(library.decide(_video_id(body),
                                                     body.get("sceneId"),
                                                     _decide_action(body)))
                # The page sends the rows to choose that its view shows, so the
                # button takes only what the user can see. Those ids only
                # narrow: which rows are ambiguous, and which are safe to take,
                # is still decided from the rows the server holds. `model`
                # narrows by group, resolved by the rule the group was drawn by.
                if path == "/api/library/match/accept-recommended":
                    return self._json(library.accept_recommended(
                        model=_model_key(body),
                        video_ids=_video_ids(body, "videoIds")))
                if path == "/api/library/match/search":
                    return self._json(library.assisted_search(
                        _video_id(body), title=body.get("title"),
                        performers=_performer_names(body),
                        page=int(body.get("page") or 1)))
                if path == "/api/library/settings":
                    return self._json(library.update_settings(
                        body.get("settings") or {}))
                if path == "/api/library/mapping":
                    return self._json(library.set_tag_mapping(
                        body.get("tagId"), body.get("categoryName"),
                        body.get("mode") or "map"))
                if path == "/api/library/preview":
                    return self._json(library.preview(
                        _video_ids(body, "includeIds")))
                if path == "/api/library/apply":
                    return self._json(library.start_apply(
                        _video_ids(body, "videoIds") or []))
                return self._fail("NOT_FOUND", "no such endpoint: " + path)
            except Exception as exc:  # noqa: BLE001
                return self._fail(code_for_exception(exc), exc)

        # static ----------------------------------------------------------

        def _static(self, path):
            target = static_target(path)
            if target is None:
                return self._fail("NOT_FOUND", "not found")
            full = os.path.join(WEB_ROOT, target.replace("/", os.sep))
            if not os.path.isfile(full):
                return self._fail("NOT_FOUND", "not found")
            with open(full, "rb") as fh:
                data = fh.read()
            ctype = CONTENT_TYPES[os.path.splitext(target)[1].lower()]
            self._send(data, 200, ctype)

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # being listened on, so the "port in use" error would never fire.
    allow_reuse_address = os.name != "nt"


def make_server(cfg, library):
    return _Server(("127.0.0.1", int(cfg["bind_port"])), make_handler(library))
