"""Review's stills: one frame of the user's own file, taken with ffmpeg.

It needs ffmpeg and nothing else, because it is a picture to look at, not a hash.
"""

import json
import os
import subprocess
import unittest

from tagger import frames


class FakeFfmpeg:
    """Stands in for ``subprocess.run``, recording every call.

    ``fail_on`` is a predicate over the argument list: return True and that call
    raises ``CalledProcessError``, which is how an unreadable file is driven.
    """

    def __init__(self, stdout=b"", text_stdout="", fail_on=None):
        self.calls, self.kwargs = [], []
        self.stdout, self.text_stdout, self.fail_on = stdout, text_stdout, fail_on

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(dict(kwargs))
        if self.fail_on and self.fail_on(args):
            raise subprocess.CalledProcessError(1, args)
        out = self.text_stdout if kwargs.get("text") else self.stdout

        class Completed:
            pass
        done = Completed()
        done.stdout = out
        return done

    def install(self, case):
        real = subprocess.run
        subprocess.run = self
        case.addCleanup(setattr, subprocess, "run", real)
        return self


class TestFrameGrab(unittest.TestCase):
    """One still from a file, for Review to show beside a StashDB cover.

    It needs ffmpeg and nothing else - no Pillow, no numpy - because it is a
    picture to look at, not a hash. Like every `safe_*` helper it never raises: a file
    that cannot be read answers with a reason.
    """

    JPEG = b"\xff\xd8\xff\xe0 a jpeg"

    def test_a_frame_is_one_jpeg_from_that_point_of_the_file(self):
        fake = FakeFfmpeg(stdout=self.JPEG).install(self)
        data, error = frames.safe_frame("D:\\a.mp4", 0.35, duration=100.0)
        self.assertEqual((data, error), (self.JPEG, None))
        args = fake.calls[-1]
        self.assertEqual(args[args.index("-ss") + 1], "35.000000")
        self.assertEqual(args[args.index("-frames:v") + 1], "1")
        self.assertEqual(args[args.index("-c:v") + 1], "mjpeg")
        self.assertEqual(args[-1], "pipe:1")

    def test_the_seek_and_the_thread_cap_come_before_the_input(self):
        # After `-i` they would apply to the output and quietly do nothing, and
        # a frame grab without the cap takes every core it can find.
        fake = FakeFfmpeg(stdout=self.JPEG).install(self)
        frames.safe_frame("D:\\a.mp4", 0.6, duration=100.0)
        args = fake.calls[-1]
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertLess(args.index("-threads"), args.index("-i"))

    def test_the_still_is_small_and_keeps_its_shape(self):
        # Fits a 480 x 270 box either way round: a portrait clip is not stretched
        # into a landscape one.
        fake = FakeFfmpeg(stdout=self.JPEG).install(self)
        frames.safe_frame("D:\\a.mp4", 0.1, duration=100.0)
        scale = fake.calls[-1][fake.calls[-1].index("-vf") + 1]
        self.assertIn("480", scale)
        self.assertIn("270", scale)
        self.assertIn("force_original_aspect_ratio=decrease", scale)

    def test_a_file_of_unknown_length_is_probed_first(self):
        fake = FakeFfmpeg(stdout=self.JPEG,
                          text_stdout=json.dumps({"format": {"duration": "200"}})
                          ).install(self)
        data, _ = frames.safe_frame("D:\\a.mp4", 0.35)
        self.assertEqual(data, self.JPEG)
        self.assertEqual(os.path.basename(fake.calls[0][0]), "ffprobe")
        self.assertEqual(fake.calls[-1][fake.calls[-1].index("-ss") + 1],
                         "70.000000")

    def test_a_length_that_cannot_be_read_takes_the_first_frame(self):
        fake = FakeFfmpeg(stdout=self.JPEG, text_stdout="not json").install(self)
        data, _ = frames.safe_frame("D:\\a.mp4", 0.85)
        self.assertEqual(data, self.JPEG)
        self.assertEqual(fake.calls[-1][fake.calls[-1].index("-ss") + 1],
                         "0.000000")

    def test_a_file_ffmpeg_cannot_read_is_a_reason_not_an_exception(self):
        FakeFfmpeg(fail_on=lambda args: True).install(self)
        data, error = frames.safe_frame("D:\\a.mp4", 0.1, duration=100.0)
        self.assertIsNone(data)
        self.assertTrue(error)

    def test_no_picture_out_of_ffmpeg_is_a_reason_too(self):
        FakeFfmpeg(stdout=b"").install(self)
        data, error = frames.safe_frame("D:\\a.mp4", 0.1, duration=100.0)
        self.assertIsNone(data)
        self.assertIn("no frame", error)

    def test_the_four_points_are_spread_through_the_file(self):
        self.assertEqual(frames.FRAME_POINTS, (0.10, 0.35, 0.60, 0.85))


if __name__ == "__main__":
    unittest.main()
