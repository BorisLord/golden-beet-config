import importlib
import py_compile
import unittest
from pathlib import Path

MODULES = [
    "gbc", "gbc.cli", "gbc.config", "gbc.logs", "gbc.beets",
    "gbc.lock", "gbc.state", "gbc.util", "gbc.admin", "gbc.sidecars",
    "gbc.anomaly", "gbc.dedup", "gbc.passes.import_",
    "gbc.passes.qa", "gbc.passes.pipeline", "gbc.passes.inbox",
    "gbc.passes.convert", "gbc.passes.verify", "gbc.passes.acousticbrainz",
]


class TestSmoke(unittest.TestCase):
    def test_every_module_imports(self):
        for m in MODULES:
            importlib.import_module(m)

    def test_cli_parser_builds(self):
        from gbc.cli import _build_parser
        parser = _build_parser()
        for cmd in ("run", "inbox", "import", "qa", "anomaly", "convert", "verify", "acousticbrainz",
                    "init", "uninstall"):
            self.assertTrue(parser.parse_args([cmd]))

    def test_helpers_have_valid_syntax(self):
        helpers = sorted((Path(__file__).resolve().parents[1] / "helpers").glob("*.py"))
        self.assertTrue(helpers, "no helper scripts found")
        for h in helpers:
            py_compile.compile(str(h), doraise=True)


if __name__ == "__main__":
    unittest.main()
