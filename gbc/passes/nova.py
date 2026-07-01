"""Nova compilation recovery -- OPT-IN, self-contained, easily DETACHABLE: delete this file, the `nova` lines
in cli.py, and the guarded `nova.reroute(...)` call in singletons.py, and the feature is gone. gbc/mb.py and
the GENERAL "promote complete albums" step stay -- they are NOT Nova-specific.

Radio Nova's "Nova Tunes" / "Nova Classics" compilations are partly dispersed as loose tracks across the
source. This caches every Nova-compilation recording from MusicBrainz (the two release-group SERIES), then,
during `gbc singletons`, RE-TAGS the loose tracks that belong to a Nova compil so they regroup under it
(mb_albumid = the Nova release). The general promote step then assembles a COMPLETE compil into
_Various Artists/<compil>/ and leaves an incomplete one as loose tracks under _Singles/Various Artists/.
"""
import json
import time
import urllib.error
from collections import defaultdict

from .. import mb
from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..util import write_json

CACHE = "gbc-nova-cache.json"
SERIES = {                                       # MusicBrainz release-group series (the canonical Nova index)
    "Nova Tunes": "091027ef-f8fa-4845-9049-39b5bdb98d08",
    "Nova Classics": "257930a3-b4aa-4f57-86cd-a2bdad9942ca",
}


def _build_cache(cfg: Config, log) -> dict:
    """Every recording on a Nova Tunes / Nova Classics release -> {recording: {compil, track, total, albumid}}.
    Box sets (titles ending '..._...', e.g. 'Nova Tunes 01_10') are skipped. MB is rate-limited (~1 req/s)."""
    rgs: dict = {}
    for sid in SERIES.values():
        for rel in mb.get(f"series/{sid}?inc=release-group-rels&fmt=json").get("relations", []):
            rg = rel.get("release_group") or {}
            title = (rg.get("title") or "").strip()
            if rg.get("id") and title and "_" not in title.split()[-1]:   # guard: empty title -> [-1] IndexError
                rgs[rg["id"]] = title
        time.sleep(1.1)
    cache: dict = {}
    for rg_id, compil in rgs.items():
        rel = mb.get(f"release?release-group={rg_id}&fmt=json&limit=1").get("releases", [])
        time.sleep(1.1)
        if not rel:
            continue
        albumid = rel[0]["id"]
        tracks = [t for m in mb.get(f"release/{albumid}?inc=recordings&fmt=json").get("media", [])
                  for t in m.get("tracks", [])]
        time.sleep(1.1)
        for n, t in enumerate(tracks, 1):        # absolute position over flattened media -> no per-disc collision
            rec = t.get("recording", {})         # (MB `position` resets each disc; `total` is the grand total)
            if rec.get("id"):
                cache[rec["id"]] = {"compil": compil, "track": n,
                                    "total": len(tracks), "albumid": albumid}
    cfg.beetsdir.mkdir(parents=True, exist_ok=True)
    write_json(cfg.beetsdir / CACHE, cache)                # atomic (tmp + replace): a crash can't corrupt the cache
    log.info("nova: cached %d recording(s) across %d compils", len(cache), len(rgs))
    return cache


def _load_cache(cfg: Config, refresh: bool, log) -> dict:
    path = cfg.beetsdir / CACHE
    if refresh or not path.exists():
        try:
            return _build_cache(cfg, log)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.error("nova: cache build failed: %s", e)
            return {}
    return json.loads(path.read_text(encoding="utf-8"))


def reroute(cfg: Config, log, apply: bool, refresh: bool = False) -> int:
    """Re-tag each loose singleton that is a Nova-compilation recording so it regroups under its compil
    (mb_albumid = the Nova release); the modify also moves incomplete ones to _Singles/Various Artists/<compil>/.
    The general promote step assembles the complete compils afterwards. Dry (count only) unless apply."""
    cache = _load_cache(cfg, refresh, log)
    if not cache:
        return 0
    _, text = run_beet(cfg, ["ls", "-f", "$id\t$mb_trackid", "singleton:1", "mb_trackid::."],
                       passname="nova", echo_lines=False)
    todo = [(sid.strip(), cache[rec.strip()])
            for sid, _, rec in (ln.partition("\t") for ln in text.splitlines())
            if sid.strip() and rec.strip() in cache]
    if not apply:
        log.info("nova: %d loose Nova track(s) would be re-tagged to their compil", len(todo))
        return len(todo)
    for sid, info in todo:                       # per-item: scope by id so an album copy of the same recording
        args = ["modify", "-y", f"id:{sid}",                  # (no-dup case) is never touched
                f"mb_albumid={info['albumid']}", f"album={info['compil']}",
                "albumartist=Various Artists", "comp=1"]
        if info.get("track") is not None:        # MB `position` can be missing -> don't write the literal "None"
            args += [f"track={info['track']}", f"tracktotal={info['total']}"]
        run_beet(cfg, args, passname="nova", echo_lines=False)
    log.info("nova: re-tagged %d loose Nova track(s) -> their compil", len(todo))
    return len(todo)


def run(cfg: Config, refresh: bool = False) -> int:
    """Standalone classify-report: which Nova compils are reconstructable from the library's loose tracks."""
    log = get_logger("nova")
    cache = _load_cache(cfg, refresh, log)
    if not cache:
        return 1
    # NO-DUP: a recording already inside a clean album is left there and never counted, so a compil is only
    # "complete" when ALL its tracks are available as loose singletons.
    _, text = run_beet(cfg, ["ls", "-f", "$mb_trackid\t$singleton", "mb_trackid::."],
                       passname="nova", echo_lines=False)
    loose: set = set()
    in_album: set = set()
    for line in text.splitlines():
        rec, _, is_singleton = line.partition("\t")
        if rec.strip():
            (loose if is_singleton.strip() == "True" else in_album).add(rec.strip())
    compils: dict = defaultdict(lambda: {"total": 0, "loose": 0})
    for rec, info in cache.items():
        c = compils[info["compil"]]
        c["total"] = info["total"]
        if rec in loose and rec not in in_album:
            c["loose"] += 1
    complete = sorted(c for c, d in compils.items() if d["total"] and d["loose"] == d["total"])
    partial = sorted((c, d["loose"], d["total"]) for c, d in compils.items() if 0 < d["loose"] < d["total"])
    log.info("=== nova: %d compil(s) reconstructable COMPLETE from loose tracks, %d partial ===",
             len(complete), len(partial))
    for c in complete:
        log.info("  COMPLETE -> _Various Artists/: %s", c)
    for c, n, t in partial:
        log.info("  partial (%d/%d loose) -> _Singles/Various Artists/: %s", n, t, c)
    return 0
