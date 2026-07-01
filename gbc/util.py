"""Small shared helpers for the passes."""
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .beets import run_beet
from .config import Config


@contextlib.contextmanager
def skip_on_error(log, passname: str, item) -> Iterator[None]:
    """Per-item guard inside a pass loop: an unexpected error on ONE item is logged + swallowed so the loop
    continues (one bad album/file never aborts the pass, nor loses the state written after it).
    Use: `for x in xs: with skip_on_error(log, "pass", x): <body>`."""
    try:
        yield
    except Exception as e:                              # deliberate catch-all: this IS the isolation boundary
        log.warning("%s: skipped %r (%s: %s)", passname, item, type(e).__name__, e)


def backup_db(cfg: Config, tag: str, log) -> None:
    """Safeguard copy of library.db before any mass write."""
    lib = cfg.library
    if lib.exists():
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        dest = lib.with_name(f"{lib.name}.{tag}-{stamp}.bak")
        shutil.copy2(lib, dest)
        for ext in ("-wal", "-shm"):                       # copy WAL/SHM sidecars too: backup stays
            side = lib.with_name(lib.name + ext)           # consistent under WAL journaling
            if side.exists():
                shutil.copy2(side, dest.with_name(dest.name + ext))
        log.info("backup %s -> %s", lib.name, dest.name)


def _atomic_write(path, write_fn) -> None:
    """Atomically write to `path` (sibling tmp + os.replace), so a crash/ENOSPC mid-write can't corrupt the file
    and silently discard a hard-won cache. `write_fn(fh)` does the writing. The single atomic-write primitive
    (state.py's watermark/progress writers go through write_json -> here too)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            write_fn(fh)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path, obj) -> None:
    """Atomically write `obj` as JSON to `path`, STREAMED (json.dump to the handle) so a large cache isn't first
    buffered into one big string in RAM."""
    _atomic_write(path, lambda fh: json.dump(obj, fh, ensure_ascii=False))


def write_text(path, text: str) -> None:
    """Atomically write `text` to `path`. Used for the JSONL id-cache compaction rewrite."""
    _atomic_write(path, lambda fh: fh.write(text))


def length_secs(s: str) -> int:
    """Parse beets' `$length` format ('M:SS', or 'H:MM:SS') into whole seconds. Used to read track durations
    from `beet ls` natively (the only template beets exposes; it floors to whole seconds -- callers correlate
    with a tolerance that absorbs the <=1s floor-vs-round difference vs ffprobe-measured source durations)."""
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


def count_items(cfg: Config, args, passname: str) -> int:
    """Count items/albums matching `beet <args>`, silently; logged under the caller's pass."""
    _, text = run_beet(cfg, args, passname=passname, echo_lines=False)
    return sum(1 for ln in text.splitlines() if ln.strip())


def prune_empty_dirs(root) -> None:
    """Remove empty dirs under root (root kept). = find -mindepth 1 -type d -empty -delete."""
    root = str(root)
    for dp, _, _ in sorted(os.walk(root), key=lambda x: x[0], reverse=True):
        if dp == root:
            continue
        with contextlib.suppress(OSError):
            Path(dp).rmdir()
