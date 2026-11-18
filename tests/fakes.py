"""In-memory doubles for the two upstreams, faithful to their contracts.

`FakePronTransport` implements enough of the Local API — including its sharp
edges — that a test can tell a correct client from a plausible one: case-sensitive
name upserts, per-provider external-id upserts, all-or-nothing batches, and a 422
for a page_size over 500.
"""

import copy
import re
import urllib.parse


class FakePronTransport:
    def __init__(self, videos=None, models=None, studios=None, categories=None,
                 scopes=("library.read", "library.write"), app_name="tagger",
                 valid_tokens=("pron_test", "pron_new", "pron_sekret"),
                 fingerprints=None):
        # Deep copies: seed rows are module-level fixtures shared by every test, and a
        # write reaches nested lists (`_set_external` appends into
        # `video["external_ids"]`), which a shallow copy would leak into later tests.
        self.videos = {v["id"]: copy.deepcopy(v) for v in (videos or ())}
        self.models = copy.deepcopy(list(models or ()))
        self.studios = copy.deepcopy(list(studios or ()))
        self.categories = copy.deepcopy(list(categories or ()))
        self.aliases = []
        self.scopes = list(scopes)
        self.app_name = app_name
        self.valid_tokens = set(valid_tokens)
        self.calls = []
        self.next_id = 1000
        #: A canned ``(status, payload)`` every ``POST /videos/batch`` answers
        #: with, in place of the real validator: "this batch always fails".
        self.batch_error = None
        #: Canned ``(status, payload)`` answers popped one per batch call, so a
        #: test can make a batch fail and its retry fail differently. Checked
        #: before ``batch_error``; when it empties, the real validator answers.
        self.batch_errors = []
        #: The ``fingerprints`` block ``/info`` reports, or ``None`` for a
        #: Pronoisseur that predates it (the key is then absent).
        self.fingerprints = fingerprints
        #: A canned ``(status, payload)`` ``/info`` answers with, in place of
        #: the real one: "this read always fails".
        self.info_error = None
        #: Provider -> stored link-pattern entry, as ``PUT /link-patterns`` keeps it.
        self.link_patterns = {}
        #: ``False`` models a Pronoisseur that predates link patterns: the routes
        #: do not exist, so every one answers ``404 not_found``.
        self.supports_link_patterns = True
        #: A canned ``(status, payload)`` the PUT answers with, or an exception to
        #: raise: "this write always fails".
        self.link_patterns_error = None

    # -- helpers ------------------------------------------------------------

    def _named_list(self, kind):
        return {"models": self.models, "studios": self.studios,
                "categories": self.categories}[kind]

    def _upsert_named(self, kind, body):
        rows = self._named_list(kind)
        name = body.get("name")
        if not name:
            return 422, {"error": {"code": "validation", "message": "name is required"}}
        if kind != "models" and "aliases" in body:
            return 422, {"error": {"code": "validation",
                                   "message": "aliases are only accepted on models"}}
        row = next((r for r in rows if r["name"] == name), None)   # case-SENSITIVE
        created = row is None
        if created:
            self.next_id += 1
            row = {"id": self.next_id, "name": name, "external_ids": [],
                   "video_count": 0}
            rows.append(row)
        for entry in body.get("external_ids") or ():
            self._set_external(row, entry)
        for alias in body.get("aliases") or ():
            self.aliases.append({"kind": "actor", "raw_token": alias,
                                 "canonical_name": name})
        return (201 if created else 200), row

    @staticmethod
    def _set_external(row, entry):
        row.setdefault("external_ids", [])
        for existing in row["external_ids"]:
            if existing["provider"] == entry["provider"]:
                existing["id"] = entry["id"]
                return
        row["external_ids"].append({"provider": entry["provider"], "id": entry["id"]})

    def _validate_item(self, item):
        video = self.videos.get(item.get("id"))
        if video is None:
            return "video %s not found" % item.get("id")
        for key, value in item.items():
            if key == "id":
                continue
            if key not in ("title", "release_date", "release_date_estimated", "studio",
                           "models", "categories", "external_ids", "rating",
                           "favourite", "metadata_source"):
                return "unknown field %s" % key
            if value is None and key not in ("title", "studio"):
                return "%s cannot be null" % key
            if key == "release_date" and not (
                    isinstance(value, str) and len(value) == 10 and value[4] == "-"):
                return "release_date must be YYYY-MM-DD"
        return None

    def _apply_item(self, item):
        video = self.videos[item["id"]]
        curated = False
        for key, value in item.items():
            if key == "id":
                continue
            if key in ("title", "release_date", "release_date_estimated"):
                video[key] = value
                curated = True
            elif key == "studio":
                video["studio"] = None if value is None else {"id": 1, "name": value}
                curated = True
                if value:
                    self._upsert_named("studios", {"name": value})
            elif key in ("models", "categories"):
                names = [n.get("name") for n in video.get(key) or ()]
                if "set" in value:
                    names = list(value["set"])
                else:
                    names = names + [n for n in value.get("add") or () if n not in names]
                    names = [n for n in names if n not in (value.get("remove") or ())]
                video[key] = [{"id": 0, "name": n} for n in names]
                for name in names:
                    self._upsert_named(key, {"name": name})
                curated = curated or key == "models"
            elif key == "external_ids":
                video.setdefault("external_ids", [])
                for entry in value.get("set") or ():
                    self._set_external(video, entry)
                video["external_ids"] = [
                    e for e in video["external_ids"]
                    if e["provider"] not in (value.get("remove") or ())]
            else:
                video[key] = value
        if "metadata_source" in item:
            video["metadata_source"] = item["metadata_source"]
        elif curated:
            video["metadata_source"] = self.app_name

    # -- link patterns ------------------------------------------------------

    LINK_KINDS = ("video", "model", "studio", "category", "picture")
    LINK_PROVIDER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

    @staticmethod
    def _invalid(message):
        return 422, {"error": {"code": "validation", "message": message}}

    def _link_pattern_problem(self, kind, pattern):
        """Why a pattern is refused, or None. Mirrors the documented rules."""
        if not isinstance(pattern, str):
            return "patterns.%s must be text" % kind
        if len(pattern) > 2048:
            return "patterns.%s is longer than 2048 characters" % kind
        if re.search(r"\s", pattern):
            return "patterns.%s must not contain whitespace" % kind
        parts = urllib.parse.urlsplit(pattern.replace("{id}", "ID"))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return "patterns.%s must be an absolute http or https address" % kind
        if pattern.count("{id}") != 1:
            return "patterns.%s must contain {id} exactly once" % kind
        # `{id}` in the host would make the address point somewhere else.
        host_end = pattern.index("//") + 2
        host_end = (pattern.find("/", host_end) if "/" in pattern[host_end:]
                    else len(pattern))
        if pattern.index("{id}") < host_end:
            return "patterns.%s must have {id} after the host" % kind
        return None

    def _put_link_patterns(self, provider, body):
        if self.link_patterns_error is not None:
            if isinstance(self.link_patterns_error, Exception):
                raise self.link_patterns_error
            return self.link_patterns_error
        if not self.LINK_PROVIDER.match(provider):
            return self._invalid("provider must be 1-64 characters of A-Za-z0-9._-")
        if not isinstance(body, dict):
            return self._invalid("a JSON object is required")
        unknown = sorted(set(body) - {"label", "patterns"})
        if unknown:
            return self._invalid("unknown field %s" % unknown[0])
        label = body.get("label", "")
        if not isinstance(label, str) or len(label) > 64:
            return self._invalid("label must be text of at most 64 characters")
        patterns = body.get("patterns")
        if not isinstance(patterns, dict) or not patterns:
            return self._invalid("patterns needs at least one kind")
        for kind, pattern in patterns.items():
            if kind not in self.LINK_KINDS:
                return self._invalid("patterns.%s is not a known kind" % kind)
            problem = self._link_pattern_problem(kind, pattern)
            if problem:
                return self._invalid(problem)
        if provider not in self.link_patterns and len(self.link_patterns) >= 32:
            return self._invalid("at most 32 providers")
        # Replaces the provider's whole entry, as the real route does.
        entry = {"provider": provider, "label": label,
                 "patterns": copy.deepcopy(patterns), "set_by": self.app_name,
                 "updated": "2026-11-13T00:00:00Z"}
        self.link_patterns[provider] = entry
        return 200, copy.deepcopy(entry)

    # -- transport ----------------------------------------------------------

    def __call__(self, method, path, body, headers):
        self.calls.append({"method": method, "path": path, "body": body})
        if "Origin" in headers:
            return 403, {"error": {"code": "forbidden_origin", "message": "no browsers"}}
        parsed = urllib.parse.urlparse(path)
        route, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if route == "/api/v1/health":
            return 200, {"ok": True}
        if headers.get("Authorization") not in (
                "Bearer " + token for token in self.valid_tokens):
            return 401, {"error": {"code": "unauthorized", "message": "bad token"}}
        if method in ("POST", "PATCH", "PUT") and "library.write" not in self.scopes:
            return 403, {"error": {"code": "insufficient_scope", "message": "read only"}}

        if route == "/api/v1/info":
            if self.info_error:
                return self.info_error
            info = {"app_version": "1.0.49", "api_version": 1,
                    "app": {"name": self.app_name, "scopes": self.scopes}}
            if isinstance(self.fingerprints, dict):
                info["fingerprints"] = copy.deepcopy(self.fingerprints)
            return 200, info
        if route == "/api/v1/sources":
            return 200, [{"id": 1, "root_path": "D:\\library", "kind": "local",
                          "is_online": True}]
        if route == "/api/v1/videos" and method == "GET":
            return self._list_videos(query)
        if route.startswith("/api/v1/videos/") and route.endswith("/batch") is False \
                and method == "PATCH":
            video_id = int(route.rsplit("/", 1)[-1])
            item = dict(body or {}, id=video_id)
            error = self._validate_item(item)
            if error:
                return 422, {"error": {"code": "validation", "message": error}}
            self._apply_item(item)
            return 200, self.videos[video_id]
        if route == "/api/v1/videos/batch" and method == "POST":
            if self.batch_errors:
                return self.batch_errors.pop(0)
            if self.batch_error:
                return self.batch_error
            items = (body or {}).get("items") or []
            if not 1 <= len(items) <= 500:
                return 422, {"error": {"code": "validation",
                                       "message": "items must hold 1-500 entries"}}
            details = []
            for index, item in enumerate(items):
                error = self._validate_item(item)
                if error:
                    details.append({"index": index, "id": item.get("id"),
                                    "error": error})
            if details:
                return 422, {"error": {"code": "validation",
                                       "message": "one or more items are invalid; "
                                                  "nothing was written",
                                       "details": details}}
            for item in items:
                self._apply_item(item)
            return 200, {"results": [{"id": i["id"], "status": "ok"} for i in items]}
        for kind in ("models", "studios", "categories"):
            if route == "/api/v1/" + kind:
                if method == "GET":
                    return 200, self._named_list(kind)
                return self._upsert_named(kind, body or {})
        if route.startswith("/api/v1/link-patterns"):
            if not self.supports_link_patterns:
                return 404, {"error": {"code": "not_found", "message": path}}
            provider = urllib.parse.unquote(route[len("/api/v1/link-patterns"):]
                                            .lstrip("/"))
            if not provider and method == "GET":
                return 200, copy.deepcopy(list(self.link_patterns.values()))
            if provider and method == "PUT":
                return self._put_link_patterns(provider, body)
            if provider and method == "DELETE":
                if self.link_patterns.pop(provider, None) is None:
                    return 404, {"error": {"code": "not_found", "message": provider}}
                return 204, None
        if route == "/api/v1/aliases":
            if method == "GET":
                return 200, self.aliases
            if not isinstance(body, list) or not body:
                return 422, {"error": {"code": "validation",
                                       "message": "a non-empty array is required"}}
            self.aliases.extend(body)
            return 200, {"count": len(body)}
        return 404, {"error": {"code": "not_found", "message": path}}

    def _list_videos(self, query):
        page = int((query.get("page") or ["1"])[0])
        page_size = int((query.get("page_size") or ["100"])[0])
        if page_size > 500:
            return 422, {"error": {"code": "validation",
                                   "message": "page_size must be 1-500"}}
        rows = sorted(self.videos.values(), key=lambda v: v["id"])
        missing = (query.get("missing_external_id") or [None])[0]
        if missing:
            rows = [r for r in rows
                    if not any(e["provider"] == missing
                               for e in r.get("external_ids") or ())]
        start = (page - 1) * page_size
        return 200, {"items": rows[start:start + page_size], "page": page,
                     "page_size": page_size, "total": len(rows)}


