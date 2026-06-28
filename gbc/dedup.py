"""Pre-import dedup: within each SOURCE album folder, quarantine duplicate audio (same title + near-equal
duration), keeping the best bitrate -- NEVER deleted. Runs before `beet import` so a duplicate can't inflate
the unmatched-tracks penalty and block a good album. Conservative: only titled files, same-title files
beyond TOL apart are kept (distinct versions/reprises). Probes via the shared ProbeCache (no re-ffprobe).
"""
import os
from collections import defaultdict
from pathlib import Path

from .logs import get_logger
from .probe import ProbeCache
from .quality import eff, rank
from .sidecars import AUDIO, quarantine_dir, safe_move, unique_dest

# Same track via the SAME probe -> tight tolerance (unlike sidecars' ±6s probe-vs-beets comparison).
TOL = 3   # seconds


def _log(log):
    return log if log is not None else get_logger("dedup")


def dedup(src, dump, do_apply, log=None, cache=None):
    """Move duplicate audio (best bitrate kept) to quarantine. Returns the count of files moved."""
    log = _log(log)
    if cache is None:
        cache = ProbeCache(None)
    by_folder = defaultdict(list)
    for dp, _, files in os.walk(src):
        for fn in files:
            if Path(fn).suffix.lower() in AUDIO:
                by_folder[dp].append(str(Path(dp) / fn))

    moved = 0
    for folder, paths in by_folder.items():
        groups = defaultdict(list)
        for p in paths:
            pr = cache.get(p)
            if pr and pr.title:              # only dedup titled files (safe key); group case-insensitively
                groups[pr.title.casefold()].append((p, pr))
        for items in groups.values():
            if len(items) < 2:
                continue
            durs = [pr.length for _, pr in items if pr.length > 0]
            if len(durs) != len(items) or max(durs) - min(durs) > TOL:
                continue        # probe failed OR genuinely different lengths -> keep all (safe)
            # quality FIRST (lossless tier, so a FLAC whose bitrate reads 0 never loses to a 320k MP3),
            # then codec-normalised bitrate (256k Opus > 320k MP3), then file size
            items.sort(key=lambda x: (rank(x[1].ext), eff(x[1].ext, x[1].bitrate), Path(x[0]).stat().st_size),
                       reverse=True)
            keep = Path(items[0][0]).name
            for p, pr in items[1:]:
                # the dup's OWN tags name its quarantine sub-folder (was a 2nd ffprobe; now from the cached probe)
                qd = quarantine_dir(dump, "duplicates", pr.artist, pr.album, pr.year, fallback=Path(folder).name)
                dest = unique_dest(qd, Path(p).name)
                if do_apply:
                    qd.mkdir(parents=True, exist_ok=True)
                if not do_apply or safe_move(p, dest, log):
                    moved += 1
                    log.info("%s dup %s -> %s/ (kept %s)",
                             "DEDUP" if do_apply else "DRY ", Path(p).name, qd, keep)
    log.info("%d duplicate audio file(s) -> quarantine", moved)
    return moved
