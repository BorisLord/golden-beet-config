import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from gbc import mb


class TestMb(unittest.TestCase):
    def test_release_recordings_collects_all_media(self):
        data = {"media": [
            {"tracks": [{"recording": {"id": "r1"}}, {"recording": {"id": "r2"}}]},
            {"tracks": [{"recording": {"id": "r3"}}]},                                  # second disc
        ]}
        with mock.patch.object(mb, "get", lambda p: data), \
             mock.patch.object(mb.time, "sleep", lambda *a: None):
            self.assertEqual(mb.release_recordings("rel1"), frozenset({"r1", "r2", "r3"}))

    def test_release_recordings_empty_on_fetch_error(self):
        with mock.patch.object(mb, "get", mock.Mock(side_effect=OSError("boom"))):
            self.assertEqual(mb.release_recordings("rel1"), frozenset())

    def test_missing_recordings_returns_absent_tracklist(self):
        with mock.patch.object(mb, "release_recordings", lambda a: frozenset({"r1", "r2", "r3"})):
            self.assertEqual(mb.missing_recordings("rel", {"r1", "r2"}), frozenset({"r3"}))   # missing r3
            self.assertEqual(mb.missing_recordings("rel", {"r1", "r2", "r3"}), frozenset())   # complete

    def test_missing_recordings_none_when_tracklist_unknown(self):
        with mock.patch.object(mb, "release_recordings", lambda a: frozenset()):              # fetch failed/empty
            self.assertIsNone(mb.missing_recordings("rel", {"r1"}))                            # -> caller leaves it

    def test_missing_recordings_cache_avoids_refetch(self):
        calls = []
        cache: dict = {}
        with mock.patch.object(mb, "release_recordings", lambda a: calls.append(a) or frozenset({"r1"})):
            mb.missing_recordings("rel", set(), cache)
            mb.missing_recordings("rel", set(), cache)
        self.assertEqual(calls, ["rel"])                                                       # fetched once

    def test_get_retries_transient_then_succeeds(self):
        import urllib.error

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": 1}'

        calls = {"n": 0}

        def fake_urlopen(req, timeout=0):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("503-ish transient")    # MB rate-limiter hiccup
            return _Resp()

        with mock.patch.object(mb.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(mb.time, "sleep", lambda *a: None):
            self.assertEqual(mb.get("release/x"), {"ok": 1})
        self.assertEqual(calls["n"], 3)                              # two failures retried, third succeeded


class TestReleaseCache(unittest.TestCase):
    """The persisted MB tracklist cache shared by verify (demote) + singletons (promote)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = types.SimpleNamespace(beetsdir=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrips_as_frozensets(self):
        mb.save_release_cache(self.cfg, {"a": frozenset({"r1", "r2"})})
        self.assertEqual(mb.load_release_cache(self.cfg), {"a": frozenset({"r1", "r2"})})

    def test_missing_file_loads_empty(self):
        self.assertEqual(mb.load_release_cache(self.cfg), {})

    def test_empty_tracklist_not_persisted(self):
        # a fetch failure is cached as an empty frozenset WITHIN a run (no mid-run retry) but must NOT be written
        # -> it gets retried next run instead of permanently poisoning the cache
        mb.save_release_cache(self.cfg, {"a": frozenset({"r1"}), "bad": frozenset()})
        loaded = mb.load_release_cache(self.cfg)
        self.assertEqual(loaded, {"a": frozenset({"r1"})})
        self.assertNotIn("bad", loaded)

    def test_refresh_ignores_persisted(self):
        mb.save_release_cache(self.cfg, {"a": frozenset({"r1"})})
        self.assertEqual(mb.load_release_cache(self.cfg, refresh=True), {})   # `--all` re-pulls from MB


if __name__ == "__main__":
    unittest.main()
