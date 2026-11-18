import copy
import unittest

from tagger import matcher

OS_A = "5e1f93a07b2c4d68"
OS_B = "c4a90d3e6b17f852"
PH_A = "9e17c2a05b48d63c"
PH_NEAR = "9e17c2a05b48d63d"      # 1 bit from PH_A
PH_TWO = "9e17c2a05b48d63f"       # 2 bits from PH_A
PH_FAR = "0000000000000000"


def scene(scene_id, oshashes=(), phashes=(), duration=1200):
    return {"id": scene_id, "title": "Scene " + scene_id, "duration": duration,
            "fingerprints": ([{"hash": h, "algorithm": "OSHASH"} for h in oshashes]
                             + [{"hash": h, "algorithm": "PHASH"} for h in phashes])}


class TestCandidateMatches(unittest.TestCase):
    def test_an_oshash_hit_wins_and_reports_distance_zero(self):
        out = matcher.candidate_matches(scene("a", oshashes=[OS_A]), OS_A, None)
        self.assertEqual(out["kind"], matcher.BY_OSHASH)
        self.assertEqual(out["distance"], 0)

    def test_oshash_comparison_ignores_case(self):
        out = matcher.candidate_matches(scene("a", oshashes=[OS_A.upper()]),
                                        OS_A, None)
        self.assertEqual(out["kind"], matcher.BY_OSHASH)

    def test_a_phash_within_tolerance_matches_and_reports_its_distance(self):
        out = matcher.candidate_matches(scene("a", phashes=[PH_NEAR]), None, PH_A)
        self.assertEqual(out["kind"], matcher.BY_PHASH)
        self.assertEqual(out["distance"], 1)

    def test_a_phash_beyond_tolerance_does_not_match(self):
        self.assertIsNone(matcher.candidate_matches(scene("a", phashes=[PH_FAR]),
                                                    None, PH_A))

    def test_the_nearest_listed_phash_is_the_one_reported(self):
        out = matcher.candidate_matches(scene("a", phashes=[PH_FAR, PH_NEAR]),
                                        None, PH_A)
        self.assertEqual(out["distance"], 1)

    def test_oshash_beats_phash_on_the_same_candidate(self):
        out = matcher.candidate_matches(scene("a", oshashes=[OS_A],
                                              phashes=[PH_NEAR]), OS_A, PH_A)
        self.assertEqual(out["kind"], matcher.BY_OSHASH)

    def test_no_fingerprints_at_all_is_no_match(self):
        self.assertIsNone(matcher.candidate_matches(scene("a"), OS_A, PH_A))

    def test_a_malformed_listed_phash_is_skipped_not_fatal(self):
        out = matcher.candidate_matches(scene("a", phashes=["zzzz", PH_NEAR]),
                                        None, PH_A)
        self.assertEqual(out["distance"], 1)

    def test_a_corrupt_file_phash_is_an_error_not_a_silent_miss(self):
        # Their bad listing is skipped; our own bad fingerprint is a broken file
        # and must not read as "no scene matches".
        with self.assertRaises(ValueError):
            matcher.candidate_matches(scene("a", phashes=[PH_NEAR]), None, "zzzz")


class TestDurationAgrees(unittest.TestCase):
    def test_within_tolerance_is_true(self):
        self.assertTrue(matcher.duration_agrees(1200.0, 1202))

    def test_beyond_tolerance_is_false(self):
        self.assertFalse(matcher.duration_agrees(1200.0, 1300))

    def test_an_unknown_duration_on_either_side_is_none_never_false(self):
        # StashDB scenes can carry a null duration.
        self.assertIsNone(matcher.duration_agrees(None, 1200))
        self.assertIsNone(matcher.duration_agrees(1200.0, None))

    def test_a_non_positive_duration_is_unknown_not_a_disagreement(self):
        # A scene carrying 0 instead of null is a gap in StashDB's data, not a
        # film of no length; reading it as a mismatch would drop the right scene.
        self.assertIsNone(matcher.duration_agrees(1200.0, 0))
        self.assertIsNone(matcher.duration_agrees(0, 1200))


