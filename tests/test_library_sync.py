import unittest

from tagger import library_sync as sync
from tagger.library_store import DEFAULT_SETTINGS


class TestExtractSceneUuid(unittest.TestCase):
    def test_reads_the_uuid_from_a_whisparr_folder_name(self):
        rel = ("Example Network\\2026-07-20 - Example Show Part 16 "
               "[01900000-aaaa-7bbb-8ccc-000000000001]\\ExampleNetwork.26.07.20.mp4")
        self.assertEqual(sync.extract_scene_uuid(rel),
                         "01900000-aaaa-7bbb-8ccc-000000000001")

    def test_forward_slashes_work_too(self):
        rel = "a/b [3a7bd4c1-5e92-4f08-a6d3-1c8e9b2f7a40]/c.mp4"
        self.assertEqual(sync.extract_scene_uuid(rel),
                         "3a7bd4c1-5e92-4f08-a6d3-1c8e9b2f7a40")

    def test_uppercase_is_accepted_and_normalised_to_lower(self):
        rel = "x [3A7BD4C1-5E92-4F08-A6D3-1C8E9B2F7A40]\\y.mp4"
        self.assertEqual(sync.extract_scene_uuid(rel),
                         "3a7bd4c1-5e92-4f08-a6d3-1c8e9b2f7a40")

    def test_the_last_segment_wins_when_several_carry_one(self):
        rel = ("[3a7bd4c1-5e92-4f08-a6d3-1c8e9b2f7a40]\\"
               "[01900000-aaaa-7bbb-8ccc-000000000002]\\c.mp4")
        self.assertEqual(sync.extract_scene_uuid(rel),
                         "01900000-aaaa-7bbb-8ccc-000000000002")

    def test_none_when_there_is_no_uuid(self):
        self.assertIsNone(sync.extract_scene_uuid("Jane Example\\clip_s01_1080.mp4"))

    def test_brackets_are_required_so_a_bare_uuid_is_ignored(self):
        self.assertIsNone(sync.extract_scene_uuid(
            "x\\3a7bd4c1-5e92-4f08-a6d3-1c8e9b2f7a40.mp4"))

    def test_a_short_or_malformed_uuid_is_ignored(self):
        self.assertIsNone(sync.extract_scene_uuid("x [3a7bd4c1-5e92-4f08]\\y.mp4"))

    def test_empty_input_is_none_not_an_exception(self):
        self.assertIsNone(sync.extract_scene_uuid(""))
        self.assertIsNone(sync.extract_scene_uuid(None))


class TestNameKey(unittest.TestCase):
    """Mirrors Pronoisseur's name matching exactly: letters and digits only,
    lower-cased. The app applies it to the STORED raw_token when it builds its
    alias map, which is why aliases can be written verbatim."""

    def test_punctuation_and_case_are_dropped(self):
        self.assertEqual(sync.name_key("J. Example"), "jexample")
        self.assertEqual(sync.name_key("Sam The Sample"),
                         "samthesample")
        self.assertEqual(sync.name_key("Example Studio"), "examplestudio")

    def test_digits_survive(self):
        self.assertEqual(sync.name_key("Example Clip #07"), "exampleclip07")

    def test_empty_and_none(self):
        self.assertEqual(sync.name_key(""), "")
        self.assertEqual(sync.name_key(None), "")

    def test_only_decimal_digits_count_as_digits(self):
        # The app's key takes Nd only. Python's str.isalnum also takes
        # Nl and No, so "Vol \u2162" or "\u00bd" would key differently on the two
        # sides and silently split one name into two entities.
        self.assertEqual(sync.name_key("Vol \u2162"), "vol")        # Nl, roman numeral
        self.assertEqual(sync.name_key("\u00bd Hour"), "hour")      # No, vulgar fraction
        self.assertEqual(sync.name_key("Scene 4\u00b2"), "scene4")  # No, superscript
        # No unicode normalisation: a diacritic is a letter and survives as itself,
        # exactly as the app's lower-casing leaves it.
        self.assertEqual(sync.name_key("Zo\u00eb 12"), "zo\u00eb12")


class TestIndexByName(unittest.TestCase):
    def test_indexes_named_entities_case_insensitively(self):
        index = sync.index_by_name([
            {"id": 5, "name": "Jamie Example", "external_ids": [], "video_count": 3},
            {"id": 6, "name": "example network", "external_ids": [], "video_count": 1},
        ])
        self.assertEqual(index["jamieexample"]["id"], 5)
        self.assertEqual(index["examplenetwork"]["name"], "example network")

    def test_first_spelling_wins_when_two_names_share_a_key(self):
        # "Acme" and "acme" are two rows on the app side (its collation is
        # byte-for-byte); the companion must pick one and stay with it.
        index = sync.index_by_name([{"id": 1, "name": "Acme"},
                                    {"id": 2, "name": "acme"}])
        self.assertEqual(index["acme"]["id"], 1)


