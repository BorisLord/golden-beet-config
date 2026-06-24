import unittest

from gbc.util import prune_empty_dirs
from tests.base import Base


class TestPruneEmptyDirs(Base):
    """prune_empty_dirs is called after verify/albumdedup/qa move files out of clean -- a destructive dir op
    that had zero direct coverage. Lock: only-empty removed, audio dirs kept, root always preserved."""

    def test_removes_empty_keeps_nonempty_and_root(self):
        root = self.tmp / "clean"
        (root / "Artist" / "Empty Album (2020)").mkdir(parents=True)   # fully empty shell (all tracks moved out)
        full = root / "Artist2" / "Full Album (2021)"
        full.mkdir(parents=True)
        (full / "01 - track.flac").write_bytes(b"x")
        prune_empty_dirs(root)
        self.assertFalse((root / "Artist" / "Empty Album (2020)").exists())   # empty shell pruned
        self.assertFalse((root / "Artist").exists())                          # then its now-empty parent, bottom-up
        self.assertTrue(full.exists())                                        # dir holding audio kept
        self.assertTrue((root / "Artist2").exists())
        self.assertTrue(root.exists())                                        # root itself never removed

    def test_root_preserved_even_when_empty(self):
        root = self.tmp / "emptyroot"
        root.mkdir()
        prune_empty_dirs(root)
        self.assertTrue(root.exists())                                        # mindepth 1: root is never a target

    def test_nested_empty_chain_fully_removed(self):
        root = self.tmp / "clean"
        (root / "a" / "b" / "c").mkdir(parents=True)
        prune_empty_dirs(root)
        self.assertFalse((root / "a").exists())                               # whole empty chain gone
        self.assertTrue(root.exists())


if __name__ == "__main__":
    unittest.main()