class TestClassify(unittest.TestCase):
    def test_exactly_one_oshash_hit_is_verified(self):
        out = matcher.classify([scene("a", oshashes=[OS_A])], OS_A, None, 1200.0)
        self.assertEqual(out["status"], matcher.VERIFIED)
        self.assertEqual(out["confidence"], matcher.BY_OSHASH)
        self.assertEqual([c["scene"]["id"] for c in out["candidates"]], ["a"])

    def test_a_phash_only_companion_does_not_make_an_oshash_hit_ambiguous(self):
        # An oshash match can arrive with a phash-only companion: a duplicate
        # listing, or a different scene with an identical duration. Counting the
        # group would prompt the user about files identified byte-exactly.
        out = matcher.classify(
            [scene("a", oshashes=[OS_A]), scene("b", phashes=[PH_NEAR])],
            OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.VERIFIED)
        self.assertEqual([c["scene"]["id"] for c in out["candidates"]], ["a"])

    def test_two_oshash_hits_are_ambiguous(self):
        # The same bytes listed under two scenes, such as a network release and its
        # channel copy. An exact hit is not proof of one scene.
        out = matcher.classify([scene("a", oshashes=[OS_A]),
                                scene("b", oshashes=[OS_A])], OS_A, None, 1200.0)
        self.assertEqual(out["status"], matcher.AMBIGUOUS)
        self.assertEqual(len(out["candidates"]), 2)

    def test_a_single_phash_hit_is_likely_not_verified(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR])], OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.LIKELY)
        self.assertEqual(out["confidence"], matcher.BY_PHASH)

    def test_several_phash_hits_are_ambiguous(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR]),
                                scene("b", phashes=[PH_NEAR])], OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.AMBIGUOUS)

    def test_no_hits_at_all_is_unmatched(self):
        out = matcher.classify([scene("a", oshashes=[OS_B])], OS_A, None, 1200.0)
        self.assertEqual(out["status"], matcher.UNMATCHED)
        self.assertEqual(out["candidates"], [])

    def test_an_empty_candidate_list_is_unmatched(self):
        self.assertEqual(matcher.classify([], OS_A, PH_A, 1200.0)["status"],
                         matcher.UNMATCHED)

    def test_a_duration_disagreement_demotes_a_likely_to_ambiguous(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR], duration=99)],
                               OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.AMBIGUOUS)
        self.assertIn("duration", out["note"].lower())

    def test_a_duration_disagreement_drops_a_candidate_from_an_ambiguous_set(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR], duration=1200),
                                scene("b", phashes=[PH_NEAR], duration=99)],
                               OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.LIKELY)
        self.assertEqual([c["scene"]["id"] for c in out["candidates"]], ["a"])

    def test_an_unknown_duration_never_drops_a_candidate(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR], duration=None)],
                               OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.LIKELY)

    def test_a_duration_disagreement_does_not_demote_an_oshash_hit(self):
        # Identical bytes beat a duration StashDB may simply have wrong.
        out = matcher.classify([scene("a", oshashes=[OS_A], duration=99)],
                               OS_A, None, 1200.0)
        self.assertEqual(out["status"], matcher.VERIFIED)

    def test_candidates_carry_their_kind_and_distance_for_the_cover_grid(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR])], OS_A, PH_A, 1200.0)
        candidate = out["candidates"][0]
        self.assertEqual(candidate["kind"], matcher.BY_PHASH)
        self.assertEqual(candidate["distance"], 1)
        self.assertIn("duration_delta", candidate)

    def test_ambiguous_candidates_are_ordered_best_first(self):
        # Both inside tolerance (2 bits and 1 bit), so this really is ambiguous and
        # the guard can go: the assertion must run on every future change to the
        # sort, because the cover grid renders these in order.
        out = matcher.classify([scene("a", phashes=[PH_TWO]),
                                scene("b", phashes=[PH_NEAR])], None, PH_A)
        self.assertEqual(out["status"], matcher.AMBIGUOUS)
        self.assertEqual([c["scene"]["id"] for c in out["candidates"]], ["b", "a"])
        self.assertEqual([c["distance"] for c in out["candidates"]], [1, 2])

    def test_even_an_all_dropped_ambiguous_set_is_ordered_best_first(self):
        # The branch where every near match lost on duration returns them all, and
        # the grid orders that list too.
        out = matcher.classify([scene("a", phashes=[PH_TWO], duration=99),
                                scene("b", phashes=[PH_NEAR], duration=99)],
                               None, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.AMBIGUOUS)
        self.assertEqual([c["scene"]["id"] for c in out["candidates"]], ["b", "a"])

    def test_one_scene_listed_twice_in_a_group_is_one_scene(self):
        # A scene can answer both arms of a single lookup. Counting the entries
        # rather than the distinct scenes would report a byte-exactly identified
        # file as ambiguous and show the user two identical covers.
        out = matcher.classify([scene("a", oshashes=[OS_A]),
                                scene("a", oshashes=[OS_A], phashes=[PH_NEAR])],
                               OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.VERIFIED)
        self.assertEqual(len(out["candidates"]), 1)

    def test_a_zero_duration_never_drops_a_candidate(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR], duration=0)],
                               OS_A, PH_A, 1200.0)
        self.assertEqual(out["status"], matcher.LIKELY)

    def test_a_corrupt_file_phash_does_not_blame_stashdb(self):
        out = matcher.classify([scene("a", phashes=[PH_NEAR])], OS_A, "zzzz", 1200.0)
        self.assertEqual(out["status"], matcher.UNMATCHED)
        self.assertIn("phash", out["note"].lower())

    def test_a_corrupt_file_phash_never_costs_an_oshash_identification(self):
        out = matcher.classify([scene("a", oshashes=[OS_A])], OS_A, "zzzz", 1200.0)
        self.assertEqual(out["status"], matcher.VERIFIED)


