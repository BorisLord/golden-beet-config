"""TEMPORARY one-off recovery -- re-merge the FALSE imposters the OLD verify over-quarantined.

The refined `verify` now keeps same-song siblings and only flags on POSITIVE evidence, so `quarantine/imposters/`
is full of tracks that should never have left clean. This moves each quarantined album back into clean and
re-imports it; the next `gbc run`'s verify re-filters (genuinely-wrong tracks return, false positives stay).

REMOVE once `quarantine/imposters/` is restored. Detachable: delete this file + the guarded lines in cli.py.
"""
import os
import re
from contextlib import suppress
from pathlib import Path

import mediafile

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..sidecars import AUDIO, safe_move, unique_dest
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


def _albumid_q(albumid: str) -> str:
    """EXACT-match query for an mb_albumid. A bare `mb_albumid:1234` is a SUBSTRING match (also hits 12345/51234 --
    Discogs ids are bare integers), and the `remove -a`/`move -a` below are irreversible on whatever matches."""
    return f"mb_albumid::^{re.escape(albumid)}$"


def _clean_album_dir(cfg: Config, albumid: str):
    """The clean album folder for `mb_albumid` (parent of one of its items), or None if it's not in clean."""
    _, text = run_beet(cfg, ["ls", _albumid_q(albumid), "-f", "$path"],
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

    # Re-import WITHOUT on-import plugins (`plugins: []` overlay): they already ran on the original import, and
    # fetchart's web fetch can HANG with no timeout. So the restore just re-adds the files fast.
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
                # partial: album still in clean (missing these tracks) -> drop them back IN, drop the lib album,
                # re-import as-is. comp=True lived in the DB not the tags; a plain -A loses it and mis-routes a VA
                # comp to 'Various Artists/' not '_Various Artists/' -> capture + restore it.
                _, ct = run_beet(cfg, ["ls", "-a", "comp:1", _albumid_q(albumid), "-f", "$id"],
                                 passname="restore-imposters", echo_lines=False)
                was_comp = bool(ct.strip())
                moved = [p for p in audio if safe_move(p, unique_dest(clean_dir, Path(p).name), log)]
                if not moved:
                    continue
                run_beet(cfg, ["remove", "-a", "-f", _albumid_q(albumid)],
                         passname="restore-imposters", echo_lines=False)
                rc, _ = run_beet(cfg, ["-c", str(noplugins), "import", "-q", "-I", "-A", "--flat", str(clean_dir)],
                                 passname="restore-imposters")
                if rc:                                        # files are already in clean_dir (survive), but no rows
                    log.error("restore-imposters: re-import rc=%d for %s -- files back in clean, lib rows may be "
                              "missing (re-run `gbc run` to re-index)", rc, label)
                    continue                                  # don't count/normalise a failed import as restored
                if was_comp:                                  # re-apply the lost comp normalisation + re-route
                    run_beet(cfg, ["modify", "-a", "-y", _albumid_q(albumid), "comp=1"],
                             passname="restore-imposters", echo_lines=False)
                    run_beet(cfg, ["move", "-a", _albumid_q(albumid)],
                             passname="restore-imposters", echo_lines=False)
                target = clean_dir.name
            else:
                # whole album was quarantined -> stage + import; beets routes it into clean via the path templates.
                staging = cfg.beetsdir / ".gbc-restore" / re.sub(r"[^\w.-]", "_", albumid)
                staging.mkdir(parents=True, exist_ok=True)
                moved = [p for p in audio if safe_move(p, unique_dest(staging, Path(p).name), log)]
                if not moved:
                    continue
                rc, _ = run_beet(cfg, ["-c", str(noplugins), "import", "-q", "-I", "-A", "--flat", str(staging)],
                                 passname="restore-imposters")
                if rc:                                        # import failed -> files sit in staging, NOT in clean
                    log.error("restore-imposters: import rc=%d for %s -- %d file(s) left in %s for manual recovery "
                              "(NOT restored, NOT deleted)", rc, label, len(moved), staging)
                    continue                                  # leave staging intact; don't count as restored
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
