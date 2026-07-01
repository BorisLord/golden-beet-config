import os
import unittest
from unittest import mock

from gbc import admin, cli
from gbc import config as configmod
from gbc.passes import (
    acousticbrainz,
    albumdedup,
    convert,
    import_,
    nova,
    pipeline,
    qa,
    singletons,
    upgrade,
    verify,
)
from tests.base import Base


class TestCliDispatch(Base):
    """The CLI routes each subcommand to the right code path (passes/lock mocked -- no beets, no network)."""

    def test_run_routes_full_and_reimport_flags(self):
        seen = {}

        def fake_pipeline(c, *, full=False, src=None, reimport=False, do_import=True):
            seen.update(full=full, reimport=reimport, do_import=do_import)
            return 0

        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(pipeline, "run", fake_pipeline):
            self.assertEqual(cli.main(["run", "--all", "--reimport"]), 0)
        self.assertTrue(seen.get("full"))            # --all -> full=True
        self.assertTrue(seen.get("reimport"))        # --reimport -> reimport=True
        self.assertTrue(seen.get("do_import"))       # no --no-import -> import runs

    def test_qa_routes_with_scope(self):
        seen = {}
        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(qa, "run", lambda c, scope="": seen.update(scope=scope) or 0):
            self.assertEqual(cli.main(["qa", "added:x.."]), 0)
        self.assertEqual(seen.get("scope"), "added:x..")

    def test_import_takes_lock_and_routes(self):
        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(import_, "run", lambda c, src=None, reimport=False: 0):
            self.assertEqual(cli.main(["import"]), 0)

    def test_singletons_takes_lock_and_routes(self):
        seen = {}
        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(singletons, "run",
                               lambda c, src=None, reimport=False, apply=False:
                               seen.update(src=src, reimport=reimport, apply=apply) or 0):
            self.assertEqual(cli.main(["singletons", "/x", "--reimport", "--apply"]), 0)
        self.assertEqual(seen.get("src"), "/x")
        self.assertTrue(seen.get("reimport"))
        self.assertTrue(seen.get("apply"))

    def test_convert_takes_lock_and_routes(self):
        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(convert, "run", lambda c: 0):
            self.assertEqual(cli.main(["convert"]), 0)

    def test_run_no_import_flag_skips_import(self):
        seen = {}
        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(pipeline, "run",
                               lambda c, *, full=False, src=None, reimport=False, do_import=True:
                               seen.update(do_import=do_import) or 0):
            self.assertEqual(cli.main(["run", "--no-import"]), 0)
        self.assertFalse(seen.get("do_import"))      # --no-import -> post-import passes only

    def test_action_passes_exit_0_despite_positive_count(self):
        """An action pass RETURNS a count of items acted on; the CLI must NOT leak it as the process
        exit code -- `gbc acousticbrainz` enriching 109 recordings is success, not exit 109 (which
        cron/systemd/`&&` read as failure). Every action command exits 0 once the pass completes."""
        for argv, modobj in [(["verify"], verify), (["acousticbrainz"], acousticbrainz),
                             (["albumdedup"], albumdedup), (["upgrade"], upgrade),
                             (["nova"], nova), (["singletons"], singletons)]:
            with mock.patch.object(cli, "configure", lambda *a, **k: None), \
                 mock.patch.object(configmod, "load", lambda: self.cfg), \
                 mock.patch.object(modobj, "run", lambda *a, **k: 109):
                self.assertEqual(cli.main(argv), 0, f"{argv[0]} must exit 0 despite a count of 109")

    def test_albumdedup_takes_lock_and_routes(self):
        seen = {}
        with mock.patch.object(cli, "configure", lambda *a, **k: None), \
             mock.patch.object(configmod, "load", lambda: self.cfg), \
             mock.patch.object(albumdedup, "run", lambda c, do_apply=True: seen.update(do_apply=do_apply) or 0):
            self.assertEqual(cli.main(["albumdedup"]), 0)
            self.assertTrue(seen["do_apply"])         # default -> move duplicates to quarantine
            self.assertEqual(cli.main(["albumdedup", "--pretend"]), 0)
            self.assertFalse(seen["do_apply"])        # --pretend -> report only