class FakeStashDBTransport:
    """Every document the tool sends: a scene, fingerprints, performers, scenes.

    The fingerprint half answers **one group per input entry, in input order**,
    which is the contract `SceneFetcher.by_fingerprints` zips its callers' rows
    against; a fake that returned only the non-empty groups would hide exactly the
    mis-alignment that method exists to refuse. `by_hash` maps a fingerprint hash
    to the scenes that carry it, and a scene reached through two of one video's
    hashes is listed once.

    `performers` maps a name to what `queryPerformers` answers with, so a name
    that is not a key resolves to nothing — which is how the assisted search's
    `unresolved` half is driven. `search` is what `queryScenes` answers with, for
    every filter: those tests assert on the input the fake *received*, and a fake
    that re-implemented stash-box's filtering could only ever prove itself right.
    """

    def __init__(self, scenes, by_hash=None, performers=None, search=None,
                 categories=None):
        self.scenes = dict(scenes)
        self.by_hash = dict(by_hash or {})
        self.performers = dict(performers or {})
        self.search = list(search or ())
        self.categories = list(categories or ())
        self.calls = []
        # Called with the 1-based number of each fingerprint query, before it is
        # answered: how a test drives a cancel or a mid-run action from the
        # StashDB side, deterministically.
        self.on_fingerprint_call = None
        self.fingerprint_calls = 0

    def __call__(self, payload):
        self.calls.append(payload)
        variables = payload.get("variables") or {}
        query = payload.get("query") or ""
        if "fps" in variables:
            self.fingerprint_calls += 1
            if self.on_fingerprint_call:
                self.on_fingerprint_call(self.fingerprint_calls)
            return {"data": {"findScenesBySceneFingerprints":
                             [self._group(entry) for entry in variables["fps"]]}}
        if "query TagCategories" in query:
            return {"data": {"queryTagCategories":
                             {"tag_categories": self.categories or []}}}
        # Matched on the operation signature rather than the bare name, so that
        # SceneFingerprints can never be read as Scenes.
        if "query Performers(" in query:
            names = (variables.get("input") or {}).get("names")
            return self._page("queryPerformers", "performers",
                              self.performers.get(names), variables)
        if "query Scenes(" in query:
            return self._page("queryScenes", "scenes", self.search, variables)
        return {"data": {"findScene": self.scenes.get(variables["id"])}}

    @staticmethod
    def _page(root, list_key, rows, variables):
        """One page of ``rows``, counted and sliced the way stash-box does."""
        rows = list(rows or ())
        inner = variables.get("input") or {}
        per_page = int(inner.get("per_page") or 100)
        start = (int(inner.get("page") or 1) - 1) * per_page
        return {"data": {root: {"count": len(rows),
                                list_key: rows[start:start + per_page]}}}

    def _group(self, entry):
        found, seen = [], set()
        for fingerprint in entry:
            for scene in self.by_hash.get(fingerprint["hash"], ()):
                if scene["id"] not in seen:
                    seen.add(scene["id"])
                    found.append(scene)
        return found
