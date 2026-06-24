"""Pass -- de-duplicate ALBUMS in the CLEAN library. The same album imported twice can match two DIFFERENT
releases (MusicBrainz vs Discogs) -> different mb_albumid, so beets' mb_albumid-keyed duplicate_action keeps
both. We correlate by CONTENT (same albumartist + track-duration multiset), keep the best copy (MusicBrainz >
Discogs, then bitrate), quarantine the rest -- NEVER deleted; library.db backed up first. Runs in BOTH import
modes (dedup.py is within-folder, track-level, move-mode only -- it cannot see this).
"""
import re
from collections import defaultdict
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..sidecars import quarantine_dir, safe_move
from ..util import backup_db, prune_empty_dirs

SEP = "\x1f"
MINTRACKS = 3   # >=3: same artist + count + all within TOL between DISTINCT albums is ~nil
TOL = 35        # seconds/track: same album, different rip drifts ~30s/track (measured IAM 30, BBC 22, De
                # Palmas 14); a different album far more (Deep Purple vs in Rock 136). Per-track, not total
                # (different albums can share a total).
_NUMWORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
             "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
             "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}


def _match(durs_a, durs_b) -> bool:
    """Same count + every (sorted) track within TOL -> same album, tolerant to a different rip."""
    return len(durs_a) == len(durs_b) and all(
        abs(x - y) <= TOL for x, y in zip(sorted(durs_a), sorted(durs_b), strict=True))


def _numbers(title: str) -> set:
    """Distinguishing volume/number tokens (digits + ordinal words + roman numerals). Titles differing here
    are DIFFERENT releases (Nova Classics Four vs Seven, Vol. 1 vs 2) even if tracks align -- must NOT merge."""
    nums = set(re.findall(r"\d+", title))
    nums |= {_NUMWORDS[w] for w in re.findall(r"[a-z]+", title.lower()) if w in _NUMWORDS}
    return nums


def _title_close(t1: str, t2: str) -> bool:
    """Same album title: >=0.5 token overlap AND same distinguishing numbers -- so a casing/punctuation variant
    merges but a different album or volume/edition does NOT (a duration-only match would wrongly merge them)."""
    a = set(re.sub(r"[^a-z0-9]+", " ", t1.lower()).split())
    b = set(re.sub(r"[^a-z0-9]+", " ", t2.lower()).split())
    if not a or not b or len(a & b) / max(len(a), len(b)) < 0.5:
        return False
    return _numbers(t1) == _numbers(t2)


def _secs(s: str) -> int:
    """beets' $length is 'M:SS' (or 'H:MM:SS') -> whole seconds."""
    s = s.strip()
    if not s:
        return 0
    try:
        if ":" in s:
            v = 0.0
            for part in s.split(":"):
                v = v * 60 + float(part)
            return round(v)
        return round(float(s))
    except ValueError:
        return 0


def _is_mb(mb_albumid: str) -> bool:
    return "-" in (mb_albumid or "")   # MusicBrainz album ids are UUIDs; Discogs ids are bare integers


def run(cfg: Config, *, do_apply: bool = True) -> int:
    """Quarantine content-duplicate albums (same artist + same track durations), keeping the best copy.
    Returns the number of duplicate albums moved (would-move count when do_apply=False)."""
    log = get_logger("albumdedup")
    fmt = f"$album_id{SEP}$albumartist{SEP}$album{SEP}$year{SEP}$length{SEP}$bitrate{SEP}$mb_albumid{SEP}$path"
    _, text = run_beet(cfg, ["ls", "-f", fmt], passname="albumdedup", echo_lines=False)

    albums: dict = {}
    for line in text.splitlines():
        p = line.split(SEP)
        if len(p) < 8 or not p[0]:
            continue
        aid, albumartist, album, year, length, bitrate, mb, path = p[:8]
        a = albums.setdefault(aid, {"artist": albumartist, "album": album, "year": year, "mb": mb,
                                    "durs": [], "br": 0, "folder": Path(path).parent})
        a["durs"].append(_secs(length))
        digits = re.sub(r"\D", "", bitrate)
        a["br"] = max(a["br"], int(digits) if digits else 0)

    # bucket by (artist, track count), cluster by TOLERANT duration match -- an exact multiset misses the
    # same album ripped differently (a few seconds drift per track).
    buckets: dict = defaultdict(list)
    for aid, a in albums.items():
        if len(a["durs"]) >= MINTRACKS and sum(a["durs"]) > 0:
            buckets[(a["artist"].casefold(), len(a["durs"]))].append(aid)
    dup_groups = []
    for aids in buckets.values():
        used: set = set()
        for i, aid in enumerate(aids):
            if aid in used:
                continue
            cluster = [aid] + [o for o in aids[i + 1:]
                               if o not in used
                               and _match(albums[aid]["durs"], albums[o]["durs"])
                               and _title_close(albums[aid]["album"], albums[o]["album"])]
            if len(cluster) > 1:
                used.update(cluster)
                dup_groups.append(cluster)
    if not dup_groups:
        log.info("=== album dedup: no content-duplicate albums ===")
        return 0
    if do_apply:
        backup_db(cfg, "albumdedup", log)

    moved = 0
    for aids in dup_groups:
        # keeper: MusicBrainz > Discogs, then bitrate, then lowest id (deterministic)
        keeper = max(aids, key=lambda i: (_is_mb(albums[i]["mb"]), albums[i]["br"], -int(i)))
        for aid in aids:
            if aid == keeper:
                continue
            a = albums[aid]
            folder = Path(a["folder"])
            if not folder.is_dir():
                continue
            qd = quarantine_dir(cfg.dump, "duplicates", a["artist"])   # duplicates/<Artist>/ ; folder = "Album (Year)"
            dest = qd / folder.name
            n = 1
            while dest.exists():
                n += 1
                dest = qd / f"{folder.name} ({n})"
            if do_apply:
                qd.mkdir(parents=True, exist_ok=True)
            if not do_apply or safe_move(folder, dest, log):
                if do_apply:
                    rc, _ = run_beet(cfg, ["remove", "-a", "-f", f"id:{aid}"], passname="albumdedup", echo_lines=False)
                    if rc:
                        log.warning("albumdedup: `beet remove` rc=%d for id:%s -- stale lib entry may remain", rc, aid)
                moved += 1
                log.info("%s dup album: %s - %s -> %s/ (kept '%s')",
                         "DEDUP" if do_apply else "DRY ", a["artist"], a["album"], dest, albums[keeper]["album"])
    if do_apply and moved:
        prune_empty_dirs(cfg.clean)            # remove the now-empty artist/album shells left behind
    log.info("=== album dedup: %d duplicate album(s) -> quarantine/duplicates ===", moved)
    return moved
