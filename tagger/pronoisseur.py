"""Pronoisseur Local API (v1) client.

The contract is the Local API that Pronoisseur serves on this computer.
Five details are load-bearing and are enforced here rather than left to the caller:

* **Never send an ``Origin`` header.** Any request carrying one is
  ``403 forbidden_origin`` on every route, health included — the app refuses to be
  called from a browser, and urllib does not add one, so the rule is simply "never".
* ``page_size`` above **500 is rejected with 422, not clamped**. Asking for 1000
  fails the whole page rather than returning 500 rows, so the ceiling is checked
  before the call.
* ``503 busy`` means the catalog is locked by another operation and carries
  ``Retry-After``. It is the one status worth retrying; everything else is final.
* ``POST /videos/batch`` is **all-or-nothing**, 1–500 items: one invalid item and
  nothing in the batch is written, with ``error.details`` naming the index.
* ``aliases`` is accepted **only** on ``POST /models``; sending it to ``/studios``
  or ``/categories`` is ``422``.

Auth is ``Authorization: Bearer pron_…`` on every route except ``GET /health``.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

API_ROOT = "/api/v1"
MAX_PAGE_SIZE = 500
MAX_BATCH_ITEMS = 500
BUSY_RETRY_SECONDS = 2.0
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


def is_loopback_url(url):
    """True for an http(s) URL with no credentials whose host is this machine.

    The token goes to whatever URL it is given, so anything else is refused.
    """
    try:
        parts = urlsplit(url or "")
        host = parts.hostname
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and parts.username is None
            and host in LOOPBACK_HOSTS)


class PronoisseurError(Exception):
    """Any Local API failure: transport, HTTP status, or the error envelope.

    Raised rather than degraded to an empty result so "nothing matched" and "the
    request failed" can never be confused by a caller.
    """

    def __init__(self, message, code=None, status=None, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


class Pronoisseur:
    def __init__(self, base_url, token, transport=None, timeout=60, retries=3,
                 sleep=time.sleep):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.retries = max(int(retries), 1)
        self._sleep = sleep
        self._transport = transport or self._http

    # ------------------------------------------------------------------ transport

    def _http(self, method, path, body, headers):
        """Return (status, payload). HTTP errors come back, they do not raise."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw) if raw.strip() else None
            except ValueError:
                return exc.code, raw[:500]
        except ValueError:
            # http.client quotes the offending header value, which is the token.
            raise PronoisseurError(
                "Pronoisseur request failed (%s %s): a request header could not "
                "be sent, or the response was unreadable" % (method, path)) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PronoisseurError(
                "Pronoisseur request failed (%s %s): %s — is the Local API enabled "
                "in Settings > Connected apps?" % (method, path, exc)) from exc

    def _headers(self, with_body, authed=True):
        headers = {"Accept": "application/json"}
        if authed:
            headers["Authorization"] = "Bearer " + self.token
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _call(self, method, path, body=None, authed=True):
        attempts = 0
        while True:
            attempts += 1
            status, payload = self._transport(
                method, API_ROOT + path, body, self._headers(body is not None, authed))
            if status == 503 and attempts < self.retries:
                self._sleep(BUSY_RETRY_SECONDS)
                continue
            if status >= 400:
                raise self._error(status, payload, method, path)
            return payload

    @staticmethod
    def _error(status, payload, method, path):
        envelope = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(envelope, dict):
            return PronoisseurError(
                "Pronoisseur %s %s: %s" % (status, envelope.get("code") or "error",
                                           envelope.get("message") or ""),
                code=envelope.get("code"), status=status,
                details=envelope.get("details"))
        return PronoisseurError(
            "Pronoisseur HTTP %s on %s %s" % (status, method, path), status=status)

    # ----------------------------------------------------------------- discovery

    def health(self):
        return self._call("GET", "/health", authed=False)

    def info(self):
        return self._call("GET", "/info")

    def scopes(self):
        return list((self.info().get("app") or {}).get("scopes") or ())

    def can_write(self):
        return "library.write" in self.scopes()

    def sources(self):
        return self._call("GET", "/sources") or []

    # -------------------------------------------------------------------- videos

    def videos(self, page=1, page_size=MAX_PAGE_SIZE, **filters):
        if int(page_size) > MAX_PAGE_SIZE:
            raise ValueError("page_size above %d is rejected by the API (422), "
                             "not clamped" % MAX_PAGE_SIZE)
        params = {k: v for k, v in filters.items() if v not in (None, "")}
        params["page"] = int(page)
        params["page_size"] = int(page_size)
        query = urllib.parse.urlencode(sorted(params.items()))
        return self._call("GET", "/videos?" + query)

    def all_videos(self, page_size=MAX_PAGE_SIZE, max_rows=None, progress=None,
                   **filters):
        """Page to completion, reconciled against the server's own ``total``."""
        rows, page, total = [], 1, None
        while True:
            payload = self.videos(page=page, page_size=page_size, **filters)
            batch = payload.get("items") or []
            total = int(payload.get("total", 0)) if total is None else total
            rows.extend(batch)
            if progress:
                progress(len(rows), total)
            if total == 0:
                return []
            if max_rows is not None and len(rows) >= max_rows:
                return rows[:max_rows]
            if len(rows) >= total:
                return rows[:total]
            if len(batch) < page_size:
                raise PronoisseurError(
                    "video listing truncated: page %d returned %d of %d rows but only "
                    "%d of %d total have been read" % (page, len(batch), page_size,
                                                       len(rows), total))
            page += 1

    def video(self, video_id):
        return self._call("GET", "/videos/%d" % int(video_id))

    def patch_video(self, video_id, body):
        return self._call("PATCH", "/videos/%d" % int(video_id), body or {})

    def batch(self, items):
        items = list(items or ())
        if not items:
            raise ValueError("a batch needs at least one item")
        if len(items) > MAX_BATCH_ITEMS:
            raise ValueError("a batch takes at most %d items" % MAX_BATCH_ITEMS)
        return self._call("POST", "/videos/batch", {"items": items})

    # ------------------------------------------------------------------ entities

    def models(self):
        return self._call("GET", "/models") or []

    def studios(self):
        return self._call("GET", "/studios") or []

    def categories(self):
        return self._call("GET", "/categories") or []

    def upsert_model(self, name, external_ids=None, aliases=None):
        body = {"name": name}
        if external_ids:
            body["external_ids"] = list(external_ids)
        if aliases:
            body["aliases"] = list(aliases)
        return self._call("POST", "/models", body)

    def upsert_studio(self, name, external_ids=None):
        return self._call("POST", "/studios", self._named(name, external_ids))

    def upsert_category(self, name, external_ids=None):
        return self._call("POST", "/categories", self._named(name, external_ids))

    @staticmethod
    def _named(name, external_ids):
        body = {"name": name}
        if external_ids:
            body["external_ids"] = list(external_ids)
        return body

    def aliases(self):
        """Every alias the app holds: ``[{kind, raw_token, canonical_name}]``.

        ``library.read``, like the other three index reads. ``kind`` is ``actor``
        for a **model** alias — the one place the wire vocabulary and the entity
        vocabulary (``/models``) diverge — and ``studio`` / ``category`` for the
        other two.
        """
        return self._call("GET", "/aliases") or []

    def put_link_patterns(self, provider, label, patterns):
        """Register how this app's ids for ``provider`` become web addresses.

        Replaces the provider's whole entry (the last app to write it owns it),
        so a caller always sends every kind it wants kept. ``library.write``.
        Pronoisseur 1.0.62 and later; an older one answers ``404 not_found``.
        The provider is quoted because it lands in the path.
        """
        return self._call(
            "PUT", "/link-patterns/" + urllib.parse.quote(str(provider), safe=""),
            {"label": label, "patterns": dict(patterns)})

    def put_aliases(self, entries):
        entries = list(entries or ())
        if not entries:
            raise ValueError("the alias array must be non-empty")
        return self._call("PUT", "/aliases", entries)
