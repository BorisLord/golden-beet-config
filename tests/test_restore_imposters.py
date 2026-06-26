import unittest
from unittest import mock

from gbc.passes import restore_imposters
from tests.base import Base


class TestRestoreImposters(Base):
    def _quarantine_album(self, artist, album_dir, tracks):
        d = self.cfg.dump / "imposters" / artist / album_dir
        d.mkdir(parents=True, exist_ok=True)
        for t in tracks:
            (d / t).write_bytes(b"x")
        return d

    def test_missing_imposters_dir_returns_0(self):
        self.assertEqual(restore_imposters.run(self.cfg), 0)            # no dump/imposters at all

    def test_dry_run_counts_without_mutating(self):
        self._quarantine_album("Artist", "Album (2001)", ["01.mp3", "02.mp3"])
        calls = []
        with mock.patch.object(restore_imposters, "_read_albumid", lambda a: ("albID", "Album")), \
             mock.patch.object(restore_imposters, "run_beet", lambda *a, **k: calls.append("beet") or (0, "")), \
             mock.patch.object(restore_imposters, "safe_move", lambda *a, **k: calls.append("move") or True), \
             mock.patch.object(restore_imposters, "backup_db", lambda *a, **k: None):
            n = restore_imposters.run(self.cfg, apply=False)
        self.assertEqual(n, 2)                                          # 2 tracks WOULD be restored
        self.assertEqual(calls, [])                                     # dry: no move / no beet / no backup
        self.assertTrue((self.cfg.dump / "imposters" / "Artist" / "Album (2001)" / "01.mp3").exists())

    def test_skip_folder_without_albumid(self):
        self._quarantine_album("X", "Y", ["1.mp3"])
        with mock.patch.object(restore_imposters, "_read_albumid", lambda a: ("", "Y")):
            n = restore_imposters.run(self.cfg, apply=False)
        self.assertEqual(n, 0)                                          # no mb_albumid -> skipped, never restored

    def test_apply_partial_album_remerges(self):
        self._quarantine_album("Artist", "Album (2001)", ["03.mp3"])
        clean_dir = self.cfg.clean / "Artist" / "Album"
        clean_dir.mkdir(parents=True, exist_ok=True)
        beet = []
        with mock.patch.object(restore_imposters, "_read_albumid", lambda a: ("albID", "Album")), \
             mock.patch.object(restore_imposters, "_clean_album_dir", lambda c, i: clean_dir), \
             mock.patch.object(restore_imposters, "run_beet", lambda c, a, **k: beet.append(a) or (0, "")), \
             mock.patch.object(restore_imposters, "safe_move", lambda s, d, log: True), \
             mock.patch.object(restore_imposters, "backup_db", lambda *a, **k: None), \
             mock.patch.object(restore_imposters, "prune_empty_dirs", lambda *a, **k: None):
            n = restore_imposters.run(self.cfg, apply=True)
        self.assertEqual(n, 1)
        # incomplete album dropped (album query, files kept) then the complete folder re-imported
        self.assertTrue(any(a[0] == "remove" and "-a" in a and "mb_albumid:albID" in a for a in beet))
        self.assertTrue(any("import" in a for a in beet))

    def test_apply_va_comp_preserved(self):
        """A VA comp: the comp=True normalisation (lost by a plain -A re-import) is re-applied + the album moved,
        so it lands in _Various Artists/ rather than Various Artists/."""
        self._quarantine_album("Various Artists", "Comp (2001)", ["05.mp3"])
        clean_dir = self.cfg.clean / "_Various Artists" / "Comp"
        clean_dir.mkdir(parents=True, exist_ok=True)
        beet = []

        def fake_beet(c, a, **k):
            beet.append(a)
            return (0, "albID\n") if (a and a[0] == "ls") else (0, "")   # comp:1 query non-empty -> was_comp

        with mock.patch.object(restore_imposters, "_read_albumid", lambda a: ("albID", "Comp")), \
             mock.patch.object(restore_imposters, "_clean_album_dir", lambda c, i: clean_dir), \
             mock.patch.object(restore_imposters, "run_beet", fake_beet), \
             mock.patch.object(restore_imposters, "safe_move", lambda s, d, log: True), \
             mock.patch.object(restore_imposters, "backup_db", lambda *a, **k: None), \
             mock.patch.object(restore_imposters, "prune_empty_dirs", lambda *a, **k: None):
            n = restore_imposters.run(self.cfg, apply=True)
        self.assertEqual(n, 1)
        self.assertTrue(any(a[0] == "modify" and "comp=1" in a for a in beet))   # comp re-applied
        self.assertTrue(any(a[0] == "move" for a in beet))                       # re-routed


if __name__ == "__main__":
    unittest.main()
