"""Pass -- per-track AcoustID fingerprint verification: detect & quarantine IMPOSTER tracks.

An imposter has the right tags but its AUDIO is a different recording; album-mode import trusts it and
`chroma` doesn't penalise a track it can't ID, so it slips into a "strong" album. We act ONLY on POSITIVE
evidence: the fingerprint CONFIDENTLY matches a DIFFERENT artist's recording. Can't confirm, or a SAME-artist
match (alt mix/edition/typo) -> KEEP. Imposter -> MOVED to $MUSIC_DUMP (never deleted) + dropped. Cached per file.
"""
import importlib.util
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..mb import load_release_cache, missing_recordings, save_release_cache
from ..sidecars import quarantine_dir, safe_move, unique_dest
from ..util import backup_db, prune_empty_dirs, skip_on_error, write_json, write_text

APIKEY = os.environ.get("GBC_ACOUSTID_APIKEY", "1vOwZtEn")  # beets' shared key; set your own to avoid throttling
MATCH_SCORE = 0.5   # AcoustID result score above which the file CONFIRMS the tagged recording
MISMATCH_SCORE = 0.9  # higher bar to REFUTE: audio matches a DIFFERENT recording this strongly -> tag likely wrong
RETRIES = 4         # attempts on rate-limit / network error before giving up -> inconclusive
SEP = "\x1f"        # US control char: can't appear in tags/paths and survives str.splitlines() (unlike \x1e)


def _acoustid_available() -> bool:
    return importlib.util.find_spec("acoustid") is not None


IDCACHE = "gbc-acoustid-id-cache.jsonl"        # one JSON object {path: entry} per line, APPEND-ONLY: a killed
LEGACY_IDCACHE = "gbc-acoustid-id-cache.json"  # singletons walk resumes from the last line (no re-fingerprint) and
                                               # never holds the whole cache in RAM. entry = [rid, artist, title,
                                               # [all_ids]] | [rid, artist, title] (verify) | null. SHARED: verify
                                               # writes each imposter's TRUE recording so singletons reuses it.


def idcache_key(path) -> str | None:
    # Key on the PATH alone (not mtime/size): re-tagging a file (mediafile.save) bumps its mtime+size but NOT its
    # path, so a killed --apply run still finds the entry on relaunch -> cache HIT skips the slow re-fingerprint
    # (the re-tag itself is cheap). A file re-encoded in place isn't part of this flow (convert touches clean only).
    return str(path) if path else None


def load_idcache(cfg: Config) -> dict:
    """Replay the append-only JSONL into {path: entry} (last line wins), migrate the legacy single-blob JSON if
    present, then COMPACT: drop entries whose file is gone (identified files get imported out of source/quarantine)
    and rewrite one clean line each -- so the append log can't grow unbounded."""
    cache: dict = {}
    legacy = cfg.beetsdir / LEGACY_IDCACHE
    with suppress(OSError, ValueError):
        blob = json.loads(legacy.read_text(encoding="utf-8"))
        if isinstance(blob, dict):
            cache.update(blob)
    with suppress(OSError):
        for line in (cfg.beetsdir / IDCACHE).read_text(encoding="utf-8").splitlines():
            if line.strip():
                with suppress(ValueError):                  # tolerate a torn final line (process killed mid-append)
                    cache.update(json.loads(line))
    live = {k: v for k, v in cache.items() if Path(k).exists()}
    if live != cache or legacy.exists():                    # compact the log + drop the migrated legacy blob
        save_idcache(cfg, live)
        legacy.unlink(missing_ok=True)
    return live


def save_idcache(cfg: Config, cache: dict) -> None:
    # full atomic rewrite as compact JSONL, evicting entries whose file is gone -> stays bounded
    live = {k: v for k, v in cache.items() if Path(k).exists()}
    write_text(cfg.beetsdir / IDCACHE,
               "".join(json.dumps({k: v}, ensure_ascii=False) + "\n" for k, v in live.items()))


def append_idcache(cfg: Config, key: str, entry) -> None:
    """Append ONE identity to the JSONL log immediately (+flush) so the singletons walk persists progress as it
    goes: a kill resumes from the last line instead of re-fingerprinting from zero, and the walker never holds the
    whole cache in RAM. A torn last line is tolerated by load_idcache; compaction also happens there."""
    path = cfg.beetsdir / IDCACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({key: entry}, ensure_ascii=False) + "\n")
        fh.flush()


