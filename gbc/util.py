"""Small shared helpers for the passes."""
import contextlib
import os
import shutil
from datetime import datetime
from pathlib import Path

from .beets import run_beet
from .config import Config


def backup_db(cfg: Config, tag: str, log) -> None:
    """Safeguard copy of library.db before any mass write."""
    lib = cfg.library
    if lib.exists():
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        dest = lib.with_name(f"{lib.name}.{tag}-{stamp}.bak")
        shutil.copy2(lib, dest)
        for ext in ("-wal", "-shm"):                       # include the SQLite WAL/SHM sidecars if present, so
            side = lib.with_name(lib.name + ext)           # the backup is consistent even under WAL journaling
            if side.exists():
                shutil.copy2(side, dest.with_name(dest.name + ext))
        log.info("backup %s -> %s", lib.name, dest.name)


def count_items(cfg: Config, args, passname: str) -> int:
    """Number of items/albums matching `beet <args>` (captured silently); logged under the caller's pass."""
    _, text = run_beet(cfg, args, passname=passname, echo_lines=False)
    return sum(1 for ln in text.splitlines() if ln.strip())


def prune_empty_dirs(root) -> None:
    """Remove now-empty directories under root (root itself kept). = find -mindepth 1 -type d -empty -delete."""
    root = str(root)
    for dp, _, _ in sorted(os.walk(root), key=lambda x: x[0], reverse=True):
        if dp == root:
            continue
        with contextlib.suppress(OSError):
            Path(dp).rmdir()        # only succeeds if empty
