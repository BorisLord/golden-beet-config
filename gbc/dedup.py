"""Pre-import dedup: within each SOURCE album folder, drop duplicate audio files (same title +
near-equal duration), keeping the best bitrate. The redundant copies go to quarantine (NEVER deleted),
so a rare false positive stays recoverable. Runs before `beet import` so a duplicate track can't inflate
the unmatched-tracks penalty and block an otherwise-good album. Conservative on purpose: only files that
expose a title are considered, and same-title files whose durations differ by more than TOL are kept
(distinct versions / reprises).
"""
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from .logs import get_logger
from .sidecars import AUDIO, quarantine_dir, safe_move

# Two files compared here are the SAME track measured by the SAME ffprobe -> real duplicates are
# near-identical in length, so the tolerance is tight (unlike sidecars' ±6s ffprobe-vs-beets comparison).
TOL = 3   # seconds
LOSSLESS = {".flac", ".wav", ".aif", ".aiff", ".alac", ".ape", ".wv", ".tta", ".dsf"}


def _log(log):
    return log if log is not None else get_logger("dedup")


def _probe(path):
    """(title_key, duration_seconds, bitrate) for an audio file; title_key='' if unreadable/untitled."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", "-i", str(path)],
                             capture_output=True, text=True).stdout
        fmt = json.loads(out).get("format", {})
    except (ValueError, OSError):
        return "", 0, 0
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    title = (tags.get("title") or "").strip().casefold()
    return title, round(float(fmt.get("duration") or 0)), int(fmt.get("bit_rate") or 0)


def _album_tags(path):
    """(albumartist|artist, album, year) from ffprobe tags -> names the quarantine sub-folder per album."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", "-i", str(path)],
                             capture_output=True, text=True).stdout
        tags = {k.lower(): v for k, v in (json.loads(out).get("format", {}).get("tags") or {}).items()}
    except (ValueError, OSError):
        return "", "", ""
    artist = (tags.get("album_artist") or tags.get("albumartist") or tags.get("artist") or "").strip()
    return artist, (tags.get("album") or "").strip(), (tags.get("date") or tags.get("year") or "").strip()


def dedup(src, dump, do_apply, log=None):
    """Move duplicate audio (best bitrate kept) to quarantine. Returns the count of files moved."""
    log = _log(log)
    by_folder = defaultdict(list)
    for dp, _, files in os.walk(src):
        for fn in files:
            if Path(fn).suffix.lower() in AUDIO:
                by_folder[dp].append(str(Path(dp) / fn))

    moved = 0
    for folder, paths in by_folder.items():
        groups = defaultdict(list)
        for p in paths:
            title, dur, br = _probe(p)
            if title:                        # only dedup files that expose a title (safe key)
                groups[title].append((p, dur, br, Path(p).stat().st_size))
        for items in groups.values():
            if len(items) < 2:
                continue
            durs = [d for _, d, _, _ in items if d > 0]
            if len(durs) != len(items) or max(durs) - min(durs) > TOL:
                continue        # a probe failed (unverifiable) OR genuinely different lengths -> keep all (safe)
            # lossless beats lossy FIRST (a FLAC whose ffprobe format bitrate reads 0 must not lose to a 320k
            # MP3), then best bitrate, then largest file
            items.sort(key=lambda x: (Path(x[0]).suffix.lower() in LOSSLESS, x[2], x[3]), reverse=True)
            keep = Path(items[0][0]).name
            for p, _, _, _ in items[1:]:
                qd = quarantine_dir(dump, "duplicates", *_album_tags(p), fallback=Path(folder).name)
                dest = qd / Path(p).name
                i = 1
                while dest.exists():
                    i += 1
                    dest = qd / f"{Path(p).stem} ({i}){Path(p).suffix}"
                if do_apply:
                    qd.mkdir(parents=True, exist_ok=True)
                if not do_apply or safe_move(p, dest, log):
                    moved += 1
                    log.info("%s dup %s -> %s/ (kept %s)",
                             "DEDUP" if do_apply else "DRY ", Path(p).name, qd, keep)
    log.info("%d duplicate audio file(s) -> quarantine", moved)
    return moved
