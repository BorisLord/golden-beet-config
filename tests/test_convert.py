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
        counts = iter([1, 1, 0, 0])     # 2 jobs pending (comprehension), then 0 remain after each convert
        p1, p2, p3 = self._patches(lambda *a, **k: next(counts), lambda cfg, args, **k: (0, ""))
        with p1, p2, p3:
            self.assertEqual(convert.run(self.cfg), 0)


if __name__ == "__main__":
    unittest.main()