class TestStatusVocabulary(unittest.TestCase):
    """The cross-module coupling nothing else pins."""

    def test_a_fingerprint_verified_row_is_writable_by_the_uuid_path_s_own_rule(self):
        # matcher and library_sync independently define "verified" for two different
        # identity routes, and a fingerprint-verified row is writable only because
        # those literals are equal. Nothing else pins that coupling.
        from tagger import library_sync
        self.assertEqual(matcher.VERIFIED, library_sync.VERIFIED)
        self.assertIn(matcher.VERIFIED, library_sync.WRITABLE_STATUSES)

    def test_likely_is_never_writable(self):
        # A near-duplicate encode of a DIFFERENT scene can score `likely` with an
        # identical duration. This assertion is what stops a future change
        # promoting it.
        from tagger import library_sync
        self.assertNotIn(matcher.LIKELY, library_sync.WRITABLE_STATUSES)


class TestAssistedTerms(unittest.TestCase):
    """The last-resort search terms, read off a row and nothing else."""

    def test_a_title_on_the_row_is_used_and_the_path_is_never_consulted(self):
        # The path here cleans to a different string, so the equality below can
        # only hold if rel_path was left alone.
        terms = matcher.assisted_terms(
            {"title": "  Sunset Drive  ",
             "rel_path": "Example Studios\\2024-01-02 - Night Shift 1080p.mp4"})
        self.assertEqual(terms["title"], "Sunset Drive")

    def test_a_missing_title_falls_back_to_the_cleaned_path_stem(self):
        terms = matcher.assisted_terms(
            {"rel_path": "Example Studios\\2024-01-02 - Sunset Drive "
                         "[7f3c1a9e] 1080p.mp4"})
        self.assertEqual(terms["title"], "Sunset Drive")

    def test_both_path_separators_are_split_on_whatever_the_host_is(self):
        # The rows come from a Windows library but the tests and the tool run on
        # POSIX too, where os.path reads "a\\b.mkv" as one segment and would
        # leave the backslash in the search term.
        terms = matcher.assisted_terms(
            {"rel_path": "example/nested\\Night Shift.mkv"})
        self.assertEqual(terms["title"], "Night Shift")

    def test_a_title_that_cleans_away_to_nothing_is_none_not_empty(self):
        terms = matcher.assisted_terms({"title": "   ",
                                        "rel_path": "example/1080p.XXX.mp4"})
        self.assertIsNone(terms["title"])

    def test_performers_read_models_and_survive_an_entry_with_no_name(self):
        terms = matcher.assisted_terms(
            {"title": "Night Shift",
             "models": [{"id": 5, "name": " Jamie Example "}, {"id": 6},
                        {"id": 7, "name": "  "}, {"id": 8, "name": "Alex Placeholder"}]})
        self.assertEqual(terms["performers"], ["Jamie Example", "Alex Placeholder"])

    def test_an_exactly_repeated_performer_name_is_listed_once(self):
        terms = matcher.assisted_terms(
            {"models": [{"name": "Jamie Example"}, {"name": "Jamie Example"}]})
        self.assertEqual(terms["performers"], ["Jamie Example"])

    def test_the_studio_name_is_stripped_and_a_missing_one_is_none(self):
        terms = matcher.assisted_terms({"studio": {"id": 3,
                                                   "name": " Example Studios "}})
        self.assertEqual(terms["studio"], "Example Studios")
        self.assertIsNone(matcher.assisted_terms({})["studio"])

    def test_a_release_date_is_taken_only_in_exact_iso_form(self):
        self.assertEqual(
            matcher.assisted_terms({"release_date": "2024-01-02"})["date"],
            "2024-01-02")
        for bad in ("2024-1-2", "", None, 20240102, "02/01/2024"):
            with self.subTest(release_date=bad):
                self.assertIsNone(
                    matcher.assisted_terms({"release_date": bad})["date"])


