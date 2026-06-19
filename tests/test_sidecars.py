import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from musicrec import sidecars
from tests.base import Base


def _fake_dur(p):
    # "NN - ...": NN*10 seconds; non-numeric basenames -> 0 (so ffprobe is never needed in tests)
    name = Path(p).name
    return int(name[:2]) * 10 if name[:2].isdigit() else 0


class TestSidecars(Base):
    def test_snapshot_picks_official_sidecars_only(self):
        alb = self.tmp / "src" / "My Album (2020)"
        alb.mkdir(parents=True)
        for i in (1, 2, 3):
            (alb / f"{i:02d} - song.flac").write_text("x")
        (alb / "cover.jpg").write_text("c")
        (alb / "booklet.pdf").write_text("b")
        (alb / "01 - song.lrc").write_text("lrc")
        (alb / "notes.txt").write_text("n")           # NOT an official sidecar
        snapf = self.tmp / "snap.json"
        with mock.patch.object(sidecars, "dur", _fake_dur):
            self.assertEqual(sidecars.snapshot(str(self.tmp / "src"), str(snapf)), 1)
        snap = json.loads(snapf.read_text())[0]
        self.assertEqual(snap["durs"], [10, 20, 30])
        self.assertEqual({Path(f).name for f in snap["files"]}, {"cover.jpg", "booklet.pdf", "01 - song.lrc"})

    def test_apply_carries_quarantines_and_skips_stale(self):
        alb = self.tmp / "src" / "My Album (2020)"
        alb.mkdir(parents=True)
        for i in (1, 2, 3):
            (alb / f"{i:02d} - s.flac").write_text("x")
        (alb / "cover.jpg").write_text("c")
        (alb / "booklet.pdf").write_text("b")
        (alb / "01 - s.lrc").write_text("l")
        snapf = self.tmp / "snap.json"
        with mock.patch.object(sidecars, "dur", _fake_dur):
            sidecars.snapshot(str(self.tmp / "src"), str(snapf))

        clean = self.tmp / "clean" / "Artist" / "My Album (2020)"
        clean.mkdir(parents=True)
        for i in (1, 2, 3):
            (clean / f"{i:02d} - s.flac").write_text("x")
        (clean / "cover.jpg").write_text("existing")          # clean already has a cover
        db = self.tmp / "library.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE items (path TEXT, length REAL)")
        for i, sec in zip((1, 2, 3), (10, 20, 30), strict=True):
            con.execute("INSERT INTO items VALUES (?,?)", (str(clean / f"{i:02d} - s.flac"), float(sec)))
        con.execute("INSERT INTO items VALUES (?,?)", (str(self.tmp / "gone" / "x.flac"), 99.0))  # stale db dir
        con.commit()
        con.close()

        sidecars.apply(str(snapf), str(db), str(self.tmp / "clean"), str(self.tmp / "dump"), True)

        self.assertTrue((clean / "booklet.pdf").exists())          # booklet carried into clean
        self.assertFalse((alb / "booklet.pdf").exists())
        self.assertTrue((clean / "01 - s.lrc").exists())           # lyrics carried into clean
        self.assertTrue((self.tmp / "dump" / "My Album (2020)" / "cover.jpg").exists())  # redundant cover quarantined
        self.assertEqual((clean / "cover.jpg").read_text(), "existing")                  # clean cover untouched

    def test_prune_shells_merges_into_one_folder(self):
        shell = self.tmp / "src" / "Alb"
        shell.mkdir(parents=True)
        (shell / "back.jpg").write_text("x")
        (shell / "scan.png").write_text("y")
        (shell / "cover.jpg").write_text("new")
        dump = self.tmp / "dump"
        (dump / "Alb").mkdir(parents=True)
        (dump / "Alb" / "cover.jpg").write_text("OLD")           # apply already dumped a cover here
        sidecars.prune_shells(str(self.tmp / "src"), str(dump), True)
        names = sorted(p.name for p in (dump / "Alb").iterdir())
        self.assertEqual(names, ["back.jpg", "cover (2).jpg", "cover.jpg", "scan.png"])  # one folder, suffixed
        self.assertEqual((dump / "Alb" / "cover.jpg").read_text(), "OLD")                # original kept
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
        dump = self.tmp / "dump" / "Alb"
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
        self.assertFalse((self.tmp / "dump" / "Skipped").exists())

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


if __name__ == "__main__":
    unittest.main()
