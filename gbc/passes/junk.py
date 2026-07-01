"""Junk-tag stripper (DATA-DRIVEN). A maintainable regex list (`junk_patterns.txt`) -- rip-site signatures,
uploader/scene tags, URLs -- is read FRESH each run; the matching junk SUBSTRING is EXCISED from the tag, the
legit remainder kept ('real note - www.junk.site' -> 'real note'); a field is blanked only when it was entirely
junk.

PATTERN + substring based on purpose: blanking a whole field blindly would nuke legit content ("Bowie @ the
BBC", a real review/lyrics that ends with a rip sig). Matching is done by beets' own `field::regex` query (so a
multiline value -- e.g. real lyrics -- is matched correctly, not line-split); excision is a Python re.sub on the
fetched value. Native beets writes (`beet modify`). Runs in the pipeline (incremental scope) and standalone
(`gbc junk [QUERY] [--apply]`).

Extend the list: edit `gbc/passes/junk_patterns.txt` (shipped) or drop a `$BEETSDIR/junk_patterns.txt` (local
additions) -- both are re-read every run.
"""
import re
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..util import backup_db

# Text fields where rip-site junk lands. Identity fields (title/artist/album/albumartist) are NEVER touched.
JUNK_FIELDS = ("comments", "grouping", "composer", "lyricist", "lyrics", "encoder", "albumdisambig")
PATTERNS_FILE = "junk_patterns.txt"          # shipped defaults live next to this module


def load_patterns(cfg: Config, log=None) -> list[str]:
    """Validated junk regexes from the shipped file PLUS an optional `$BEETSDIR/junk_patterns.txt`, read FRESH
    each run (edit the list -> takes effect next run). One regex per line; '#' comments and blanks ignored;
    invalid regexes are skipped with a warning (one bad line never breaks the pass)."""
    out: list[str] = []
    seen: set[str] = set()
    for path in (Path(__file__).with_name(PATTERNS_FILE), cfg.beetsdir / PATTERNS_FILE):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line in seen:
                continue
            try:
                re.compile(line)
            except re.error:
                (log or get_logger("junk")).warning("junk: skipping invalid regex: %s", line)
                continue
            seen.add(line)
            out.append(line)
    return out


def _excise(value: str, rx: "re.Pattern[str]") -> str:
    """Remove only the junk SUBSTRINGS that match `rx`, keeping the legit remainder (a comment may be
    'real note - www.junk.site' -> 'real note'), then tidy the orphan separators/whitespace left behind.
    Returns '' when the value was ENTIRELY junk -> the caller blanks the field."""
    cleaned = rx.sub("", value)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)                       # collapse runs of spaces
    cleaned = re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", cleaned)     # collapse blank-line runs
    return cleaned.strip(" \t\r\n|/:;-–—·•")  # noqa: RUF001 -- intentional unicode separators (en/em dash, bullet)


def run(cfg: Config, scope: str = "", apply: bool = False) -> int:
    """Blank every JUNK_FIELD whose stored value matches a junk pattern, for items in `scope` (whole library if
    empty). Dry by default (lists what it WOULD strip); `--apply` writes via `beet modify`. Backs up library.db
    before any write. Returns the count of (item, field) values stripped."""
    log = get_logger("junk")
    pats = load_patterns(cfg, log)
    if not pats:
        log.info("junk: no patterns loaded -> nothing to strip")
        return 0
    combined = "(?i)(" + "|".join(pats) + ")"       # one alternation; beets matches it against the whole value
    rx = re.compile(combined)
    sc = [scope] if scope else []
    # 1. find which (item, field) hold junk -- beets runs the regex (multiline-safe, no value parsing here)
    flagged: dict[str, set[str]] = {}
    for field in JUNK_FIELDS:
        _, text = run_beet(cfg, ["ls", "-f", "$id", f"{field}::{combined}", *sc],
                           passname="junk", echo_lines=False, check=True)
        for line in text.splitlines():
            if line.strip():
                flagged.setdefault(line.strip(), set()).add(field)
    if not flagged:
        log.info("junk: clean -- no junk found%s", f" (scope={scope})" if scope else "")
        return 0
    # 2. EXCISE only the junk substring from each flagged value (keep legit text); blank only if all junk
    changes: dict[str, dict[str, str]] = {}
    for iid, fields in flagged.items():
        for field in sorted(fields):
            _, raw = run_beet(cfg, ["ls", "-f", f"${field}", f"id:{iid}"], passname="junk", echo_lines=False)
            value = raw.rstrip("\n")                  # one item -> the whole output IS the field value
            cleaned = _excise(value, rx)
            if cleaned == value:                      # rx matched a substring beets flagged but nothing trimmable
                continue
            changes.setdefault(iid, {})[field] = cleaned
            log.info("  junk%s id:%s %s: %r -> %s", "" if apply else " (dry)", iid, field, value[:60],
                     repr(cleaned[:60]) if cleaned else "(blanked)")
    n = sum(len(f) for f in changes.values())
    if not n:
        log.info("junk: nothing trimmable after excision")
        return 0
    if apply:
        backup_db(cfg, "junk", log)
        for iid, fmap in changes.items():
            run_beet(cfg, ["modify", "-y", f"id:{iid}", *[f"{f}={c}" for f, c in fmap.items()]],
                     passname="junk", echo_lines=False)
    log.info("junk: %d junk value(s) across %d item(s) %s", n, len(changes),
             "stripped" if apply else "would be stripped (dry-run, use --apply)")
    return n
