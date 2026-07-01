import unittest

from gbc.beetscfg import BeetsImport, parse_import, read_import
from tests.base import Base


class TestBeetsImport(unittest.TestCase):
    def test_move_consumes_source(self):
        bi = BeetsImport(move=True)
        self.assertTrue(bi.source_consumed)
        self.assertFalse(bi.source_preserved)
        self.assertEqual(bi.label, "move")

    def test_copy_preserves_source(self):
        bi = BeetsImport(copy=True)
        self.assertTrue(bi.source_preserved)
        self.assertFalse(bi.source_consumed)
        self.assertEqual(bi.label, "copy")

    def test_copy_plus_delete_consumes(self):
        bi = BeetsImport(copy=True, delete=True)
        self.assertTrue(bi.source_consumed)
        self.assertEqual(bi.label, "delete")          # delete wins the label over copy

    def test_symlink_preserved(self):
        bi = BeetsImport(link=True)
        self.assertTrue(bi.source_preserved)
        self.assertEqual(bi.label, "link")

    def test_reflink_and_hardlink_preserve_source(self):
        self.assertTrue(BeetsImport(reflink=True).source_preserved)
        self.assertTrue(BeetsImport(hardlink=True).source_preserved)

    def test_inplace_preserved(self):
        bi = BeetsImport()
        self.assertTrue(bi.source_preserved)
        self.assertEqual(bi.label, "in-place")


class TestParseImport(unittest.TestCase):
    def test_yaml_yes_no(self):
        bi = parse_import("import:\n  move: yes\n  copy: no\n")
        self.assertTrue(bi.move)
        self.assertFalse(bi.copy)

    def test_yaml_true_false_copy(self):
        bi = parse_import("import:\n  move: false\n  copy: true\n")
        self.assertFalse(bi.move)
        self.assertTrue(bi.copy)

    def test_string_values_coerced(self):
        bi = parse_import("import:\n  move: 'no'\n  hardlink: 'yes'\n")
        self.assertFalse(bi.move)
        self.assertTrue(bi.hardlink)

    def test_reflink_auto_is_truthy(self):
        bi = parse_import("import:\n  reflink: auto\n")        # beets' documented 'auto' value
        self.assertTrue(bi.reflink)
        self.assertEqual(bi.label, "reflink")

    def test_missing_import_block(self):
        self.assertEqual(parse_import("directory: /x\n"), BeetsImport())

    def test_garbage_is_safe(self):
        self.assertIsInstance(parse_import(":::not: yaml: ["), BeetsImport)


class TestReadImport(Base):
    """`read_import` runs `beet config` with check=True: a FAILED config read leaves the move-vs-copy op
    unknown, so it must ABORT (RuntimeError) rather than silently return a default 'preserved' BeetsImport
    that would skip dedup/prune while the real import still consumes the source."""

    def test_beet_config_failure_raises_runtimeerror(self):
        self.cfg.beet = self.fake_beet(stdout="import:\n  move: yes\n", code=2)   # nonzero exit
        with self.assertRaises(RuntimeError):
            read_import(self.cfg)

    def test_beet_config_success_returns_parsed_import(self):
        self.cfg.beet = self.fake_beet(stdout="import:\n  move: yes\n", code=0)
        bi = read_import(self.cfg)
        self.assertIsInstance(bi, BeetsImport)
        self.assertTrue(bi.move)                    # parsed from the fake `beet config` YAML
        self.assertTrue(bi.source_consumed)


if __name__ == "__main__":
    unittest.main()
