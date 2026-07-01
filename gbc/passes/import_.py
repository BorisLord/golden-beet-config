"""Pass 1 -- album match import (AcoustID + tags): source -> clean album lib.

Branches on the EFFECTIVE beets import op (via `beetscfg`):
  - source CONSUMED (move / copy+delete): dedup the source (keep the best copy), then sweep the emptied shells.
  - source PRESERVED (copy / reflink / hardlink / symlink / in-place): source READ-ONLY -- dedup/prune skipped.
Album extra files (booklet/scans/back art, .cue/.log, paired .lrc) ride along natively via the `filetote`
plugin during import, in BOTH modes; fetchart owns the primary cover.
"""
from pathlib import Path

from .. import artfix, beetscfg, sidecars
from ..beets import run_beet
from ..config import Config
from ..dedup import dedup
from ..logs import get_logger
from ..probe import ProbeCache
from ..util import backup_db, count_items, prune_empty_dirs


def _beet_import(cfg: Config, src: Path, reimport: bool, log) -> int:
    inc = "-I" if reimport else "-i"      # -I = noincremental: re-evaluate already-seen (modified) folders
    rc, _ = run_beet(cfg, ["import", "-q", inc, str(src)], passname="import")
    if rc:
        log.error("beet import failed (rc=%d)", rc)
    return rc


def _normalize_va_comp(cfg: Config, log) -> None:
    """A Various-Artists compilation matched via Discogs (or an MB release without the VA flag) lands with
    albumartist='Various Artists' but comp=False -- an inconsistency that fragments the album in players.
    Normalize it natively: `beet modify` comp=1 wherever the album artist is VA but comp is unset (writes the
    DB + the compilation tag; -M never moves)."""
    n = count_items(cfg, ["ls", "albumartist::Various Artists", "comp:False"], "import")
    if n:
        run_beet(cfg, ["modify", "-y", "-M", "albumartist::Various Artists", "comp:False", "comp=1"],
                 passname="import", echo_lines=False)
        log.info("normalised %d Various-Artists track(s): comp False -> True (compilation flag)", n)


def run(cfg: Config, src=None, reimport=False) -> int:
    log = get_logger("import")
    src = Path(src) if src else cfg.src
    if not src.is_dir():
        log.error("source missing: %s", src)
        return 1
    artfix.run(cfg, src=src, log=log)          # strip mime=None WMA art so scrub can't crash beet import
    bi = beetscfg.read_import(cfg)
    backup_db(cfg, "import", log)

    if bi.source_consumed:
        cache = ProbeCache(cfg.beetsdir / "gbc-probe-cache.json")   # one probe/file for dedup (upgrade reuses it)
        dedup(str(src), str(cfg.dump), True, log, cache=cache)      # best bitrate kept
        cache.save()
        rc = _beet_import(cfg, src, reimport, log)                  # filetote carries booklet/scans/.lrc/.cue/.log
        if rc == 0:                                 # only mutate the consumed source on a CLEAN import (else retry)
            sidecars.prune_shells(str(src), str(cfg.dump), True, log)
            prune_empty_dirs(src)
        else:
            log.error("import rc=%d -> skip prune; source left intact for the next run's retry", rc)
    else:
        log.info("source preserved (beets import=%s) -> dedup/sidecars/prune skipped; source untouched", bi.label)
        rc = _beet_import(cfg, src, reimport, log)

    _normalize_va_comp(cfg, log)               # VA-but-comp=False (Discogs/non-VA-MB matches) -> comp=True

    art_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    covers = sum(1 for p in cfg.clean.rglob("cover.*") if p.suffix.lower() in art_exts) if cfg.clean.exists() else 0
    log.info("items: %d | albums: %d | covers: %d",
             count_items(cfg, ["ls"], "import"), count_items(cfg, ["ls", "-a"], "import"), covers)
    return rc
