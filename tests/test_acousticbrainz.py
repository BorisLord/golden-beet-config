import importlib.util
import json
import shutil
import subprocess
import sys
import typing
import unittest
from pathlib import Path
from unittest import mock

from gbc.passes import acousticbrainz as ab
from tests.base import Base

# A merged low+high-level AB document (the shape ABSCHEME maps against). Includes fields we deliberately
# DROP (gender, average_loudness) to prove the curated scheme ignores them.
DOC = {
    "highlevel": {
        "danceability": {"all": {"danceable": 0.8763, "not_danceable": 0.1237}, "value": "not_danceable"},
        "gender": {"value": "female", "all": {"female": 0.65, "male": 0.35}},
        "mood_happy": {"all": {"happy": 0.05, "not_happy": 0.95}, "value": "not_happy"},
        "voice_instrumental": {"value": "instrumental"},
    },
    "lowlevel": {"average_loudness": 0.0145},
    "rhythm": {"bpm": 83.735},
    "tonal": {"key_key": "F#", "key_scale": "major", "key_strength": 0.71},
}


class TestMapping(unittest.TestCase):
    def test_maps_curated_fields(self):
        f = ab._fields_for(DOC)
        self.assertEqual(f["danceable"], 0.8763)          # "all" leaf -> positive-class probability
        self.assertEqual(f["mood_happy"], 0.05)
        self.assertEqual(f["voice_instrumental"], "instrumental")
        self.assertEqual(f["bpm"], 83.735)
        self.assertEqual(f["initial_key"], "F#")          # beets-canonical key (major root, sharp kept)
        self.assertEqual(f["key_strength"], 0.71)
        self.assertNotIn("not_danceable", f)              # only scheme leaves are kept
        self.assertNotIn("gender", f)                     # curated out (not musically useful)
        self.assertNotIn("average_loudness", f)           # curated out (redundant with ReplayGain)

    def test_minor_key_canonical(self):
        doc = {"tonal": {"key_key": "C", "key_scale": "minor"}}
        self.assertEqual(ab._fields_for(doc)["initial_key"], "Cm")

    def test_value_rounds_bpm_keeps_others(self):
        self.assertEqual(ab._value("bpm", 83.735), 84)                   # rounded int media field
        self.assertEqual(ab._value("danceable", 0.8763), 0.8763)        # float kept as-is
        self.assertEqual(ab._value("initial_key", "F#m"), "F#m")        # str kept as-is

    def test_value_bad_bpm_falls_back(self):
        self.assertEqual(ab._value("bpm", "not-a-number"), "not-a-number")  # must not raise / abort the batch
        self.assertIsNone(ab._value("bpm", None))


class TestFetch(unittest.TestCase):
    def _raise(self, code):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return boom

    def test_http_4xx_returns_partial_not_none(self):
        # a malformed/absent id (4xx) must NOT poison the batch forever -> partial result, caller caches absent
        with mock.patch.object(ab.urllib.request, "urlopen", self._raise(400)):
            self.assertEqual(ab._fetch(["badid"]), {})

    def test_http_5xx_returns_none_for_retry(self):
        with mock.patch.object(ab.urllib.request, "urlopen", self._raise(503)):
            self.assertIsNone(ab._fetch(["x"]))             # transient -> None -> retried next run


