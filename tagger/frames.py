"""Stills of the user's own files, for Review to set beside a StashDB cover.

Pronoisseur's Local API hands out no poster, so the tool takes its own: one
frame at each of :data:`FRAME_POINTS`, taken the first time Review
asks for it and kept on disk beside the state database. The file is named by
the video's content hash, so a re-scan finds the same frame again, and by the
video id when Pronoisseur gave no hash.

Only files in the current scan can be asked for, by id: nothing here takes a
path from the browser.
"""

import json
import ntpath
import os
import re
import subprocess
import threading
import time

from .library_common import LibraryStateError

#: Hard ceiling on every ffmpeg/ffprobe call, in seconds. It exists for a **stalled
#: share, not for a slow file**: ffmpeg blocked in uninterruptible I/O on a dropped
#: share never returns, and Python cannot kill the background thread running it, so
#: one hung file would hang the whole run and its cancel button. 60 is far above
#: any honest read.
SUBPROCESS_TIMEOUT = 60

#: Threads per ffmpeg call. Review can ask for several stills at once, and
#: ffmpeg's default is every core it can find, so a screenful of cards would take
#: over the machine. Two keeps a 1080p HEVC seek off the critical path.
#:
#: It changes no output pixel: thread count is a decoder scheduling choice, not a
#: filtering one.
FFMPEG_THREADS = "2"

