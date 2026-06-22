import json
import unittest
from pathlib import Path
from unittest import mock

from gbc.passes import verify
from tests.base import Base


class TestVerify(Base):
    def _items(self, specs):
        """specs: [(stem, mbid)] -> create the files (so stat() works) + return the fake `beet ls` text
        (8 fields: $id $path $mb_trackid $albumartist $album $year $artist $title)."""
        lines = []
        for i, (stem, mbid) in enumerate(specs, 1):
            (self.tmp / f"{stem}.m4a").write_bytes(b"x")
            p = self.tmp / f"{stem}.m4a"
            lines.append(f"{i}{verify.SEP}{p}{verify.SEP}{mbid}{verify.SEP}TestArtist{verify.SEP}TestAlbum"
                         f"{verify.SEP}2001{verify.SEP}TestArtist{verify.SEP}TestTitle")
        return "\n".join(lines)

    def test_quarantines_only_conclusive_imposters(self):
        text = self._items([("a", "mbA"), ("b", "mbB"), ("c", "mbC"), ("d", "mbD")])
        # a: genuine match -> kept ; b: no-match + official KNOWN -> IMPOSTER (quarantined) ;
        # c: throttled -> inconclusive (kept) ; d: no-match + official ALSO unknown -> rare/genuine (kept)
        fv = {"a": ("ok", True, None), "b": ("ok", False, None), "c": ("error", False, None), "d": ("ok", False, None)}
        of = {"mbB": True, "mbD": False}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem]), \
             mock.patch.object(verify, "_official_known", lambda m: of.get(m)):
            n = verify.run(self.cfg)
        self.assertEqual(n, 1)                                    # only the real imposter (b)
        self.assertFalse((self.tmp / "b.m4a").exists())           # imposter moved out of "clean"
        self.assertTrue((self.cfg.dump / "imposters" / "TestArtist" / "TestAlbum (2001)" / "b.m4a").exists())
        for stem in ("a", "c", "d"):
            self.assertTrue((self.tmp / f"{stem}.m4a").exists())  # genuine / inconclusive kept
        verd = json.loads((self.cfg.beetsdir / "gbc-verify-verdicts.json").read_text())
        self.assertEqual(verd[str(self.tmp / "a.m4a")], "ok")     # reclaim input: genuine
        self.assertEqual(verd[str(self.tmp / "b.m4a")], "imposter")
        self.assertEqual(verd[str(self.tmp / "d.m4a")], "rare")
        self.assertNotIn(str(self.tmp / "c.m4a"), verd)           # inconclusive -> no verdict recorded

    def test_skips_cleanly_without_pyacoustid(self):
        with mock.patch.object(verify, "_acoustid_available", lambda: False):
            self.assertEqual(verify.run(self.cfg), 0)

    def test_imposter_cached_inconclusive_not_cached(self):
        text = self._items([("b", "mbB"), ("c", "mbC")])      # b -> imposter, c -> inconclusive (throttled)
        fv = {"b": ("ok", False, None), "c": ("error", False, None)}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem]), \
             mock.patch.object(verify, "_official_known", lambda m: True):
            verify.run(self.cfg)
        cache = json.loads((self.cfg.beetsdir / "gbc-verify-cache.json").read_text())
        self.assertEqual(list(cache.values()), ["imposter"])  # imposter cached; inconclusive deliberately not

    def test_file_verdict_detects_strong_mismatch(self):
        """Audio matches a DIFFERENT recording with high confidence -> mismatch (artist, title, score)."""
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.93, "recordings": [
            {"id": "mbOther", "title": "These Boots (radio edit)", "artists": [{"name": "Barcode Brothers"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            status, present, mismatch = verify._file_verdict("x.m4a", "mbTagged")
        self.assertEqual((status, present), ("ok", False))
        self.assertEqual(mismatch, ("Barcode Brothers", "These Boots (radio edit)", 0.93))

    def test_file_verdict_present_has_no_mismatch(self):
        """Tagged recording IS among the matches -> present, no mismatch flagged."""
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.95, "recordings": [
            {"id": "mbTagged", "title": "T", "artists": [{"name": "A"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            _, present, mismatch = verify._file_verdict("x.m4a", "mbTagged")
        self.assertTrue(present)
        self.assertIsNone(mismatch)

    def test_file_verdict_weak_other_not_flagged(self):
        """A weak (<MISMATCH_SCORE) match to another recording is NOT a mismatch (conservative refute bar)."""
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.6, "recordings": [
            {"id": "mbOther", "title": "T", "artists": [{"name": "A"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            status, present, mismatch = verify._file_verdict("x.m4a", "mbTagged")
        self.assertEqual((status, present, mismatch), ("ok", False, None))

    def test_mismatch_logged_but_track_kept(self):
        """detect+log: a DIFFERENT-artist mismatch (tag stub unknown) is LOGGED but stays 'rare' (KEPT)."""
        text = self._items([("a", "mbA")])
        fv = {"a": ("ok", False, ("Barcode Brothers", "These Boots (radio edit)", 0.93))}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem]), \
             mock.patch.object(verify, "_official_known", lambda m: False), \
             self.assertLogs("gbc", "WARNING") as cm:
            n = verify.run(self.cfg)
        self.assertEqual(n, 0)                                   # nothing quarantined
        self.assertTrue((self.tmp / "a.m4a").exists())           # KEPT in clean
        self.assertTrue(any("MISMATCH" in m and "Barcode Brothers" in m for m in cm.output))
        verd = json.loads((self.cfg.beetsdir / "gbc-verify-verdicts.json").read_text())
        self.assertEqual(verd[str(self.tmp / "a.m4a")], "rare")  # genuine-kept despite the wrong tag

    def test_same_artist_other_recording_not_flagged(self):
        """Cypress Hill case: audio matches the SAME artist (other recording id) -> NOT flagged (no noise)."""
        text = self._items([("a", "mbA")])
        fv = {"a": ("ok", False, ("TestArtist", "Other Title", 0.99))}   # same artist as the tag
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem]), \
             mock.patch.object(verify, "_official_known", lambda m: False), \
             self.assertNoLogs("gbc", "WARNING"):
            n = verify.run(self.cfg)
        self.assertEqual(n, 0)
        self.assertTrue((self.tmp / "a.m4a").exists())

    def test_same_artist_helper(self):
        self.assertTrue(verify._same_artist("Cypress Hill", "Cypress Hill"))
        self.assertTrue(verify._same_artist("Cypress Hill", "Cypress Hill feat. Sen Dog"))
        self.assertFalse(verify._same_artist("Radio Rental", "Barcode Brothers"))
        self.assertFalse(verify._same_artist("", "X"))


if __name__ == "__main__":
    unittest.main()