class TestAdminInit(Base):
    def test_init_deploys_patched_config(self):
        cenv = self.tmp / "config.env"          # point resolution at a tmp file so init won't write the repo's
        cenv.write_text('MUSIC_CLEAN="${MUSIC_CLEAN:-/x}"\n')
        with mock.patch.dict(os.environ, {"GBC_CONFIG": str(cenv)}):
            self.assertEqual(admin.init(self.cfg, cron=False), 0)
        deployed = self.cfg.beetsdir / "config.yaml"
        self.assertTrue(deployed.exists())
        text = deployed.read_text()
        self.assertIn(f"directory: {self.cfg.clean}", text)                       # patched
        self.assertIn(f"log: {self.cfg.log_dir}/import-decisions.log", text)      # patched
        for d in (self.cfg.src, self.cfg.clean, self.cfg.dump, self.cfg.log_dir):
            self.assertTrue(d.is_dir())                                           # dirs created
        self.assertTrue((self.cfg.beetsdir / "qa.yaml").exists())                 # qa overlay deployed

    def test_init_fills_api_keys_from_config_env(self):
        cenv = self.tmp / "config.env"
        cenv.write_text('MUSIC_CLEAN="${MUSIC_CLEAN:-/x}"\nDISCOGS_TOKEN="mytoken123"\nLASTFM_KEY=""\n')
        with mock.patch.dict(os.environ, {"GBC_CONFIG": str(cenv)}):
            admin.init(self.cfg, cron=False)
        text = (self.cfg.beetsdir / "config.yaml").read_text()
        self.assertIn("user_token: mytoken123", text)            # Discogs token injected from config.env
        self.assertNotRegex(text, r"(?m)^\s*user_token:\s*REPLACE_ME\s*$")   # field line filled (comment may keep it)
        self.assertRegex(text, r"(?m)^\s*lastfm_key:\s*REPLACE_ME\s*$")      # empty key -> field placeholder kept
        self.assertRegex(text, r"(?m)^\s*fanarttv_key:\s*REPLACE_ME\s*$")    # absent key -> field placeholder kept

    def test_init_treats_path_with_regex_backreference_literally(self):
        """A clean/log path containing a regex backreference-like sequence (e.g. '\\g<0>') must be written
        VERBATIM into config.yaml's `directory:`/`log:` lines. The fill uses a replacement FUNCTION, not a
        replacement string -- so re.sub never interprets the value as a backref (old code: '\\g<0>' expanded
        to the whole matched line; '\\1' raised re.error and aborted init)."""
        weird_clean = self.tmp / r"cl\g<0>ean"
        weird_logs = self.tmp / r"lo\g<0>gs"
        self.cfg.clean = weird_clean
        self.cfg.log_dir = weird_logs
        cenv = self.tmp / "config.env"
        cenv.write_text('MUSIC_CLEAN="${MUSIC_CLEAN:-/x}"\n')
        with mock.patch.dict(os.environ, {"GBC_CONFIG": str(cenv)}):
            self.assertEqual(admin.init(self.cfg, cron=False), 0)   # no re.error raised
        text = (self.cfg.beetsdir / "config.yaml").read_text()
        self.assertIn(f"directory: {weird_clean}", text)           # backref-like path kept literal
        self.assertIn(f"log: {weird_logs}/import-decisions.log", text)


class TestAdminUninstallPurge(Base):
    """`uninstall --purge` may rmtree the beets config dir, but NEVER when doing so would reach into a music
    dir (src/clean/dump). Overlap in EITHER direction (beetsdir is an ancestor of, or nested inside, a music
    dir) must refuse the rmtree; a disjoint beetsdir is removed."""

    def _cfg(self, beetsdir, clean):
        return configmod.Config(beet="beet", beetsdir=beetsdir, src=self.tmp / "src",
                                clean=clean, dump=self.tmp / "dump", log_dir=self.tmp / "logs")

    def _uninstall(self, cfg):
        # keep it hermetic: never touch the real crontab or the repo's config.env.
        with mock.patch.object(admin.shutil, "which", lambda _n: None), \
             mock.patch.object(admin, "config_path", lambda: None):
            return admin.uninstall(cfg, purge=True)

    def test_purge_refuses_when_beetsdir_is_parent_of_music(self):
        # music nested UNDER beetsdir -> rmtree(beetsdir) would delete the music. Must refuse.
        beetsdir = self.tmp / "beets"
        clean = beetsdir / "music"
        clean.mkdir(parents=True)
        track = clean / "song.flac"
        track.write_bytes(b"audio")
        self._uninstall(self._cfg(beetsdir, clean))
        self.assertTrue(track.exists())              # music survived the refused purge
        self.assertTrue(beetsdir.is_dir())           # beetsdir NOT rmtree'd

    def test_purge_refuses_when_beetsdir_inside_music(self):
        # beetsdir nested INSIDE clean -> rmtree(beetsdir) carves a hole out of the music tree. Must refuse.
        clean = self.tmp / "clean"
        beetsdir = clean / "catalog"
        beetsdir.mkdir(parents=True)
        marker = beetsdir / "library.db"
        marker.write_bytes(b"db")
        self._uninstall(self._cfg(beetsdir, clean))
        self.assertTrue(marker.exists())             # beetsdir (inside music) left intact
        self.assertTrue(clean.is_dir())

    def test_purge_removes_disjoint_beetsdir(self):
        # beetsdir disjoint from every music dir -> safe to remove; music untouched.
        beetsdir = self.tmp / "beets"
        clean = self.tmp / "clean"
        beetsdir.mkdir(parents=True)
        (beetsdir / "library.db").write_bytes(b"db")
        clean.mkdir(parents=True)
        track = clean / "song.flac"
        track.write_bytes(b"audio")
        self._uninstall(self._cfg(beetsdir, clean))
        self.assertFalse(beetsdir.exists())          # disjoint config dir removed
        self.assertTrue(track.exists())              # music untouched


if __name__ == "__main__":
    unittest.main()
