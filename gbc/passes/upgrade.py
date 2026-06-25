"""Pass -- replace a clean album with a HIGHER-QUALITY copy sitting in the source.

`duplicate_action: skip` keeps the FIRST import of a release; a later, better copy (FLAC over the clean MP3, or
a much higher-bitrate lossy) of the SAME release dup-skips at import and never enters -- so albumdedup (which
only compares copies already IN the library) never sees it. This pass closes that gap: correlate each source
album folder to a clean album by duration multiset + artist + title, and on a genuine upgrade move the clean
copy to $MUSIC_DUMP/upgraded/ (NEVER deleted) + re-import the source.

LOSSLESS replaces lossy; lossy->lossy only on a clear >= MIN_DELTA codec-normalised effective-bitrate jump (a
256k Opus is never downgraded to a 320k MP3); a WMA source is skipped (`convert` would transcode it); an
already-lossless clean album is the cutoff. Runs in the pipeline (cheap track-count pre-filter, then a mediafile
probe + duration/artist/title correlation) and as `gbc upgrade [DIR] [--apply]`.
"""
import re
from collections import Counter, defaultdict
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..quality import eff as _eff
from ..quality import rank as _rank
from ..sidecars import AUDIO, quarantine_dir, safe_move
from ..util import backup_db, length_secs, prune_empty_dirs, skip_on_error
from . import import_
from .albumdedup import _match, _title_close  # reuse the proven content-correlation (duration multiset + title)

SEP = "\x1f"
MINTRACKS = 3
MIN_DELTA = 64   # MP3-equivalent kbps: lossy->lossy needs a CLEAR jump -- that margin is the safeguard
_TIER = {3: "lossless", 2: "lossy", 1: "?"}


def _artist_match(a: str, b: str) -> bool:
    """True if two artists share a primary artist -- one's WORD set is a subset of the other's (so 'U2' matches
    'U2 feat. X' but 'Eve' does NOT match 'Steve', unlike a naive substring). Guards the duration+title
    correlation against a same-track-count, same-title album by a DIFFERENT artist."""
    ta = {w for w in (re.sub(r"\W+", "", t) for t in a.lower().split()) if w}
    tb = {w for w in (re.sub(r"\W+", "", t) for t in b.lower().split()) if w}
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _is_upgrade(src_rank: int, src_ebr: int, lib_rank: int, lib_ebr: int) -> bool:
    """Worthwhile replacement? Lossless replaces lossy; both-lossless = cutoff (no churn); both-lossy only on a
    clear >= MIN_DELTA effective-bitrate jump (a 256k Opus is never downgraded to a 320k MP3)."""
    if src_rank != lib_rank:
        return src_rank > lib_rank                 # lossless replaces lossy; never the reverse
    if src_rank >= 3:                              # both lossless -> cutoff reached, leave it
        return False
    return (src_ebr - lib_ebr) >= MIN_DELTA        # both lossy -> a clear effective-bitrate jump (>= MIN_DELTA)


