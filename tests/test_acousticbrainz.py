import json
import shutil
import subprocess
import typing
import unittest
from unittest import mock

from gbc.passes import acousticbrainz as ab
from tests.base import Base

# AB is keyed by MB recording UUIDs; run() now filters out anything that isn't UUID-shaped.
MB_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MB_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

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

    def test_value_bad_bpm_dropped(self):
        self.assertIsNone(ab._value("bpm", "not-a-number"))   # non-numeric bpm -> None -> dropped from payload
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
        """Drive run() with a fake `beet` (ls -> mbids) and a stubbed _fetch, reconstructing what was applied
        from the native `beet modify mb_trackid:<id> field=value ...` calls.

        Returns (n, calls, applied): n = recordings enriched, calls = every fake run_beet argv, applied =
        {mbid: {field: value_str}} parsed from the modify calls (values are the strings beet modify received).

        path_map: optional {mbid: filepath} for the flex-tag injection `ls -f $mb_trackid\\t$path` query.
        """
        calls = []

        def fake_run_beet(cfg, args, **k):
            calls.append(args)
            if args and args[0] == "ls":                  # run() lists "$mb_trackid\t$path" in one scoped query
                return 0, "\n".join(f"{m}\t{(path_map or {}).get(m, '/x/' + m + '.flac')}" for m in mbids)
            return 0, ""                                  # modify / write

        with mock.patch.object(ab, "run_beet", fake_run_beet), \
             mock.patch.object(ab, "_fetch", fetch):
            n = ab.run(self.cfg)

        applied: dict = {}
        for c in calls:
            if c and c[0] == "modify":
                mbid = next(a.split(":", 1)[1] for a in c if a.startswith("mb_trackid:"))
                applied[mbid] = dict(a.split("=", 1) for a in c if "=" in a)
        return n, calls, applied

    def test_enriches_present_and_caches(self):
        n, _, applied = self._run([MB_A, MB_B], lambda batch: {MB_A: DOC})  # only mbA known to AB
        self.assertEqual(n, 1)
        self.assertEqual(list(applied), [MB_A])          # exactly the one enriched recording
        self.assertEqual(applied[MB_A]["bpm"], "84")     # beet modify receives the rounded int (as a string)
        self.assertEqual(applied[MB_A]["initial_key"], "F#")
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text())
        self.assertIsNone(cache[MB_B])                   # confirmed absent -> cached as None
        self.assertEqual(cache[MB_A]["voice_instrumental"], "instrumental")

    def test_network_failure_not_cached(self):
        n, _, applied = self._run([MB_A], lambda batch: None)  # AB unreachable -> pending, not cached
        self.assertEqual(n, 0)
        self.assertFalse(applied)                          # nothing handed to the bulk writer
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text() or "{}") \
            if (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").exists() else {}
        self.assertNotIn(MB_A, cache)                    # left uncached -> retried next run

    def test_uses_cache_without_refetch(self):
        (self.cfg.beetsdir).mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").write_text(
            json.dumps({MB_A: {"bpm": 90, "gender": "male"}}))

        def boom(batch):
            raise AssertionError("should not fetch a cached mbid")

        n, _, applied = self._run([MB_A], boom)
        self.assertEqual(n, 1)
        self.assertEqual(applied[MB_A]["bpm"], "90")

    def test_no_mbids_in_scope(self):
        n, _, applied = self._run([], lambda batch: {})
        self.assertEqual(n, 0)
        self.assertFalse(applied)

    def test_non_uuid_ids_dropped_before_batching(self):
        """A Discogs id ('14266022-1') makes AB 400 the WHOLE batch -> it must be dropped before batching so
        it never poisons its co-batched UUIDs (which would then cache as absent)."""
        seen = []

        def fetch(batch):
            seen.extend(batch)
            return {MB_A: DOC}

        _, _, applied = self._run([MB_A, "14266022-1"], fetch)
        self.assertEqual(seen, [MB_A])                    # Discogs id never handed to AB
        self.assertEqual(list(applied), [MB_A])
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text())
        self.assertNotIn("14266022-1", cache)             # not even cached as absent

    def test_flex_tags_use_paths_captured_before_apply(self):
        """Regression: file-tag injection must use the paths captured in the FIRST query, not a re-query AFTER
        the bpm write -- a scope like '^bpm:1..' would otherwise match 0 rows post-write and tag 0 files."""
        f = self.tmp / "song.flac"
        f.write_bytes(b"x")                               # real file so Path(path).is_file() is True
        written = []
        with mock.patch.object(ab, "_write_file_tags",
                               lambda p, flex, log: (written.append((p, flex)), True)[1]):
            self._run([MB_A], lambda batch: {MB_A: DOC}, path_map={MB_A: str(f)})
        self.assertEqual([p for p, _ in written], [str(f)])           # tagged the captured path
        self.assertIn("voice_instrumental", written[0][1])           # a curated flex attr reached the writer
        self.assertNotIn("bpm", written[0][1])                       # bpm is a native field, not a flex tag here

    def test_flex_tag_ls_query_is_scoped_not_per_id_or(self):
        """run() lists paths for tagging via the SAME scoped `mb_trackid::.` query, not a
        `mb_trackid:<id>,...` OR of every modified id (which overflows MAX_ARG_STRLEN on a full run)."""
        path_map = {MB_A: "/nonexistent/path.flac"}
        _, calls, _ = self._run([MB_A], lambda batch: {MB_A: DOC}, path_map=path_map)
        flex_ls = [c for c in calls if c and c[0] == "ls" and any("$path" in str(a) for a in c)]
        self.assertEqual(len(flex_ls), 1)
        self.assertIn("mb_trackid::.", flex_ls[0])                               # scoped query reused
        self.assertFalse(any(f"mb_trackid:{MB_A}" in str(a) for a in flex_ls[0]))    # NOT a per-id OR

    def test_mutagen_absent_does_not_crash(self):
        """When mutagen is not installed, run() still succeeds (flex attrs stay db-only)."""
        with mock.patch("importlib.util.find_spec", return_value=None):
            n, _, applied = self._run([MB_A], lambda batch: {MB_A: DOC})
        self.assertEqual(n, 1)
        self.assertIn(MB_A, applied)


