import unittest
from unittest import mock

from gbc import dedup
from tests.base import Base


class TestDedup(Base):
    def _album(self, *names):
        alb = self.tmp / "src" / "Album"
        alb.mkdir(parents=True, exist_ok=True)
        paths = []
        for n in names:
            p = alb / n
            p.write_text("x")
            paths.append(p)
        return paths

    def test_keeps_best_bitrate_quarantines_dup(self):
        a, b, c = self._album("01 - Intro.mp3", "01 - Intro_dup.mp3", "02 - Other.mp3")
        meta = {str(a): ("intro", 97, 320000), str(b): ("intro", 97, 128000), str(c): ("other", 200, 320000)}
        with mock.patch.object(dedup, "_probe", lambda p: meta[str(p)]):
            moved = dedup.dedup(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(moved, 1)
        self.assertTrue(a.exists())                                          # best bitrate kept
        self.assertFalse(b.exists())                                         # lower-bitrate dup moved out
        self.assertTrue((self.tmp / "dump" / "duplicates" / "Album" / "01 - Intro_dup.mp3").exists())  # <reason>/<src>
        self.assertTrue(c.exists())                                          # distinct track untouched

    def test_same_title_different_duration_not_merged(self):
        a, b = self._album("intro.mp3", "intro-reprise.mp3")
        meta = {str(a): ("intro", 60, 320000), str(b): ("intro", 200, 320000)}
        with mock.patch.object(dedup, "_probe", lambda p: meta[str(p)]):
            moved = dedup.dedup(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(moved, 0)                                           # different lengths -> kept both
        self.assertTrue(a.exists() and b.exists())

    def test_untitled_files_are_never_touched(self):
        a, b = self._album("a.mp3", "b.mp3")
        meta = {str(a): ("", 100, 320000), str(b): ("", 100, 128000)}        # no title -> no safe key
        with mock.patch.object(dedup, "_probe", lambda p: meta[str(p)]):
            moved = dedup.dedup(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(moved, 0)
        self.assertTrue(a.exists() and b.exists())

    def test_unverifiable_duration_not_merged(self):
        a, b = self._album("a.mp3", "b.mp3")
        meta = {str(a): ("t", 0, 320000), str(b): ("t", 100, 128000)}   # a's duration unreadable (probe failed)
        with mock.patch.object(dedup, "_probe", lambda p: meta[str(p)]):
            moved = dedup.dedup(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(moved, 0)                                       # can't confirm -> keep both (safe)
        self.assertTrue(a.exists() and b.exists())

    def test_lossless_kept_over_lossy_when_bitrate_unknown(self):
        # a FLAC whose ffprobe format bitrate reads 0 vs a 320k MP3 of the SAME track -> keep the lossless one
        a, b = self._album("01 - Song.flac", "01 - Song.mp3")
        meta = {str(a): ("song", 100, 0), str(b): ("song", 100, 320000)}
        with mock.patch.object(dedup, "_probe", lambda p: meta[str(p)]):
            moved = dedup.dedup(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(moved, 1)
        self.assertTrue(a.exists())                  # lossless FLAC kept despite bitrate reading 0
        self.assertFalse(b.exists())                 # lossy MP3 quarantined

    def test_dry_run_counts_but_moves_nothing(self):
        a, b = self._album("a.mp3", "b.mp3")
        meta = {str(a): ("t", 100, 320000), str(b): ("t", 100, 128000)}
        with mock.patch.object(dedup, "_probe", lambda p: meta[str(p)]):
            moved = dedup.dedup(str(self.tmp / "src"), str(self.tmp / "dump"), False)
        self.assertEqual(moved, 1)                                           # detected
        self.assertTrue(a.exists() and b.exists())                           # but not moved (dry-run)


if __name__ == "__main__":
    unittest.main()
