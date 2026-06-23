import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from gbc import sidecars
from gbc.beetscfg import BeetsImport
from gbc.passes import reclaim
from tests.base import Base


def _fake_dur(p):
    """'NN - ...': NN*10 seconds (so ffprobe is never spawned in tests)."""
    name = Path(p).name
    return int(name[:2]) * 10 if name[:2].isdigit() else 0


class TestReclaim(Base):
    def _src_album(self, name, tracks):
        d = self.cfg.src / name
        d.mkdir(parents=True)
        for t in tracks:
            (d / f"{t} - s.flac").write_text("x")
        return d

    def _build_db(self, albums):
        """albums: {clean_subdir: [(filename, length)]} -> create library.db (with ids, like beets);
        return {(subdir, filename): id_str} so verdicts can be keyed by item id (as verify now writes them)."""
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.cfg.library)
        con.execute("CREATE TABLE items "
                    "(id INTEGER PRIMARY KEY, path TEXT, length REAL, albumartist TEXT, album TEXT, year INTEGER)")
        ids, next_id = {}, 0
        for sub, items in albums.items():
            for fn, length in items:
                next_id += 1
                p = str(self.cfg.clean / sub / fn)
                con.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", (next_id, p, float(length), "Tigran", sub, 2015))
                ids[(sub, fn)] = str(next_id)
        con.commit()
        con.close()
        return ids

    def _write_verdicts(self, mapping):
        (self.cfg.beetsdir / "gbc-verify-verdicts.json").write_text(json.dumps(mapping), encoding="utf-8")

    def _scenario(self):
        good = self._src_album("AlbumGood", ["01", "02", "03"])        # 10,20,30 -> all ok -> RECLAIM
        rare = self._src_album("AlbumRare", ["04", "05"])              # 40,50 -> one rare -> keep
        part = self._src_album("AlbumPartial", ["06", "07", "08"])     # 60,70,80, but clean has 2 -> keep
        ambig = self._src_album("AlbumAmbig", ["09"])                  # 90 -> matches two clean albums -> keep
        ids = self._build_db({
            "Good": [("01.flac", 10), ("02.flac", 20), ("03.flac", 30)],
            "Rare": [("04.flac", 40), ("05.flac", 50)],
            "Partial": [("06.flac", 60), ("07.flac", 70)],            # only 2 of 3 imported
            "AmbigX": [("x.flac", 90)],
            "AmbigY": [("y.flac", 90)],
        })
        self._write_verdicts({
            ids[("Good", "01.flac")]: "ok",
            ids[("Good", "02.flac")]: "ok",
            ids[("Good", "03.flac")]: "ok",
            ids[("Rare", "04.flac")]: "ok",
            ids[("Rare", "05.flac")]: "rare",
            ids[("Partial", "06.flac")]: "ok",
            ids[("Partial", "07.flac")]: "ok",
            ids[("AmbigX", "x.flac")]: "ok",
            ids[("AmbigY", "y.flac")]: "ok",
        })
        return good, rare, part, ambig

    def test_reclaims_only_fully_verified_albums(self):
        good, rare, part, ambig = self._scenario()
        with mock.patch.object(reclaim.beetscfg, "read_import", lambda c: BeetsImport(copy=True)), \
             mock.patch.object(sidecars, "dur", _fake_dur):
            moved = reclaim.run(self.cfg)
        self.assertEqual(moved, 1)
        self.assertFalse(good.exists())                                  # all-ok album reclaimed
        self.assertTrue((self.cfg.dump / "reclaimed" / "Tigran" / "Good (2015)").exists())   # reason/artist/album
        self.assertTrue(rare.exists())                                   # a rare track -> kept
        self.assertTrue(part.exists())                                   # count mismatch -> kept
        self.assertTrue(ambig.exists())                                  # ambiguous match -> kept

    def test_move_mode_reclaims_nothing(self):
        good, *_ = self._scenario()
        with mock.patch.object(reclaim.beetscfg, "read_import", lambda c: BeetsImport(move=True)), \
             mock.patch.object(sidecars, "dur", _fake_dur):
            moved = reclaim.run(self.cfg)
        self.assertEqual(moved, 0)
        self.assertTrue(good.exists())                                   # source consumed by beets -> never reclaim

    def test_duplicate_source_folders_neither_reclaimed(self):
        # two identical-duration source folders but ONE clean copy -> can't tell which was copied -> keep both
        d1 = self._src_album("DupA", ["01", "02"])      # durs 10,20
        d2 = self._src_album("DupB", ["01", "02"])      # same multiset
        ids = self._build_db({"Clean": [("01.flac", 10), ("02.flac", 20)]})
        self._write_verdicts({ids[("Clean", fn)]: "ok" for fn in ("01.flac", "02.flac")})
        with mock.patch.object(reclaim.beetscfg, "read_import", lambda c: BeetsImport(copy=True)), \
             mock.patch.object(sidecars, "dur", _fake_dur):
            moved = reclaim.run(self.cfg)
        self.assertEqual(moved, 0)
        self.assertTrue(d1.exists() and d2.exists())    # both kept in source (clean backs at most one reclaim)

    def test_unreadable_track_skips_whole_folder(self):
        # a folder with an unprobeable file -> shrunk multiset must NOT be matched against a smaller clean album
        alb = self._src_album("Partial", ["01", "02"])  # durs 10,20
        (alb / "bad.flac").write_text("x")              # _fake_dur -> 0 (probe failure); folder now has 3 files
        ids = self._build_db({"Clean": [("01.flac", 10), ("02.flac", 20)]})  # would match SHRUNK [10,20] sans guard
        self._write_verdicts({ids[("Clean", fn)]: "ok" for fn in ("01.flac", "02.flac")})
        with mock.patch.object(reclaim.beetscfg, "read_import", lambda c: BeetsImport(copy=True)), \
             mock.patch.object(sidecars, "dur", _fake_dur):
            moved = reclaim.run(self.cfg)
        self.assertEqual(moved, 0)                       # unmeasurable folder never reclaimed
        self.assertTrue(alb.exists())

    def test_no_library_is_noop(self):
        with mock.patch.object(reclaim.beetscfg, "read_import", lambda c: BeetsImport(copy=True)):
            self.assertEqual(reclaim.run(self.cfg), 0)


if __name__ == "__main__":
    unittest.main()
