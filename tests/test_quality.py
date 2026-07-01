import unittest

from gbc import quality, sidecars


class TestRank(unittest.TestCase):
    """rank() tiers a format: lossless=3 > lossy=2 > unknown=1. The keep-vs-drop decision in dedup/upgrade
    branches on this, so a lossy audiobook (.m4b) misclassified as unknown (1) would let a lossless copy be
    dropped in its favour by an efficiency tiebreak, or vice versa."""

    def test_lossy_formats_rank_2(self):
        for ext in (".m4b", ".m4a", ".mp3", ".opus", ".aac"):
            self.assertEqual(quality.rank(ext), 2, f"{ext} must rank as lossy")

    def test_lossless_formats_rank_3(self):
        for ext in (".flac", ".alac", ".wav"):
            self.assertEqual(quality.rank(ext), 3, f"{ext} must rank as lossless")

    def test_unknown_ranks_1(self):
        self.assertEqual(quality.rank(".txt"), 1)

    def test_rank_is_case_insensitive(self):
        self.assertEqual(quality.rank(".M4B"), 2)

    def test_m4b_in_lossy_set(self):
        self.assertIn(".m4b", quality.LOSSY)
        self.assertNotIn(".m4b", quality.LOSSLESS)


class TestAudioCoverage(unittest.TestCase):
    """Every audio extension gbc recognises (sidecars.AUDIO) must be classified as either lossless or lossy --
    an uncovered ext falls through rank() to tier 1 (unknown) and would be wrongly discarded against anything
    else during dedup. Invariant: AUDIO - LOSSLESS - LOSSY == empty set."""

    def test_every_audio_ext_is_classified(self):
        unclassified = sidecars.AUDIO - quality.LOSSLESS - quality.LOSSY
        self.assertEqual(unclassified, set(), f"unclassified audio exts: {sorted(unclassified)}")

    def test_lossless_and_lossy_are_disjoint(self):
        self.assertEqual(quality.LOSSLESS & quality.LOSSY, set())


if __name__ == "__main__":
    unittest.main()
