import json
import shutil
import subprocess
import typing
import unittest
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

    def test_assign_formats_bpm_and_floats(self):
        self.assertEqual(ab._assign("bpm", 83.735), "bpm=84")            # rounded int media field
        self.assertEqual(ab._assign("danceable", 0.8763), "danceable=0.876300")
        self.assertEqual(ab._assign("initial_key", "F# major"), "initial_key=F# major")


class TestRun(Base):
    def _run(self, mbids, fetch, path_map=None):
        """Drive run() with a fake `beet` (ls -> mbids, modify -> recorded) and a stubbed _fetch.

        path_map: optional {mbid: filepath} for the flex-tag injection `ls -f $mb_trackid\\t$path` query.
        When provided, the second ls call returns tab-separated mbid+path lines; otherwise it returns
        bare mbids (the old behaviour, which still passes because os.path.isfile rejects non-existent paths).
        """
        calls = []

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

        with mock.patch.object(ab, "run_beet", fake_run_beet), \
             mock.patch.object(ab, "_fetch", fetch):
            n = ab.run(self.cfg)
        return n, calls

    def test_enriches_present_and_caches(self):
        n, calls = self._run(["mbA", "mbB"], lambda batch: {"mbA": DOC})  # only mbA known to AB
        self.assertEqual(n, 1)
        modifies = [c for c in calls if c and c[0] == "modify"]
        self.assertEqual(len(modifies), 1)
        self.assertEqual(modifies[0][:3], ["modify", "-y", "mb_trackid:mbA"])
        self.assertIn("bpm=84", modifies[0])
        self.assertIn("initial_key=F#", modifies[0])
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text())
        self.assertIsNone(cache["mbB"])                   # confirmed absent -> cached as None
        self.assertEqual(cache["mbA"]["voice_instrumental"], "instrumental")

    def test_network_failure_not_cached(self):
        n, calls = self._run(["mbA"], lambda batch: None)  # AB unreachable -> pending, not cached
        self.assertEqual(n, 0)
        self.assertFalse([c for c in calls if c and c[0] == "modify"])
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text() or "{}") \
            if (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").exists() else {}
        self.assertNotIn("mbA", cache)                    # left uncached -> retried next run

    def test_uses_cache_without_refetch(self):
        (self.cfg.beetsdir).mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").write_text(
            json.dumps({"mbA": {"bpm": 90, "gender": "male"}}))

        def boom(batch):
            raise AssertionError("should not fetch a cached mbid")

        n, calls = self._run(["mbA"], boom)
        self.assertEqual(n, 1)
        self.assertIn("bpm=90", next(c for c in calls if c and c[0] == "modify"))

    def test_no_mbids_in_scope(self):
        n, _ = self._run([], lambda batch: {})
        self.assertEqual(n, 0)

    def test_flex_tag_ls_query_uses_comma_separated_mbids(self):
        """After modify, run() issues a batch `ls -f $mb_trackid\\t$path` with comma-joined mbids."""
        path_map = {"mbA": "/nonexistent/path.flac"}
        _, calls = self._run(["mbA"], lambda batch: {"mbA": DOC}, path_map=path_map)
        flex_ls = [c for c in calls if c and c[0] == "ls" and any("\\t" in str(a) or "\t" in str(a) for a in c)]
        self.assertEqual(len(flex_ls), 1)
        query_arg = next(a for a in flex_ls[0] if "mb_trackid:" in str(a))
        self.assertIn("mb_trackid:mbA", query_arg)

    def test_mutagen_absent_does_not_crash(self):
        """When mutagen is not installed, run() still succeeds (flex attrs stay db-only)."""
        with mock.patch("importlib.util.find_spec", return_value=None):
            n, calls = self._run(["mbA"], lambda batch: {"mbA": DOC})
        self.assertEqual(n, 1)
        self.assertTrue(any(c and c[0] == "modify" for c in calls))


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


if __name__ == "__main__":
    unittest.main()
