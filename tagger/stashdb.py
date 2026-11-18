"""StashDB (stash-box) GraphQL transport.

Two things here are load-bearing, and both match stashapp/stash-box's behaviour:

1. The auth header is ``ApiKey`` (internal/auth/authorization.go). It is *not*
   ``X-Api-Key`` — that is Whisparr's.
2. ``per_page`` is clamped to 100 server-side, silently
   (internal/service/query/helpers.go, ``MaxPerPage``). Asking for 500 returns
   100 rows with no error and no truncation flag.

Point 2 is why :meth:`StashDB.paginate` reconciles rows against the server's own
``count`` on every page instead of trusting the page size it asked for. A loop
that trusts its request would quietly read 4% of a result set and look healthy.
"""

import json
import threading
import time
import urllib.error
import urllib.request

MAX_PER_PAGE = 100

#: Identify the tool honestly. A stash-box instance behind Cloudflare may reject
#: urllib's default User-Agent with a 403 that reads like an auth failure.
USER_AGENT = "stashdb-pronoisseur-tagger/0.1"


class StashDBError(Exception):
    """Any StashDB failure: transport, HTTP, GraphQL errors, or truncation.

    Raised rather than degraded to an empty result, so that "no matches" and
    "the request failed" can never be confused by a caller.
    """


class StashDB:
    #: ``config.MIN_REQUEST_INTERVAL``: StashDB allows 60 requests a minute.
    def __init__(self, endpoint, api_key, interval=1.0, transport=None, timeout=30):
        self.endpoint = endpoint
        self.api_key = api_key
        self.interval = interval
        self.timeout = timeout
        self._transport = transport or self._http
        self._last_call = 0.0
        # One client serves the identify run and the page's own clicks (a pick, an
        # assisted search) at the same time, so the gap is kept under a lock:
        # without it two callers wait out the same gap and then fire together.
        self._pace_lock = threading.Lock()

    # ------------------------------------------------------------------ transport

    def _http(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "ApiKey": self.api_key,
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise StashDBError(
                "StashDB HTTP %s: %s" % (exc.code, detail or exc.reason)) from exc
        except ValueError as exc:
            # http.client quotes the offending header value, which is the key.
            # Say nothing of what it said.
            raise StashDBError(
                "StashDB request failed: a request header could not be sent, "
                "or the response was unreadable") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise StashDBError("StashDB request failed: %s" % exc) from exc

    def _pace(self):
        if self.interval <= 0:
            return
        with self._pace_lock:
            wait = self.interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    # -------------------------------------------------------------------- queries

    def execute(self, query, variables):
        """Run one GraphQL document and return its ``data`` payload."""
        self._pace()
        result = self._transport({"query": query, "variables": variables})
        if not isinstance(result, dict):
            raise StashDBError("StashDB returned a non-object response")
        if result.get("errors"):
            messages = "; ".join(
                str(e.get("message", e)) for e in result["errors"])
            raise StashDBError("StashDB GraphQL error: %s" % messages)
        if "data" not in result or result["data"] is None:
            raise StashDBError("StashDB response carried no data")
        return result["data"]

    def count(self, query, variables, root):
        """Return the total row count for a filter, fetching a single row."""
        payload = _with_page(variables, page=1, per_page=1)
        data = self.execute(query, payload)
        return int(_root(data, root).get("count", 0))

    def page(self, query, variables, root, list_key, page=1,
             per_page=MAX_PER_PAGE):
        """Fetch a single page and its total count.

        This is what interactive browsing wants: one upstream request per page
        the user actually looks at. `paginate` exists for the compound scene
        plan, which genuinely needs the whole set before it can do anything.
        """
        per_page = min(int(per_page), MAX_PER_PAGE)
        data = self.execute(query, _with_page(variables, int(page), per_page))
        node = _root(data, root)
        return {
            "rows": node.get(list_key) or [],
            "count": int(node.get("count", 0)),
            "page": int(page),
            "perPage": per_page,
        }

    def paginate(self, query, variables, root, list_key, per_page=MAX_PER_PAGE,
                 max_rows=None):
        """Page a query to completion, verifying against the server's count.

        A page shorter than requested is acceptable only once the accumulated
        rows have reached ``count``. Anything else is truncation and raises.
        """
        per_page = min(int(per_page), MAX_PER_PAGE)
        collected = []
        page = 1
        total = None

        while True:
            data = self.execute(query, _with_page(variables, page, per_page))
            node = _root(data, root)
            total = int(node.get("count", 0)) if total is None else total
            batch = node.get(list_key) or []
            collected.extend(batch)

            if total == 0:
                return []
            if max_rows is not None and len(collected) >= max_rows:
                return collected[:max_rows]
            if len(collected) >= total:
                return collected[:total]
            if len(batch) < per_page:
                raise StashDBError(
                    "StashDB result truncated: page %d returned %d of %d requested "
                    "rows but only %d of %d total have been read. Re-run; if it "
                    "persists the query shape has changed."
                    % (page, len(batch), per_page, len(collected), total))
            page += 1


def _with_page(variables, page, per_page):
    """Return variables with pagination applied to the ``input`` object."""
    merged = dict(variables)
    inner = dict(merged.get("input") or {})
    inner["page"] = page
    inner["per_page"] = per_page
    merged["input"] = inner
    return merged


def _root(data, root):
    node = data.get(root)
    if not isinstance(node, dict):
        raise StashDBError("StashDB response missing %r" % root)
    return node