# generic tokens two UNRELATED artists routinely share -- never enough alone to call it "same artist":
# connectives + multilingual articles ("De La Soul" vs "La Roux", "DJ X" vs "DJ Y").
_GENERIC = {"the", "and", "feat", "ft", "featuring", "with", "dj", "mc", "of", "vs", "for", "an",
            "la", "le", "les", "el", "los", "las", "de", "del", "da", "du", "des", "et", "und"}


def _credit_tokens(name: str) -> set:
    """Distinctive tokens of an artist credit. A SINGLE-token credit keeps its token (so 1-char -M-/K matches
    itself); a MULTI-token credit drops stray 1-char tokens ('A Tribe...' must not read as 'A Perfect...')."""
    toks = [t for t in re.split(r"\W+", name.lower()) if t and t not in _GENERIC]
    return set(toks) if len(toks) == 1 else {t for t in toks if len(t) >= 2}


def _same_artist(m_artist: str, artist: str) -> bool:
    """Audio matched a different recording but the credits share a DISTINCTIVE token -> a version/edition/typo
    variant WITHIN the artist, KEEP it. Only a COMPLETELY different artist is the evidence we quarantine on:
    AcoustID's title is too noisy ('feat.' moves around, alt mixes), so artist identity is the airtight signal."""
    return bool(_credit_tokens(m_artist) & _credit_tokens(artist))


def _lookup(path):
    """ONE AcoustID fingerprint + lookup for a file -> the (score-sorted) results list, or None if the audio is
    unfingerprintable or the service stays unreachable after RETRIES. Single source of the AcoustID call so the
    imposter verdict AND the identity come from the same fingerprint (no double fingerprinting)."""
    import acoustid
    try:                                            # fingerprint is deterministic + expensive -> do it ONCE
        dur, fp = acoustid.fingerprint_file(path)
    except acoustid.FingerprintGenerationError:
        return None                                 # can't fingerprint -> inconclusive
    except OSError:
        return None                                 # file vanished/unreadable mid-run (TOCTOU) -> inconclusive
    for attempt in range(RETRIES):                  # only the network lookup is retried
        try:
            resp = acoustid.lookup(APIKEY, fp, dur, meta="recordings")
        except acoustid.WebServiceError:
            if attempt < RETRIES - 1:               # no point sleeping after the final attempt
                time.sleep(2 ** attempt)
            continue
        if resp.get("status") != "ok":
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
            continue
        return resp.get("results") or []
    return None


def _dominant_from_results(results):
    """The single CONFIDENT recording among AcoustID `results` -> (recording_mbid, artist, title), or None if
    AMBIGUOUS (several DIFFERENT songs match strongly) or nothing reaches MISMATCH_SCORE. Same audio on several
    releases (one song, many recording ids) is NOT ambiguous -- they share a title, so we keep the first."""
    recs = [rec for r in (results or []) if (r.get("score") or 0) >= MISMATCH_SCORE
            for rec in (r.get("recordings") or []) if rec.get("id") and rec.get("title")]
    if not recs or len({(rec["title"] or "").strip().lower() for rec in recs}) != 1:
        return None
    rec = recs[0]
    artist = ", ".join(a.get("name", "") for a in (rec.get("artists") or []))
    return rec["id"], artist, rec["title"]


def _all_recording_ids(results):
    """Every recording MBID AcoustID confidently (>=MISMATCH_SCORE) links to this AUDIO. The same audio is
    linked to one recording per release, so this is the full set of ids any clean copy could legitimately
    carry -- used to dedup a loose track against the clean library by ANY of them (the album's id often differs
    from AcoustID's single dominant pick, so a one-id check misses the duplicate)."""
    return {rec["id"] for r in (results or []) if (r.get("score") or 0) >= MISMATCH_SCORE
            for rec in (r.get("recordings") or []) if rec.get("id")}


def _file_verdict(path, mbid):
    """(status, present, mismatch, dominant). status='ok' once AcoustID answers, else 'error'. present=True when
    the file's fingerprint lists the TAGGED recording -> genuine. mismatch=(artist, title, score) when the audio
    matches a DIFFERENT recording >= MISMATCH_SCORE -- the positive evidence of an imposter. dominant = the
    confident identity of the AUDIO (for the shared id-cache so singletons need not re-fingerprint), or None."""
    results = _lookup(path)
    if results is None:
        return "error", False, None, None
    present = any(rec.get("id") == mbid
                  for r in results if (r.get("score") or 0) >= MATCH_SCORE
                  for rec in (r.get("recordings") or []))
    mismatch = None
    if not present:                                     # audio != tag: is it confidently some other known recording?
        for r in results:                               # results are best-score first
            if (r.get("score") or 0) < MISMATCH_SCORE:
                break                                   # sorted desc -> nothing below the bar matters
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
    return "ok", present, mismatch, _dominant_from_results(results)


