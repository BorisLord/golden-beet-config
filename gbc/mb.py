"""Tiny MusicBrainz read client (no key needed). Shared by passes; not coupled to any one feature (deleting
nova leaves it intact)."""
import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from .util import write_text

if TYPE_CHECKING:
    from .config import Config

_UA = "gbc/0.9 (golden-beets-config)"
_BASE = "https://musicbrainz.org/ws/2/"
RELEASE_CACHE = "gbc-mb-release-cache.json"   # albumid -> recording ids. Persisted + SHARED by verify (demote
                                              # incomplete) and singletons (promote complete) so the same MB
                                              # release tracklist isn't re-fetched across passes/runs (cron-friendly).


def get(path: str, retries: int = 4):
    """GET a MusicBrainz endpoint as JSON, with backoff retry on transient errors (MB 503s under its rate
    limiter are routine). A 4xx (bad id) is not retried. Raises the last error if all attempts fail."""
    req = urllib.request.Request(_BASE + path, headers={"User-Agent": _UA})
    last: Exception = RuntimeError("no attempt made")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise                       # client error (e.g. bad/absent id) -> retrying won't help
            last = e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
        if attempt < retries - 1:                   # don't sleep after the final attempt -> raise immediately
            time.sleep(2 ** attempt)
    raise last


def release_recordings(albumid: str) -> frozenset:
    """Every recording MBID on a release (all discs). Empty frozenset on fetch error -> caller treats it as
    'cannot verify' and leaves the album alone (never a wrong promotion)."""
    try:
        data = get(f"release/{albumid}?inc=recordings&fmt=json")
    except (urllib.error.URLError, OSError, ValueError):
        return frozenset()
    time.sleep(1.1)                                  # MB rate limit (~1 req/s)
    return frozenset(t["recording"]["id"]
                     for m in data.get("media", []) for t in m.get("tracks", [])
                     if t.get("recording", {}).get("id"))


def missing_recordings(albumid: str, present_trackids, cache: dict | None = None):
    """The release's recordings NOT present in `present_trackids`, or None if the tracklist can't be fetched
    (caller leaves the album alone). Empty frozenset = the album is COMPLETE. `cache` (albumid -> recordings)
    avoids re-fetching the same release across albums. Shared by verify (demote incomplete) + singletons
    (promote complete) so both judge completeness against the live MB tracklist the same way."""
    if cache is not None and albumid in cache:
        official = cache[albumid]
    else:
        official = release_recordings(albumid)
        if cache is not None:
            cache[albumid] = official
    if not official:
        return None

    return official - frozenset(present_trackids)


def load_release_cache(cfg: "Config", refresh: bool = False) -> dict:
    """{albumid: frozenset(recording ids)} from disk -- the `cache` you hand to release_recordings /
    missing_recordings. PERSISTED + SHARED by verify (demote) and singletons (promote) so the same MB tracklist
    isn't re-fetched across passes/runs (cron-friendly). `refresh` (an --all / --reimport run) starts empty to
    re-pull."""
    if refresh:
        return {}
    try:
        raw = json.loads((cfg.beetsdir / RELEASE_CACHE).read_text(encoding="utf-8"))
        return {k: frozenset(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_release_cache(cfg: "Config", cache: dict) -> None:
    # persist only SUCCESSFUL (non-empty) tracklists -> a transient MB 503 (cached as an empty frozenset within a
    # run so it isn't retried mid-run) is NOT written, so it gets retried on the next run instead of poisoning it.
    live = {k: sorted(v) for k, v in cache.items() if v}
    write_text(cfg.beetsdir / RELEASE_CACHE, json.dumps(live, ensure_ascii=False))
