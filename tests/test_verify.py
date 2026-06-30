import json
import unittest
from pathlib import Path
from unittest import mock

from gbc.passes import verify
from tests.base import Base


class TestVerify(Base):
    def _items(self, specs):
        """specs: [(stem, mbid)] -> create files under clean TestArtist/TestAlbum (2001)/ + return the fake
        `beet ls` text (7 fields: $id $path $mb_trackid $artist $title $length $bitrate)."""
        self.adir = self.cfg.clean / "TestArtist" / "TestAlbum (2001)"
        self.adir.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, (stem, mbid) in enumerate(specs, 1):
            p = self.adir / f"{stem}.m4a"
            p.write_bytes(b"x")
            # 9 fields; album_id=alb1, mb_albumid empty (-> demote skipped in the imposter-detection tests)
            lines.append(f"{i}{verify.SEP}{p}{verify.SEP}{mbid}{verify.SEP}TestArtist{verify.SEP}TestTitle"
                         f"{verify.SEP}3:30{verify.SEP}256kbps{verify.SEP}alb1{verify.SEP}")
        return "\n".join(lines)

    def test_quarantines_only_conclusive_imposters(self):
        text = self._items([("a", "mbA"), ("b", "mbB"), ("c", "mbC"), ("d", "mbD")])
        # a: genuine match -> kept ; b: audio confidently matches a DIFFERENT recording -> IMPOSTER ;
        # c: throttled -> inconclusive (kept) ; d: no-match but NO confident alternative -> kept (unprovable)
        fv = {"a": ("ok", True, None), "b": ("ok", False, ("Den Harrow", "OtherSong", 0.95)),
              "c": ("error", False, None), "d": ("ok", False, None)}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem] + (None,)):
            n = verify.run(self.cfg)
        self.assertEqual(n, 1)                                    # only the real imposter (b)
        self.assertFalse((self.adir / "b.m4a").exists())           # imposter moved out of "clean"
        self.assertTrue((self.cfg.dump / "imposters" / "TestArtist" / "TestAlbum (2001)" / "b.m4a").exists())
        for stem in ("a", "c", "d"):
            self.assertTrue((self.adir / f"{stem}.m4a").exists())  # genuine / inconclusive kept

    def test_skips_cleanly_without_pyacoustid(self):
        with mock.patch.object(verify, "_acoustid_available", lambda: False):
            self.assertEqual(verify.run(self.cfg), 0)

    def test_imposter_cached_inconclusive_not_cached(self):
        text = self._items([("b", "mbB"), ("c", "mbC")])      # b -> imposter, c -> inconclusive (throttled)
        fv = {"b": ("ok", False, ("Den Harrow", "OtherSong", 0.95)), "c": ("error", False, None)}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem] + (None,)):
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
            status, present, mismatch, dominant = verify._file_verdict("x.m4a", "mbTagged")
        self.assertEqual((status, present), ("ok", False))
        self.assertEqual(mismatch, ("Barcode Brothers", "These Boots (radio edit)", 0.93))
        self.assertEqual(dominant, ("mbOther", "Barcode Brothers", "These Boots (radio edit)"))  # same FP, one call

    def test_file_verdict_present_has_no_mismatch(self):
        """Tagged recording IS among the matches -> present, no mismatch flagged."""
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.95, "recordings": [
            {"id": "mbTagged", "title": "T", "artists": [{"name": "A"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            _, present, mismatch, _ = verify._file_verdict("x.m4a", "mbTagged")
        self.assertTrue(present)
        self.assertIsNone(mismatch)

    def test_file_verdict_weak_other_not_flagged(self):
        """A weak (<MISMATCH_SCORE) match to another recording is NOT a mismatch (conservative refute bar)."""
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.6, "recordings": [
            {"id": "mbOther", "title": "T", "artists": [{"name": "A"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            status, present, mismatch, dominant = verify._file_verdict("x.m4a", "mbTagged")
        self.assertEqual((status, present, mismatch, dominant), ("ok", False, None, None))   # weak -> no dominant

    def test_identify_dominant_returns_single_strong_recording(self):
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.96, "recordings": [
            {"id": "mbTrue", "title": "Autumn Leaves", "artists": [{"name": "Cannonball Adderley"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            self.assertEqual(verify.identify_dominant("x.flac"),
                             ("mbTrue", "Cannonball Adderley", "Autumn Leaves"))

    def test_identify_dominant_same_song_many_releases_keeps_first(self):
        # one song, several recording ids (album + compilation) share the title -> NOT ambiguous
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.97, "recordings": [
            {"id": "mb1", "title": "Ceora", "artists": [{"name": "Lee Morgan"}]},
            {"id": "mb2", "title": "Ceora", "artists": [{"name": "Lee Morgan"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            self.assertEqual(verify.identify_dominant("x.flac")[0], "mb1")

    def test_identify_dominant_ambiguous_two_songs_returns_none(self):
        # two DIFFERENT songs both above the bar -> unsafe to pick -> None
        import acoustid
        resp = {"status": "ok", "results": [
            {"score": 0.95, "recordings": [{"id": "mbA", "title": "Song A", "artists": [{"name": "X"}]}]},
            {"score": 0.93, "recordings": [{"id": "mbB", "title": "Song B", "artists": [{"name": "Y"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            self.assertIsNone(verify.identify_dominant("x.flac"))

    def test_identify_dominant_weak_returns_none(self):
        import acoustid
        resp = {"status": "ok", "results": [{"score": 0.6, "recordings": [
            {"id": "mbX", "title": "T", "artists": [{"name": "A"}]}]}]}
        with mock.patch.object(acoustid, "fingerprint_file", lambda p: (190, "FP")), \
             mock.patch.object(acoustid, "lookup", lambda *a, **k: resp):
            self.assertIsNone(verify.identify_dominant("x.flac"))

    def test_confident_mismatch_quarantined(self):
        """A confident different-artist match (not a sibling) -> quarantined as imposter + IMPOSTER warning logged."""
        text = self._items([("a", "mbA")])
        fv = {"a": ("ok", False, ("Barcode Brothers", "Some Other Song", 0.93))}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem] + (None,)), \
             self.assertLogs("gbc", "WARNING") as cm:
            n = verify.run(self.cfg)
        self.assertEqual(n, 1)                                   # quarantined (audio is a different recording)
        self.assertFalse((self.adir / "a.m4a").exists())          # moved out of clean
        self.assertTrue(any("IMPOSTER" in m and "Barcode Brothers" in m for m in cm.output))

    def test_sibling_recording_kept_even_if_known(self):
        """Zenzile case: confident match to the SAME title with an overlapping artist credit (sibling) -> kept,
        no warning, even though the tagged id is known to AcoustID."""
        text = self._items([("a", "mbA")])
        fv = {"a": ("ok", False, ("TestArtist Crew", "TestTitle", 0.99))}   # same title, credit-variant artist
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem] + (None,)), \
             self.assertNoLogs("gbc", "WARNING"):
            n = verify.run(self.cfg)
        self.assertEqual(n, 0)                                   # NOT quarantined (same-song sibling)
        self.assertTrue((self.adir / "a.m4a").exists())

    def test_empty_matched_artist_kept(self):
        """AcoustID's confident match has NO artist -> can't prove a DIFFERENT artist -> keep, don't quarantine."""
        text = self._items([("a", "mbA")])
        fv = {"a": ("ok", False, ("", "Some Other Title", 0.95))}        # mismatch with empty artist
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem] + (None,)):
            n = verify.run(self.cfg)
        self.assertEqual(n, 0)                                            # NOT quarantined (no artist to compare)
        self.assertTrue((self.adir / "a.m4a").exists())

    def test_same_title_unrelated_artist_still_imposter(self):
        """UB40 'Don't Break My Heart' vs Den Harrow's: same title but no shared artist token -> real imposter."""
        text = self._items([("a", "mbA")])
        fv = {"a": ("ok", False, ("Den Harrow", "TestTitle", 0.97))}        # same title, unrelated artist
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem] + (None,)):
            n = verify.run(self.cfg)
        self.assertEqual(n, 1)                                   # genuine imposter quarantined

    def test_imposter_db_remove_issued_after_move(self):
        """stale-DB regression: after quarantining an imposter the lib entry MUST be dropped by id, so beets
        never points at the moved file."""
        text = self._items([("b", "mbB")])              # single item -> id 1
        calls = []
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda cfg, a, **k: calls.append(a) or (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: ("ok", False, ("X Artist", "Y Song", 0.95), None)):
            n = verify.run(self.cfg)
        self.assertEqual(n, 1)
        self.assertIn(["remove", "-f", "id:1"], calls)   # DB-sync by id

    def test_failed_move_leaves_file_and_no_db_remove(self):
        """safe_move fails -> imposter stays in clean and NO lib-remove (no stale entry pointing at it)."""
        text = self._items([("b", "mbB")])
        calls = []
        imp = ("ok", False, ("X Artist", "Y Song", 0.95), None)   # imposter verdict (4-tuple)
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda cfg, a, **k: calls.append(a) or (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: imp), \
             mock.patch.object(verify, "safe_move", lambda *a, **k: False):
            n = verify.run(self.cfg)
        self.assertEqual(n, 0)
        self.assertTrue((self.adir / "b.m4a").exists())   # kept in clean
        self.assertFalse(any(a[0] == "remove" for a in calls))

    def test_same_artist_helper(self):
        # SAME artist (shared non-generic token) -> kept, regardless of title (alt mix / feat. / typo / wrong track)
        self.assertTrue(verify._same_artist("Zenzile, High Tone", "Zenzile Meets High Tone"))
        self.assertTrue(verify._same_artist("Timmi Magic, PGS", "Timmi Magic & PSG"))
        self.assertTrue(verify._same_artist("Björk", "Björk"))                  # alt-mix: same artist, diff title
        self.assertTrue(verify._same_artist("M", "M"))                          # 1-char artist (-M-/Matthieu Chedid)
        self.assertFalse(verify._same_artist("M", "K"))                         # different 1-char artists
        self.assertTrue(verify._same_artist("Bassekou Kouyate & Ngoni Ba",
                                            "Bassekou Kouyate, Ngoni ba, Kassé Mady Diabaté"))  # feat. in credit
        # COMPLETELY different artist -> real imposter
        self.assertFalse(verify._same_artist("Den Harrow", "UB40"))
        self.assertFalse(verify._same_artist("Dire Straits", "Bedlam"))
        # only a GENERIC token shared ("the" / "dj") -> NOT the same artist
        self.assertFalse(verify._same_artist("The Beatles", "The Rolling Stones"))
        self.assertFalse(verify._same_artist("DJ Abdel", "DJ Shadow"))
        # only a leading article / stray 1-char token shared -> NOT the same artist (the over-broad-keep regression)
        self.assertFalse(verify._same_artist("A Tribe Called Quest", "A Perfect Circle"))
        self.assertFalse(verify._same_artist("De La Soul", "La Roux"))
        self.assertFalse(verify._same_artist("S Club 7", "S Express"))

    # --- _demote_incomplete_albums: an album made incomplete by quarantine -> singletons ---

    def test_demote_incomplete_album_when_recordings_missing(self):
        def run_beet(cfg, args, **k):
            if args and args[0] == "ls":          # the album's surviving items
                return (0, f"i1{verify.SEP}r1{verify.SEP}/c/a.flac\ni2{verify.SEP}r2{verify.SEP}/c/b.flac")
            return (0, "")
        with mock.patch.object(verify, "run_beet", run_beet), \
             mock.patch.object(verify, "missing_recordings", lambda alb, present, cache: frozenset({"r3"})), \
             mock.patch.object(verify, "_demote_album", lambda *a, **k: True):
            n = verify._demote_incomplete_albums(self.cfg, {"albX": "mbRel"}, mock.MagicMock())
        self.assertEqual(n, 1)

    def test_demote_keeps_complete_album(self):
        with mock.patch.object(verify, "run_beet", lambda c, a, **k: (0, f"i1{verify.SEP}r1{verify.SEP}/x")), \
             mock.patch.object(verify, "missing_recordings", lambda *a, **k: frozenset()), \
             mock.patch.object(verify, "_demote_album", lambda *a, **k: 1 / 0):   # must NOT demote
            self.assertEqual(verify._demote_incomplete_albums(self.cfg, {"alb": "mbRel"}, mock.MagicMock()), 0)

    def test_demote_skips_album_without_mb_release(self):
        called = []
        with mock.patch.object(verify, "missing_recordings", lambda *a, **k: called.append(1) or None):
            n = verify._demote_incomplete_albums(self.cfg, {"alb": ""}, mock.MagicMock())   # no mb_albumid
        self.assertEqual((n, called), (0, []))           # never even queried MB

    def test_demote_album_stages_and_reimports_as_singletons(self):
        adir = self.cfg.clean / "Art" / "Alb"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "1.flac").write_bytes(b"x")
        (adir / "2.flac").write_bytes(b"y")
        calls = []
        with mock.patch.object(verify, "run_beet", lambda c, a, **k: calls.append(a) or (0, "")):
            ok = verify._demote_album(self.cfg, "albX",
                                      [("i1", str(adir / "1.flac")), ("i2", str(adir / "2.flac"))], mock.MagicMock())
        self.assertTrue(ok)
        self.assertFalse((adir / "1.flac").exists())     # moved out of clean (into staging)
        imp = next(c for c in calls if c and c[0] == "import")
        self.assertIn("-s", imp)                          # singleton
        self.assertIn("-A", imp)                          # no re-match: keep the tracks' own tags
        self.assertIn("id:i1", next(c for c in calls if c and c[0] == "remove"))

    # --- shared AcoustID id-cache: verify writes each imposter's identity for singletons to reuse ---

    def test_idcache_roundtrips_and_evicts_missing(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        f = self.cfg.beetsdir / "f.flac"
        f.write_bytes(b"abc")
        k = verify.idcache_key(f)
        self.assertTrue(k.startswith(str(f)))
        verify.save_idcache(self.cfg, {k: ["r", "a", "t"], "/gone/x.flac": ["r2", "a2", "t2"]})
        reloaded = verify.load_idcache(self.cfg)
        self.assertEqual(reloaded.get(k), ["r", "a", "t"])          # live file kept
        self.assertNotIn("/gone/x.flac", reloaded)                 # missing file evicted

    def test_idcache_appends_incrementally_and_survives_retag(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        f = self.cfg.beetsdir / "g.flac"
        f.write_bytes(b"abc")
        k = verify.idcache_key(f)
        verify.append_idcache(self.cfg, k, ["r", "a", "t", ["r"]])
        # on disk immediately (no end-of-run save) -> a killed walk resumes from here
        self.assertEqual(verify.load_idcache(self.cfg).get(k), ["r", "a", "t", ["r"]])
        # a re-tag bumps mtime+size but NOT the path -> key unchanged -> still a cache HIT on relaunch
        f.write_bytes(b"abcdefgh-much-bigger")
        self.assertEqual(verify.idcache_key(f), k)
        self.assertIn(k, verify.load_idcache(self.cfg))

    def test_idcache_migrates_legacy_json_blob(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        f = self.cfg.beetsdir / "h.flac"
        f.write_bytes(b"x")
        (self.cfg.beetsdir / verify.LEGACY_IDCACHE).write_text(json.dumps({str(f): ["r", "a", "t"]}))
        reloaded = verify.load_idcache(self.cfg)                    # one-time migration of the old single-blob form
        self.assertEqual(reloaded.get(str(f)), ["r", "a", "t"])
        self.assertFalse((self.cfg.beetsdir / verify.LEGACY_IDCACHE).exists())   # legacy dropped after migration

    def test_imposter_identity_cached_for_singletons(self):
        text = self._items([("b", "mbB")])
        # b is an imposter; its AUDIO is identified as Den Harrow - OtherSong (the dominant) -> cached at the
        # quarantine path so singletons re-tags it later WITHOUT re-fingerprinting.
        fv = {"b": ("ok", False, ("Den Harrow", "OtherSong", 0.95), ("mbDen", "Den Harrow", "OtherSong"))}
        with mock.patch.object(verify, "_acoustid_available", lambda: True), \
             mock.patch.object(verify, "run_beet", lambda *a, **k: (0, text)), \
             mock.patch.object(verify, "_file_verdict", lambda p, m: fv[Path(p).stem]):
            verify.run(self.cfg)
        idcache = verify.load_idcache(self.cfg)            # JSONL append-only -> read back via the public API
        self.assertIn(["mbDen", "Den Harrow", "OtherSong"], list(idcache.values()))


if __name__ == "__main__":
    unittest.main()
