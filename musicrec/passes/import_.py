"""Pass 1 -- album match import (AcoustID + tags): source -> clean album lib.

config = move:yes: matched albums MOVE to clean; non-imported files stay in source (the leftover pile to
curate). Official sidecars are carried into matched albums; imported shells go to quarantine. Same logic
the old 01-import.sh had, now importable + logged to the single musicrec.log.
"""
import tempfile
from pathlib import Path

from .. import sidecars
from ..beets import run_beet
from ..dedup import dedup
from ..logs import get_logger
from ..util import backup_db, count_items, prune_empty_dirs


def run(cfg, src=None) -> int:
    log = get_logger("import")
    src = Path(src) if src else cfg.src
    if not src.is_dir():
        log.error("source missing: %s", src)
        return 1
    backup_db(cfg, "rebuild", log)
    dedup(str(src), str(cfg.dump), True, log)                       # drop duplicate audio (best bitrate kept) first
    snap = tempfile.NamedTemporaryFile(prefix="sidecars-", suffix=".json", delete=False).name  # noqa: SIM115
    try:
        sidecars.snapshot(str(src), snap, log)                      # capture sidecars while source has its audio
        rc, _ = run_beet(cfg, ["import", "-q", "-i", str(src)], passname="import")  # auto: art+genres+rg+scrub
        if rc:
            log.error("beet import failed (rc=%d)", rc)
        sidecars.apply(snap, str(cfg.library), str(cfg.clean), str(cfg.dump), True, log)  # carry into clean
        sidecars.prune_shells(str(src), str(cfg.dump), True, log)   # imported shells -> quarantine
        prune_empty_dirs(src)
    finally:
        Path(snap).unlink(missing_ok=True)
    art_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    covers = sum(1 for p in cfg.clean.rglob("cover.*") if p.suffix.lower() in art_exts) if cfg.clean.exists() else 0
    log.info("items: %d | albums: %d | covers: %d",
             count_items(cfg, ["ls"]), count_items(cfg, ["ls", "-a"]), covers)
    return rc