class TestRun(Base):
    def _run(self, mbids, fetch, path_map=None):
        """Drive run() with a fake `beet` (ls -> mbids) and a stubbed _fetch, capturing the batch handed to
        _bulk_apply (the in-process replacement for the per-recording `beet modify`).

        Returns (n, calls, applied): n = recordings enriched, calls = every fake run_beet argv, applied =
        the {mbid: fields} dict _bulk_apply would have written.

        path_map: optional {mbid: filepath} for the flex-tag injection `ls -f $mb_trackid\\t$path` query.
        When provided, the second ls call returns tab-separated mbid+path lines; otherwise bare mbids.
        """
        calls = []
        applied: dict = {}

        def fake_run_beet(cfg, args, **k):
            calls.append(args)
            if args and args[0] == "ls":
                fmt = args[2] if len(args) > 2 else ""
                if "\\t" in fmt or "\t" in fmt:
                    if path_map:
                        return 0, "\n".join(f"{m}\t{path_map[m]}" for m in mbids if m in path_map)
                    return 0, ""
                return 0, "\n".join(mbids)
            return 0, ""

        def fake_bulk(cfg, modified, log):
            applied.update(modified)

        with mock.patch.object(ab, "run_beet", fake_run_beet), \
             mock.patch.object(ab, "_fetch", fetch), \
             mock.patch.object(ab, "_bulk_apply", fake_bulk):
            n = ab.run(self.cfg)
        return n, calls, applied

    def test_enriches_present_and_caches(self):
        n, _, applied = self._run(["mbA", "mbB"], lambda batch: {"mbA": DOC})  # only mbA known to AB
        self.assertEqual(n, 1)
        self.assertEqual(list(applied), ["mbA"])          # exactly the one enriched recording
        self.assertEqual(applied["mbA"]["bpm"], 83.735)   # raw value (rounding happens inside _bulk_apply)
        self.assertEqual(applied["mbA"]["initial_key"], "F#")
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text())
        self.assertIsNone(cache["mbB"])                   # confirmed absent -> cached as None
        self.assertEqual(cache["mbA"]["voice_instrumental"], "instrumental")

    def test_network_failure_not_cached(self):
        n, _, applied = self._run(["mbA"], lambda batch: None)  # AB unreachable -> pending, not cached
        self.assertEqual(n, 0)
        self.assertFalse(applied)                          # nothing handed to the bulk writer
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text() or "{}") \
            if (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").exists() else {}
        self.assertNotIn("mbA", cache)                    # left uncached -> retried next run

    def test_uses_cache_without_refetch(self):
        (self.cfg.beetsdir).mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").write_text(
            json.dumps({"mbA": {"bpm": 90, "gender": "male"}}))

        def boom(batch):
            raise AssertionError("should not fetch a cached mbid")

        n, _, applied = self._run(["mbA"], boom)
        self.assertEqual(n, 1)
        self.assertEqual(applied["mbA"]["bpm"], 90)

    def test_no_mbids_in_scope(self):
        n, _, applied = self._run([], lambda batch: {})
        self.assertEqual(n, 0)
        self.assertFalse(applied)

    def test_flex_tag_ls_query_is_scoped_not_per_id_or(self):
        """run() lists paths for tagging via the SAME scoped `mb_trackid::.` query, not a
        `mb_trackid:<id>,...` OR of every modified id (which overflows MAX_ARG_STRLEN on a full run)."""
        path_map = {"mbA": "/nonexistent/path.flac"}
        _, calls, _ = self._run(["mbA"], lambda batch: {"mbA": DOC}, path_map=path_map)
        flex_ls = [c for c in calls if c and c[0] == "ls" and any("$path" in str(a) for a in c)]
        self.assertEqual(len(flex_ls), 1)
        self.assertIn("mb_trackid::.", flex_ls[0])                               # scoped query reused
        self.assertFalse(any("mb_trackid:mbA" in str(a) for a in flex_ls[0]))    # NOT a per-id OR

    def test_mutagen_absent_does_not_crash(self):
        """When mutagen is not installed, run() still succeeds (flex attrs stay db-only)."""
        with mock.patch("importlib.util.find_spec", return_value=None):
            n, _, applied = self._run(["mbA"], lambda batch: {"mbA": DOC})
        self.assertEqual(n, 1)
        self.assertIn("mbA", applied)


_FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(_FFMPEG, "ffmpeg not available")
class TestWriteFileTags(Base):
    """Unit tests for _write_file_tags using real audio files created by ffmpeg."""

    _FLEX: typing.ClassVar[dict] = {"mood_relaxed": 0.95, "danceable": 0.42, "voice_instrumental": "vocal"}

    def _make(self, fmt):
        """Create a 0.1s silent audio file; return its path."""
        p = str(self.tmp / f"test.{fmt}")
        if fmt == "flac":
            subprocess.run([_FFMPEG, "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=mono", "-t", "0.1", "-c:a", "flac", p],
                           capture_output=True, check=True)
        elif fmt == "mp3":
            subprocess.run([_FFMPEG, "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=mono", "-t", "0.1", "-c:a", "libmp3lame", p],
                           capture_output=True, check=True)
        elif fmt == "m4a":
            subprocess.run([_FFMPEG, "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=mono", "-t", "0.1", "-c:a", "aac", p],
                           capture_output=True, check=True)
        elif fmt == "opus":
            subprocess.run([_FFMPEG, "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=mono", "-t", "0.1", "-c:a", "libopus", p],
                           capture_output=True, check=True)
        return p

    def _log(self):
        return mock.MagicMock()

    def test_flac_vorbis_comments(self):
        p = self._make("flac")
        self.assertTrue(ab._write_file_tags(p, self._FLEX, self._log()))
        from mutagen.flac import FLAC
        tags = FLAC(p)
        self.assertEqual(tags["mood_relaxed"], ["0.95"])
        self.assertEqual(tags["danceable"], ["0.42"])
        self.assertEqual(tags["voice_instrumental"], ["vocal"])

    def test_mp3_txxx_frames(self):
        p = self._make("mp3")
        self.assertTrue(ab._write_file_tags(p, self._FLEX, self._log()))
        from mutagen.id3 import ID3
        tags = ID3(p)
        self.assertEqual(str(tags["TXXX:mood_relaxed"]), "0.95")
        self.assertEqual(str(tags["TXXX:danceable"]), "0.42")
        self.assertEqual(str(tags["TXXX:voice_instrumental"]), "vocal")

    def test_m4a_freeform_atoms(self):
        p = self._make("m4a")
        self.assertTrue(ab._write_file_tags(p, self._FLEX, self._log()))
        from mutagen.mp4 import MP4
        tags = MP4(p)
        self.assertEqual(tags["----:com.apple.itunes:mood_relaxed"], [b"0.95"])
        self.assertEqual(tags["----:com.apple.itunes:danceable"], [b"0.42"])
        self.assertEqual(tags["----:com.apple.itunes:voice_instrumental"], [b"vocal"])

    def test_opus_vorbis_comments(self):
        p = self._make("opus")
        self.assertTrue(ab._write_file_tags(p, self._FLEX, self._log()))
        from mutagen.oggopus import OggOpus
        tags = OggOpus(p)
        self.assertEqual(tags["mood_relaxed"], ["0.95"])
        self.assertEqual(tags["danceable"], ["0.42"])
        self.assertEqual(tags["voice_instrumental"], ["vocal"])

    def test_unsupported_format_returns_false(self):
        p = str(self.tmp / "test.wav")
        subprocess.run([_FFMPEG, "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", "0.1", p],
                       capture_output=True, check=True)
        self.assertFalse(ab._write_file_tags(p, self._FLEX, self._log()))

    def test_idempotent_rewrite(self):
        """Writing the same flex attrs twice doesn't duplicate TXXX frames."""
        p = self._make("mp3")
        ab._write_file_tags(p, {"mood_relaxed": 0.5}, self._log())
        ab._write_file_tags(p, {"mood_relaxed": 0.9}, self._log())
        from mutagen.id3 import ID3
        tags = ID3(p)
        txxx_frames = [k for k in tags if k.startswith("TXXX:mood_relaxed")]
        self.assertEqual(len(txxx_frames), 1)
        self.assertEqual(str(tags["TXXX:mood_relaxed"]), "0.9")


class TestBeetsPython(Base):
    """_beets_python resolves the BEETS venv's python from the `beet` entry point's shebang."""

    def test_reads_shebang(self):
        py = self.tmp / "py3"
        py.write_text("#!/bin/sh\n")
        beet = self.tmp / "beet"
        beet.write_text(f"#!{py}\nprint('hi')\n")
        self.assertEqual(ab._beets_python(str(beet)), str(py))

    def test_falls_back_when_unreadable(self):
        self.assertEqual(ab._beets_python("/no/such/beet-xyz"), "python3")


def _beets_importable() -> bool:
    # NB: gate on the `beets.library` submodule, not `beets` -- the repo's own `beets/` config dir makes
    # find_spec("beets") resolve to a (library-less) namespace package whenever cwd is the repo root.
    try:
        return importlib.util.find_spec("beets.library") is not None
    except (ImportError, AttributeError, ValueError):
        return False


@unittest.skipUnless(_beets_importable(), "beets not importable in this venv")
class TestBulkApply(Base):
    """Integration test for _ab_bulk.py: applies fields to real beets items in ONE process (the in-process
    replacement for ~N `beet modify`). Skipped unless beets is importable (run via
    `uv run --with beets python -m unittest`)."""

    def test_applies_fields_to_every_item_of_a_recording(self):
        from beets.library import Item, Library
        db = str(self.cfg.library)
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        lib = Library(db)
        for rec, title in [("rec-1", "A"), ("rec-1", "A (live)"), ("rec-2", "B")]:   # rec-1 on two albums
            it = Item(mb_trackid=rec, title=title)
            it.path = f"/tmp/gbc-nonexistent-{title}.mp3".encode()                    # no real file: try_write no-ops
            lib.add(it)
        del lib

        jp = self.cfg.beetsdir / "fields.json"
        jp.write_text(json.dumps({"rec-1": {"bpm": 84, "mood_happy": 0.05, "initial_key": "F#"}}))
        script = Path(ab.__file__).with_name("_ab_bulk.py")
        out = subprocess.run([sys.executable, str(script), db, str(jp)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "2")                  # both rec-1 items, not rec-2

        items = {(i.mb_trackid, i.title): i for i in Library(db).items()}
        self.assertEqual(items[("rec-1", "A")].bpm, 84)            # native int field
        self.assertEqual(str(items[("rec-1", "A")].initial_key), "F#")             # MusicalKey keeps the sharp
        self.assertEqual(float(items[("rec-1", "A (live)")]["mood_happy"]), 0.05)  # flex attr on the 2nd item
        self.assertEqual(items[("rec-2", "B")].bpm, 0)            # untouched (no fields for rec-2)


if __name__ == "__main__":
    unittest.main()
