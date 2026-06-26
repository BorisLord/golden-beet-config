import os
import unittest
from unittest import mock

from gbc import admin, cli
from gbc import config as configmod
from gbc.passes import albumdedup, convert, import_, pipeline, qa, singletons
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


if __name__ == "__main__":
    unittest.main()
