"""Pass -- per-track AcoustID fingerprint verification: detect & quarantine IMPOSTER tracks.

An imposter is a file with the right title/duration/tags but whose AUDIO is not the matched recording.
Album-mode import trusts the lot, and `chroma` gives NO penalty to a track it cannot identify at all -> such
a file slips into an otherwise "strong" album (a real blind spot). We re-fingerprint each accepted track and
act ONLY when BOTH hold: its own fingerprint matches don't include the tagged recording (AcoustID status=ok)
AND the official recording (mb_trackid) is itself known to AcoustID. Any rate-limit/timeout ->
"inconclusive" -> left alone (we never act). A conclusive imposter is MOVED to $MUSIC_DUMP (never deleted)
and dropped from the lib, so the clean library stays clean. Verdicts are cached per file (checked once).
"""
import importlib.util
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..sidecars import quarantine_dir, safe_move
from ..util import backup_db

APIKEY = os.environ.get("GBC_ACOUSTID_APIKEY", "1vOwZtEn")  # beets' shared key; set your own to avoid throttling
MATCH_SCORE = 0.5   # AcoustID result score above which the file CONFIRMS the tagged recording
MISMATCH_SCORE = 0.9  # higher bar to REFUTE: audio matches a DIFFERENT recording this strongly -> tag likely wrong
RETRIES = 4         # attempts on rate-limit / network error before giving up -> inconclusive
SEP = "\x1f"        # US control char: can't appear in tags/paths and survives str.splitlines() (unlike \x1e)


def _acoustid_available() -> bool:
    return importlib.util.find_spec("acoustid") is not None


def _same_artist(a: str, b: str) -> bool:
    """True if two artist strings share a primary artist (one normalises into the other). So the SAME song
    on another release (same artist, different mb_trackid) or a 'feat.' variant is NOT flagged as a wrong
    tag -- only a genuinely DIFFERENT artist is (e.g. a cover matched to the wrong performer)."""
    na, nb = re.sub(r"\W+", "", a.lower()), re.sub(r"\W+", "", b.lower())
    return bool(na) and bool(nb) and (na in nb or nb in na)


def _file_verdict(path, mbid):
    """('ok', present, mismatch) once AcoustID answers conclusively, else ('error', False, None).
    present=True when the file's OWN fingerprint matches list the tagged recording (mbid) -> genuine. False =>
    the audio is something else (unknown to AcoustID, or a different known song -> both are imposters if the
    tagged recording is known). mismatch=(artist, title, score) when the audio instead matches a DIFFERENT
    recording with high confidence (>= MISMATCH_SCORE) -> the tag is likely wrong (logged only, never acted on)."""
    import acoustid
    for attempt in range(RETRIES):
        try:
            dur, fp = acoustid.fingerprint_file(path)
            resp = acoustid.lookup(APIKEY, fp, dur, meta="recordings")
        except acoustid.FingerprintGenerationError:
            return "error", False, None                 # can't fingerprint -> inconclusive (never act)
        except acoustid.WebServiceError:
            time.sleep(2 ** attempt)
            continue
        if resp.get("status") != "ok":
            time.sleep(2 ** attempt)
            continue
        results = resp.get("results") or []
        present = any(rec.get("id") == mbid
                      for r in results if (r.get("score") or 0) >= MATCH_SCORE
                      for rec in (r.get("recordings") or []))
        mismatch = None
        if not present:                                 # audio != tag: is it CONFIDENTLY some other known recording?
            for r in results:                           # AcoustID returns results best-score first
                if (r.get("score") or 0) < MISMATCH_SCORE:
                    break                               # sorted desc -> nothing below the bar is worth checking
                for rec in (r.get("recordings") or []):
                    if rec.get("id") == mbid:
                        continue
                    artist = ", ".join(a.get("name", "") for a in (rec.get("artists") or []))
                    title = rec.get("title") or ""
                    if artist or title:
                        mismatch = (artist, title, round(r.get("score") or 0, 2))
                        break
                if mismatch:
                    break
        return "ok", present, mismatch
    return "error", False, None


def _official_known(mbid):
    """True if the MusicBrainz recording is registered in AcoustID (>=1 fingerprint); None if inconclusive."""
    url = f"https://api.acoustid.org/v2/track/list_by_mbid?format=json&client={APIKEY}&mbid={mbid}"
    req = urllib.request.Request(url, headers={"User-Agent": "gbc/0.7 (golden-beets-config)"})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            time.sleep(2 ** attempt)
            continue
        if data.get("status") != "ok":
            time.sleep(2 ** attempt)
            continue
        return len(data.get("tracks") or []) >= 1
    return None


