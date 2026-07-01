"""Shared import lock (filelock): `inbox` (cron) takes it non-blocking and bows out if busy; `run` waits."""
from contextlib import contextmanager

from filelock import FileLock, Timeout

from .config import Config
from .logs import get_logger


@contextmanager
def import_lock(cfg: Config, *, blocking: bool = True):
    """Yields True if acquired (released on exit), False if busy (non-blocking only)."""
    cfg.beetsdir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(cfg.beetsdir / ".import.lock"))
    try:
        lock.acquire(timeout=0)                        # fast path: acquire immediately if free
    except Timeout:
        if not blocking:                               # cron door -> bow out, another run holds it
            yield False
            return
        get_logger("lock").info("waiting for the import lock (another gbc run is active)...")
        lock.acquire(timeout=-1)                        # then block until the holder releases
    try:
        yield True
    finally:
        lock.release()