#: Windows only: ffmpeg and ffprobe start below normal so the cores they *do* use
#: still yield to whatever the user is doing. Empty everywhere else —
#: ``creationflags`` is a Windows-only argument and a non-zero one elsewhere is a
#: ValueError, so the dict is spread into the call rather than passed as a zero.
PRIORITY_KWARGS = ({"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}
                   if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS") else {})


def _run(args, **kwargs):
    """Every ffmpeg/ffprobe call in this module goes through here.

    One place for the two things every one of them needs: the stalled-share
    timeout, and below-normal priority on Windows. It calls ``subprocess.run``
    by attribute so the suite's existing monkeypatches still bite.
    """
    kwargs.setdefault("timeout", SUBPROCESS_TIMEOUT)
    for key, value in PRIORITY_KWARGS.items():
        kwargs.setdefault(key, value)
    return subprocess.run(args, **kwargs)


def probe_duration(path, ffprobe="ffprobe"):
    """Duration in seconds via ffprobe, or None if it cannot be read."""
    args = [ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "json", path]
    try:
        out = _run(args, capture_output=True, text=True, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        # TimeoutExpired is a SubprocessError, so a stalled share reads here as
        # "duration unknown" — the same row-level note as any other probe failure.
        return None


#: Where Review's stills of the user's own file are taken, as fractions of its
#: length. Four, spread through the file and clear of the first and last tenth,
#: where intros and credits look alike from one scene to the next.
FRAME_POINTS = (0.10, 0.35, 0.60, 0.85)

#: Fits a 480 x 270 box without changing shape, so a portrait clip stays
#: portrait. Small on purpose: it sits beside a StashDB cover of the same size.
FRAME_SCALE = "scale=w=480:h=270:force_original_aspect_ratio=decrease"


def frame_jpeg(path, fraction, duration=None, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """One still at ``fraction`` of the file's length, as JPEG bytes.

    Needs ffmpeg and nothing else: it is a picture to look at. No hardware decode
    either — one frame is not worth the probe. A length that cannot be read takes
    the first frame rather than none.
    """
    if duration is None:
        duration = probe_duration(path, ffprobe)
    at = float(fraction) * float(duration) if duration and duration > 0 else 0.0
    args = [ffmpeg, "-v", "error", "-threads", FFMPEG_THREADS,
            "-ss", "%.6f" % at, "-i", path, "-frames:v", "1", "-vf", FRAME_SCALE,
            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"]
    data = _run(args, capture_output=True, check=True).stdout
    if not data:
        raise ValueError("ffmpeg produced no frame at %.2fs of %s" % (at, path))
    return data


def safe_frame(path, fraction, duration=None, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """``(jpeg bytes, None)`` or ``(None, message)`` — never raises."""
    try:
        return frame_jpeg(path, fraction, duration, ffmpeg, ffprobe), None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, str(exc)


#: The folder, beside the state database, that holds the kept frames.
FRAMES_DIR_NAME = "tagger-frames"

#: ffmpeg runs at once for stills. Review asks for a frame per card on screen,
#: and every one of them is a read over the share, which a scan may be reading
#: too.
FRAME_SLOTS = 2

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: Seconds a failure is remembered: a frame that could not be taken, and a
#: drive that was not there. Long enough that stepping through a card's frames,
#: or a page full of cards on an offline network drive, costs one look; short
#: enough that a drive reconnected now shows its frames a minute later.
FRAME_RETRY_SECS = 60

#: What Pronoisseur's catalog calls a file it did not find on its last scan.
UNAVAILABLE = "unavailable"

UNAVAILABLE_NOTE = ("Pronoisseur lists this file as unavailable - it was not "
                    "found on Pronoisseur's last scan")
UNREACHABLE_NOTE = ("%s is not reachable - if it is a network drive, connect it "
                    "and the frames will load")
MISSING_NOTE = "the file is not at %s"


def _drive(path):
    """The drive a Windows path lives on: ``Z:``, or ``\\\\server\\share``.

    Pronoisseur is a Windows app, so its paths are Windows paths whatever this
    runs on, and ``ntpath`` reads them the same everywhere.
    """
    return ntpath.splitdrive(str(path or ""))[0]


class FramesMixin:
    def _init_frames(self, framer, frames_dir, path_exists=None, clock=None):
        self._framer = framer
        self._frames_dir = frames_dir
        self._frame_slots = threading.BoundedSemaphore(FRAME_SLOTS)
        # Injected so tests decide what exists and what time it is; the real
        # thing is the disk and a monotonic clock.
        self._path_exists = path_exists or os.path.exists
        self._clock = clock or time.monotonic
        self._frame_memory_lock = threading.Lock()
        self._frame_failures = {}      # (key, n) -> (until, message)
        self._drives_away = {}         # drive -> until

    def frames_dir(self):
        """Where kept frames live: ``tagger-frames`` beside the state database."""
        if self._frames_dir:
            return self._frames_dir
        base = os.path.dirname(os.path.abspath(self.library_store.path))
        return os.path.join(base, FRAMES_DIR_NAME)

    def frame(self, video_id, n):
        """JPEG bytes of frame ``n`` of one scanned file, taken once and kept.

        A point outside :data:`FRAME_POINTS` is a ``ValueError``,
        refused before ffmpeg runs. A file that is not in the scan, that cannot
        be reached, or that ffmpeg cannot read, is a :class:`LibraryStateError`
        saying which. A failure is never kept on disk, only remembered for
        :data:`FRAME_RETRY_SECS`, because a share that was asleep the first time
        often is not a minute later.

        A kept frame is shown first, whatever the drive is doing: it was taken
        while the file could be read, and it is still a picture of that file.

        Before ffmpeg runs, three cheap answers: Pronoisseur already lists the
        file as unavailable; its drive was found missing a moment ago; or the
        file is not there, and then whether its drive is - one look at a drive
        that has gone offline covers every file on it.
        """
        n = int(n)
        if not 0 <= n < len(FRAME_POINTS):
            raise ValueError("frame must be 0 to %d; got %d"
                             % (len(FRAME_POINTS) - 1, n))
        rows = self._rows          # bound once: a scan may replace the list
        video = rows[self._row_index(rows, video_id)]["video"]
        key = self._frame_key(video)
        target = os.path.join(self.frames_dir(), "%s-%d.jpg" % (key, n))
        try:
            with open(target, "rb") as fh:
                return fh.read()
        except OSError:
            pass
        remembered = self._remembered_failure(key, n)
        if remembered:
            raise LibraryStateError(remembered)
        try:
            data = self._take_frame(video, n)
        except LibraryStateError as exc:
            self._remember_failure(key, n, str(exc))
            raise
        self._keep_frame(target, data)
        return data

    def _take_frame(self, video, n):
        """The file's still, or a :class:`LibraryStateError` saying why not."""
        path = video.get("path") or ""
        if video.get("availability") == UNAVAILABLE:
            raise LibraryStateError(UNAVAILABLE_NOTE)
        drive = _drive(path)
        if drive and self._drive_away(drive):
            raise LibraryStateError(UNREACHABLE_NOTE % drive)
        if not self._path_exists(path):
            if drive and not self._path_exists(drive + "\\"):
                self._mark_drive_away(drive)
                raise LibraryStateError(UNREACHABLE_NOTE % drive)
            raise LibraryStateError(MISSING_NOTE % path)
        with self._frame_slots:
            data, error = self._framer(
                path, FRAME_POINTS[n],
                duration=video.get("duration_secs") or None,
                ffmpeg=self.cfg.get("ffmpeg_path", "ffmpeg"),
                ffprobe=self.cfg.get("ffprobe_path", "ffprobe"))
        if not data:
            raise LibraryStateError("no frame from this file: %s"
                                    % (error or "ffmpeg returned nothing"))
        return data

    def _remembered_failure(self, key, n):
        with self._frame_memory_lock:
            until, message = self._frame_failures.get((key, n), (0, None))
            return message if self._clock() < until else None

    def _remember_failure(self, key, n, message):
        with self._frame_memory_lock:
            self._frame_failures[(key, n)] = (self._clock() + FRAME_RETRY_SECS,
                                              message)

    def _drive_away(self, drive):
        with self._frame_memory_lock:
            return self._clock() < self._drives_away.get(drive.lower(), 0)

    def _mark_drive_away(self, drive):
        with self._frame_memory_lock:
            self._drives_away[drive.lower()] = self._clock() + FRAME_RETRY_SECS

    @staticmethod
    def _frame_key(video):
        key = _UNSAFE.sub("", str(video.get("content_hash") or ""))
        return key or "v%d" % int(video.get("id") or 0)

    @staticmethod
    def _keep_frame(target, data):
        """Write through a temporary name, so a half-written file is never read."""
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            partial = "%s.%d.tmp" % (target, threading.get_ident())
            with open(partial, "wb") as fh:
                fh.write(data)
            os.replace(partial, target)
        except OSError:
            # Keeping it is a saving, not a requirement: the frame still goes back
            # to the page, and the next ask takes it again.
            pass
