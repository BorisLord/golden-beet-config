"""Guard against drift between the two committed beets configs: beets/config.yaml (what `gbc init` deploys)
and golden-beets-config.yaml (the standalone, fully-commented reference the README points users at). They
legitimately differ only on `directory` (the clean-library path, set per environment) and the import-decisions
log filename; EVERY other setting must match, or the README reference silently lies about what gbc runs."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _settings(rel: str) -> dict:
    data = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
    data.pop("directory", None)                       # set per environment (setup.sh patch / standalone user)
    if isinstance(data.get("import"), dict):
        data["import"].pop("log", None)               # log filename differs (deployed vs standalone)
    return data


class TestConfigParity(unittest.TestCase):
    def test_reference_matches_deployed(self):
        deployed = _settings("beets/config.yaml")
        reference = _settings("golden-beets-config.yaml")
        self.assertEqual(deployed, reference,
                         "golden-beets-config.yaml drifted from beets/config.yaml -- sync the settings "
                         "(only `directory` and import `log` may differ)")

    def test_filetote_plugin_enabled(self):
        # gbc no longer carries booklet/scans/.lrc itself -- that lives in the filetote plugin. If it silently
        # drops out of the plugins line, extras stop being carried with no other signal. Parity keeps both
        # configs in lock-step, so asserting the deployed one is enough.
        plugins = _settings("beets/config.yaml")["plugins"].split()
        self.assertIn("filetote", plugins)


if __name__ == "__main__":
    unittest.main()
