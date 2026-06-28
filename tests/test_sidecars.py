import json
import unittest
from pathlib import Path
from unittest import mock

from gbc import probe, sidecars
from tests.base import Base


def _fake_dur(p):
    # "NN - ...": NN*10 seconds; non-numeric basenames -> 0
    name = Path(p).name
    return int(name[:2]) * 10 if name[:2].isdigit() else 0


def _get(self, p):
    # a ProbeCache.get whose length follows the filename (snapshot only consumes pr.length)
    return probe.Probe(title="", length=_fake_dur(p), bitrate=0,
                       ext=Path(p).suffix.lower(), artist="", album="", year="")


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
        with mock.patch.object(probe.ProbeCache, "get", _get):
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
        with mock.patch.object(probe.ProbeCache, "get", _get):
            sidecars.snapshot(str(self.tmp / "src"), str(snapf))

        clean = self.tmp / "clean" / "Artist" / "My Album (2020)"
        clean.mkdir(parents=True)
        for i in (1, 2, 3):
            (clean / f"{i:02d} - s.flac").write_text("x")
        (clean / "cover.jpg").write_text("existing")          # clean already has a cover
        # clean items are read NATIVELY now: build the `beet ls -f '$path\t$length'` output (M:SS) + a stale dir
        ls_lines = [f"{clean / f'{i:02d} - s.flac'}\t{sec // 60}:{sec % 60:02d}"
                    for i, sec in zip((1, 2, 3), (10, 20, 30), strict=True)]
        ls_lines.append(f"{self.tmp / 'gone' / 'x.flac'}\t1:39")          # 99s, stale db dir (clean dir gone)
        with mock.patch.object(sidecars, "run_beet", lambda cfg, a, **k: (0, "\n".join(ls_lines))):
            sidecars.apply(self.cfg, str(snapf), str(self.tmp / "dump"), True)

        self.assertTrue((clean / "booklet.pdf").exists())          # booklet carried into clean
        self.assertFalse((alb / "booklet.pdf").exists())
        self.assertTrue((clean / "01 - s.lrc").exists())           # lyrics carried into clean
        self.assertTrue((self.tmp / "dump" / "redundant-art" / "Artist" / "My Album (2020)" / "cover.jpg").exists())
        self.assertEqual((clean / "cover.jpg").read_text(), "existing")                  # clean cover untouched

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


if __name__ == "__main__":
    unittest.main()
