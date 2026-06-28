import contextlib
import unittest
from pathlib import Path
from unittest import mock

from gbc.passes import upgrade
from tests.base import Base


class TestUpgradeDecision(unittest.TestCase):
    """Pure decision logic: format ladder + bitrate delta + lossless cutoff."""

    def test_rank(self):
        self.assertEqual(upgrade._rank(".flac"), 3)
        self.assertEqual(upgrade._rank(".MP3"), 2)
        self.assertEqual(upgrade._rank(".xyz"), 1)

    def test_lossless_beats_lossy_regardless_of_bitrate(self):
        self.assertTrue(upgrade._is_upgrade(3, 5, 2, 320))       # FLAC (tier decides) replaces a 320k MP3
        self.assertFalse(upgrade._is_upgrade(2, 320, 3, 5))      # never downgrade FLAC -> MP3

    def test_both_lossless_is_cutoff(self):
        self.assertFalse(upgrade._is_upgrade(3, 1100, 3, 800))   # bitrate of lossless is meaningless -> no churn

    def test_lossy_to_lossy_needs_a_clear_min_delta(self):
        self.assertTrue(upgrade._is_upgrade(2, 320, 2, 192))    # +128 >= 64 -> upgrade (the margin IS the safeguard)
        self.assertFalse(upgrade._is_upgrade(2, 320, 2, 280))   # +40 < 64 -> too small
        self.assertFalse(upgrade._is_upgrade(2, 192, 2, 320))   # lower -> never

    def test_artist_match_word_subset_not_substring(self):
        self.assertTrue(upgrade._artist_match("U2", "U2 feat. Mary J. Blige"))   # word subset -> same artist
        self.assertTrue(upgrade._artist_match("Daft Punk", "daft punk"))
        self.assertFalse(upgrade._artist_match("Eve", "Steve"))                  # naive substring would mis-match
        self.assertFalse(upgrade._artist_match("U2", "Muse"))

    def test_cross_codec_uses_effective_bitrate(self):
        # a 320k MP3 must NOT replace a 256k Opus -- Opus is more efficient per kbps
        mp3_320 = upgrade._eff(".mp3", 320)
        opus_256 = upgrade._eff(".opus", 256)
        self.assertGreater(opus_256, mp3_320)                                # 256k Opus > 320k MP3, effective
        self.assertFalse(upgrade._is_upgrade(2, mp3_320, 2, opus_256))       # MP3 320 does NOT beat Opus 256
        self.assertTrue(upgrade._is_upgrade(2, opus_256, 2, upgrade._eff(".mp3", 192)))  # Opus 256 beats MP3 192


