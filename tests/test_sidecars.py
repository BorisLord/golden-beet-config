import unittest
from pathlib import Path
from unittest import mock

from gbc import sidecars
from tests.base import Base


class TestSidecars(Base):
    def test_prune_shells_merges_into_one_folder(self):
        shell = self.tmp / "src" / "Alb"
        shell.mkdir(parents=True)
        (shell / "back.jpg").write_text("x")
        (shell / "scan.png").write_text("y")
        (shell / "cover.jpg").write_text("new")
        dump = self.tmp / "dump"
        sh = dump / "shells" / "Alb"
        sh.mkdir(parents=True)
        (sh / "cover.jpg").write_text("OLD")                     # a prior shell dump already sits here
        sidecars.prune_shells(str(self.tmp / "src"), str(dump), True)
        names = sorted(p.name for p in sh.iterdir())
        self.assertEqual(names, ["back.jpg", "cover (2).jpg", "cover.jpg", "scan.png"])  # one folder, suffixed
        self.assertEqual((sh / "cover.jpg").read_text(), "OLD")                          # original kept
        self.assertFalse(shell.exists())                                                 # emptied shell removed

    def test_prune_shells_quarantines_shell_with_subdir(self):
        # imported shell: no audio anywhere, but has a Scans/ subfolder + parasites -> WHOLE tree quarantined
        shell = self.tmp / "src" / "Alb"
        (shell / "Scans").mkdir(parents=True)
        (shell / "Scans" / "booklet.jpg").write_text("b")
        (shell / "release.nfo").write_text("n")
        (shell / "Thumbs.db").write_text("t")
        n = sidecars.prune_shells(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(n, 1)                                          # the shell, not the subfolder, counted
        dump = self.tmp / "dump" / "shells" / "Alb"
        self.assertTrue((dump / "release.nfo").exists())
        self.assertTrue((dump / "Scans" / "booklet.jpg").exists())     # subfolder moved WITH its parent shell
        self.assertFalse(shell.exists())                               # source shell removed

    def test_prune_shells_keeps_folder_that_still_has_audio(self):
        # a skipped album (audio still present anywhere in its subtree) must NOT be quarantined
        alb = self.tmp / "src" / "Skipped"
        (alb / "Disc 1").mkdir(parents=True)
        (alb / "Disc 1" / "01 - s.flac").write_text("x")
        (alb / "cover.jpg").write_text("c")
        n = sidecars.prune_shells(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(n, 0)
        self.assertTrue((alb / "Disc 1" / "01 - s.flac").exists())     # left in source
        self.assertFalse((self.tmp / "dump" / "shells" / "Skipped").exists())

    def test_quarantine_dir_nested_by_reason(self):
        qd = sidecars.quarantine_dir
        self.assertEqual(qd("/d", "imposters", "Artist", "Album", "2020"), Path("/d/imposters/Artist/Album (2020)"))
        self.assertEqual(qd("/d", "duplicates", "Artist", "Album", 2020), Path("/d/duplicates/Artist/Album (2020)"))
        self.assertEqual(qd("/d", "imposters", "Artist", "Album", "0"), Path("/d/imposters/Artist/Album"))  # year 0
        self.assertEqual(qd("/d", "shells", "A/B", "C", "2020"), Path("/d/shells/A_B/C (2020)"))   # slash sanitised
        self.assertEqual(qd("/d", "shells", "", "", "", fallback="Src"), Path("/d/shells/Src"))    # no meta -> fallback
        self.assertEqual(qd("/d", "shells", "", "", ""), Path("/d/shells/_unknown"))

    def test_safe_move_failure_is_logged_not_raised(self):
        import logging
        ok = sidecars.safe_move(self.tmp / "does-not-exist.mp3", self.tmp / "dest.mp3", logging.getLogger("t"))
        self.assertFalse(ok)                                # returns False, no traceback
        self.assertFalse((self.tmp / "dest.mp3").exists())

    def test_safe_move_success(self):
        import logging
        src = self.tmp / "a.txt"
        src.write_text("x")
        ok = sidecars.safe_move(src, self.tmp / "b.txt", logging.getLogger("t"))
        self.assertTrue(ok)
        self.assertTrue((self.tmp / "b.txt").exists())
        self.assertFalse(src.exists())

    def test_prune_shells_failed_child_move_keeps_shell_uncounted(self):
        # A shell where ONE child fails to move: the shell is only PARTIALLY cleared, so it must NOT be counted
        # and its source dir must be KEPT (never rmdir'd over files still inside) -- no phantom "quarantined".
        shell = self.tmp / "src" / "Alb"
        shell.mkdir(parents=True)
        (shell / "a.jpg").write_text("a")
        (shell / "b.jpg").write_text("b")
        real_move = sidecars.safe_move

        def flaky_move(src, dst, log):
            if Path(src).name == "b.jpg":
                return False                                 # simulate one child that won't move
            return real_move(src, dst, log)

        with mock.patch.object(sidecars, "safe_move", flaky_move):
            n = sidecars.prune_shells(str(self.tmp / "src"), str(self.tmp / "dump"), True)
        self.assertEqual(n, 0)                               # partial move -> NOT counted as quarantined
        self.assertTrue(shell.is_dir())                      # source shell kept (not rmdir'd over the leftover)
        self.assertTrue((shell / "b.jpg").exists())          # the file that couldn't move stays in the shell


if __name__ == "__main__":
    unittest.main()