def _probe(folder: Path) -> dict | None:
    """{durs, rank, ext, br, ebr, artist} for a source album folder, read via mediafile (uniform across
    formats -- crucially gives a normalised albumartist for the correlation guard). None if no readable audio."""
    from mediafile import MediaFile
    durs: list[int] = []
    exts: Counter = Counter()
    brs: list[int] = []
    artists: Counter = Counter()
    albums: Counter = Counter()
    for f in folder.rglob("*"):
        if not (f.is_file() and f.suffix.lower() in AUDIO):
            continue
        try:
            mf = MediaFile(str(f))
        except Exception:                          # one unreadable file never aborts the folder probe
            continue
        if mf.length:
            durs.append(round(mf.length))
        exts[f.suffix.lower()] += 1
        if mf.bitrate:
            brs.append(mf.bitrate // 1000)
        art = (mf.albumartist or mf.artist or "").strip()
        if art:
            artists[art] += 1
        if (mf.album or "").strip():
            albums[mf.album.strip()] += 1
    if not durs:
        return None
    ext = exts.most_common(1)[0][0] if exts else ""
    br = round(sum(brs) / len(brs)) if brs else 0
    return {"durs": durs, "rank": _rank(ext), "ext": ext, "br": br, "ebr": _eff(ext, br),
            "artist": artists.most_common(1)[0][0] if artists else "",
            "album": albums.most_common(1)[0][0] if albums else ""}


def _clean_albums(cfg: Config) -> dict:
    """Every clean album -> {artist, album, year, durs, rank, br, folder} (rank/bitrate from the items)."""
    fmt = f"$album_id{SEP}$albumartist{SEP}$album{SEP}$year{SEP}$length{SEP}$bitrate{SEP}$path"
    _, text = run_beet(cfg, ["ls", "-f", fmt], passname="upgrade", echo_lines=False)
    albums: dict = {}
    for line in text.splitlines():
        p = line.split(SEP)
        if len(p) < 7 or not p[0]:
            continue
        aid, artist, album, year, length, bitrate, path = p[:7]
        a = albums.setdefault(aid, {"artist": artist, "album": album, "year": year,
                                    "durs": [], "exts": Counter(), "brs": [], "folder": Path(path).parent})
        a["durs"].append(length_secs(length))
        a["exts"][Path(path).suffix.lower()] += 1
        digits = re.sub(r"\D", "", bitrate)
        if digits:
            a["brs"].append(int(digits))
    for a in albums.values():
        ext = a["exts"].most_common(1)[0][0] if a["exts"] else ""
        a["ext"] = ext
        a["rank"] = _rank(ext)
        a["br"] = round(sum(a["brs"]) / len(a["brs"])) if a["brs"] else 0
        a["ebr"] = _eff(ext, a["br"])
    return albums


def _source_album_folders(src: Path) -> dict:
    """{folder: audio-file count} for every folder holding audio (one cheap stat-only walk). The count alone
    then pre-filters which folders get the expensive per-file mediafile probe (folder names are too junky)."""
    counts: Counter = Counter()
    for p in src.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIO:
            counts[p.parent] += 1
    return counts


def _album_ids(cfg: Config) -> set:
    _, text = run_beet(cfg, ["ls", "-a", "-f", "$id"], passname="upgrade", echo_lines=False)
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def _do_upgrade(cfg: Config, folder: Path, aid: str, a: dict, log) -> bool:
    """Move the clean (inferior) album to quarantine/upgraded (NEVER deleted), drop it from the lib, re-import
    the source. On a re-import failure the clean copy is safe in quarantine (recoverable)."""
    clean_folder = Path(a["folder"])
    if not clean_folder.is_dir():
        log.warning("upgrade: clean folder gone, skip: %s", clean_folder)
        return False
    qd = quarantine_dir(cfg.dump, "upgraded", a["artist"])   # artist only; the folder name carries "Album (Year)"
    dest = qd / clean_folder.name
    n = 1
    while dest.exists():
        n += 1
        dest = qd / f"{clean_folder.name} ({n})"
    qd.mkdir(parents=True, exist_ok=True)
    # order matters: park the clean copy in quarantine FIRST, then drop its row, then re-import the source. A
    # crash mid-sequence leaves the album in $MUSIC_DUMP/upgraded/ (recoverable) -- never lost.
    if not safe_move(clean_folder, dest, log):
        return False
    run_beet(cfg, ["remove", "-a", "-f", f"id:{aid}"], passname="upgrade", echo_lines=False)
    before = _album_ids(cfg)                                      # album ids once the clean copy is dropped
    import_.run(cfg, src=folder, reimport=True)   # full import (sidecars + dedup + VA-comp normalize), not a raw beet
    if not (_album_ids(cfg) - before):       # a SPECIFIC new album must appear; a quiet weak/dup skip returns rc 0
        log.warning("upgrade: re-import added no album for %s -- clean copy kept safe in %s, NOT upgraded (restore?)",
                    folder, dest)
        return False
    log.info("  UPGRADED %s - %s: clean now from %s (old copy -> %s)", a["artist"], a["album"], folder, dest)
    return True


def run(cfg: Config, src=None, apply: bool = False) -> int:
    """Find source album folders that are a quality upgrade over a clean album; report (or, with apply, swap).
    Lossless replaces lossy, and lossy replaces lossy on a clear >= MIN_DELTA effective-bitrate jump."""
    log = get_logger("upgrade")
    src = Path(src) if src else cfg.src
    if not src.is_dir():
        log.error("source missing: %s", src)
        return 1
    clean = _clean_albums(cfg)
    by_count: dict = defaultdict(list)             # bucket clean albums by track count for fast lookup
    for aid, a in clean.items():
        if len(a["durs"]) >= MINTRACKS:
            by_count[len(a["durs"])].append(aid)

    upgrades = []
    for folder, n in _source_album_folders(src).items():
        cands = by_count.get(n)                          # cheap pre-filter: track count only (folder names are junk)
        if n < MINTRACKS or not cands:
            continue
        with skip_on_error(log, "upgrade", folder):
            sp = _probe(folder)
            if not sp or len(sp["durs"]) < MINTRACKS or sp["ext"] == ".wma":   # WMA -> `convert` would transcode it
                continue
            for aid in cands:
                a = clean[aid]
                # durations + artist are the strong signals; title-match the folder name OR the probed album tag
                # (raw folder names like "U2 - War (1983) [FLAC]" miss the strict check -- the tag often won't)
                if (_match(sp["durs"], a["durs"]) and _artist_match(sp["artist"], a["artist"])
                        and (_title_close(folder.name, a["album"]) or _title_close(sp["album"], a["album"]))):
                    if _is_upgrade(sp["rank"], sp["ebr"], a["rank"], a["ebr"]):
                        upgrades.append((folder, aid, sp, a))
                    break                          # correlated (better or not) -> don't double-count this folder

    if not upgrades:
        log.info("=== upgrade: no clean album has a higher-quality source copy ===")
        return 0
    log.info("=== upgrade: %d clean album(s) have a better source copy ===", len(upgrades))
    if apply:
        backup_db(cfg, "upgrade", log)
    done = 0
    for folder, aid, sp, a in upgrades:
        log.info("  %s %s - %s: clean %s %dk %s -> source %s %dk %s  [%s]",
                 "UPGRADE" if apply else "would upgrade", a["artist"], a["album"],
                 _TIER[a["rank"]], a["br"], a["ext"].lstrip("."),
                 _TIER[sp["rank"]], sp["br"], sp["ext"].lstrip("."), folder)
        if apply:
            with skip_on_error(log, "upgrade", folder):
                if _do_upgrade(cfg, folder, aid, a, log):
                    done += 1
    if apply and done:
        prune_empty_dirs(cfg.clean)                # remove now-empty artist/album shells of the replaced copies
    log.info("=== upgrade: %d album(s) %s out of source ===",
             done if apply else len(upgrades), "upgraded" if apply else "would be upgraded")
    return done if apply else len(upgrades)
