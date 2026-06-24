"""Carry an album's OFFICIAL sidecars (booklet/cover/back/scan + .lrc) from source into the matched clean
album. snapshot BEFORE import (source still has audio) -> apply AFTER the move; matched by audio DURATION
(robust to tag/name changes) within TOL. A file already in clean, or a redundant cover, is quarantined --
never deleted.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing, suppress
from pathlib import Path

from .logs import get_logger


def safe_move(src, dst, log) -> bool:
    """Move src -> dst; on failure log a clear error and return False (no raw traceback)."""
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        log.error("move failed: %s -> %s (%s)", src, dst, e)
        return False
    return True

AUDIO = {".mp3", ".flac", ".m4a", ".m4b", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wma",
         ".wav", ".aif", ".aiff", ".ape", ".wv", ".mpc", ".tta", ".dsf", ".dff", ".mp2"}
ART = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".pdf"}
# official sidecar basenames only (case-insensitive, optional trailing number)
OFFICIAL = re.compile(r"^(cover|front|folder|back|booklet|inlay|inside|sleeve|scan|scans|artwork|art|obi"
                      r"|matrix|label|digipak|digipack|cd|disc|disk)([ _.\-]?\d+)?$", re.I)
TOL = 6   # seconds: ffprobe vs beets/mutagen durations differ by a few seconds
COVER = re.compile(r"^(cover|front|folder|artwork|art|sleeve|label)([ _.\-]?\d+)?$", re.I)  # "the cover" names


def _san(s):
    """One safe path component: drop separators, strip leading/trailing dots & spaces."""
    return str(s).replace("/", "_").replace("\\", "_").strip(". ")


def quarantine_dir(dump, reason, albumartist="", album="", year="", *, fallback=""):
    """Canonical $MUSIC_DUMP layout, grouped by WHY, mirroring clean: <reason>/<Albumartist>/<Album (Year)>/.
    `reason` = category (imposters/duplicates/reclaimed/redundant-art/shells). Falls back to <reason>/<fallback>
    when there is no metadata (audio-less shells, untagged files)."""
    base = Path(dump) / reason
    artist = _san(albumartist)
    album_dir = _san(album)
    y = str(year).strip()[:4]
    if y and y not in ("0", "0000", "None"):
        album_dir = f"{album_dir} ({y})" if album_dir else f"({y})"
    if artist and album_dir:
        return base / artist / album_dir
    if artist or album_dir:
        return base / (artist or album_dir)
    return base / (_san(fallback) or "_unknown")


def _log(log):
    return log if log is not None else get_logger("sidecars")


def is_sidecar(fn):
    p = Path(fn)
    ext = p.suffix.lower()
    if ext == ".lrc":                       # lyrics: any name
        return True
    return ext in ART and bool(OFFICIAL.match(p.stem))


def dur(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", "-i", path], capture_output=True, text=True).stdout.strip()
        return round(float(out)) if out else 0
    except (ValueError, OSError):
        return 0


def durs_of(paths):
    return sorted(d for d in (dur(p) for p in paths) if d > 0)


def matches(a, b):                          # both sorted; same count + each pair within TOL
    return len(a) == len(b) and all(abs(x - y) <= TOL for x, y in zip(a, b, strict=False))


def snapshot(src, out, log=None):
    log = _log(log)
    audio, side = defaultdict(list), defaultdict(list)
    for dp, _, files in os.walk(src):
        for fn in files:
            ext = Path(fn).suffix.lower()
            if ext in AUDIO:
                audio[dp].append(str(Path(dp) / fn))
            elif is_sidecar(fn):
                side[dp].append(str(Path(dp) / fn))
    snap = []
    for d, files in side.items():
        if not audio.get(d):
            continue
        ds = durs_of(audio[d])
        if ds:
            snap.append({"durs": ds, "files": sorted(files)})
    Path(out).write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    log.info("snapshot %d album(s) with official sidecars", len(snap))
    return len(snap)


def apply(snapfile, db, clean_root, dump, do_apply, log=None):
    log = _log(log)
    snap = json.loads(Path(snapfile).read_text(encoding="utf-8"))
    if not snap:
        log.info("nothing to carry")
        return
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        rows = con.execute("SELECT path, length FROM items").fetchall()
    dst = defaultdict(list)
    for (path, length) in rows:
        p = path.decode("utf-8", "surrogateescape") if isinstance(path, bytes) else path
        pp = Path(p)
        if not pp.is_absolute():                       # beets >=2.10 stores paths relative to lib root
            pp = Path(clean_root) / pp
        dst[pp.parent].append(round(length or 0))
    moved = dumped = miss = ambig = stale = 0
    for ddir, lengths in dst.items():
        if not ddir.is_dir():           # stale db: clean dir gone -> skip, can't carry (no crash)
            stale += 1
            continue
        dd = sorted(x for x in lengths if x > 0)
        cands = [e for e in snap if matches(e["durs"], dd)]
        if len(cands) != 1:
            ambig += len(cands) > 1
            miss += len(cands) == 0
            continue
        has_cover = bool(list(ddir.glob("cover.*")))
        for f in cands[0]["files"]:
            fp = Path(f)
            if not fp.exists():
                continue
            dest = ddir / fp.name
            redundant = fp.suffix.lower() in ART and bool(COVER.match(fp.stem)) and has_cover  # cover already there
            if not redundant and not dest.exists():        # free slot -> move in
                if not do_apply or safe_move(fp, dest, log):
                    moved += 1
                    log.info("%s %s -> %s", "MOVE" if do_apply else "DRY ", fp.name, ddir)
            elif dump:                                     # dup / redundant cover -> quarantine
                qd = quarantine_dir(dump, "redundant-art", ddir.parent.name, ddir.name, fallback=fp.parent.name)
                if do_apply:
                    qd.mkdir(parents=True, exist_ok=True)
                if not do_apply or safe_move(fp, qd / fp.name, log):
                    dumped += 1
                    log.info("%s %s -> %s/", "DUMP" if do_apply else "DRY ", fp.name, qd)
    log.info("%s %d file(s), %d dup(s) -> quarantine; %d unmatched, %d ambiguous, %d stale (clean dir gone)",
             "moved" if do_apply else "would move", moved, dumped, miss, ambig, stale)


def prune_shells(src, dump, do_apply, log=None):
    """Imported-album shells (source dirs whose ENTIRE subtree has no audio left) -> quarantine, one folder per
    album. Bottom-up scan, take the TOPMOST audio-empty dir, so a leftover subfolder (Scans/, @eaDir/...) moves
    WITH its parent shell. Folders still holding audio (skipped albums) stay in source."""
    log = _log(log)
    src = str(src)
    has_audio = {}
    for dp, dirs, files in os.walk(src, topdown=False):
        has_audio[dp] = (any(Path(f).suffix.lower() in AUDIO for f in files)
                         or any(has_audio.get(str(Path(dp) / d), False) for d in dirs))
    targets = []
    for dp, dirs, files in os.walk(src):
        if dp == src or has_audio.get(dp, False) or not (files or dirs):
            continue
        parent = str(Path(dp).parent)
        if parent == src or has_audio.get(parent, False):   # topmost audio-empty dir = the album shell
            targets.append(dp)
    moved = 0
    for dp in targets:
        dpath = Path(dp)
        if not dpath.is_dir():
            continue
        if any(Path(f).suffix.lower() in AUDIO for _, _, fs in os.walk(dp) for f in fs):
            log.warning("prune: skip %s/ -- audio appeared since the scan (not an empty shell)", dpath.name)
            continue
        dest = quarantine_dir(dump, "shells", fallback=dpath.name)   # no audio -> no metadata, use source name
        if do_apply:
            dest.mkdir(parents=True, exist_ok=True)  # may already exist (redundant cover dumped here by apply)
            for child in dpath.iterdir():            # merge leftovers in, don't spawn a "(2)" sibling
                d = dest / child.name
                i = 1
                while d.exists():
                    i += 1
                    d = dest / f"{child.stem} ({i}){child.suffix}"
                safe_move(child, d, log)
            with suppress(OSError):
                dpath.rmdir()                        # empty now (unless a move failed -> shell left in place)
        moved += 1
        log.info("%s %s/ -> %s", "SHELL" if do_apply else "DRY ", dpath.name, dest)
    log.info("%d imported shell(s) -> quarantine", moved)
    return moved
