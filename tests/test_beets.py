import unittest

from gbc.beets import run_beet
from tests.base import Base


class TestBeetsRunner(Base):
    def test_merges_stdout_and_stderr(self):
        self.cfg.beet = self.fake_beet(stdout="OUT line\n", stderr="ERR line\n", code=0)
        rc, text = run_beet(self.cfg, ["whatever"], passname="test", echo_lines=False)
        self.assertEqual(rc, 0)
        # both streams captured -- the old bash gate dropped stderr, which caused the import bug
        self.assertIn("OUT line", text)
        self.assertIn("ERR line", text)

    def test_merge_stderr_false_keeps_stdout_clean(self):
        # `beet config` -> YAML on stdout; a warning on stderr must NOT pollute it (else beetscfg mis-parses)
        self.cfg.beet = self.fake_beet(stdout="import:\n  copy: yes\n", stderr="discogs: deprecated noise\n", code=0)
        _, text = run_beet(self.cfg, ["config"], passname="test", echo_lines=False, merge_stderr=False)
        self.assertIn("copy: yes", text)
        self.assertNotIn("deprecated noise", text)

    def test_returns_nonzero_code(self):
        self.cfg.beet = self.fake_beet(stderr="boom\n", code=3)
        rc, text = run_beet(self.cfg, ["x"], passname="test", echo_lines=False)
        self.assertEqual(rc, 3)
        self.assertIn("boom", text)

    def test_missing_beet_gives_clear_error(self):
        self.cfg.beet = "definitely-not-a-real-binary-xyz"
        with self.assertRaises(RuntimeError) as ctx:
            run_beet(self.cfg, ["ls"], passname="test", echo_lines=False)
        self.assertIn("beet not found", str(ctx.exception))

    def test_passes_overlay_and_beetsdir(self):
        script = self.tmp / "echobeet"
        script.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                          "print('ARGS', ' '.join(sys.argv[1:]))\n"
                          "print('BEETSDIR', os.environ.get('BEETSDIR'))\n")
        script.chmod(0o755)
        self.cfg.beet = str(script)
        _, text = run_beet(self.cfg, ["import", "-q"], overlay="fetchart-fs.yaml", passname="test", echo_lines=False)
        self.assertIn("-c", text)
        self.assertIn("fetchart-fs.yaml", text)
        self.assertIn("import -q", text)
        self.assertIn(f"BEETSDIR {self.cfg.beetsdir}", text)

    def test_check_true_raises_on_nonzero_exit(self):
        # a failed scope query must ABORT the pass (so the pipeline HOLDS the watermark) rather than look like
        # an empty scope; check=True turns a nonzero exit into a RuntimeError
        self.cfg.beet = self.fake_beet(stderr="query blew up\n", code=2)
        with self.assertRaises(RuntimeError) as ctx:
            run_beet(self.cfg, ["ls", "-f", "$id", "bogus::query"], passname="test", echo_lines=False, check=True)
        self.assertIn("rc=2", str(ctx.exception))     # the real nonzero code is surfaced in the abort message

    def test_check_true_does_not_raise_on_success(self):
        # check=True aborts ONLY on failure -- a clean rc=0 run still returns (rc, text) normally
        self.cfg.beet = self.fake_beet(stdout="ok line\n", code=0)
        rc, text = run_beet(self.cfg, ["ls"], passname="test", echo_lines=False, check=True)
        self.assertEqual(rc, 0)
        self.assertIn("ok line", text)


if __name__ == "__main__":
    unittest.main()
