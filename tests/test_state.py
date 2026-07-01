import unittest

from gbc import state
from tests.base import Base


class TestState(Base):
    def test_watermark_roundtrip(self):
        self.assertIsNone(state.get_watermark(self.cfg))            # none yet
        state.set_watermark(self.cfg, "2026-06-17T08:00:00")
        self.assertEqual(state.get_watermark(self.cfg), "2026-06-17T08:00:00")

    def test_added_query(self):
        self.assertEqual(state.added_query(None), "")               # first run -> whole library
        self.assertEqual(state.added_query("2026-06-17T08:00:00"), "added:2026-06-17T08:00:00..")

    def test_corrupt_state_is_none(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / "gbc-state.json").write_text("{ not json")
        self.assertIsNone(state.get_watermark(self.cfg))            # fail soft, not crash

    def test_nondict_json_watermark_is_none(self):
        # valid JSON but the WRONG shape (a bare list / int) -> .get() would crash on the old code; must fail soft.
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        for payload in ("[]", "42"):
            (self.cfg.beetsdir / "gbc-state.json").write_text(payload)
            self.assertIsNone(state.get_watermark(self.cfg), f"non-dict state {payload!r} -> None (no crash)")

    def test_nondict_json_progress_is_empty(self):
        # a corrupted non-dict progress file must start the run FRESH ({}), not raise on the missing .get/'done'.
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        for payload in ("[]", "42"):
            (self.cfg.beetsdir / "gbc-run-progress.json").write_text(payload)
            self.assertEqual(state.get_progress(self.cfg), {}, f"non-dict progress {payload!r} -> {{}} (no crash)")


if __name__ == "__main__":
    unittest.main()
