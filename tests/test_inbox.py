import unittest
from itertools import count
from unittest import mock

from gbc.lock import import_lock
from gbc.passes import inbox
from tests.base import Base


class TestInboxGate(Base):
    def test_has_new_reads_the_plan(self):
        self.assertTrue(inbox.has_new("Skipped 5 paths.\nAlbum: /x/Y\n  /x/Y/01.flac\n"))
        self.assertTrue(inbox.has_new("Singleton: /x/track.mp3\n"))
        self.assertFalse(inbox.has_new("Skipped 5 paths.\n"))     # the old gate bug: nothing new
        self.assertFalse(inbox.has_new(""))

    def test_inbox_nothing_new_exits_clean(self):
        (self.cfg.src / "Some Folder").mkdir(parents=True)
        (self.cfg.src / "Some Folder" / "a.flac").write_text("x")
        self.cfg.beet = self.fake_beet(stderr="Skipped 1 paths.\n")   # --pretend plan on STDERR, nothing new
        self.assertEqual(inbox.run(self.cfg), 0)
        self.assertFalse((self.cfg.beetsdir / "gbc-state.json").exists())   # pipeline never ran

    def test_debounce_bounded_on_growing_source(self):
        clk = count(0, 600)                       # monotonic jumps 600s/call -> exceeds max_wait=1800 fast
        grow = count(0, 1000)                     # source size never stabilises
        with mock.patch.object(inbox.time, "monotonic", lambda: next(clk)), \
             mock.patch.object(inbox.time, "sleep", lambda s: None), \
             mock.patch.object(inbox, "_dir_size", lambda s: next(grow)):
            inbox._debounce(self.cfg, interval=0, max_wait=1800)   # must RETURN, not loop forever

    def test_debounce_runs_before_the_new_gate(self):
        """The drop must settle BEFORE the --pretend plan is judged: a still-copying tree would otherwise read
        as 'nothing new' and the whole tick would be skipped."""
        (self.cfg.src / "Some Folder").mkdir(parents=True)
        (self.cfg.src / "Some Folder" / "a.flac").write_text("x")
        order = []
        with mock.patch.object(inbox, "_debounce", lambda cfg, **k: order.append("debounce")), \
             mock.patch.object(inbox, "run_beet", lambda cfg, args, **k: (order.append("pretend"), (0, ""))[1]):
            inbox.run(self.cfg)                       # empty plan -> exits after debounce + pretend
        self.assertEqual(order, ["debounce", "pretend"])

    def test_inbox_bows_out_when_locked(self):
        (self.cfg.src / "f").mkdir(parents=True)
        self.cfg.beet = self.fake_beet(stderr="Album: /x\n")
        with import_lock(self.cfg, blocking=True) as got:
            self.assertTrue(got)
            self.assertEqual(inbox.run(self.cfg), 0)                  # busy -> exits without importing
        self.assertFalse((self.cfg.beetsdir / "gbc-state.json").exists())


if __name__ == "__main__":
    unittest.main()
