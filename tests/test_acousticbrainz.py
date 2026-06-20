import json
import unittest
from unittest import mock

from gbc.passes import acousticbrainz as ab
from tests.base import Base

# A merged low+high-level AB document (the shape ABSCHEME maps against). Includes fields we deliberately
# DROP (gender, average_loudness) to prove the curated scheme ignores them.
DOC = {
    "highlevel": {
        "danceability": {"all": {"danceable": 0.8763, "not_danceable": 0.1237}, "value": "not_danceable"},
        "gender": {"value": "female", "all": {"female": 0.65, "male": 0.35}},
        "mood_happy": {"all": {"happy": 0.05, "not_happy": 0.95}, "value": "not_happy"},
        "voice_instrumental": {"value": "instrumental"},
    },
    "lowlevel": {"average_loudness": 0.0145},
    "rhythm": {"bpm": 83.735},
    "tonal": {"key_key": "F#", "key_scale": "major", "key_strength": 0.71},
}


class TestMapping(unittest.TestCase):
    def test_maps_curated_fields(self):
        f = ab._fields_for(DOC)
        self.assertEqual(f["danceable"], 0.8763)          # "all" leaf -> positive-class probability
        self.assertEqual(f["mood_happy"], 0.05)
        self.assertEqual(f["voice_instrumental"], "instrumental")
        self.assertEqual(f["bpm"], 83.735)
        self.assertEqual(f["initial_key"], "F#")          # beets-canonical key (major root, sharp kept)
        self.assertEqual(f["key_strength"], 0.71)
        self.assertNotIn("not_danceable", f)              # only scheme leaves are kept
        self.assertNotIn("gender", f)                     # curated out (not musically useful)
        self.assertNotIn("average_loudness", f)           # curated out (redundant with ReplayGain)

    def test_minor_key_canonical(self):
        doc = {"tonal": {"key_key": "C", "key_scale": "minor"}}
        self.assertEqual(ab._fields_for(doc)["initial_key"], "Cm")

    def test_assign_formats_bpm_and_floats(self):
        self.assertEqual(ab._assign("bpm", 83.735), "bpm=84")            # rounded int media field
        self.assertEqual(ab._assign("danceable", 0.8763), "danceable=0.876300")
        self.assertEqual(ab._assign("initial_key", "F# major"), "initial_key=F# major")


class TestRun(Base):
    def _run(self, mbids, fetch):
        """Drive run() with a fake `beet` (ls -> mbids, modify -> recorded) and a stubbed _fetch."""
        calls = []

        def fake_run_beet(cfg, args, **k):
            calls.append(args)
            if args and args[0] == "ls":
                return 0, "\n".join(mbids)
            return 0, ""

        with mock.patch.object(ab, "run_beet", fake_run_beet), \
             mock.patch.object(ab, "_fetch", fetch):
            n = ab.run(self.cfg)
        return n, calls

    def test_enriches_present_and_caches(self):
        n, calls = self._run(["mbA", "mbB"], lambda batch: {"mbA": DOC})  # only mbA known to AB
        self.assertEqual(n, 1)
        modifies = [c for c in calls if c and c[0] == "modify"]
        self.assertEqual(len(modifies), 1)
        self.assertEqual(modifies[0][:3], ["modify", "-y", "mb_trackid:mbA"])
        self.assertIn("bpm=84", modifies[0])
        self.assertIn("initial_key=F#", modifies[0])
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text())
        self.assertIsNone(cache["mbB"])                   # confirmed absent -> cached as None
        self.assertEqual(cache["mbA"]["voice_instrumental"], "instrumental")

    def test_network_failure_not_cached(self):
        n, calls = self._run(["mbA"], lambda batch: None)  # AB unreachable -> pending, not cached
        self.assertEqual(n, 0)
        self.assertFalse([c for c in calls if c and c[0] == "modify"])
        cache = json.loads((self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").read_text() or "{}") \
            if (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").exists() else {}
        self.assertNotIn("mbA", cache)                    # left uncached -> retried next run

    def test_uses_cache_without_refetch(self):
        (self.cfg.beetsdir).mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / "gbc-acousticbrainz-cache.json").write_text(
            json.dumps({"mbA": {"bpm": 90, "gender": "male"}}))

        def boom(batch):
            raise AssertionError("should not fetch a cached mbid")

        n, calls = self._run(["mbA"], boom)
        self.assertEqual(n, 1)
        self.assertIn("bpm=90", next(c for c in calls if c and c[0] == "modify"))

    def test_no_mbids_in_scope(self):
        n, _ = self._run([], lambda batch: {})
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
