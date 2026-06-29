import unittest
from unittest import mock

from gbc.passes import convert
from tests.base import Base


class TestConvert(Base):
    def _patches(self, count_fn, run_fn):
        return (mock.patch.object(convert, "count_items", count_fn),
                mock.patch.object(convert, "run_beet", run_fn),
                mock.patch.object(convert, "backup_db", lambda *a, **k: None))

    def test_nothing_to_convert_is_noop(self):
        with mock.patch.object(convert, "count_items", lambda *a, **k: 0):
            self.assertEqual(convert.run(self.cfg), 0)

    def test_nonzero_convert_rc_propagates_and_stops(self):
        calls = []
        p1, p2, p3 = self._patches(lambda *a, **k: 1,                       # every job pending
                                   lambda cfg, args, **k: (calls.append(args), (2, ""))[1])  # beet convert fails
        with p1, p2, p3:
            rc = convert.run(self.cfg)
        self.assertEqual(rc, 2)                                             # rc surfaced, not swallowed
        convs = [a for a in calls if a and a[0] == "convert"]
        self.assertEqual(len(convs), 1)                                     # stopped after the first failure

    def test_successful_convert_returns_zero(self):
        p1, p2, p3 = self._patches(lambda *a, **k: 1,                       # every job pending
                                   lambda cfg, args, **k: (0, ""))          # every convert + ls succeeds
        with p1, p2, p3:
            self.assertEqual(convert.run(self.cfg), 0)

    def test_hires_flac_downsampled_to_flac16(self):
        # only hi-res FLAC present (>48 kHz) -> exactly one convert, to flac16, scoped by the samplerate range
        calls = []

        def count(cfg, args, passname):
            return 1 if "samplerate:48001.." in args else 0

        def run_beet(cfg, args, **k):
            calls.append(args)
            return (0, "")

        with mock.patch.object(convert, "count_items", count), \
             mock.patch.object(convert, "run_beet", run_beet), \
             mock.patch.object(convert, "backup_db", lambda *a, **k: None):
            self.assertEqual(convert.run(self.cfg), 0)
        conv = [a for a in calls if a and a[0] == "convert"]
        self.assertEqual(len(conv), 1)
        self.assertIn("flac16", conv[0])
        self.assertIn("format:FLAC", conv[0])
        self.assertIn("samplerate:48001..", conv[0])

    def test_24bit_flac_downsampled_to_flac16(self):
        # 24-bit FLAC at a normal rate is caught by the bit-depth job (not the samplerate one)
        calls = []

        def count(cfg, args, passname):
            return 1 if "bitdepth:17.." in args else 0

        def run_beet(cfg, args, **k):
            calls.append(args)
            return (0, "")

        with mock.patch.object(convert, "count_items", count), \
             mock.patch.object(convert, "run_beet", run_beet), \
             mock.patch.object(convert, "backup_db", lambda *a, **k: None):
            self.assertEqual(convert.run(self.cfg), 0)
        conv = [a for a in calls if a and a[0] == "convert"]
        self.assertEqual(len(conv), 1)
        self.assertIn("flac16", conv[0])
        self.assertIn("bitdepth:17..", conv[0])
        self.assertIn("samplerate:..48000", conv[0])    # disjoint from the >48 kHz job -> no double count

    def test_failed_encode_reaps_stale_row(self):
        # a WMA still matching the query AFTER convert but whose file vanished = failed encode (keep_new moved
        # the original to quarantine, the encode errored) -> the stale db row must be dropped by exact id
        calls = []

        def count(cfg, args, passname):
            return 1 if "format::Windows" in args else 0

        def run_beet(cfg, args, **k):
            calls.append(args)
            if args and args[0] == "ls" and any("$id" in str(a) for a in args):
                return (0, "42\t/gone/x.wma")      # the still-matching item; its file does NOT exist
            return (0, "")

        with mock.patch.object(convert, "count_items", count), \
             mock.patch.object(convert, "run_beet", run_beet), \
             mock.patch.object(convert, "backup_db", lambda *a, **k: None):
            self.assertEqual(convert.run(self.cfg), 0)
        removes = [a for a in calls if a and a[0] == "remove"]
        self.assertEqual(len(removes), 1)
        self.assertIn("id:42", removes[0])         # stale row reaped (original is safe in quarantine)

    def test_alac_job_converts_to_flac(self):
        # only ALAC items present -> exactly one convert, to flac, querying format:ALAC
        calls = []

        def count(cfg, args, passname):
            return 1 if "format:ALAC" in args else 0

        def run_beet(cfg, args, **k):
            calls.append(args)
            return (0, "")

        with mock.patch.object(convert, "count_items", count), \
             mock.patch.object(convert, "run_beet", run_beet), \
             mock.patch.object(convert, "backup_db", lambda *a, **k: None):
            self.assertEqual(convert.run(self.cfg), 0)
        conv = [a for a in calls if a and a[0] == "convert"]
        self.assertEqual(len(conv), 1)
        self.assertIn("flac", conv[0])            # lossless -> FLAC
        self.assertIn("format:ALAC", conv[0])


if __name__ == "__main__":
    unittest.main()