class TestRunWatermarkHold(Base):
    """A failed scope query must ABORT the pass (RuntimeError) so the pipeline HOLDS the watermark, rather than
    look like an empty scope (return 0) and let the un-enriched window be silently skipped."""

    def test_failed_scope_query_raises_instead_of_returning_zero(self):
        def fake_run_beet(cfg, args, **k):
            if k.get("check"):                        # the scope query -- real run_beet RAISES here on rc=2
                raise RuntimeError("beet 'ls ...' failed (rc=2, pass=acousticbrainz)")
            return 0, ""
        with mock.patch.object(ab, "run_beet", fake_run_beet), self.assertRaises(RuntimeError):
            ab.run(self.cfg, scope="added:2024-01..")


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


class TestApply(Base):
    """_apply emits one native `beet modify mb_trackid:<id> field=value ...` per recording (DB + file write +
    failure logging are beets' own); bad-bpm fields are dropped, empty recordings skipped."""

    def test_builds_modify_per_recording_with_rounded_bpm(self):
        calls = []
        with mock.patch.object(ab, "run_beet", lambda cfg, a, **k: calls.append(a) or (0, "")):
            applied, failed = ab._apply(self.cfg, {MB_A: {"bpm": 83.735, "initial_key": "F#m",
                                                          "mood_happy": 0.05}}, mock.MagicMock())
        self.assertEqual((applied, failed), (1, 0))
        cmd = next(c for c in calls if c and c[0] == "modify")
        self.assertEqual(cmd[:4], ["modify", "-y", "-M", f"mb_trackid:{MB_A}"])   # nomove, scoped by recording
        self.assertIn("bpm=84", cmd)                       # rounded int
        self.assertIn("initial_key=F#m", cmd)              # minor key, sharp preserved
        self.assertIn("mood_happy=0.05", cmd)

    def test_drops_bad_bpm_and_counts_failures(self):
        calls = []
        with mock.patch.object(ab, "run_beet", lambda cfg, a, **k: calls.append(a) or (1, "")):  # modify "fails"
            applied, failed = ab._apply(self.cfg, {MB_A: {"bpm": "nope", "initial_key": "C"}}, mock.MagicMock())
        cmd = next(c for c in calls if c and c[0] == "modify")
        self.assertNotIn("bpm=nope", cmd)                  # non-numeric bpm dropped, not passed to beets
        self.assertNotIn("bpm=None", cmd)
        self.assertIn("initial_key=C", cmd)                # the good field still applied
        self.assertEqual((applied, failed), (0, 1))        # rc!=0 -> counted as failed

    def test_recording_with_only_bad_bpm_is_skipped(self):
        calls = []
        with mock.patch.object(ab, "run_beet", lambda cfg, a, **k: calls.append(a) or (0, "")):
            applied, failed = ab._apply(self.cfg, {MB_A: {"bpm": "nope"}}, mock.MagicMock())
        self.assertEqual((applied, failed), (0, 0))
        self.assertFalse(any(c and c[0] == "modify" for c in calls))   # no valid field -> no modify call


if __name__ == "__main__":
    unittest.main()
