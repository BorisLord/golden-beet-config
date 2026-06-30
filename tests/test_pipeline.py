import contextlib
import unittest
from unittest import mock

from gbc import state
from gbc.passes import acousticbrainz, albumdedup, convert, import_, junk, pipeline, qa, upgrade, verify
from tests.base import Base


class TestPipeline(Base):
    def _trace_all_passes(self, calls):
        """Patch every pass to just record its name + return 0 (so resume can be observed deterministically)."""
        return [
            mock.patch.object(import_, "run", lambda c, *a, **k: calls.append("import") or 0),
            mock.patch.object(upgrade, "run", lambda c, *a, **k: calls.append("upgrade") or 0),
            mock.patch.object(albumdedup, "run", lambda c, *a, **k: calls.append("albumdedup") or 0),
            mock.patch.object(convert, "run", lambda c, *a, **k: calls.append("convert") or 0),
            mock.patch.object(junk, "run", lambda c, *a, **k: calls.append("junk") or 0),
            mock.patch.object(verify, "run", lambda c, *a, **k: calls.append("verify") or 0),
            mock.patch.object(acousticbrainz, "run", lambda c, *a, **k: calls.append("acousticbrainz") or 0),
            mock.patch.object(qa, "run", lambda c, *a, **k: calls.append("qa") or 0),
        ]

    def test_resume_skips_already_done_passes(self):
        calls = []
        # an in-flight run (same identity as a no-watermark run) that already finished import + upgrade
        state.set_progress(self.cfg, {"key": "initial", "wm_new": "2026-06-25T10:00:00",
                                      "done": ["import", "upgrade"]})
        with contextlib.ExitStack() as es:
            for p in self._trace_all_passes(calls):
                es.enter_context(p)
            rc = pipeline.run(self.cfg)
        self.assertEqual(rc, 0)
        self.assertNotIn("import", calls)                          # skipped -> no source re-walk
        self.assertNotIn("upgrade", calls)
        self.assertEqual(calls, ["albumdedup", "convert", "junk", "verify", "acousticbrainz", "qa"])
        self.assertEqual(state.get_progress(self.cfg), {})         # cleared on clean finish
        self.assertEqual(state.get_watermark(self.cfg), "2026-06-25T10:00:00")  # the resumed wm_new is reused

    def test_progress_from_other_run_identity_is_ignored(self):
        calls = []
        state.set_progress(self.cfg, {"key": "full", "wm_new": "x", "done": ["import", "albumdedup", "convert"]})
        with contextlib.ExitStack() as es:
            for p in self._trace_all_passes(calls):
                es.enter_context(p)
            pipeline.run(self.cfg)                                 # incremental -> key "initial" != "full"
        self.assertEqual(calls[0], "import")                       # different identity -> fresh, nothing skipped
        self.assertIn("albumdedup", calls)
    def test_pass_error_holds_watermark_and_keeps_progress(self):
        """A post-import pass that ERRORS must NOT advance the watermark or clear progress -> the next run
        re-scopes the same window and retries the failed pass (the succeeded ones are skipped)."""
        state.set_watermark(self.cfg, "2026-06-20T00:00:00")       # the prior watermark

        def boom(c, *a, **k):
            raise RuntimeError("boom")
        patches = [mock.patch.object(import_, "run", lambda c, *a, **k: 0),
                   mock.patch.object(upgrade, "run", lambda c, *a, **k: 0),
                   mock.patch.object(albumdedup, "run", lambda c, *a, **k: 0),
                   mock.patch.object(convert, "run", lambda c, *a, **k: 0),
                   mock.patch.object(junk, "run", lambda c, *a, **k: 0),
                   mock.patch.object(verify, "run", boom),         # one file-moving pass errors
                   mock.patch.object(acousticbrainz, "run", lambda c, *a, **k: 0),
                   mock.patch.object(qa, "run", lambda c, *a, **k: 0)]
        with contextlib.ExitStack() as es:
            for p in patches:
                es.enter_context(p)
            rc = pipeline.run(self.cfg)
        self.assertEqual(rc, 0)                                    # best-effort: the run itself still returns 0
        self.assertEqual(state.get_watermark(self.cfg), "2026-06-20T00:00:00")   # HELD, not advanced
        self.assertNotEqual(state.get_progress(self.cfg), {})      # progress kept -> next run resumes + retries

    def test_upgrade_scan_gated_off_on_cron_path(self):
        # `gbc inbox` passes upgrade_scan=False: the costly full-source upgrade walk must NOT run on the cron door,
        # but every other pass still does. `gbc run` (default True) keeps it.
        for scan, expect in [(False, False), (True, True)]:
            calls = []
            with contextlib.ExitStack() as es:
                for p in self._trace_all_passes(calls):
                    es.enter_context(p)
                rc = pipeline.run(self.cfg, upgrade_scan=scan)
            self.assertEqual(rc, 0)
            self.assertEqual("upgrade" in calls, expect)               # gated exactly by the flag
            self.assertEqual(calls[0], "import")                       # the rest of the pipeline is unaffected
            self.assertEqual(calls[-1], "qa")

    def test_qa_scope_follows_watermark(self):
        seen = {}

        def fake_qa(c, scope="", cull=False):
            seen["qa"] = scope
            return 0

        # mock EVERY post-import pass, not just import+qa: an unmocked pass errors when `beet` is absent
        # (CI), and the watermark-HOLD would then block the advance this test asserts -> isolate the scope logic
        with mock.patch.object(import_, "run", lambda c, src=None, reimport=False: 0), \
             mock.patch.object(upgrade, "run", lambda *a, **k: 0), \
             mock.patch.object(albumdedup, "run", lambda *a, **k: 0), \
             mock.patch.object(convert, "run", lambda *a, **k: 0), \
             mock.patch.object(junk, "run", lambda *a, **k: 0), \
             mock.patch.object(verify, "run", lambda *a, **k: 0), \
             mock.patch.object(acousticbrainz, "run", lambda *a, **k: 0), \
             mock.patch.object(qa, "run", fake_qa):
            # first run: no watermark -> qa over whole library; watermark set afterwards
            self.assertEqual(pipeline.run(self.cfg), 0)
            self.assertEqual(seen["qa"], "")
            wm = state.get_watermark(self.cfg)
            self.assertTrue(wm)

            # second run: qa scoped to added since the watermark
            pipeline.run(self.cfg)
            self.assertTrue(seen["qa"].startswith("added:"))
            self.assertTrue(seen["qa"].endswith(".."))

            # full mode ignores the watermark
            pipeline.run(self.cfg, full=True)
            self.assertEqual(seen["qa"], "")

    def test_failed_import_aborts_before_qa_and_keeps_watermark(self):
        reached = {"qa": False}

        def fake_qa(c, scope="", cull=False):
            reached["qa"] = True
            return 0

        with mock.patch.object(import_, "run", lambda c, src=None, reimport=False: 2), \
             mock.patch.object(qa, "run", fake_qa):
            rc = pipeline.run(self.cfg)
        self.assertEqual(rc, 2)
        self.assertFalse(reached["qa"])                    # import failure aborts the pipeline
        self.assertIsNone(state.get_watermark(self.cfg))   # watermark NOT advanced


if __name__ == "__main__":
    unittest.main()
