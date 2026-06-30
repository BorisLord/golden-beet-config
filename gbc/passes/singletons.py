"""Singleton recovery (OPT-IN) -- import LOOSE source tracks (and quarantined imposters) as singletons, filed
under _Singles/ (config `singleton:` path). Tracks already in clean DUP-SKIP by mb_trackid.

FINGERPRINT-FIRST: before importing, every loose file is identified by its AUDIO (AcoustID) -- the source of
truth -- and re-tagged to that recording, so the import matches it instead of skipping on bad tags. Metadata
(MB/Discogs/Deezer/Bandcamp) corroborates at import time. What AcoustID can't identify is LEFT IN PLACE (the
default-skip import keeps it in the source = the curation backlog); nothing is force-tagged.

Then two reassembly steps run (dry unless --apply):
  1. nova.reroute() -- OPT-IN/detachable: re-tag dispersed Nova-compilation tracks to their compil (Nova first).
  2. _promote_complete() -- any album whose ENTIRE MusicBrainz tracklist is now present as singletons is
     re-imported as a real album (the inverse of verify's demote of incomplete albums). Incomplete sets stay.

NOT part of `gbc run` (which stays album-only by design); run it deliberately with `gbc singletons`.
"""
import re
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

from .. import artfix
from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..mb import load_release_cache, release_recordings, save_release_cache
from ..sidecars import safe_move, unique_dest
from ..util import backup_db, count_items, prune_empty_dirs, skip_on_error
from . import verify  # AcoustID identity + the shared id-cache helpers live in verify

try:                                  # Nova is OPT-IN + detachable: deleting nova.py just disables the re-tag
    from . import nova
except Exception:                     # pragma: no cover
    nova = None                       # type: ignore[assignment]


def run(cfg: Config, src=None, reimport: bool = False, apply: bool = False) -> int:
    log = get_logger("singletons")
    src = Path(src) if src else cfg.src
    if not src.is_dir():
        log.error("source missing: %s", src)
        return 1
    # Always ALSO recover quarantined imposters (audio != tag = mislabeled; the fingerprint finds their TRUE
    # recording). Skip silently if the folder isn't there.
    dirs = [src]
    imposters = cfg.dump / "imposters"
    if imposters.is_dir():
        dirs.append(imposters)                     # quarantined imposters get the same fingerprint-first pass
    else:
        log.info("singletons: no %s -> imposters step skipped", imposters)
    backup_db(cfg, "singletons", log)
    # FINGERPRINT-FIRST: identify every loose file by its audio + re-tag to the true recording BEFORE import,
    # so a bad tag no longer makes it skip. Cached -> re-runs only fingerprint new files.
    cache = verify.load_idcache(cfg)               # prior identities (incl. verify's imposters) + resume state.
    # Unlike the MB tracklist cache, the idcache is intentionally NOT refreshed by --reimport: re-fingerprinting
    # the whole source is a multi-day op, so cached AcoustID identities (incl. `null` for unidentified files)
    # persist across runs. To force a clean re-fingerprint, delete BEETSDIR/gbc-acoustid-id-cache.jsonl.
    clean_ids = _clean_recording_ids(cfg)          # so a loose copy of a track already in clean is NOT re-added
    for d in dirs:
        _fingerprint_retag(cfg, d, cache, clean_ids, log, apply)
    # (no save here: _fingerprint_retag APPENDS each fresh identity to the JSONL cache as it goes -- a killed walk
    # resumes from the last line and never holds the whole cache in RAM)
    # DRY-RUN = identification only. The import must NOT run dry: it would mark the folders "seen" (incremental),
    # so a later `--apply` would skip the now-re-tagged files. So gate import + re-tag-dependent steps on --apply.
    if apply:
        before = count_items(cfg, ["ls"], "singletons")
        inc = "-I" if reimport else "-i"           # -I re-evaluates album-rejected folders as singletons
        for d in dirs:
            artfix.run(cfg, src=d, log=log)        # strip mime=None WMA art so scrub can't crash beet import
            rc, _ = run_beet(cfg, ["import", "-q", "-s", inc, str(d)], passname="singletons")
            if rc:
                log.error("beet import -s %s failed (rc=%d) -- nothing deleted", d, rc)
                return rc
        added = count_items(cfg, ["ls"], "singletons") - before   # already-present tracks dup-skip; delta = new
        log.info("singletons: +%d loose track(s) recovered -> _Singles/", added)
    else:
        log.info("singletons: dry-run -- identification only; re-run with --apply to re-tag + import")
    if nova is not None:
        nova.reroute(cfg, log, apply)              # NOVA FIRST: dispersed Nova tracks regroup under their compil
    _promote_complete(cfg, log, apply, reimport)   # then promote ANY now-complete album out of _Singles/
    return 0


_AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav", ".aiff", ".aif"}


def _clean_recording_ids(cfg: Config) -> set:
    """Every mb_trackid currently in the clean library (snapshot, ONE query). A loose track whose AUDIO maps to
    ANY of these is already in clean -> we re-tag it to that in-clean id so beets DUP-skips it at import instead
    of adding a second copy as a singleton (the album's recording id often differs from AcoustID's dominant
    pick, so the bare mb_trackid match would miss it)."""
    _, text = run_beet(cfg, ["ls", "-f", "$mb_trackid", "mb_trackid::."], passname="singletons", echo_lines=False)
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def _fingerprint_retag(cfg: Config, directory: Path, cache: dict, clean_ids: set, log, apply: bool) -> tuple[int, int]:
    """Fingerprint-FIRST identity for every loose audio file under `directory`: ask AcoustID what the audio
    really is and overwrite its artist/title/mb_trackid with that recording, so the singleton import matches it
    instead of skipping on bad tags (audio = source of truth; metadata only corroborates at import). If the
    audio is ALREADY in clean (any of its recording ids in `clean_ids`), re-tag it to the in-clean id so the
    import DUP-skips it rather than adding a duplicate single. Ambiguous/unidentifiable files are LEFT UNTOUCHED.
    `cache` is verify's SHARED id-cache; values are [rid, artist, title, [all_ids]] (or the 3-field form verify
    pre-writes for imposters), or null. Writes only with --apply. Returns (identified, left)."""
    if not verify._acoustid_available():
        log.info("%s: pyacoustid absent -> AcoustID identify skipped", directory.name)
        return 0, 0
    try:
        import mediafile
    except ImportError:
        log.info("%s: mediafile absent -> AcoustID identify skipped", directory.name)
        return 0, 0
    fixed = dup = left = scanned = 0
    for p in sorted(directory.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _AUDIO_EXT:
            continue
        scanned += 1
        if scanned % 500 == 0:                 # liveness on a huge source (the fingerprint walk is silent + slow)
            log.info("  ...%s: %d scanned (%d new, %d dup, %d left)", directory.name, scanned, fixed, dup, left)
        with skip_on_error(log, "singletons", p.name):
            key = verify.idcache_key(p)
            if key is None:                        # unreadable file (stat failed) -> skip
                continue
            if key in cache:
                entry = cache[key]                 # cached (incl. imposter identities verify pre-wrote) -> no lookup
            else:
                results = verify._lookup(str(p))
                tup = verify._dominant_from_results(results) if results is not None else None
                entry = [*tup, sorted(verify._all_recording_ids(results))] if tup else None
                verify.append_idcache(cfg, key, entry)   # persist now (resume) without holding the cache in RAM
            if not entry:
                left += 1
                continue
            rid, artist, title = entry[0], entry[1], entry[2]
            all_ids = set(entry[3]) if len(entry) > 3 else {rid}   # 3-field (verify) -> dominant only
            in_clean = all_ids & clean_ids
            tag_id = sorted(in_clean)[0] if in_clean else rid      # in-clean id -> the import DUP-skips it
            if in_clean:
                dup += 1
            else:
                fixed += 1
            log.info("  re-id%s: %s -> %s - %s [%s]%s", "" if apply else " (dry)", p.name, artist, title, tag_id,
                     "  (already in clean -> dup-skip)" if in_clean else "")
            if apply:
                mf = mediafile.MediaFile(str(p))
                mf.title = title
                if artist:
                    mf.artist = artist
                mf.mb_trackid = tag_id
                mf.save()
    log.info("%s: %d new identified, %d already-in-clean (dup-skip), %d unidentified%s",
             directory.name, fixed, dup, left, "" if apply else " (dry-run, not written)")
    return fixed, left


def _promote_complete(cfg: Config, log, apply: bool, refresh: bool = False) -> int:
    """Group loose singletons by their matched release; an album whose ENTIRE MusicBrainz tracklist is now
    present as singletons is re-imported as a real album (beets routes it to <artist>/_Various Artists/
    _Soundtracks per the paths rules). ROBUST: completeness is decided against the live MB release tracklist,
    not the stored `tracktotal` -- tracktotal is only a cheap pre-filter to skip pointless MB calls. `refresh`
    (a --reimport run) re-pulls the persisted MB tracklist cache instead of reusing it."""
    _, text = run_beet(cfg, ["ls", "-f", "$mb_albumid\t$id\t$mb_trackid\t$tracktotal\t$path",
                             "singleton:1", "mb_albumid::."], passname="singletons", echo_lines=False)
    albums: dict = defaultdict(lambda: {"items": [], "total": 0})
    for line in text.splitlines():
        albumid, _, rest = line.partition("\t")
        sid, _, rest = rest.partition("\t")
        tid, _, rest = rest.partition("\t")
        tt, _, path = rest.partition("\t")
        if albumid.strip() and path:
            a = albums[albumid.strip()]
            a["items"].append((sid.strip(), tid.strip(), path))
            a["total"] = max(a["total"], int(tt) if tt.strip().isdigit() else 0)
    cache = load_release_cache(cfg, refresh)          # persisted MB tracklists, shared with verify's demote
    promoted = 0
    for albumid, a in albums.items():
        items = a["items"]
        if a["total"] and len(items) < a["total"]:
            continue                                  # cheap pre-filter: fewer tracks present than the album has
        if albumid not in cache:
            cache[albumid] = release_recordings(albumid)
        official = cache[albumid]
        have = {tid for _, tid, _ in items if tid}
        if not official or not official <= have:      # robust: every MB tracklist recording must be present
            continue
        if _assemble_album(cfg, albumid, items, log, apply):
            promoted += 1
    save_release_cache(cfg, cache)                     # persist tracklists fetched this pass for the next run/pass
    log.info("singletons: %d complete album(s) %s out of _Singles/",
             promoted, "promoted" if apply else "would be promoted")
    return promoted


def _assemble_album(cfg: Config, albumid: str, items, log, apply: bool) -> bool:
    """Stage the album's files, drop their singleton rows, re-import the staging dir AS ONE ALBUM from its
    EXISTING tags (`-A --flat -m`). No MB re-match: `_promote_complete` already verified the set against the live
    tracklist, so `-A` files it deterministically (offline, never quiet-mode 'Skipping'); beets routes by tags
    into <artist>/ | _Various Artists/ | _Soundtracks/. Leftover files are put back as singletons -- never lost."""
    label = f"{albumid} ({len(items)} trk)"
    if not apply:
        log.info("  COMPLETE -> would promote album %s", label)
        return True
    staging = cfg.beetsdir / ".gbc-assemble" / re.sub(r"[^\w.-]", "_", albumid)
    staging.mkdir(parents=True, exist_ok=True)
    moved = []
    for sid, _tid, path in items:                     # de-collide same-basename tracks (VA comps) -> never overwrite
        if Path(path).exists() and safe_move(path, unique_dest(staging, Path(path).name), log):
            moved.append(sid)
    if not moved:
        log.warning("  promote %s: no files moved -> skipped", label)
        return False
    rm = ["remove", "-f"]                              # drop the now-staged singleton rows (else import dup-skips)
    for i, sid in enumerate(moved):
        rm += ([","] if i else []) + [f"id:{sid}"]
    run_beet(cfg, rm, passname="singletons", echo_lines=False)
    rc, _ = run_beet(cfg, ["import", "-q", "-I", "-A", "--flat", "-m", str(staging)], passname="singletons")
    if any(p.is_file() for p in staging.iterdir()):   # leftover -> restore as singletons (no loss)
        log.warning("  promote %s: album import left files -> restoring as singletons", label)
        run_beet(cfg, ["import", "-q", "-I", "-s", "-A", "-m", str(staging)], passname="singletons")
    with suppress(OSError):
        staging.rmdir()
        staging.parent.rmdir()
    prune_empty_dirs(cfg.clean / "_Singles")
    log.info("  PROMOTED album -> %s", label)
    return rc == 0