class TestIndexAliases(unittest.TestCase):
    """`GET /aliases` folded the way the app itself folds it."""

    ROWS = [{"kind": "actor", "raw_token": "Jane E",
             "canonical_name": "Jane Example"},
            {"kind": "studio", "raw_token": "EA", "canonical_name": "Example Network"}]

    def test_keys_on_kind_and_the_apps_own_name_key(self):
        index = sync.index_aliases(self.ROWS)
        self.assertEqual(index[("actor", "janee")], "Jane Example")
        self.assertEqual(index[("studio", "ea")], "Example Network")

    def test_one_token_under_two_kinds_is_two_aliases(self):
        # `actor` and `studio` are different namespaces on the app side, and an
        # index that dropped the kind would skip a studio alias because a
        # performer happens to share the token.
        index = sync.index_aliases(
            [{"kind": "actor", "raw_token": "EA", "canonical_name": "Ella A"}]
            + self.ROWS)
        self.assertEqual(index[("actor", "ea")], "Ella A")
        self.assertEqual(index[("studio", "ea")], "Example Network")

    def test_a_row_with_no_kind_or_no_token_is_not_indexed(self):
        self.assertEqual(sync.index_aliases([
            {"kind": "", "raw_token": "x", "canonical_name": "X"},
            {"kind": "actor", "raw_token": "  ", "canonical_name": "X"},
            {"kind": "actor", "raw_token": None, "canonical_name": "X"}]), {})

    def test_the_first_row_for_a_key_wins(self):
        index = sync.index_aliases(
            [{"kind": "actor", "raw_token": "Jane E", "canonical_name": "First"},
             {"kind": "actor", "raw_token": "jane e", "canonical_name": "Second"}])
        self.assertEqual(index[("actor", "janee")], "First")

    def test_nothing_at_all_is_an_empty_index(self):
        self.assertEqual(sync.index_aliases(None), {})


def scene_with(fingerprints=(), duration=1200):
    return {"id": "scene-1", "duration": duration,
            "fingerprints": [dict(f) for f in fingerprints]}


OSHASH_A = "5e1f93a07b2c4d68"
OSHASH_B = "c4a90d3e6b17f852"


class TestClassifyUuidMatch(unittest.TestCase):
    """A folder can carry the wrong scene's UUID. Nothing is written on an unproven
    identity."""

    def test_an_oshash_hit_is_verified(self):
        scene = scene_with([{"hash": OSHASH_A, "algorithm": "OSHASH", "duration": 1900}])
        out = sync.classify_uuid_match(OSHASH_A, 1900.8, scene)
        self.assertEqual(out["status"], sync.VERIFIED)

    def test_oshash_comparison_ignores_case(self):
        scene = scene_with([{"hash": OSHASH_A.upper(), "algorithm": "oshash"}])
        self.assertEqual(sync.classify_uuid_match(OSHASH_A, 10.0, scene)["status"],
                         sync.VERIFIED)

    def test_a_phash_entry_that_happens_to_equal_the_oshash_does_not_count(self):
        scene = scene_with([{"hash": OSHASH_A, "algorithm": "PHASH"}], duration=99)
        self.assertNotEqual(sync.classify_uuid_match(OSHASH_A, 1900.8, scene)["status"],
                            sync.VERIFIED)

    def test_no_oshash_hit_but_matching_duration_is_verified_by_duration(self):
        scene = scene_with([{"hash": OSHASH_B, "algorithm": "OSHASH"}], duration=1200)
        out = sync.classify_uuid_match(OSHASH_A, 1201.5, scene)
        self.assertEqual(out["status"], sync.VERIFIED_BY_DURATION)

    def test_duration_tolerance_is_inclusive_and_configurable(self):
        scene = scene_with(duration=1200)
        self.assertEqual(sync.classify_uuid_match(None, 1203.0, scene)["status"],
                         sync.VERIFIED_BY_DURATION)
        self.assertEqual(sync.classify_uuid_match(None, 1203.5, scene)["status"],
                         sync.UUID_MISMATCH)
        self.assertEqual(
            sync.classify_uuid_match(None, 1203.5, scene, tolerance=5.0)["status"],
            sync.VERIFIED_BY_DURATION)

    def test_a_null_stashdb_duration_is_unknown_never_a_mismatch(self):
        # A scene can have no duration on StashDB.
        scene = scene_with([{"hash": OSHASH_B, "algorithm": "OSHASH"}], duration=None)
        out = sync.classify_uuid_match(OSHASH_A, 1900.8, scene)
        self.assertEqual(out["status"], sync.UNVERIFIED)
        self.assertIn("no duration", out["note"].lower())

    def test_a_missing_local_duration_is_also_unknown(self):
        scene = scene_with(duration=1200)
        self.assertEqual(sync.classify_uuid_match(None, None, scene)["status"],
                         sync.UNVERIFIED)

    def test_a_real_duration_disagreement_is_a_mismatch(self):
        # A folder can claim a 2500 s scene while the file runs 1901 s.
        scene = scene_with([{"hash": OSHASH_B, "algorithm": "OSHASH"}], duration=2500)
        out = sync.classify_uuid_match(OSHASH_A, 1900.9, scene)
        self.assertEqual(out["status"], sync.UUID_MISMATCH)
        self.assertIn("2500", out["note"])
        self.assertIn("1900", out["note"])

    def test_an_unreadable_file_falls_back_to_duration(self):
        scene = scene_with([{"hash": OSHASH_B, "algorithm": "OSHASH"}], duration=1200)
        self.assertEqual(sync.classify_uuid_match(None, 1200.4, scene)["status"],
                         sync.VERIFIED_BY_DURATION)

    def test_a_scene_with_no_fingerprints_at_all_still_classifies(self):
        scene = {"id": "s", "duration": 1200}
        self.assertEqual(sync.classify_uuid_match(OSHASH_A, 1200.0, scene)["status"],
                         sync.VERIFIED_BY_DURATION)

    def test_only_verified_statuses_are_writable_without_a_tick(self):
        self.assertEqual(set(sync.WRITABLE_STATUSES),
                         {sync.VERIFIED, sync.VERIFIED_BY_DURATION})
        self.assertNotIn(sync.UUID_MISMATCH, sync.WRITABLE_STATUSES)
        self.assertNotIn(sync.UNVERIFIED, sync.WRITABLE_STATUSES)