class TestUpgradeRun(Base):
    def _patches(self, clean, folder, probe, extra=None):
        ps = [
            mock.patch.object(upgrade, "_clean_albums", lambda c: clean),
            mock.patch.object(upgrade, "_source_album_folders", lambda s: {folder: 3}),
            mock.patch.object(upgrade, "_probe", lambda f, c: probe),
        ]
        return ps + (extra or [])

    def _album(self, **over):
        a = {"artist": "U2", "album": "War", "year": "1983", "durs": [100, 200, 300],
             "rank": 2, "ext": ".mp3", "br": 320, "ebr": 320, "folder": Path("/clean/U2/War")}
        a.update(over)
        return a

    def test_reports_format_upgrade_dry(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        folder = self.cfg.src / "War"                            # name must align with the album title
        probe = {"durs": [100, 200, 300], "rank": 3, "ext": ".flac", "br": 900, "ebr": 900, "artist": "U2"}
        with contextlib.ExitStack() as es:
            for p in self._patches({"a1": self._album()}, folder, probe):
                es.enter_context(p)
            with self.assertLogs("gbc", "INFO") as cm:
                n = upgrade.run(self.cfg, apply=False)
        self.assertEqual(n, 1)
        self.assertIn("would upgrade U2 - War", "\n".join(cm.output))

    def test_apply_invokes_swap(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        folder = self.cfg.src / "War"
        probe = {"durs": [100, 200, 300], "rank": 3, "ext": ".flac", "br": 900, "ebr": 900, "artist": "U2"}
        swapped = []
        extra = [
            mock.patch.object(upgrade, "backup_db", lambda *a, **k: None),
            mock.patch.object(upgrade, "prune_empty_dirs", lambda *a, **k: None),
            mock.patch.object(upgrade, "_do_upgrade", lambda c, f, aid, a, log: swapped.append(aid) or True),
        ]
        with contextlib.ExitStack() as es:
            for p in self._patches({"a1": self._album()}, folder, probe, extra):
                es.enter_context(p)
            n = upgrade.run(self.cfg, apply=True)
        self.assertEqual(n, 1)
        self.assertEqual(swapped, ["a1"])

    def test_do_upgrade_restores_clean_copy_when_reimport_adds_nothing(self):
        """Re-import adds no album (dup-skipped/weak) -> RESTORE the clean copy, not leave it removed (the
        -7-albums data-safety regression)."""
        clean_folder = self.cfg.clean / "Artist" / "Album (2001)"
        clean_folder.mkdir(parents=True)
        a = {"folder": str(clean_folder), "artist": "Artist", "album": "Album"}
        moves, beet = [], []
        with mock.patch.object(upgrade, "safe_move", lambda s, d, log: moves.append((Path(s), Path(d))) or True), \
             mock.patch.object(upgrade, "run_beet", lambda c, args, **k: beet.append(args) or (0, "")), \
             mock.patch.object(upgrade.import_, "run", lambda c, **k: 0), \
             mock.patch.object(upgrade, "_album_ids", lambda c: {"x"}):     # same before+after -> no new album
            ok = upgrade._do_upgrade(self.cfg, Path("/src/folder"), "99", a, mock.Mock())
        self.assertFalse(ok)                                        # not upgraded
        self.assertEqual(len(moves), 2)                            # clean -> quarantine, then quarantine -> clean
        self.assertEqual(moves[1][1], clean_folder)                # the 2nd move RESTORES the clean folder
        self.assertTrue(any("import" in args for args in beet))     # re-registered the lib row in place

    def test_wma_source_is_never_an_upgrade(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        folder = self.cfg.src / "War"
        # WMA 320 (eff 288) would beat the clean MP3 128 on bitrate, but WMA -> Opus transcode = loss -> skip it
        probe = {"durs": [100, 200, 300], "rank": 2, "ext": ".wma", "br": 320,
                 "ebr": upgrade._eff(".wma", 320), "artist": "U2"}
        with contextlib.ExitStack() as es:
            for p in self._patches({"a1": self._album(rank=2, ext=".mp3", br=128, ebr=128)}, folder, probe):
                es.enter_context(p)
            n = upgrade.run(self.cfg, apply=False)               # a WMA source is always excluded
        self.assertEqual(n, 0)

    def test_no_upgrade_when_source_is_worse(self):
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        folder = self.cfg.src / "War"
        probe = {"durs": [100, 200, 300], "rank": 2, "ext": ".mp3", "br": 192, "ebr": 192, "artist": "U2"}
        with contextlib.ExitStack() as es:
            for p in self._patches({"a1": self._album(rank=3, ext=".flac", br=900, ebr=900)}, folder, probe):
                es.enter_context(p)
            n = upgrade.run(self.cfg, apply=False)
        self.assertEqual(n, 0)

    def test_correlates_via_probed_album_tag_when_folder_name_is_junk(self):
        # the common real case: a junky source folder name that won't strict-title-match the clean album,
        # but the file's album TAG does -> must still correlate (the #3 false-negative fix)
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        folder = self.cfg.src / "U2 - War (1983) [FLAC]"
        probe = {"durs": [100, 200, 300], "rank": 3, "ext": ".flac", "br": 900, "ebr": 900,
                 "artist": "U2", "album": "War"}
        with contextlib.ExitStack() as es:
            for p in self._patches({"a1": self._album()}, folder, probe):
                es.enter_context(p)
            n = upgrade.run(self.cfg, apply=False)
        self.assertEqual(n, 1)                           # fired via the probed album tag, not the folder name

    def test_wrong_artist_blocks_correlation(self):
        # same track count + durations + title, but a DIFFERENT artist -> must NOT correlate (no wrong swap)
        self.cfg.src.mkdir(parents=True, exist_ok=True)
        folder = self.cfg.src / "War"
        probe = {"durs": [100, 200, 300], "rank": 3, "ext": ".flac", "br": 900, "ebr": 900, "artist": "Some Band"}
        with contextlib.ExitStack() as es:
            for p in self._patches({"a1": self._album()}, folder, probe):     # clean artist = "U2"
                es.enter_context(p)
            n = upgrade.run(self.cfg, apply=False)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