class TestDateWindow(unittest.TestCase):
    def test_a_window_is_three_ascending_days(self):
        self.assertEqual(matcher.date_window("2024-05-10"),
                         ["2024-05-09", "2024-05-10", "2024-05-11"])

    def test_a_window_crosses_a_leap_month_boundary(self):
        # 2024 is a leap year, so the day before 1 March is 29 February - the
        # case any hand-rolled day arithmetic gets wrong.
        self.assertEqual(matcher.date_window("2024-03-01"),
                         ["2024-02-29", "2024-03-01", "2024-03-02"])

    def test_no_date_is_an_empty_window(self):
        self.assertEqual(matcher.date_window(None), [])

    def test_a_well_shaped_impossible_date_is_an_empty_window(self):
        self.assertEqual(matcher.date_window("2024-02-31"), [])


class TestBuildAssistedQuery(unittest.TestCase):
    def video(self):
        return {"id": 1, "title": "Sunset Drive", "release_date": "2024-01-02",
                "rel_path": "Example Studios\\Sunset Drive.mp4",
                "studio": {"id": 3, "name": "Example Studios"},
                "models": [{"id": 5, "name": "Jamie Example"}]}

    def test_the_date_never_reaches_the_filter_but_does_reach_the_window(self):
        # The query filter has no BETWEEN, so a +/-1 day window is not one
        # criterion, and an EQUALS on the filename's date would drop the correct
        # scene in the very case the window exists for - a date a day out. The
        # window ranks; it must never filter.
        out = matcher.build_assisted_query(self.video())
        self.assertNotIn("date", out["filter"])
        self.assertEqual(out["date_window"],
                         ["2024-01-01", "2024-01-02", "2024-01-03"])

    def test_the_terms_ride_beside_the_filter_that_cannot_carry_them(self):
        out = matcher.build_assisted_query(self.video())
        self.assertEqual(out["terms"], matcher.assisted_terms(self.video()))
        self.assertEqual(out["terms"]["performers"], ["Jamie Example"])

    def test_studios_are_carried_only_when_ids_are_supplied(self):
        self.assertNotIn("studios",
                         matcher.build_assisted_query(self.video())["filter"])
        out = matcher.build_assisted_query(self.video(),
                                           studio_ids=["s-1", "s-2"])
        self.assertEqual(out["filter"]["studios"], ["s-1", "s-2"])

    def test_performer_ids_ride_outside_the_filter_build_scene_input_reads(self):
        out = matcher.build_assisted_query(self.video(), performer_ids=["p-1"])
        self.assertEqual(out["performer_ids"], ["p-1"])
        self.assertNotIn("performers", out["filter"])

    def test_a_row_with_nothing_but_a_noisy_path_still_builds_a_query(self):
        out = matcher.build_assisted_query({"rel_path": "example/1080p.mp4"})
        self.assertNotIn("title", out["filter"])
        self.assertEqual(out["date_window"], [])

    def test_the_filter_is_one_build_scene_input_accepts(self):
        # Importing queries here is fine - the purity rule binds the module, not
        # its tests - and this is the only thing pinning the two vocabularies
        # together, since matcher cannot import queries to check for itself.
        from tagger import queries
        out = matcher.build_assisted_query(self.video(),
                                           performer_ids=["p-1"],
                                           studio_ids=["s-1"])
        scene_input = queries.build_scene_input(
            out["filter"], performer_ids=out["performer_ids"])
        self.assertEqual(scene_input["title"], "Sunset Drive")
        self.assertEqual(scene_input["sort"], "DATE")
        self.assertEqual(scene_input["direction"], "DESC")
        self.assertEqual(scene_input["studios"],
                         {"value": ["s-1"], "modifier": "INCLUDES"})
        self.assertEqual(scene_input["performers"],
                         {"value": ["p-1"], "modifier": "INCLUDES"})
        self.assertNotIn("date", scene_input)