def full_scene():
    return {
        "id": "scene-1", "title": "Example Scene One", "release_date": "2020-05-23",
        "duration": 1800,
        "studio": {"id": "s-1", "name": "Example Studio",
                   "parent": {"id": "s-0", "name": "Example Network"}},
        "performers": [
            {"as": None, "performer": {"id": "p-1", "name": "Jane Example",
                                       "aliases": ["Jane E"], "gender": "FEMALE"}},
            {"as": "Sam The Sample",
             "performer": {"id": "p-2", "name": "Sam Sample", "aliases": [],
                           "gender": "MALE"}},
        ],
        "tags": [
            {"id": "t-1", "name": "Outdoor",
             "category": {"id": "c-setting", "name": "Setting", "group": "SCENE"}},
            {"id": "t-2", "name": "Blonde",
             "category": {"id": "c-look", "name": "Look", "group": "PEOPLE"}},
            {"id": "t-3", "name": "Uncategorised", "category": None},
        ],
        "images": [{"id": "i-1", "url": "https://cdn/x.jpg"}],
        "fingerprints": [],
    }


class TestStudioResolution(unittest.TestCase):
    def test_parent_studio_wins_by_default(self):
        self.assertEqual(sync.scene_studio(full_scene())["name"], "Example Network")

    def test_parent_carries_the_parent_external_id(self):
        self.assertEqual(sync.scene_studio(full_scene())["id"], "s-0")

    def test_sub_studio_when_the_toggle_is_off(self):
        studio = sync.scene_studio(full_scene(), use_parent=False)
        self.assertEqual((studio["name"], studio["id"]), ("Example Studio", "s-1"))

    def test_falls_back_to_the_studio_itself_when_there_is_no_parent(self):
        scene = full_scene()
        scene["studio"]["parent"] = None
        self.assertEqual(sync.scene_studio(scene)["name"], "Example Studio")

    def test_no_studio_at_all_is_none(self):
        scene = full_scene()
        scene["studio"] = None
        self.assertIsNone(sync.scene_studio(scene))


class TestPerformerResolution(unittest.TestCase):
    def test_uses_the_canonical_name_not_the_credited_as_name(self):
        names = [p["name"] for p in sync.scene_performers(full_scene())]
        self.assertEqual(names, ["Jane Example", "Sam Sample"])

    def test_credited_as_becomes_an_alias_when_it_differs(self):
        sam = sync.scene_performers(full_scene())[1]
        self.assertIn("Sam The Sample", sam["aliases"])

    def test_credited_as_is_not_an_alias_when_the_toggle_is_off(self):
        sam = sync.scene_performers(full_scene(), credited_as_aliases=False)[1]
        self.assertNotIn("Sam The Sample", sam["aliases"])

    def test_stashdb_aliases_are_carried(self):
        self.assertEqual(sync.scene_performers(full_scene())[0]["aliases"], ["Jane E"])

    def test_an_alias_equal_to_the_canonical_name_is_dropped(self):
        scene = full_scene()
        scene["performers"][0]["performer"]["aliases"] = ["jane  example!", "Jane E"]
        self.assertEqual(sync.scene_performers(scene)[0]["aliases"], ["Jane E"])

    def test_a_performer_row_with_no_performer_object_is_skipped(self):
        scene = full_scene()
        scene["performers"].append({"as": "Ghost", "performer": None})
        self.assertEqual(len(sync.scene_performers(scene)), 2)


class TestTagResolution(unittest.TestCase):
    def test_nothing_imports_when_the_allowlist_is_empty(self):
        self.assertEqual(sync.resolve_scene_tags(full_scene(), [], {}, {}), [])

    def test_only_allowlisted_categories_are_considered(self):
        out = sync.resolve_scene_tags(full_scene(), ["c-setting"], {}, {})
        self.assertEqual([t["name"] for t in out], ["Outdoor"])

    def test_a_tag_with_no_category_is_never_allowlisted(self):
        out = sync.resolve_scene_tags(full_scene(), ["c-setting", "c-look"], {}, {})
        self.assertNotIn("Uncategorised", [t["name"] for t in out])

    def test_an_explicit_tag_map_row_wins_over_everything(self):
        existing = sync.index_by_name([{"id": 1, "name": "outdoor"}])
        out = sync.resolve_scene_tags(
            full_scene(), ["c-setting"],
            {"t-1": {"category_name": "Outside", "mode": "map"}}, existing)
        self.assertEqual(out[0]["name"], "Outside")
        self.assertEqual(out[0]["source"], "tag_map")

    def test_an_ignored_tag_is_dropped_even_though_its_category_is_allowlisted(self):
        out = sync.resolve_scene_tags(full_scene(), ["c-setting"],
                                      {"t-1": {"category_name": None, "mode": "ignore"}},
                                      {})
        self.assertEqual(out, [])

    def test_an_existing_category_name_is_reused_with_its_own_spelling(self):
        existing = sync.index_by_name([{"id": 9, "name": "outdoor"}])
        out = sync.resolve_scene_tags(full_scene(), ["c-setting"], {}, existing)
        self.assertEqual(out[0]["name"], "outdoor")
        self.assertEqual(out[0]["source"], "existing")

    def test_otherwise_the_stashdb_tag_name_is_used_verbatim(self):
        out = sync.resolve_scene_tags(full_scene(), ["c-setting"], {}, {})
        self.assertEqual(out[0]["name"], "Outdoor")
        self.assertEqual(out[0]["source"], "stashdb")

    def test_duplicates_after_resolution_collapse(self):
        scene = full_scene()
        scene["tags"].append({"id": "t-9", "name": "outdoor",
                              "category": {"id": "c-setting", "name": "Setting"}})
        out = sync.resolve_scene_tags(scene, ["c-setting"], {}, {})
        self.assertEqual(len(out), 1)

    def test_a_tag_with_no_id_is_skipped_not_keyed_as_the_string_none(self):
        # str(None) is "None", which would be POSTed as a real external id:
        # {"provider": "stashdb", "id": "None"}.
        scene = {"tags": [{"name": "Outdoor",
                           "category": {"id": "c-setting", "name": "Setting"}}]}
        self.assertEqual(sync.resolve_scene_tags(scene, ["c-setting"], {}, {}), [])

    def test_allowlisted_tags_reports_what_a_choice_would_import(self):
        tags = sync.allowlisted_tags(full_scene(), ["c-setting", "c-look"])
        self.assertEqual({t["name"] for t in tags}, {"Outdoor", "Blonde"})


