"""The cron door. Same pipeline as `run`, triggered by a drop: take the import lock (bow out if busy),
debounce until the drop finished copying, skip if nothing NEW to import, then run the pipeline.
"""
import re
import time
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..lock import import_lock
from ..logs import get_logger
from . import pipeline


def _dir_size(path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:                                 # file can vanish between is_file() and stat() mid-drop -> skip it
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def has_new(plan: str) -> bool:
    """True if beet's --pretend plan lists something to import. beet writes the plan to STDERR (callers must
    capture stderr; reading stdout alone always looks empty)."""
    return bool(re.search(r"(?m)^(Album|Singleton):", plan))


def _debounce(cfg: Config, interval: int = 20, max_wait: int = 1800) -> None:
    """Wait until source size is stable across two samples (drop finished copying), capped at `max_wait`s so
    a continuously-growing source can't wedge the import lock forever."""
    prev = -1
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        cur = _dir_size(cfg.src)
        if cur == prev:
            return
        prev = cur
        time.sleep(interval)
    get_logger("inbox").warning("debounce: source still changing after %ds -> proceeding anyway", max_wait)


def run(cfg: Config) -> int:
    log = get_logger("inbox")
    with import_lock(cfg, blocking=False) as got:
        if not got:
            log.info("another import in progress -> exit")
            return 0
        if not cfg.src.exists() or not any(cfg.src.iterdir()):
            log.info("source empty -> exit")
            return 0
        _debounce(cfg)                       # settle the drop BEFORE the has_new gate: a still-copying tree can
                                             # show a partial --pretend plan and wrongly skip the whole tick
        _, plan = run_beet(cfg, ["import", "-q", "-i", "--pretend", str(cfg.src)],
                           passname="inbox", echo_lines=False)
        if not has_new(plan):
            log.info("nothing new to import -> exit")
            return 0
        return pipeline.run(cfg, upgrade_scan=False)   # cron door: skip the costly full-source upgrade scan
