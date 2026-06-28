import unittest
from unittest import mock

from gbc import probe
from tests.base import Base

_PROBE = {"title": "T", "length": 100, "bitrate": 320, "artist": "A", "album": "Alb", "year": "2020", "ext": ".mp3"}


class TestProbeCache(Base):
    def _file(self, name="a.mp3"):
        p = self.tmp / name
        p.write_text("x")
        return p

    def test_miss_reads_then_hit_serves_cached(self):
        p = self._file()
        calls = []
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: calls.append(fp) or dict(_PROBE))):
            c = probe.ProbeCache(self.tmp / "c.json")
            pr1 = c.get(p)
            pr2 = c.get(p)                          # 2nd get = cache hit -> no re-read
        self.assertEqual(pr1, probe.Probe(**_PROBE))
        self.assertEqual(pr2, pr1)
        self.assertEqual(len(calls), 1)            # read EXACTLY once

    def test_unreadable_cached_as_sentinel_not_reread(self):
        p = self._file()
        calls = []
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: calls.append(fp) or None)):
            c = probe.ProbeCache(self.tmp / "c.json")
            self.assertIsNone(c.get(p))
            self.assertIsNone(c.get(p))            # False sentinel hit -> not re-probed
        self.assertEqual(len(calls), 1)

    def test_mtime_or_size_change_invalidates(self):
        p = self._file()
        calls = []
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: calls.append(1) or dict(_PROBE))):
            c = probe.ProbeCache(self.tmp / "c.json")
            c.get(p)
            p.write_text("xxxxxx")                  # size (and mtime) change -> new key -> re-read
            c.get(p)
        self.assertEqual(len(calls), 2)

    def test_missing_file_returns_none(self):
        c = probe.ProbeCache(self.tmp / "c.json")
        self.assertIsNone(c.get(self.tmp / "nope.mp3"))   # stat fails -> None, nothing cached

    def test_save_roundtrips_and_serves_without_reread(self):
        p = self._file()
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: dict(_PROBE))):
            c = probe.ProbeCache(self.tmp / "c.json")
            c.get(p)
            c.save()
        self.assertTrue((self.tmp / "c.json").exists())
        c2 = probe.ProbeCache(self.tmp / "c.json")        # reload from disk; _read NOT patched here
        calls = []
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: calls.append(1) or None)):
            self.assertEqual(c2.get(p), probe.Probe(**_PROBE))   # same mtime/size -> served from persisted cache
        self.assertEqual(calls, [])                       # never re-read

    def test_none_path_is_in_memory_and_save_noop(self):
        c = probe.ProbeCache(None)
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: dict(_PROBE))):
            self.assertEqual(c.get(self._file()), probe.Probe(**_PROBE))
        c.save()                                          # no path -> no crash, writes nothing

    def test_corrupt_non_dict_cache_starts_empty(self):
        cpath = self.tmp / "c.json"
        cpath.write_text("[]")                            # valid JSON but a list, not a dict -> must not crash
        c = probe.ProbeCache(cpath)
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: dict(_PROBE))):
            self.assertEqual(c.get(self._file()), probe.Probe(**_PROBE))   # started from {}, works

    def test_save_evicts_stale_entries(self):
        p = self._file()
        cpath = self.tmp / "c.json"
        with mock.patch.object(probe.ProbeCache, "_read", staticmethod(lambda fp: dict(_PROBE))):
            c = probe.ProbeCache(cpath)
            c.get(p)                                      # one live entry (touched this run -> kept)
            c._c["/gone/x.mp3:123:45"] = dict(_PROBE)     # a stale entry: its file does not exist
            c._dirty = True
            c.save()                                      # eviction drops the stale, keeps the live one
        reloaded = probe.ProbeCache(cpath)._c
        self.assertNotIn("/gone/x.mp3:123:45", reloaded)  # missing-file key pruned -> no unbounded growth
        self.assertEqual(len(reloaded), 1)                # only p's live entry remains


if __name__ == "__main__":
    unittest.main()