class TestResolveName(unittest.TestCase):
    def test_an_existing_spelling_is_preferred(self):
        index = sync.index_by_name([{"id": 1, "name": "example network"}])
        self.assertEqual(sync.resolve_name("Example Network", index), "example network")

    def test_an_unknown_name_passes_through_unchanged(self):
        self.assertEqual(sync.resolve_name("Example Network", {}), "Example Network")


def settings(**over):
    out = dict(DEFAULT_SETTINGS)
    out.update(over)
    return out


def video(**over):
    row = {"id": 42, "source_id": 1, "rel_path": "Example Network\\x [scene-1]\\a.mp4",
           "path": "D:\\library\\scenes\\Example Network\\x [scene-1]\\a.mp4",
           "duration_secs": 1800.0, "title": "A Mp4", "release_date": None,
           "release_date_estimated": True, "studio": None, "models": [],
           "categories": [], "external_ids": [], "rating": 0, "favourite": False,
           "availability": "available", "metadata_source": "filename"}
    row.update(over)
    return row


def plan(video_row=None, scene=None, **over):
    return sync.plan_change(video_row or video(), scene or full_scene(),
                            settings=settings(**over), existing={})


class TestPlanChange(unittest.TestCase):
    def test_a_fresh_video_proposes_every_enabled_field(self):
        change = plan(allowlist=["c-setting"])
        fields = {d["field"] for d in change["diff"]}
        self.assertEqual(fields, {"title", "release_date", "studio", "models",
                                  "categories", "external_ids"})
        self.assertEqual(change["patch"]["title"], "Example Scene One")
        self.assertEqual(change["patch"]["release_date"], "2020-05-23")
        self.assertEqual(change["patch"]["release_date_estimated"], False)
        self.assertEqual(change["patch"]["studio"], "Example Network")
        self.assertEqual(change["patch"]["models"],
                         {"add": ["Jane Example", "Sam Sample"]})
        self.assertEqual(change["patch"]["categories"], {"add": ["Outdoor"]})
        self.assertEqual(change["patch"]["external_ids"],
                         {"set": [{"provider": "stashdb", "id": "scene-1"}]})

    def test_models_and_categories_are_add_only_never_set(self):
        # Removal is never offered: the app is the user's library, not StashDB's.
        change = plan(allowlist=["c-setting"])
        self.assertNotIn("set", change["patch"]["models"])
        self.assertNotIn("remove", change["patch"]["models"])
        self.assertNotIn("set", change["patch"]["categories"])

    def test_a_video_already_in_sync_proposes_nothing(self):
        row = video(title="Example Scene One", release_date="2020-05-23",
                    release_date_estimated=False,
                    studio={"id": 3, "name": "Example Network"},
                    models=[{"id": 1, "name": "Jane Example"},
                            {"id": 2, "name": "Sam Sample"}],
                    categories=[{"id": 8, "name": "Outdoor"}],
                    external_ids=[{"provider": "stashdb", "id": "scene-1"}])
        change = sync.plan_change(row, full_scene(),
                                  settings=settings(allowlist=["c-setting"]),
                                  existing={})
        self.assertEqual(change["patch"], {})
        self.assertEqual(change["diff"], [])

    def test_case_only_differences_are_not_changes(self):
        row = video(title="Example Scene One", release_date="2020-05-23",
                    release_date_estimated=False,
                    studio={"id": 3, "name": "example network"},
                    models=[{"id": 1, "name": "jane example"},
                            {"id": 2, "name": "SAM SAMPLE"}],
                    external_ids=[{"provider": "stashdb", "id": "scene-1"}])
        change = sync.plan_change(row, full_scene(), settings=settings(), existing={})
        self.assertEqual(change["patch"], {})

    def test_each_toggle_switches_its_field_off(self):
        self.assertNotIn("title", plan(write_title=False)["patch"])
        self.assertNotIn("release_date", plan(write_release_date=False)["patch"])
        self.assertNotIn("studio", plan(write_studio=False)["patch"])
        self.assertNotIn("models", plan(add_models=False)["patch"])
        self.assertNotIn("categories",
                         plan(allowlist=["c-setting"], add_categories=False)["patch"])

    def test_the_external_id_is_written_whatever_the_toggles_say(self):
        change = plan(write_title=False, write_release_date=False, write_studio=False,
                      add_models=False, add_categories=False)
        self.assertEqual(change["patch"],
                         {"external_ids": {"set": [{"provider": "stashdb",
                                                    "id": "scene-1"}]}})

    def test_a_missing_stashdb_field_leaves_the_video_alone(self):
        scene = full_scene()
        scene["title"] = None
        scene["release_date"] = None
        scene["studio"] = None
        change = sync.plan_change(video(), scene, settings=settings(), existing={})
        for key in ("title", "release_date", "studio"):
            self.assertNotIn(key, change["patch"])

    def test_release_date_is_never_sent_as_null_or_empty(self):
        scene = full_scene()
        scene["release_date"] = ""
        change = sync.plan_change(video(), scene, settings=settings(), existing={})
        self.assertNotIn("release_date", change["patch"])

    def test_a_matching_date_still_clears_the_estimated_flag(self):
        row = video(release_date="2020-05-23", release_date_estimated=True)
        change = sync.plan_change(row, full_scene(), settings=settings(), existing={})
        self.assertEqual(change["patch"]["release_date_estimated"], False)
        self.assertNotIn("release_date", change["patch"])


