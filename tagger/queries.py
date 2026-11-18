"""GraphQL documents and input builders for StashDB.

A schema mismatch does not raise locally: StashDB rejects the whole document,
which surfaces as a GraphQL error that StashDBError carries through.
"""

# ------------------------------------------------------------------ documents

PERFORMER_FIELDS = """
  id
  name
"""

SCENE_FIELDS = """
  id
  title
  release_date
  production_date
  code
  duration
  details
  studio { id name }
  tags { id name }
  performers { as performer { id name gender } }
  images { id url width height }
  urls { url }
"""

# Wider than SCENE_FIELDS: the parent studio (the network is the useful unit),
# performer aliases and credited-as name, the tag's category (the allowlist
# gates on it), and the fingerprints, since an OSHASH match is what proves a
# folder UUID is the right scene at all.
SCENE_DETAIL_FIELDS = """
  id
  title
  release_date
  production_date
  code
  duration
  details
  studio { id name parent { id name } }
  performers { as performer { id name disambiguation aliases gender } }
  tags { id name category { id name group } }
  images { id url width height }
  fingerprints { hash algorithm duration }
  urls { url }
"""

FIND_SCENE = """
query FindScene($id: ID!) {
  findScene(id: $id) { %s }
}
""" % SCENE_DETAIL_FIELDS

# Many videos, one request. The server returns one group per input entry,
# aligned by index, so the caller can zip them positionally. Only the
# multi-fingerprint queries apply the PHASH distance tolerance;
# findSceneByFingerprint and findScenesByFingerprints are exact-match only.
FIND_SCENES_BY_SCENE_FINGERPRINTS = """
query SceneFingerprints($fps: [[FingerprintQueryInput!]!]!) {
  findScenesBySceneFingerprints(fingerprints: $fps) { %s }
}
""" % SCENE_DETAIL_FIELDS

QUERY_PERFORMERS = """
query Performers($input: PerformerQueryInput!) {
  queryPerformers(input: $input) {
    count
    performers { %s }
  }
}
""" % PERFORMER_FIELDS

QUERY_SCENES = """
query Scenes($input: SceneQueryInput!) {
  queryScenes(input: $input) {
    count
    scenes { %s }
  }
}
""" % SCENE_FIELDS

QUERY_TAG_CATEGORIES = """
query TagCategories {
  queryTagCategories {
    count
    tag_categories { id name group description }
  }
}
"""


# --------------------------------------------------------------------- inputs

def build_performer_input(name):
    """A PerformerQueryInput that searches performers by name."""
    return {"names": name, "sort": "NAME", "direction": "ASC"}


def build_scene_input(filt, performer_ids=None):
    """Map a search filter plus performer ids to a SceneQueryInput.

    Unset values are omitted rather than sent as null: stash-box treats an
    explicit null as a filter on null, which returns nothing.
    """
    filt = filt or {}
    out = {}
    if performer_ids:
        out["performers"] = {"value": list(performer_ids), "modifier": "INCLUDES"}
    if filt.get("studios"):
        out["studios"] = {"value": list(filt["studios"]), "modifier": "INCLUDES"}
    title = filt.get("title")
    if isinstance(title, str) and title.strip():
        out["title"] = title
    out["sort"] = "DATE"
    out["direction"] = "DESC"
    return out
