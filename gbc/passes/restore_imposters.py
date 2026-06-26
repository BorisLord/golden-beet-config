"""TEMPORARY one-off recovery -- re-merge the FALSE imposters the OLD verify over-quarantined.

The refined `verify` now keeps same-song siblings (credit/edition variants) and only flags on POSITIVE
evidence (the audio confidently matches a different recording). The existing `quarantine/imposters/` is
therefore full of tracks that should never have left clean. This pass moves each quarantined album back into
its clean album and re-imports it as-is; the NEXT `gbc run`'s (refined) verify then re-filters -- the
genuinely-wrong tracks return to quarantine, the false positives stay as complete albums. So no manual
false-vs-real sorting.

REMOVE THIS once `quarantine/imposters/` has been restored -- it is NOT needed for normal operation.
Detachable like nova: delete this file + the guarded `restore_imposters` lines in cli.py and the command is
gone (nothing else imports it).
"""
import os
import re
from contextlib import suppress
from pathlib import Path

import mediafile

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..sidecars import AUDIO, safe_move
from ..util import backup_db, prune_empty_dirs, skip_on_error


def _album_folders(root: Path) -> dict:
    """Leaf folders under `root` that directly hold audio -> {folder(str): sorted [audio file paths]}."""
    out: dict = {}
    for dp, _, files in os.walk(root):
        audio = sorted(str(Path(dp) / fn) for fn in files if Path(fn).suffix.lower() in AUDIO)
        if audio:
            out[dp] = audio
    return out


def _read_albumid(audio_files):
    """(mb_albumid, album) from the first readable file (Discogs stores its release id in mb_albumid too)."""
    for f in audio_files:
        try:
            mf = mediafile.MediaFile(f)
        except Exception:                                     # unreadable/corrupt -> try the next file
            continue
        return (mf.mb_albumid or "", mf.album or "")
    return ("", "")


def _clean_album_dir(cfg: Config, albumid: str):
    """The clean album folder for `mb_albumid` (parent of one of its items), or None if it's not in clean."""
    _, text = run_beet(cfg, ["ls", f"mb_albumid:{albumid}", "-f", "$path"],
                       passname="restore-imposters", echo_lines=False)
    for line in text.splitlines():
        if line.strip():
            return Path(line.strip()).parent
    return None


def run(cfg: Config, apply: bool = False) -> int:
    """Re-merge each quarantine/imposters album back into clean (dry unless apply). Returns the track count."""
    log = get_logger("restore-imposters")
    root = cfg.dump / "imposters"
    if not root.is_dir():
        log.info("restore-imposters: no %s -> nothing to do", root)
        return 0

    # Re-import WITHOUT the on-import plugins: fetchart/replaygain/lastgenre already ran on the original import,
    # re-running them is pointless and fetchart's web fetch can HANG with no timeout. A `plugins: []` overlay
    # (passed via `beet -c`) disables them, so the restore just re-adds the files fast.
    noplugins = cfg.beetsdir / ".gbc-restore-noplugins.yaml"
    if apply:
        cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        noplugins.write_text("plugins: []\n", encoding="utf-8")

    backed = False
    albums = tracks = 0
    for folder, audio in _album_folders(root).items():
        albumid, album = _read_albumid(audio)
        label = album or Path(folder).name
        if not albumid:
            log.warning("restore-imposters: skip %s -- no mb_albumid (can't correlate safely)", label)
            continue
        if not apply:
            log.info("restore-imposters: would restore %d track(s) -> %s", len(audio), label)
            albums += 1
            tracks += len(audio)
            continue
        with skip_on_error(log, "restore-imposters", label):
            if not backed:
                backup_db(cfg, "restore-imposters", log)      # one safeguard copy before the first mutation
                backed = True
            clean_dir = _clean_album_dir(cfg, albumid)
            if clean_dir is not None and clean_dir.is_dir():
                # partial: album still in clean (missing these tracks) -> drop them back IN, drop the lib album
                # (keep files), re-import the now-complete folder as-is.
                # The original import set comp=True in the DB (NOT the file tags); a plain -A re-import loses it
                # and mis-routes a VA comp to 'Various Artists/' instead of '_Various Artists/'. Capture + restore.
                _, ct = run_beet(cfg, ["ls", "-a", "comp:1", f"mb_albumid:{albumid}", "-f", "$id"],
                                 passname="restore-imposters", echo_lines=False)
                was_comp = bool(ct.strip())
                moved = [p for p in audio if safe_move(p, clean_dir / Path(p).name, log)]
                if not moved:
                    continue
                run_beet(cfg, ["remove", "-a", "-f", f"mb_albumid:{albumid}"],
                         passname="restore-imposters", echo_lines=False)
                run_beet(cfg, ["-c", str(noplugins), "import", "-q", "-I", "-A", "--flat", str(clean_dir)],
                         passname="restore-imposters")
                if was_comp:                                  # re-apply the lost comp normalisation + re-route
                    run_beet(cfg, ["modify", "-a", "-y", f"mb_albumid:{albumid}", "comp=1"],
                             passname="restore-imposters", echo_lines=False)
                    run_beet(cfg, ["move", "-a", f"mb_albumid:{albumid}"],
                             passname="restore-imposters", echo_lines=False)
                target = clean_dir.name
            else:
                # whole album was quarantined -> stage + import; beets routes it into clean via the path templates.
                staging = cfg.beetsdir / ".gbc-restore" / re.sub(r"[^\w.-]", "_", albumid)
                staging.mkdir(parents=True, exist_ok=True)
                moved = [p for p in audio if safe_move(p, staging / Path(p).name, log)]
                if not moved:
                    continue
                run_beet(cfg, ["-c", str(noplugins), "import", "-q", "-I", "-A", "--flat", str(staging)],
                         passname="restore-imposters")
                with suppress(OSError):
                    staging.rmdir()
                    staging.parent.rmdir()
                target = label
            albums += 1
            tracks += len(moved)
            log.info("restore-imposters: %d track(s) -> %s", len(moved), target)

    if apply:
        prune_empty_dirs(root)                                # drop the now-empty quarantine shells
    log.info("=== restore-imposters: %s %d album(s) / %d track(s) (re-verify on next gbc run) ===",
             "restored" if apply else "would restore", albums, tracks)
    return tracks