def fields_of(change):
    """The per-field picture, by field name."""
    return {f["field"]: f for f in change["fields"]}


#: The six fields the preview always shows, in the order it shows them.
FIELD_ORDER = ["title", "release_date", "studio", "models", "categories",
               "external_ids"]

#: Every action a field can report. There is no "remove": nothing the tool writes
#: takes anything away from a video.
ACTIONS = {"same", "off", "set", "rename", "change", "replace", "add"}


class TestPlanFields(unittest.TestCase):
    """What the write does to each field, said for every field.

    ``diff`` lists only what changes, as old -> new, which reads as "replace" even
    for models and categories, where the tool only ever adds. ``fields`` says,
    for all six fields, what is there now and what the write does to it.
    """

    def test_every_field_is_reported_in_order_even_when_nothing_changes(self):
        row = video(title="Example Scene One", release_date="2020-05-23",
                    release_date_estimated=False,
                    studio={"id": 3, "name": "Example Network"},
                    models=[{"id": 1, "name": "Jane Example"},
                            {"id": 2, "name": "Sam Sample"}],
                    categories=[{"id": 8, "name": "Outdoor"}],
                    external_ids=[{"provider": "stashdb", "id": "scene-1"}])
        change = sync.plan_change(row, full_scene(),
                                  settings=settings(allowlist=["c-setting"]),
                                  existing={})
        self.assertEqual([f["field"] for f in change["fields"]], FIELD_ORDER)
        self.assertEqual({f["action"] for f in change["fields"]}, {"same"})
        got = fields_of(change)
        self.assertEqual(got["title"]["current"], "Example Scene One")
        self.assertEqual(got["models"]["kept"], ["Jane Example", "Sam Sample"])
        self.assertEqual(got["models"]["added"], [])

    def test_a_blank_field_is_set_and_a_filled_one_is_renamed_or_replaced(self):
        blank = fields_of(plan(video(title="", studio=None, release_date=None)))
        self.assertEqual(blank["title"]["action"], "set")
        self.assertEqual(blank["studio"]["action"], "set")
        self.assertEqual(blank["release_date"]["action"], "set")

        filled = fields_of(plan(video(title="A Mp4", studio={"name": "Old Studio"},
                                      release_date="2019-01-01")))
        self.assertEqual((filled["title"]["action"], filled["title"]["current"],
                          filled["title"]["proposed"]),
                         ("rename", "A Mp4", "Example Scene One"))
        self.assertEqual((filled["studio"]["action"], filled["studio"]["current"],
                          filled["studio"]["proposed"]),
                         ("replace", "Old Studio", "Example Network"))
        self.assertEqual(filled["release_date"]["action"], "change")

    def test_only_the_estimated_flag_changing_is_a_change_to_the_date(self):
        row = video(release_date="2020-05-23", release_date_estimated=True)
        date = fields_of(plan(row))["release_date"]
        self.assertEqual(date["action"], "change")
        self.assertEqual((date["current"], date["proposed"]),
                         ("2020-05-23", "2020-05-23"))
        self.assertEqual((date["current_estimated"], date["proposed_estimated"]),
                         (True, False))

    def test_models_say_what_is_kept_and_what_is_added(self):
        row = video(models=[{"id": 1, "name": "Jane Example"}])
        models = fields_of(plan(row))["models"]
        self.assertEqual(models["action"], "add")
        self.assertEqual(models["kept"], ["Jane Example"])
        self.assertEqual(models["added"], [{"name": "Sam Sample", "new": True}])

    def test_an_addition_the_library_already_has_is_not_called_new(self):
        existing = {"models": sync.index_by_name([{"id": 7, "name": "Sam Sample"}]),
                    "studios": sync.index_by_name(
                        [{"id": 4, "name": "Example Network"}])}
        change = sync.plan_change(video(), full_scene(), settings=settings(),
                                  existing=existing)
        got = fields_of(change)
        self.assertEqual(got["models"]["added"],
                         [{"name": "Jane Example", "new": True},
                          {"name": "Sam Sample", "new": False}])
        self.assertFalse(got["studio"]["new"])
        self.assertTrue(fields_of(plan())["studio"]["new"])

    def test_categories_say_what_is_kept_and_what_is_added(self):
        row = video(categories=[{"id": 2, "name": "4K"}])
        cats = fields_of(plan(row, allowlist=["c-setting"]))["categories"]
        self.assertEqual(cats["action"], "add")
        self.assertEqual(cats["kept"], ["4K"])
        self.assertEqual(cats["added"], [{"name": "Outdoor", "new": True}])

    def test_a_field_switched_off_says_so_rather_than_no_change(self):
        # "No change" would read as StashDB agreeing with what is there.
        got = fields_of(plan(video(categories=[{"id": 2, "name": "4K"}]),
                             write_title=False, write_release_date=False,
                             write_studio=False, add_models=False,
                             add_categories=False))
        for name in ("title", "release_date", "studio", "models", "categories"):
            with self.subTest(field=name):
                self.assertEqual(got[name]["action"], "off")
        self.assertEqual(got["title"]["current"], "A Mp4")
        self.assertEqual(got["categories"]["kept"], ["4K"])

    def test_the_stashdb_link_is_added_or_replaced(self):
        self.assertEqual(fields_of(plan())["external_ids"]["action"], "add")
        moved = fields_of(plan(video(external_ids=[{"provider": "stashdb",
                                                    "id": "scene-0"}])))
        self.assertEqual((moved["external_ids"]["action"],
                          moved["external_ids"]["current"],
                          moved["external_ids"]["proposed"]),
                         ("replace", "scene-0", "scene-1"))

    def test_a_field_stashdb_has_nothing_for_is_left_as_it_is(self):
        scene = full_scene()
        scene["title"] = None
        scene["studio"] = None
        got = fields_of(sync.plan_change(video(), scene, settings=settings(),
                                         existing={}))
        self.assertEqual(got["title"]["action"], "same")
        self.assertEqual(got["studio"]["action"], "same")

    def test_no_field_is_ever_reported_as_removed(self):
        for change in (plan(), plan(allowlist=["c-setting"]),
                       plan(video(models=[{"name": "Someone Else"}]))):
            self.assertTrue({f["action"] for f in change["fields"]} <= ACTIONS)