class TestRankAssisted(unittest.TestCase):
    WINDOW = ["2024-01-01", "2024-01-02", "2024-01-03"]

    def scenes(self):
        return [{"id": "a", "release_date": "2024-06-01"},
                {"id": "b", "release_date": "2024-01-03"},
                {"id": "c", "release_date": None},
                {"id": "d", "release_date": "2024-01-01"}]

    def test_date_matches_come_first_and_server_order_holds_within_a_group(self):
        ranked = matcher.rank_assisted(self.scenes(), self.WINDOW)
        self.assertEqual([r["scene"]["id"] for r in ranked], ["b", "d", "a", "c"])
        self.assertEqual([r["date_match"] for r in ranked],
                         [True, True, False, False])

    def test_an_empty_window_matches_nothing_and_keeps_server_order(self):
        ranked = matcher.rank_assisted(self.scenes(), [])
        self.assertEqual([r["scene"]["id"] for r in ranked],
                         ["a", "b", "c", "d"])
        self.assertEqual([r["date_match"] for r in ranked],
                         [False, False, False, False])

    def test_nothing_handed_in_is_mutated(self):
        scenes = self.scenes()
        before = copy.deepcopy(scenes)
        matcher.rank_assisted(scenes, self.WINDOW)
        self.assertEqual(scenes, before)
        self.assertEqual([s["id"] for s in scenes], ["a", "b", "c", "d"])


class TestHamming(unittest.TestCase):
    def test_identical_hashes_are_distance_zero(self):
        self.assertEqual(matcher.hamming("9e17c2a05b48d63c",
                                          "9e17c2a05b48d63c"), 0)

    def test_counts_differing_bits(self):
        self.assertEqual(matcher.hamming("0000000000000000",
                                          "0000000000000003"), 2)

    def test_case_insensitive(self):
        self.assertEqual(matcher.hamming("9E17C2A05B48D63C",
                                          "9e17c2a05b48d63c"), 0)

    def test_a_malformed_hash_is_a_value_error_not_a_wrong_distance(self):
        with self.assertRaises(ValueError):
            matcher.hamming("nothex", "0000000000000000")

    def test_hashes_of_different_lengths_are_refused(self):
        # int(x, 16) left-zero-pads, so a truncated listing would otherwise score
        # a tiny distance against a file whose own phash is near zero - and a
        # blank sprite genuinely hashes to all zeros, so such a file exists.
        with self.assertRaises(ValueError):
            matcher.hamming("9e17", "9e17c2a05b48d63c")
        with self.assertRaises(ValueError):
            matcher.hamming("0000000000000000", "00")

    def test_surrounding_whitespace_is_not_a_length_difference(self):
        self.assertEqual(matcher.hamming("  9e17c2a05b48d63c  ",
                                          "9e17c2a05b48d63c"), 0)


if __name__ == "__main__":
    unittest.main()
