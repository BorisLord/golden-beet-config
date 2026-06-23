import logging
import unittest
from unittest import mock

from gbc.passes import qa
from gbc.passes.qa import _container_mismatch
from tests.base import Base


class TestContainerMismatch(Base):
    """Magic-byte container/extension check (catches RIFF/WAVE files disguised as .mp3 -> break TagLib)."""

    def _w(self, name, data):
        p = self.tmp / name
        p.write_bytes(data)
        return str(p)

    def test_riff_disguised_as_mp3_is_flagged(self):
        why = _container_mismatch(self._w("x.mp3", b"RIFF\x00\x00\x00\x00WAVEfmt "))
        self.assertIn("RIFF", why)

    def test_valid_signatures_pass(self):
        self.assertEqual(_container_mismatch(self._w("a.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00\x00")), "")
        self.assertEqual(_container_mismatch(self._w("b.mp3", b"\xff\xfb\x90\x00\x00\x00\x00\x00\x00")), "")
        self.assertEqual(_container_mismatch(self._w("c.flac", b"fLaC\x00\x00\x00\x22\x00\x00\x00\x00")), "")
        self.assertEqual(_container_mismatch(self._w("d.ogg", b"OggS\x00\x02\x00\x00\x00\x00\x00\x00")), "")
        self.assertEqual(_container_mismatch(self._w("e.m4a", b"\x00\x00\x00\x20ftypM4A ")), "")

    def test_flac_with_id3_tag_accepted(self):
        # a FLAC carrying a leading ID3v2 tag (some rippers add one) is VALID and playable -> must NOT be culled
        self.assertEqual(_container_mismatch(self._w("f.flac", b"ID3\x04\x00\x00\x00\x00\x00\x00\x00")), "")

    def test_empty_file_flagged(self):
        self.assertEqual(_container_mismatch(self._w("g.mp3", b"")), "empty file")

    def test_unchecked_extension_ignored(self):
        # .wav legitimately IS RIFF; extensions we don't map are never flagged
        self.assertEqual(_container_mismatch(self._w("h.wav", b"RIFF\x00\x00\x00\x00WAVE")), "")


class TestCull(Base):
    def test_cull_moves_corrupt_to_reason_layout(self):
        alb = self.cfg.clean / "Tigran" / "Mockroot (2015)"
        alb.mkdir(parents=True)
        bad = alb / "03 - bad.flac"
        bad.write_bytes(b"x")
        with mock.patch.object(qa, "run_beet", lambda *a, **k: (0, "")):     # stub the lib remove
            n = qa._cull(self.cfg, [str(bad), str(bad)], logging.getLogger("t"))   # duplicate path -> deduped
        self.assertEqual(n, 1)
        self.assertFalse(bad.exists())                                       # moved out of clean
        dest = self.cfg.dump / "corrupt" / "Tigran" / "Mockroot (2015)" / "03 - bad.flac"
        self.assertTrue(dest.exists())                                       # quarantine/corrupt/<artist>/<album>/


if __name__ == "__main__":
    unittest.main()