class TestPartialReleaseDates(unittest.TestCase):
    """StashDB dates a scene to the year, the month, or the day.

    Pronoisseur's Local API takes only the day, and `POST /videos/batch` is
    all-or-nothing, so one scene dated `"2019"` is a `422` that writes nothing for
    every other row in its batch.
    """

    def dated(self, raw, **video_over):
        scene = full_scene()
        scene["release_date"] = raw
        return sync.plan_change(video(**video_over), scene, settings=settings(),
                                existing={})

    def test_a_full_date_is_written_as_it_stands_and_asserted_not_estimated(self):
        change = self.dated("2019-04-11")
        self.assertEqual(change["patch"]["release_date"], "2019-04-11")
        self.assertIs(change["patch"]["release_date_estimated"], False)
        self.assertFalse(change["scene_date_estimated"])

    def test_a_month_becomes_the_first_of_that_month_and_says_it_is_estimated(self):
        change = self.dated("2019-04")
        self.assertEqual(change["patch"]["release_date"], "2019-04-01")
        self.assertIs(change["patch"]["release_date_estimated"], True)
        self.assertEqual(change["scene_date"], "2019-04-01")
        self.assertTrue(change["scene_date_estimated"])

    def test_a_year_becomes_the_first_of_january_and_says_it_is_estimated(self):
        change = self.dated("2019")
        self.assertEqual(change["patch"]["release_date"], "2019-01-01")
        self.assertIs(change["patch"]["release_date_estimated"], True)
        self.assertTrue(change["scene_date_estimated"])

    def test_a_shape_this_tool_will_not_guess_at_omits_the_date_and_says_why(self):
        change = self.dated("late 2019")
        self.assertNotIn("release_date", change["patch"])
        self.assertNotIn("release_date_estimated", change["patch"])
        self.assertIn("StashDB date 'late 2019' not usable", change["note"])
        self.assertIsNone(change["scene_date"])
        # The rest of the write is untouched: an unusable date costs the row its
        # date, never its title, studio, models or external id.
        self.assertEqual(change["patch"]["title"], "Example Scene One")

    def test_a_day_that_does_not_exist_is_not_repaired_into_a_nearby_one(self):
        change = self.dated("2019-02-31")
        self.assertNotIn("release_date", change["patch"])
        self.assertIn("StashDB date '2019-02-31' not usable", change["note"])

    def test_the_unusable_note_joins_the_one_the_row_already_carried(self):
        scene = full_scene()
        scene["release_date"] = "2019-13"        # month 13
        change = sync.plan_change(video(), scene, settings=settings(), existing={},
                                  note="no duration")
        self.assertEqual(change["note"],
                         "no duration (StashDB date '2019-13' not usable)")

    def test_a_video_already_holding_the_widened_date_still_flags_the_estimate(self):
        # The date matches, so nothing about it is written — but the flag says
        # this day is known, and after this write it is not.
        change = self.dated("2019", release_date="2019-01-01",
                            release_date_estimated=False)
        self.assertNotIn("release_date", change["patch"])
        self.assertIs(change["patch"]["release_date_estimated"], True)

    def test_a_video_already_flagged_estimated_on_a_widened_date_is_left_alone(self):
        change = self.dated("2019", release_date="2019-01-01",
                            release_date_estimated=True)
        self.assertNotIn("release_date", change["patch"])
        self.assertNotIn("release_date_estimated", change["patch"])


