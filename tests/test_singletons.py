import contextlib
import unittest
from unittest import mock

from gbc.passes import singletons
from tests.base import Base


class TestSingletons(Base):
    def _patches(self, calls, counts=(0, 0), rc=0):
        return [
            mock.patch.object(singletons.artfix, "run", lambda *a, **k: 0),
            mock.patch.object(singletons, "backup_db", lambda *a, **k: None),
            mock.patch.object(singletons, "count_items", lambda *a, **k: next(counts)),
            mock.patch.object(singletons, "run_beet", lambda c, a, **k: calls.append(a) or (rc, "")),
            mock.patch.object(singletons, "_promote_complete", lambda *a, **k: 0),   # tested separately
            mock.patch.object(singletons.nova, "reroute", lambda *a, **k: 0),        # tested in test_nova
        ]

    def test_missing_source_returns_1(self):
        self.assertEqual(singletons.run(self.cfg, src=self.tmp / "nope"), 1)

    def test_imports_singletons_incremental_by_default(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        calls = []
        with self._stack(self._patches(calls, counts=iter([5, 8]))):
            rc = singletons.run(self.cfg, apply=True)
        self.assertEqual(rc, 0)
        imp = next(c for c in calls if c and c[0] == "import")
        self.assertIn("-s", imp)                         # singleton mode
        self.assertIn("-i", imp)                         # incremental by default
        self.assertEqual(imp[-1], str(self.cfg.src))     # imports the source dir

    def test_reimport_uses_noincremental(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        calls = []
        with self._stack(self._patches(calls, counts=iter([0, 0]))):
            singletons.run(self.cfg, reimport=True, apply=True)
        imp = next(c for c in calls if c and c[0] == "import")
        self.assertIn("-I", imp)                         # --reimport -> noincremental
        self.assertNotIn("-i", imp)

    def test_beet_import_failure_propagates(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        calls = []
        with self._stack(self._patches(calls, counts=iter([0, 0]), rc=2)):
            self.assertEqual(singletons.run(self.cfg, apply=True), 2)

    def test_also_imports_imposters_when_present(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        (self.cfg.dump / "imposters").mkdir(parents=True, exist_ok=True)
        calls = []
        with self._stack(self._patches(calls, counts=iter([0, 0]))):
            singletons.run(self.cfg, apply=True)
        imports = [c for c in calls if c and c[0] == "import"]
        self.assertEqual(len(imports), 2)                                 # source + quarantine/imposters
        self.assertEqual(imports[0][-1], str(self.cfg.src))
        self.assertEqual(imports[1][-1], str(self.cfg.dump / "imposters"))

    def test_imposters_skipped_when_absent(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)                   # no dump/imposters dir
        calls = []
        with self._stack(self._patches(calls, counts=iter([0, 0]))):
            singletons.run(self.cfg, apply=True)
        self.assertEqual(len([c for c in calls if c and c[0] == "import"]), 1)   # source only

    # --- _fingerprint_retag: AcoustID-first identity + dedup of loose tracks/imposters before re-import ---

    @staticmethod
    def _results(rid="mbTrue", title="TrueTitle", artist="TrueArtist", extra_ids=()):
        recs = [{"id": i, "title": title, "artists": [{"name": artist}]} for i in (rid, *extra_ids)]
        return [{"score": 0.95, "recordings": recs}]

    def test_fingerprint_retag_writes_true_recording_on_apply(self):
        from gbc.passes import verify as verifymod
        d = self.cfg.dump / "imposters"
        (d / "A" / "Alb (2001)").mkdir(parents=True, exist_ok=True)
        (d / "A" / "Alb (2001)" / "01 - wrong.flac").write_bytes(b"x")
        saved = {}

        class FakeMF:
            def __init__(self, path):
                self.title = self.artist = self.mb_trackid = None

            def save(self):
                saved.update(title=self.title, artist=self.artist, mb_trackid=self.mb_trackid)

        with mock.patch.object(verifymod, "_acoustid_available", lambda: True), \
             mock.patch.object(verifymod, "_lookup", lambda p: self._results()), \
             mock.patch("mediafile.MediaFile", FakeMF):
            fixed, left = singletons._fingerprint_retag(self.cfg, d, {}, set(), mock.MagicMock(), apply=True)
        self.assertEqual((fixed, left), (1, 0))
        self.assertEqual(saved, {"title": "TrueTitle", "artist": "TrueArtist", "mb_trackid": "mbTrue"})

    def test_fingerprint_retag_dedups_against_clean(self):
        # audio maps to mbTrue (dominant) + mbAlbum (same audio, the album's recording id). mbAlbum is in clean
        # -> NOT a new single: re-tag to mbAlbum so the import dup-skips it (the bug the user spotted).
        from gbc.passes import verify as verifymod
        d = self.cfg.dump / "imposters"
        d.mkdir(parents=True, exist_ok=True)
        (d / "dupe.flac").write_bytes(b"x")
        saved = {}

        class FakeMF:
            def __init__(self, path):
                self.title = self.artist = self.mb_trackid = None

            def save(self):
                saved["mb_trackid"] = self.mb_trackid

        with mock.patch.object(verifymod, "_acoustid_available", lambda: True), \
             mock.patch.object(verifymod, "_lookup", lambda p: self._results(extra_ids=("mbAlbum",))), \
             mock.patch("mediafile.MediaFile", FakeMF):
            fixed, left = singletons._fingerprint_retag(self.cfg, d, {}, {"mbAlbum"}, mock.MagicMock(), apply=True)
        self.assertEqual((fixed, left), (0, 0))               # not counted as a NEW single
        self.assertEqual(saved["mb_trackid"], "mbAlbum")      # tagged the IN-CLEAN id -> import dup-skips it

    def test_fingerprint_retag_dry_run_does_not_write(self):
        from gbc.passes import verify as verifymod
        d = self.cfg.dump / "imposters"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.flac").write_bytes(b"x")
        opened = []
        with mock.patch.object(verifymod, "_acoustid_available", lambda: True), \
             mock.patch.object(verifymod, "_lookup", lambda p: self._results()), \
             mock.patch("mediafile.MediaFile", lambda p: opened.append(p)):
            fixed, left = singletons._fingerprint_retag(self.cfg, d, {}, set(), mock.MagicMock(), apply=False)
        self.assertEqual((fixed, left), (1, 0))
        self.assertEqual(opened, [])              # dry-run: no file opened for writing

    def test_fingerprint_retag_leaves_unidentified_in_place(self):
        from gbc.passes import verify as verifymod
        d = self.cfg.dump / "imposters"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.flac").write_bytes(b"x")
        with mock.patch.object(verifymod, "_acoustid_available", lambda: True), \
             mock.patch.object(verifymod, "_lookup", lambda p: None):   # AcoustID can't identify
            fixed, left = singletons._fingerprint_retag(self.cfg, d, {}, set(), mock.MagicMock(), apply=True)
        self.assertEqual((fixed, left), (0, 1))

    def test_fingerprint_retag_cache_skips_repeat_lookup(self):
        from gbc.passes import verify as verifymod
        d = self.cfg.dump / "imposters"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.flac").write_bytes(b"x")
        calls = []
        cache: dict = {}
        with mock.patch.object(verifymod, "_acoustid_available", lambda: True), \
             mock.patch.object(verifymod, "_lookup", lambda p: calls.append(p) or None):
            singletons._fingerprint_retag(self.cfg, d, cache, set(), mock.MagicMock(), apply=False)
            singletons._fingerprint_retag(self.cfg, d, cache, set(), mock.MagicMock(), apply=False)
        self.assertEqual(len(calls), 1)           # 2nd run hits the cache -> AcoustID called only once

    # --- _promote_complete: robust completeness via the live MB tracklist ---

    @staticmethod
    def _ls(rows):
        # rows = [(albumid, id, mb_trackid, tracktotal, path)] -> the `beet ls -f` output
        return "\n".join("\t".join(map(str, r)) for r in rows) + "\n"

    def test_promote_assembles_complete_album(self):
        rows = [("albX", 1, "r1", 3, "/c/_Singles/a/1.flac"),
                ("albX", 2, "r2", 3, "/c/_Singles/a/2.flac"),
                ("albX", 3, "r3", 3, "/c/_Singles/a/3.flac")]
        seen = []
        with mock.patch.object(singletons, "run_beet", lambda c, a, **k: (0, self._ls(rows))), \
             mock.patch.object(singletons, "release_recordings", lambda aid: frozenset({"r1", "r2", "r3"})), \
             mock.patch.object(singletons, "_assemble_album", lambda *a, **k: seen.append(a[1]) or True):
            n = singletons._promote_complete(self.cfg, mock.MagicMock(), apply=True)
        self.assertEqual(n, 1)
        self.assertEqual(seen, ["albX"])                 # the complete album was handed to the assembler

    def test_promote_prefilter_skips_short_album_without_mb_call(self):
        rows = [("albX", 1, "r1", 3, "/c/a/1.flac"),
                ("albX", 2, "r2", 3, "/c/a/2.flac")]     # only 2 of tracktotal 3
        rr = mock.Mock()
        with mock.patch.object(singletons, "run_beet", lambda c, a, **k: (0, self._ls(rows))), \
             mock.patch.object(singletons, "release_recordings", rr), \
             mock.patch.object(singletons, "_assemble_album", lambda *a, **k: True):
            n = singletons._promote_complete(self.cfg, mock.MagicMock(), apply=True)
        self.assertEqual(n, 0)
        rr.assert_not_called()                           # pre-filter avoided the network call

    def test_promote_robust_mb_overrides_stale_tracktotal(self):
        # count == tracktotal (2) so the pre-filter passes, but MB says the release has 3 recordings -> incomplete
        rows = [("albX", 1, "r1", 2, "/c/a/1.flac"),
                ("albX", 2, "r2", 2, "/c/a/2.flac")]
        with mock.patch.object(singletons, "run_beet", lambda c, a, **k: (0, self._ls(rows))), \
             mock.patch.object(singletons, "release_recordings", lambda aid: frozenset({"r1", "r2", "r3"})), \
             mock.patch.object(singletons, "_assemble_album", lambda *a, **k: True):
            n = singletons._promote_complete(self.cfg, mock.MagicMock(), apply=True)
        self.assertEqual(n, 0)                            # robust check beats the stale tracktotal

    def test_promote_unverifiable_release_left_alone(self):
        rows = [("albX", 1, "r1", 1, "/c/a/1.flac")]
        with mock.patch.object(singletons, "run_beet", lambda c, a, **k: (0, self._ls(rows))), \
             mock.patch.object(singletons, "release_recordings", lambda aid: frozenset()), \
             mock.patch.object(singletons, "_assemble_album", lambda *a, **k: True):
            n = singletons._promote_complete(self.cfg, mock.MagicMock(), apply=True)
        self.assertEqual(n, 0)                            # MB fetch failed -> never a blind promotion

    @staticmethod
    def _stack(patches):
        es = contextlib.ExitStack()
        for p in patches:
            es.enter_context(p)
        return es


if __name__ == "__main__":
    unittest.main()
