import re
import unittest
from unittest import mock

from gbc.passes import junk
from tests.base import Base


class TestJunk(Base):
    def _rx(self):
        return re.compile("(?i)(" + "|".join(junk.load_patterns(self.cfg)) + ")")

    # --- the shipped pattern list: catches the real junk we found, spares legit text ---
    def test_shipped_patterns_catch_known_junk_not_legit(self):
        rx = self._rx()
        for j in ["RnBXclusive.se", "www.soulection.com", "http://sinmasmp3.tk", "@@@PinkyLeFou@@@",
                  ".::Y::S::P::.", ":: MiNDSCAPE 2005 ::", "Ripped from vinyl.", "@+ Rob", "collection.by.ru",
                  "Encoded for DELit Music Navigator", "SoB", "rock that shit", "YEAR: 1968 ID3G: 115"]:
            self.assertTrue(rx.search(j), f"should flag junk: {j!r}")
        for ok in ["Bowie @ the BBC (Beeb) 1968-", "Recorded Live at Wembley 1973", "Johann Sebastian Bach",
                   "originally released 1971", "feat. Dr. Dre", "Note: remastered",
                   "a sober reflection", "we rock that shit live"]:   # anchored handles must NOT hit embedded text
            self.assertFalse(rx.search(ok), f"should NOT flag legit: {ok!r}")

    # --- excision: strip only the junk substring, keep the legit remainder ---
    def test_excise_keeps_legit_strips_junk(self):
        rx = self._rx()
        self.assertEqual(junk._excise("RnBXclusive.se", rx), "")                       # all junk -> blank
        self.assertEqual(junk._excise(".::Y::S::P::.", rx), "")                         # scene tag + its '.' decoration
        self.assertEqual(junk._excise("Encoded for DELit Music Navigator", rx), "")    # greedy rip/encode sig
        self.assertEqual(junk._excise("Great album\n\nRnBXclusive.se", rx), "Great album")
        self.assertEqual(junk._excise("Original Dub Gathering - www.odgprod.com", rx), "Original Dub Gathering")
        self.assertEqual(junk._excise("www.boxson.net | creative commons (by)", rx), "creative commons (by)")
        self.assertEqual(junk._excise("Recorded Live 1973", rx), "Recorded Live 1973")  # untouched

    def test_load_skips_comments_blanks_and_invalid_regex(self):
        self.cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        (self.cfg.beetsdir / junk.PATTERNS_FILE).write_text("# a comment\n\nmyjunk\n[bad(regex\nother\n")
        pats = junk.load_patterns(self.cfg, mock.MagicMock())
        self.assertIn("myjunk", pats)
        self.assertIn("other", pats)
        self.assertNotIn("[bad(regex", pats)         # invalid regex skipped, not crash

    def _fake(self, field_values):
        """field_values = {field: {id: raw value}} -- drives both the detection query and the per-item fetch."""
        captured = []

        def run_beet(cfg, args, **k):
            captured.append(args)
            if args and args[0] == "ls":
                fmt = args[2] if len(args) > 2 else ""
                idarg = next((a for a in args if a.startswith("id:")), None)
                if fmt.startswith("$") and idarg:                       # value fetch: -f '$field' id:X
                    return (0, field_values.get(fmt[1:], {}).get(idarg[3:], "") + "\n")
                term = next((a for a in args if "::" in a), "")          # detection: -f '$id' field::regex
                ids = list(field_values.get(term.split("::", 1)[0], {}))
                return (0, "\n".join(ids) + ("\n" if ids else ""))
            return (0, "")
        return run_beet, captured

    def test_apply_excises_per_field(self):
        rb, captured = self._fake({"comments": {"7": "Great album\n\nRnBXclusive.se", "9": "RnBXclusive.se"}})
        with mock.patch.object(junk, "run_beet", rb), mock.patch.object(junk, "backup_db", lambda *a, **k: None):
            n = junk.run(self.cfg, apply=True)
        self.assertEqual(n, 2)
        m7 = next(a for a in captured if a[0] == "modify" and "id:7" in a)
        self.assertIn("comments=Great album", m7)     # legit kept
        m9 = next(a for a in captured if a[0] == "modify" and "id:9" in a)
        self.assertIn("comments=", m9)                # pure junk -> blanked

    def test_dry_run_makes_no_modify(self):
        rb, captured = self._fake({"grouping": {"5": "x www.boxson.net y"}})
        with mock.patch.object(junk, "run_beet", rb):
            n = junk.run(self.cfg, apply=False)
        self.assertEqual(n, 1)
        self.assertFalse(any(a[0] == "modify" for a in captured))   # dry-run never writes

    def test_no_junk_returns_zero(self):
        rb, captured = self._fake({})                  # every detection query returns nothing
        with mock.patch.object(junk, "run_beet", rb):
            self.assertEqual(junk.run(self.cfg, apply=True), 0)
        self.assertFalse(any(a[0] == "modify" for a in captured))

    def _checked_run_beet(self, rc):
        """Fake run_beet honouring the real one's check= contract: the scope-detection query passes check=True,
        so a nonzero rc must RAISE there (never return, which the pre-fix code mistook for an empty scope)."""
        def fake(cfg, args, **k):
            if k.get("check") and rc:
                raise RuntimeError(f"beet {' '.join(map(str, args))!r} failed (rc={rc}, pass=junk)")
            return (rc, "")
        return fake

    def test_failed_scope_query_raises_instead_of_returning_zero(self):
        # scope `beet ls` query fails (rc=2) -> run must RAISE so the pipeline holds the watermark, not report 0
        with mock.patch.object(junk, "run_beet", self._checked_run_beet(2)), \
             mock.patch.object(junk, "backup_db", lambda *a, **k: None), \
             self.assertRaises(RuntimeError):
            junk.run(self.cfg, scope="added:2024-01..", apply=True)


if __name__ == "__main__":
    unittest.main()