class TestNormaliseReleaseDate(unittest.TestCase):
    def test_the_three_shapes_stashdb_hands_out(self):
        self.assertEqual(sync.normalise_release_date("2019-04-11"),
                         ("2019-04-11", False))
        self.assertEqual(sync.normalise_release_date("2019-04"),
                         ("2019-04-01", True))
        self.assertEqual(sync.normalise_release_date("2019"),
                         ("2019-01-01", True))

    def test_whitespace_is_trimmed_before_anything_is_decided(self):
        self.assertEqual(sync.normalise_release_date("  2019-04  "),
                         ("2019-04-01", True))

    def test_everything_else_is_refused_rather_than_guessed_at(self):
        for value in (None, "", "   ", "2019-13", "2019-04-32", "2019-02-31",
                      "19-04-11", "2019/04/11", "2019-4-1", "late 2019",
                      "2019-2029", "20190411"):
            with self.subTest(value=value):
                self.assertEqual(sync.normalise_release_date(value), (None, False))

    def test_an_existing_library_spelling_is_reused_instead_of_creating_a_twin(self):
        existing = {"studios": sync.index_by_name([{"id": 3, "name": "EXAMPLE NETWORK"}]),
                    "models": sync.index_by_name([{"id": 1, "name": "jane example"}]),
                    "categories": {}}
        change = sync.plan_change(video(), full_scene(), settings=settings(),
                                  existing=existing)
        self.assertEqual(change["patch"]["studio"], "EXAMPLE NETWORK")
        self.assertEqual(change["patch"]["models"]["add"],
                         ["jane example", "Sam Sample"])

    def test_the_cover_and_scene_headline_travel_with_the_change(self):
        change = plan()
        self.assertEqual(change["cover"], "https://cdn/x.jpg")
        self.assertEqual(change["scene_title"], "Example Scene One")
        self.assertEqual(change["scene_date"], "2020-05-23")

    def test_status_and_note_are_carried_through(self):
        change = sync.plan_change(video(), full_scene(), settings=settings(),
                                  existing={}, status=sync.UNVERIFIED, note="no duration")
        self.assertEqual(change["status"], sync.UNVERIFIED)
        self.assertEqual(change["note"], "no duration")

    def test_entities_record_what_would_have_to_exist(self):
        change = plan(allowlist=["c-setting"])
        self.assertEqual(change["entities"]["studios"],
                         [{"name": "Example Network", "stashdb_id": "s-0"}])
        self.assertEqual([m["name"] for m in change["entities"]["models"]],
                         ["Jane Example", "Sam Sample"])
        self.assertEqual(change["entities"]["categories"],
                         [{"name": "Outdoor", "stashdb_id": "t-1"}])

    def test_a_mapped_category_is_added_to_the_video_but_never_stamped(self):
        # The user said "file Outdoor under Outside", not "Outside IS StashDB tag
        # t-1". Stamping it would assert an equivalence they never made, and the
        # contract has no way to take an external id back.
        change = plan(allowlist=["c-setting"],
                      tag_map={"t-1": {"category_name": "Outside", "mode": "map"}})
        self.assertEqual(change["patch"]["categories"], {"add": ["Outside"]})
        self.assertEqual(change["entities"]["categories"], [])

    def test_an_unmapped_category_is_both_added_and_stamped(self):
        change = plan(allowlist=["c-setting"])
        self.assertEqual(change["patch"]["categories"], {"add": ["Outdoor"]})
        self.assertEqual(change["entities"]["categories"],
                         [{"name": "Outdoor", "stashdb_id": "t-1"}])

    def test_aliases_are_actor_kind_and_verbatim(self):
        change = plan()
        self.assertIn({"kind": "actor", "raw_token": "Sam The Sample",
                       "canonical_name": "Sam Sample"}, change["entities"]["aliases"])

    def test_no_aliases_when_models_are_not_being_written(self):
        self.assertEqual(plan(add_models=False)["entities"]["aliases"], [])


class TestBatchItem(unittest.TestCase):
    def test_the_video_id_joins_the_patch(self):
        change = plan()
        item = sync.batch_item(change)
        self.assertEqual(item["id"], 42)
        self.assertEqual(item["title"], "Example Scene One")

    def test_a_no_op_change_makes_no_item(self):
        change = plan()
        change["patch"] = {}
        self.assertIsNone(sync.batch_item(change))


class TestEntityUpsertPlan(unittest.TestCase):
    def test_only_new_or_unstamped_entities_are_upserted(self):
        # Each POST is a write on the app side and refreshes its library view even
        # when it changes nothing, so a no-op POST is a real cost.
        existing = {
            "models": sync.index_by_name([
                {"id": 1, "name": "Jane Example",
                 "external_ids": [{"provider": "stashdb", "id": "p-1"}]},
                {"id": 2, "name": "Sam Sample", "external_ids": []}]),
            "studios": sync.index_by_name([
                {"id": 3, "name": "Example Network",
                 "external_ids": [{"provider": "stashdb", "id": "s-0"}]}]),
            "categories": {},
        }
        changes = [plan(allowlist=["c-setting"])]
        upserts = sync.plan_entity_upserts(changes, existing)
        self.assertEqual([m["name"] for m in upserts["models"]], ["Sam Sample"])
        self.assertEqual(upserts["studios"], [])
        self.assertEqual([c["name"] for c in upserts["categories"]], ["Outdoor"])

    def test_a_different_stashdb_id_on_an_existing_name_is_re_stamped(self):
        existing = {"models": {}, "categories": {}, "studios": sync.index_by_name([
            {"id": 3, "name": "Example Network",
             "external_ids": [{"provider": "stashdb", "id": "OLD"}]}])}
        upserts = sync.plan_entity_upserts([plan()], existing)
        self.assertEqual(upserts["studios"],
                         [{"name": "Example Network", "stashdb_id": "s-0"}])

    def test_entities_are_deduplicated_across_changes(self):
        changes = [plan(allowlist=["c-setting"]), plan(allowlist=["c-setting"])]
        upserts = sync.plan_entity_upserts(changes, {"models": {}, "studios": {},
                                                     "categories": {}})
        self.assertEqual(len(upserts["studios"]), 1)
        self.assertEqual(len(upserts["models"]), 2)

    def test_aliases_are_deduplicated_and_ordered(self):
        upserts = sync.plan_entity_upserts([plan(), plan()],
                                           {"models": {}, "studios": {}, "categories": {}})
        raw = [a["raw_token"] for a in upserts["aliases"]]
        self.assertEqual(len(raw), len(set(raw)))

    def test_changes_with_nothing_to_write_contribute_no_upserts(self):
        change = plan()
        change["patch"] = {}
        upserts = sync.plan_entity_upserts([change], {"models": {}, "studios": {},
                                                      "categories": {}})
        self.assertEqual(upserts["models"], [])


