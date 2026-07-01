"""Source-side helpers for a CONSUMED import: the quarantine layout, safe move/unique-dest, and pruning the
emptied album shells beets leaves behind after moving the audio out. Carrying an album's extra files
(booklet/scans/back art, .cue/.log, paired .lrc) into the clean album is done natively by the `filetote`
beets plugin during import (see config.yaml) -- fetchart already owns the primary cover.
"""
import os
import shutil
from contextlib import suppress
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


def unique_dest(folder, name):
    """A NON-colliding destination path in `folder` for `name`: append ' (N)' before the suffix while it exists.
    `shutil.move` overwrites an existing file, so callers staging same-basename tracks (VA comps) must use this."""
    dest = Path(folder) / name
    n = 1
    while dest.exists():
        n += 1
        dest = Path(folder) / f"{Path(name).stem} ({n}){Path(name).suffix}"
    return dest

AUDIO = {".mp3", ".flac", ".m4a", ".m4b", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wma",
         ".wav", ".aif", ".aiff", ".ape", ".wv", ".mpc", ".tta", ".dsf", ".dff", ".mp2"}


def _san(s):
    """One safe path component: drop separators, strip leading/trailing dots & spaces."""
    return str(s).replace("/", "_").replace("\\", "_").strip(". ")


def quarantine_dir(dump, reason, albumartist="", album="", year="", *, fallback=""):
    """Canonical $MUSIC_DUMP layout, grouped by WHY, mirroring clean: <reason>/<Albumartist>/<Album (Year)>/.
    `reason` = category (imposters/duplicates/redundant-art/shells). Falls back to <reason>/<fallback>
    when there is no metadata (audio-less shells, untagged files)."""
    base = Path(dump) / reason
    # mirror clean's "_"-prefixed VA collection (callers may pass it already, or a metadata-only/de-underscored name)
    artist = {"Various Artists": "_Various Artists"}.get(_san(albumartist), _san(albumartist))
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


def prune_shells(src, dump, do_apply, log=None):
    """Imported-album shells (source dirs whose ENTIRE subtree has no audio left) -> quarantine, one folder per
    album. Bottom-up scan, take the TOPMOST audio-empty dir, so a leftover subfolder (Scans/, @eaDir/...) moves
    WITH its parent shell. Folders still holding audio (skipped albums) stay in source."""
    log = _log(log)
    src = str(src)
    has_audio, direct_audio = {}, {}
    for dp, dirs, files in os.walk(src, topdown=False):
        direct_audio[dp] = any(Path(f).suffix.lower() in AUDIO for f in files)
        has_audio[dp] = direct_audio[dp] or any(has_audio.get(str(Path(dp) / d), False) for d in dirs)
    targets = []
    for dp, dirs, files in os.walk(src):
        if dp == src or has_audio.get(dp, False) or not (files or dirs):
            continue
        parent = str(Path(dp).parent)
        # topmost audio-empty dir = an album shell: parent has audio only via OTHER subdirs (artist folder), not
        # directly. A parent with audio DIRECTLY is a LIVE (skipped) album -> don't strip its Scans/ @eaDir/ subfolder.
        if parent == src or (has_audio.get(parent, False) and not direct_audio.get(parent, False)):
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
            ok = True
            for child in dpath.iterdir():            # merge leftovers in, don't spawn a "(2)" sibling
                if not safe_move(child, unique_dest(dest, child.name), log):
                    ok = False                       # a failed move leaves a child behind -> shell not fully cleared
            if not ok:
                log.warning("prune: %s/ only partially moved -- shell left in place, NOT counted", dpath.name)
                continue                             # don't rmdir, don't count/log as quarantined
            with suppress(OSError):
                dpath.rmdir()                        # empty now that every child moved
        moved += 1
        log.info("%s %s/ -> %s", "SHELL" if do_apply else "DRY ", dpath.name, dest)
    log.info("%d imported shell(s) -> quarantine", moved)
    return moved