def identify_dominant(path):
    """The single CONFIDENT AcoustID recording for this file's AUDIO -> (recording_mbid, artist, title), or None
    if AcoustID is unavailable, inconclusive, or AMBIGUOUS. Lets a caller re-tag a mislabeled file to its TRUE
    recording before re-import."""
    results = _lookup(path)
    return _dominant_from_results(results) if results is not None else None


def run(cfg: Config, scope="", refresh: bool = False) -> int:
    """Flag imposter tracks among items in `scope` (whole library if empty). Returns the imposter count.
    `refresh` (a `gbc run --all`) re-pulls the MB tracklist cache instead of reusing the persisted one."""
    log = get_logger("verify")
    if not _acoustid_available():
        log.warning("pyacoustid not available -> fingerprint verification skipped")
        return 0
    sc = [scope] if scope else []
    fmt = f"$id{SEP}$path{SEP}$mb_trackid{SEP}$artist{SEP}$title{SEP}$length{SEP}$bitrate{SEP}$album_id{SEP}$mb_albumid"
    _, text = run_beet(cfg, ["ls", "-f", fmt, "mb_trackid::.", *sc], passname="verify", echo_lines=False, check=True)
    rows = [ln.split(SEP, 8) for ln in text.splitlines() if ln.count(SEP) >= 8]

    cpath = cfg.beetsdir / "gbc-verify-cache.json"
    try:
        cache = json.loads(cpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    idcache = load_idcache(cfg)                         # share each imposter's identity with singletons

    moved, checked, incon, backed = [], 0, 0, False
    mismatches = 0
    affected: dict = {}                                 # album_id -> mb_albumid for albums that lost a track
    for itemid, path, mbid, artist, title, length, bitrate, album_id, mb_albumid in rows:
        if not Path(path).exists():
            continue
        dominant = None                                # the audio's true identity (only when freshly looked up)
        # Key on the file PLUS its audio identity (mbid + duration + bitrate), NOT mtime/size: tag writes
        # (acousticbrainz, comp normalisation) change mtime but not the audio -> an mtime key would invalidate
        # the whole cache every run; mbid/length/bitrate flip only on a re-tag to another id or a re-encode.
        key = f"{path}:{mbid}:{length}:{bitrate}"
        verdict = cache.get(key)
        if verdict is None:
            status, present, mismatch, dominant = _file_verdict(path, mbid)
            if status != "ok":
                incon += 1
                continue                                       # inconclusive -> not cached, retried next run
            # matched artist has NO distinctive token (empty/generic "DJ"/"The") -> can't prove a different artist
            sibling = bool(mismatch) and (not _credit_tokens(mismatch[0]) or _same_artist(mismatch[0], artist))
            if present or sibling:                 # tagged recording present, or a match by the SAME artist (kept)
                verdict = "ok"
            elif mismatch:                         # audio matches a DIFFERENT artist's recording -> proven imposter
                mismatches += 1
                log.warning("IMPOSTER: %s - %s | audio = %s - %s (%.2f)",
                            artist, title, mismatch[0], mismatch[1], mismatch[2])
                verdict = "imposter"
            else:                                  # tagged id absent but NO confident alternative -> unprovable, KEEP
                verdict = "rare"
            cache[key] = verdict
            checked += 1
        if verdict == "imposter":                              # quarantine, never deleted
            with skip_on_error(log, "verify", path):           # one bad move never loses the run's verdicts
                if not backed:
                    backup_db(cfg, "verify", log)
                    backed = True
                # mirror the EXACT clean sub-path (any depth: _Various Artists/_Soundtracks/_Singles/Artist-Album)
                folder = Path(path).parent
                try:
                    qd = cfg.dump / "imposters" / folder.relative_to(cfg.clean)
                except ValueError:                          # not under clean (shouldn't happen) -> flat fallback
                    qd = quarantine_dir(cfg.dump, "imposters", fallback=folder.name)
                dest = unique_dest(qd, Path(path).name)
                qd.mkdir(parents=True, exist_ok=True)
                if safe_move(path, dest, log):                 # move out of clean, then drop the stale lib entry
                    rc, _ = run_beet(cfg, ["remove", "-f", f"id:{itemid}"], passname="verify", echo_lines=False)
                    if rc:
                        log.warning("verify: `beet remove` rc=%d for id:%s -- stale lib entry may remain", rc, itemid)
                    moved.append(path)
                    affected[album_id] = mb_albumid    # this album just lost a track -> re-check completeness
                    if dominant:                       # carry the audio's TRUE identity -> singletons skips the re-FP
                        idkey = idcache_key(dest)
                        if idkey:
                            idcache[idkey] = list(dominant)
                    log.info("QUARANTINE imposter (audio != tagged recording): %s -> %s/", Path(path).name, qd)

    write_json(cpath, cache)                               # atomic (tmp + replace): a crash can't corrupt the cache
    save_idcache(cfg, idcache)                             # persist imposter identities for singletons to reuse
    log.info("=== fingerprint verify: %d check(s), %d imposter(s) quarantined, %d mismatch(es), %d inconclusive ===",
             checked, len(moved), mismatches, incon)
    demoted = _demote_incomplete_albums(cfg, affected, log, refresh) if affected else 0
    if moved or demoted:
        prune_empty_dirs(cfg.clean)                            # remove album shells left empty by quarantine / demote
    if moved:
        log.info("  [IMPOSTER] %d track(s) (audio != tagged recording) moved to %s -- recoverable, never deleted",
                 len(moved), cfg.dump)
    return len(moved)


def _demote_incomplete_albums(cfg: Config, affected: dict, log, refresh: bool = False) -> int:
    """An album that just lost a track to imposter-quarantine may no longer be COMPLETE. Re-check each against
    its live MB tracklist; if recordings are now missing, re-file its surviving tracks as singletons under
    _Singles/ (the album library keeps only complete albums). Reversible: singletons `_promote_complete`
    re-assembles the album if the missing track is recovered later."""
    rcache = load_release_cache(cfg, refresh)        # persisted MB tracklists, shared with singletons' promote
    demoted = 0
    for album_id, mb_albumid in affected.items():
        with skip_on_error(log, "verify", f"album_id:{album_id}"):
            if not album_id or not mb_albumid:
                continue                               # a singleton, or no MB release id -> nothing to demote
            _, text = run_beet(cfg, ["ls", "-f", f"$id{SEP}$mb_trackid{SEP}$path", f"album_id:{album_id}"],
                               passname="verify", echo_lines=False)
            items = [ln.split(SEP, 2) for ln in text.splitlines() if ln.count(SEP) >= 2]
            present = {tid for _, tid, _ in items if tid}
            missing = missing_recordings(mb_albumid, present, rcache)
            if missing is None or not missing:         # can't verify, or still complete -> keep it as an album
                continue
            if _demote_album(cfg, album_id, [(i, p) for i, _, p in items], log):
                demoted += 1
                log.info("  DEMOTE incomplete album (missing %d MB track(s)) -> _Singles/: album_id:%s",
                         len(missing), album_id)
    save_release_cache(cfg, rcache)                  # persist tracklists fetched this pass for the next run/pass
    if demoted:
        log.info("=== verify: %d incomplete album(s) demoted to singletons ===", demoted)
    return demoted


def _demote_album(cfg: Config, album_id, items, log) -> bool:
    """Stage the album's surviving files, drop their album rows, re-import as singletons (`-s -A`: keep tags +
    mb_trackid, no re-match) so beets re-files them under _Singles/<artist>/<album>/. Files are never lost: a
    leftover staging is logged, not deleted."""
    staging = cfg.beetsdir / ".gbc-demote" / re.sub(r"[^\w.-]", "_", str(album_id))
    staging.mkdir(parents=True, exist_ok=True)
    moved = []
    for itemid, path in items:
        if Path(path).exists() and safe_move(path, unique_dest(staging, Path(path).name), log):
            moved.append(itemid)
    if not moved:
        return False
    rm = ["remove", "-f"]
    for i, sid in enumerate(moved):                    # drop the album rows so the re-import doesn't dup-skip
        rm += ([","] if i else []) + [f"id:{sid}"]
    run_beet(cfg, rm, passname="verify", echo_lines=False)
    run_beet(cfg, ["import", "-q", "-I", "-s", "-A", "-m", str(staging)], passname="verify")
    if any(staging.iterdir()):
        log.warning("  demote album_id:%s left files in %s -- recoverable, not lost", album_id, staging)
    else:
        with suppress(OSError):
            staging.rmdir()
            staging.parent.rmdir()
    return True