def _write_verdicts(cfg: Config, verdicts: dict) -> None:
    """Persist per-ITEM-ID verdicts for the reclaim pass (id, not path: representation-independent, so reclaim
    can't miss a match on a path-rendering difference). Rewritten EACH run (empty until proven), so a crash or
    a skipped verify leaves reclaim nothing stale to trust."""
    cfg.beetsdir.mkdir(parents=True, exist_ok=True)
    (cfg.beetsdir / "gbc-verify-verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")


def run(cfg: Config, scope="") -> int:
    """Flag imposter tracks among items added in `scope` (whole library if empty). Returns the imposter count."""
    log = get_logger("verify")
    _write_verdicts(cfg, {})                                   # fresh slate: reclaim trusts only this run's verdicts
    if not _acoustid_available():
        log.warning("pyacoustid not available -> fingerprint verification skipped")
        return 0
    sc = [scope] if scope else []
    fmt = f"$id{SEP}$path{SEP}$mb_trackid{SEP}$albumartist{SEP}$album{SEP}$year{SEP}$artist{SEP}$title"
    _, text = run_beet(cfg, ["ls", "-f", fmt, "mb_trackid::.", *sc], passname="verify", echo_lines=False)
    rows = [ln.split(SEP, 7) for ln in text.splitlines() if ln.count(SEP) >= 7]

    cpath = cfg.beetsdir / "gbc-verify-cache.json"
    try:
        cache = json.loads(cpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    moved, checked, incon, backed = [], 0, 0, False
    mismatches = 0
    verdicts: dict[str, str] = {}
    for itemid, path, mbid, albumartist, album, year, artist, title in rows:
        try:
            st = Path(path).stat()
        except OSError:
            continue
        key = f"{int(st.st_mtime)}:{st.st_size}:{mbid}"        # re-check only if the file changed
        verdict = cache.get(key)
        if verdict is None:
            status, present, mismatch = _file_verdict(path, mbid)
            if status != "ok":
                incon += 1
                continue                                       # inconclusive -> not cached, retried next run
            # flag only a genuinely DIFFERENT artist; a same-artist other-recording-id is the same song -> skip
            if mismatch and not _same_artist(mismatch[0], artist):
                mismatches += 1
                log.warning("MISMATCH: %s - %s | audio = %s - %s (%.2f) -- kept, tag likely wrong",
                            artist, title, mismatch[0], mismatch[1], mismatch[2])
            if present:
                verdict = "ok"
            else:
                known = _official_known(mbid)
                if known is None:
                    incon += 1
                    continue
                verdict = "imposter" if known else "rare"      # rare = file & official both unknown -> genuine, kept
            cache[key] = verdict
            checked += 1
        verdicts[itemid] = verdict                             # conclusive verdict keyed by item id -> reclaim input
        if verdict == "imposter":                              # conclusive imposter -> quarantine (never deleted)
            if not backed:
                backup_db(cfg, "verify", log)
                backed = True
            qd = quarantine_dir(cfg.dump, "imposters", albumartist, album, year, fallback=Path(path).parent.name)
            dest = qd / Path(path).name
            i = 1
            while dest.exists():
                i += 1
                dest = qd / f"{Path(path).stem} ({i}){Path(path).suffix}"
            qd.mkdir(parents=True, exist_ok=True)
            if safe_move(path, dest, log):                     # move out of clean, then drop the now-stale lib entry
                run_beet(cfg, ["remove", "-f", f"id:{itemid}"], passname="verify", echo_lines=False)  # exact id
                moved.append(path)
                log.info("QUARANTINE imposter (audio != tagged recording): %s -> %s/", Path(path).name, qd)

    cfg.beetsdir.mkdir(parents=True, exist_ok=True)
    with cpath.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    _write_verdicts(cfg, verdicts)                             # genuine ("ok") paths drive the reclaim pass
    log.info("=== fingerprint verify: %d check(s), %d imposter(s) quarantined, %d mismatch(es), %d inconclusive ===",
             checked, len(moved), mismatches, incon)
    if moved:
        log.info("  [IMPOSTER] %d track(s) (audio != tagged recording) moved to %s -- recoverable, never deleted",
                 len(moved), cfg.dump)
    return len(moved)