class TestAliasUpsertPlan(unittest.TestCase):
    """An alias the app already holds is not pushed again.

    A preview that says "N alias rows" the moment after a run wrote those same N
    is a preview that never converges, so the planner needs an index of the aliases
    the app already holds.
    """

    def upserts(self, aliases=(), changes=None):
        existing = {"models": {}, "studios": {}, "categories": {},
                    "aliases": sync.index_aliases(aliases)}
        return sync.plan_entity_upserts(changes or [plan()], existing)

    def test_an_alias_the_app_already_holds_is_not_proposed_again(self):
        upserts = self.upserts([{"kind": "actor", "raw_token": "Jane E",
                                 "canonical_name": "Jane Example"}])
        self.assertEqual([a["raw_token"] for a in upserts["aliases"]],
                         ["Sam The Sample"])

    def test_a_token_the_app_spells_differently_is_still_the_same_alias(self):
        # The app keys its alias map on `name_key(raw_token)`, so "jane-e" and
        # "Jane E" are one row to it. Re-pushing would be a no-op write per run.
        upserts = self.upserts([{"kind": "actor", "raw_token": "jane-e",
                                 "canonical_name": "Jane Example"}])
        self.assertNotIn("Jane E", [a["raw_token"] for a in upserts["aliases"]])

    def test_a_changed_canonical_name_is_proposed_again(self):
        # The token is the same, the name it folds onto is not: that is a real
        # write, and the whole point of `PUT /aliases` being an upsert.
        upserts = self.upserts([{"kind": "actor", "raw_token": "Jane E",
                                 "canonical_name": "Someone Else"}])
        self.assertIn({"kind": "actor", "raw_token": "Jane E",
                       "canonical_name": "Jane Example"}, upserts["aliases"])

    def test_a_canonical_that_differs_only_in_case_is_a_different_target(self):
        # The app's name tables collate byte-for-byte, so "jane example" is not
        # the entity "Jane Example" and folding a token onto it is a real change.
        upserts = self.upserts([{"kind": "actor", "raw_token": "Jane E",
                                 "canonical_name": "jane example"}])
        self.assertIn("Jane E", [a["raw_token"] for a in upserts["aliases"]])

    def test_the_same_token_under_another_kind_does_not_skip_it(self):
        upserts = self.upserts([{"kind": "studio", "raw_token": "Jane E",
                                 "canonical_name": "Jane Example"}])
        self.assertIn("Jane E", [a["raw_token"] for a in upserts["aliases"]])

    def test_with_every_alias_already_held_nothing_is_proposed(self):
        upserts = self.upserts([{"kind": "actor", "raw_token": "Jane E",
                                 "canonical_name": "Jane Example"},
                                {"kind": "actor",
                                 "raw_token": "Sam The Sample",
                                 "canonical_name": "Sam Sample"}])
        self.assertEqual(upserts["aliases"], [])

    def test_no_alias_index_at_all_still_proposes_everything(self):
        # `_existing` falls back to `{}` when Pronoisseur has gone away, and the
        # planner has to keep working from it rather than silently writing less.
        upserts = sync.plan_entity_upserts([plan()], {})
        self.assertEqual(len(upserts["aliases"]), 2)


class TestChunked(unittest.TestCase):
    def test_splits_at_the_chunk_size(self):
        self.assertEqual(sync.chunked([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_an_empty_list_is_no_chunks(self):
        self.assertEqual(sync.chunked([], 100), [])

    def test_a_chunk_size_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            sync.chunked([1], 0)


class TestReviewModels(unittest.TestCase):
    """The models a Review row is grouped under.

    Two sources, because each one misses rows the other has: what the video
    already carries in Pronoisseur (usually read off the filename), and the
    performers on the scene StashDB recommends for it. A video with two models
    is grouped under both.
    """

    def names(self, video_row, scene=None, index=None):
        return [m["name"] for m in sync.review_models(video_row, scene, index or {})]

    def test_the_videos_own_models_come_first_then_the_scenes(self):
        row = video(models=[{"id": 1, "name": "Kay Example"}])
        self.assertEqual(self.names(row, full_scene()),
                         ["Kay Example", "Jane Example", "Sam Sample"])

    def test_each_entry_carries_the_key_it_is_grouped_by(self):
        row = video(models=[{"id": 1, "name": "Kay Example"}])
        self.assertEqual(sync.review_models(row, None, {}),
                         [{"name": "Kay Example", "key": "kayexample"}])

    def test_a_model_on_both_sides_is_listed_once(self):
        # Spelled differently on each side, which is the usual case: one came
        # from a filename and one from StashDB.
        row = video(models=[{"id": 1, "name": "JANE EXAMPLE"}])
        self.assertEqual(self.names(row, full_scene()),
                         ["JANE EXAMPLE", "Sam Sample"])

    def test_a_scene_performer_takes_the_librarys_own_spelling(self):
        # The same name the write would use, so the group a video sits in and
        # the model the write adds are spelled alike.
        index = sync.index_by_name([{"id": 9, "name": "jane example"}])
        self.assertEqual(self.names(video(), full_scene(), index),
                         ["jane example", "Sam Sample"])

    def test_a_credited_as_name_is_not_a_second_model(self):
        # "Sam The Sample" is how one scene credits Sam Sample: an alias, and
        # grouping on it would split one person across two groups.
        self.assertNotIn("Sam The Sample", self.names(video(), full_scene()))

    def test_no_scene_means_the_videos_own_models_only(self):
        row = video(models=[{"id": 1, "name": "Kay Example"}])
        self.assertEqual(self.names(row, None), ["Kay Example"])

    def test_nothing_on_either_side_is_no_models(self):
        self.assertEqual(sync.review_models(video(), {"performers": []}, {}), [])

    def test_a_blank_name_is_skipped_rather_than_grouped(self):
        row = video(models=[{"id": 1, "name": "  "}, {"id": 2, "name": None}])
        self.assertEqual(sync.review_models(row, None, {}), [])


if __name__ == "__main__":
    unittest.main()
