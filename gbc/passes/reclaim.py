"""Reclaim verified source originals -- preserve-mode only (beets copy / reflink / hardlink).

After `beet import` COPIED matched albums into clean and `verify` confirmed each track by AcoustID
fingerprint, a SOURCE album whose every track has a positively-verified ("ok") clean copy is redundant:
the whole source folder is moved to $MUSIC_DUMP (never deleted). PER-ALBUM only -- a folder is reclaimed
solely when ALL its audio is accounted for in clean AND every matched track verified ok, so a
partially-matched or any-track-unverified album stays intact in source for curation.

clean<->source correlation reuses sidecars' proven DURATION-MULTISET match (robust to tag/name rewrites),
kept strictly BIJECTIVE: a source leaf folder is reclaimed only when it matches exactly ONE clean album
AND that clean album is matched by exactly ONE source folder. So two duplicate source folders (legitimate
in copy mode -- dedup is off) that both match the single imported clean copy are BOTH kept (we cannot tell
which one was copied). A folder with any unreadable track (ffprobe failure -> unmeasurable) is skipped, never
guessed. Multi-disc / nested / ambiguous folders likewise stay in source.

Verdicts come from the verify pass (BEETSDIR/gbc-verify-verdicts.json), written fresh each run so stale data
can never trigger a reclaim. verify is watermark-scoped, so reclaim only acts on albums verified THIS run;
older albums kept earlier are revisited with `gbc run --all` (logged as "unverified-this-run").
"""
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .. import beetscfg
from ..config import Config
from ..logs import get_logger
from ..sidecars import AUDIO, durs_of, matches, quarantine_dir, safe_move

VERDICTS = "gbc-verify-verdicts.json"


def _source_albums(src: str) -> dict[str, list[int]]:
    """Leaf source folders (those directly holding audio) -> sorted track durations."""
    by_dir: dict[str, list[str]] = defaultdict(list)
    for dp, _, files in os.walk(src):
        for fn in files:
            if Path(fn).suffix.lower() in AUDIO:
                by_dir[dp].append(str(Path(dp) / fn))
    out = {}
    for d, paths in by_dir.items():
        ds = durs_of(paths)
        if ds and len(ds) == len(paths):    # every track measured; a probe failure -> can't fully measure -> skip
            out[d] = ds
    return out


def _dec(v):
    return v.decode("utf-8", "surrogateescape") if isinstance(v, bytes) else ("" if v is None else str(v))


def _clean_albums(db: str, clean_root: str) -> dict[str, dict]:
    """beets items grouped by clean album dir -> {durs, ids, meta=(albumartist, album, year)}. Verdicts are
    keyed by item id (not path), so reclaim matches verify regardless of path-rendering differences."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        rows = con.execute("SELECT id, path, length, albumartist, album, year FROM items").fetchall()
    out: dict[str, dict] = defaultdict(lambda: {"durs": [], "ids": [], "meta": ("", "", "")})
    for (itemid, path, length, albumartist, album, year) in rows:
        pp = Path(_dec(path))
        if not pp.is_absolute():                       # beets >=2.10 stores paths relative to the lib root
            pp = Path(clean_root) / pp
        d = out[str(pp.parent)]
        d["durs"].append(round(length or 0))
        d["ids"].append(str(itemid))
        a, al, y = d["meta"]                            # fill each field from the first NON-EMPTY track value
        d["meta"] = (a or _dec(albumartist), al or _dec(album), y or _dec(year))
    for v in out.values():
        v["durs"].sort()
    return out


def run(cfg: Config, log=None) -> int:
    """Move fully-verified source albums to quarantine (preserve+independent mode). Returns the count moved."""
    log = log or get_logger("reclaim")
    bi = beetscfg.read_import(cfg)
    if not bi.clean_independent:
        log.info("reclaim skipped: beets import=%s (source consumed, or clean not an independent copy)", bi.label)
        return 0
    if not cfg.library.exists():
        return 0
    try:
        verdicts = json.loads((cfg.beetsdir / VERDICTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        verdicts = {}

    src_albums = _source_albums(str(cfg.src))
    clean_albums = _clean_albums(str(cfg.library), str(cfg.clean))
    src_root = str(Path(cfg.src).resolve())

    # Correlate source<->clean by duration-multiset, then keep ONLY bijective pairs: a source album is
    # reclaimed solely when it matches exactly one clean album AND that clean album is matched by exactly
    # one source album. Two duplicate source folders (legitimate in copy mode -- dedup is off) match the
    # single clean copy beets imported -> NEITHER is reclaimed (we cannot tell which one was copied).
    src_match: dict[str, str] = {}
    clean_hits: dict[str, int] = defaultdict(int)
    moved = kept = ambig = unverified = 0
    for sdir, sdurs in src_albums.items():
        if str(Path(sdir).resolve()) == src_root:      # never move the source root itself
            continue
        cdirs = [cdir for cdir, c in clean_albums.items()
                 if len(c["ids"]) == len(sdurs) and matches(c["durs"], sdurs)]
        if len(cdirs) == 1:
            src_match[sdir] = cdirs[0]
            clean_hits[cdirs[0]] += 1
        else:
            kept += 1
            ambig += len(cdirs) > 1

    for sdir, cdir in src_match.items():
        c = clean_albums[cdir]
        if clean_hits[cdir] != 1:                      # >1 source matches this clean album -> can't tell which
            kept += 1
            ambig += 1
            continue
        if not all(i in verdicts for i in c["ids"]):   # no verdict this run (out of verify scope) -> revisit --all
            kept += 1
            unverified += 1
            continue
        if not all(verdicts.get(i) == "ok" for i in c["ids"]):
            kept += 1                                  # a track is imposter / rare / inconclusive -> keep source
            continue
        qd = quarantine_dir(cfg.dump, "reclaimed", *c["meta"], fallback=Path(sdir).name)
        dest = qd
        i = 1
        while dest.exists():
            i += 1
            dest = qd.with_name(f"{qd.name} ({i})")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if safe_move(sdir, dest, log):
            moved += 1
            log.info("RECLAIM verified album: %s -> %s/ (%d track(s) all ok)", Path(sdir).name, dest, len(c["ids"]))
    log.info("=== reclaim: %d album(s) -> %s; %d kept (%d ambiguous, %d unverified-this-run; --all to revisit) ===",
             moved, cfg.dump, kept, ambig, unverified)
    return moved
