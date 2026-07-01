import json
import unittest
from unittest import mock

from gbc.passes import nova
from tests.base import Base


def _fake_mb(path):
    if "series/" in path:
        return {"relations": [
            {"release_group": {"id": "rg1", "title": "Nova Tunes 99"}},
            {"release_group": {"id": "rgbox", "title": "Nova Tunes 01_10"}},   # box set -> must be skipped
        ]}
    if path.startswith("release?release-group="):
        return {"releases": [{"id": "rel1"}]}
    if path.startswith("release/rel1"):
        return {"media": [{"tracks": [
            {"position": 1, "recording": {"id": "recA"}},
            {"position": 2, "recording": {"id": "recB"}},
        ]}]}
    return {}


class TestNova(Base):
    def test_build_cache_skips_box_sets_and_records_albumid(self):
        with mock.patch.object(nova.mb, "get", _fake_mb), \
             mock.patch.object(nova.time, "sleep", lambda *a: None):
            cache = nova._build_cache(self.cfg, mock.MagicMock())
        self.assertEqual(cache["recA"], {"compil": "Nova Tunes 99", "track": 1, "total": 2, "albumid": "rel1"})
        self.assertIn("recB", cache)
        self.assertNotIn("rgbox", str(cache))            # box set excluded
        self.assertEqual(json.loads((self.cfg.beetsdir / nova.CACHE).read_text()), cache)

    def test_run_classifies_complete_vs_partial(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / nova.CACHE).write_text(json.dumps({
            "r1": {"compil": "Nova Tunes A", "track": 1, "total": 2, "albumid": "relA"},
            "r2": {"compil": "Nova Tunes A", "track": 2, "total": 2, "albumid": "relA"},
            "r3": {"compil": "Nova Tunes B", "track": 1, "total": 2, "albumid": "relB"},
            "r4": {"compil": "Nova Tunes B", "track": 2, "total": 2, "albumid": "relB"},
        }))
        ls = "r1\tTrue\nr2\tTrue\nr3\tTrue\nr4\tFalse\n"      # r4 = compil B track already in a clean album
        with mock.patch.object(nova, "run_beet", lambda c, a, **k: (0, ls)), \
             self.assertLogs("gbc", "INFO") as cm:
            rc = nova.run(self.cfg)
        self.assertEqual(rc, 0)
        out = "\n".join(cm.output)
        self.assertIn("COMPLETE -> _Various Artists/: Nova Tunes A", out)        # 2/2 loose
        self.assertIn("partial (1/2 loose) -> _Singles/Various Artists/: Nova Tunes B", out)

    def test_reroute_retags_only_loose_nova_singletons(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / nova.CACHE).write_text(json.dumps({
            "rA": {"compil": "Nova Tunes 5", "track": 1, "total": 2, "albumid": "relX"},
        }))
        calls = []

        def fake(_c, a, **_k):
            if a[:2] == ["ls", "-f"]:
                return (0, "10\trA\n11\trZ\n")              # singleton 10 = Nova rA; 11 = rZ not a Nova track
            calls.append(a)
            return (0, "")

        with mock.patch.object(nova, "run_beet", fake):
            n = nova.reroute(self.cfg, mock.MagicMock(), apply=True)
        self.assertEqual(n, 1)
        self.assertEqual(len(calls), 1)                     # only the one Nova singleton re-tagged
        mod = calls[0]
        self.assertEqual(mod[0], "modify")
        self.assertIn("id:10", mod)
        self.assertIn("mb_albumid=relX", mod)
        self.assertIn("album=Nova Tunes 5", mod)
        self.assertIn("albumartist=Various Artists", mod)

    def test_network_failure_returns_1(self):
        with mock.patch.object(nova, "_build_cache", mock.Mock(side_effect=OSError("no net"))):
            self.assertEqual(nova.run(self.cfg, refresh=True), 1)

    def test_build_cache_numbers_tracks_absolutely_across_discs(self):
        """A multi-disc (box-less) release has MB `position` RESET per disc (disc2 tracks are 1,2,3 again).
        _build_cache must assign UNIQUE absolute track numbers over the FLATTENED media (1..N), never the
        per-disc position -- otherwise disc1 t1 and disc2 t1 collide and later tracks overwrite earlier cache
        entries."""
        def fake_mb(path):
            if "series/" in path:
                return {"relations": [{"release_group": {"id": "rgm", "title": "Nova Tunes 42"}}]}
            if path.startswith("release?release-group="):
                return {"releases": [{"id": "relm"}]}
            if path.startswith("release/relm"):
                return {"media": [
                    {"tracks": [{"position": 1, "recording": {"id": "d1a"}},
                                {"position": 2, "recording": {"id": "d1b"}},
                                {"position": 3, "recording": {"id": "d1c"}}]},
                    {"tracks": [{"position": 1, "recording": {"id": "d2a"}},   # position RESETS on disc 2
                                {"position": 2, "recording": {"id": "d2b"}}]},
                ]}
            return {}

        with mock.patch.object(nova.mb, "get", fake_mb), \
             mock.patch.object(nova.time, "sleep", lambda *a: None):
            cache = nova._build_cache(self.cfg, mock.MagicMock())
        self.assertEqual(len(cache), 5)                                        # no entry overwritten
        tracks = {rid: e["track"] for rid, e in cache.items()}
        self.assertEqual(tracks, {"d1a": 1, "d1b": 2, "d1c": 3, "d2a": 4, "d2b": 5})
        self.assertEqual(sorted(e["track"] for e in cache.values()), [1, 2, 3, 4, 5])  # full 1..N, no collision
        self.assertTrue(all(e["total"] == 5 for e in cache.values()))         # grand total, not per-disc


if __name__ == "__main__":
    unittest.main()
