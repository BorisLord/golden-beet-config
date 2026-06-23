"""Pass -- enrich imported tracks with AcousticBrainz acoustic metadata (BPM, key, moods, danceability...).

AcousticBrainz stopped accepting submissions in 2022, but its database is FROZEN, not gone: the read API
still serves every recording it ever analysed, keyed by MusicBrainz recording id. Since gbc only keeps
strongly MB-matched albums, the `mb_trackid` beets assigns is exactly AB's key -> coverage is high (the
whole sample library returned 100%). So this is a cheap network-only enrichment, no local DSP needed
(that would be `beets-xtractor` + an Essentia build -- far heavier, for a gain that only matters on
non-MusicBrainz tracks gbc doesn't keep anyway).

We DON'T use beets' built-in `acousticbrainz` plugin: it is deprecated (logs "This plugin is deprecated
since AcousticBrainz has shut down") and could vanish from a future beets. Instead we hit the same public
API ourselves and write a CURATED SUBSET of its canonical field names (ABSCHEME below -- only the useful
ones: moods, danceability, voice/instrumental, key). `bpm` and `initial_key` are real media fields ->
written into the file tags by beets' own mediafile (a Subsonic/Navidrome player sees them); the
moods/danceability classifiers are non-standard -> stored as beets flexible attributes (typed via the
`types` plugin so `mood_relaxed:0.9..` ranges work). Since beets' mediafile silently skips flex attrs
on the file side (no tag frame mapping), we additionally inject them as standard custom-tag frames via
mutagen: TXXX (ID3/MP3), Vorbis comments (FLAC/OGG/Opus), freeform atoms (MP4/M4A). These are the
official extension mechanisms of each format -- not a hack -- and Navidrome reads them natively when
configured via `Tags.*.Aliases` in its navidrome.toml.

Frozen source => verdicts are cached forever per recording id (BEETSDIR/gbc-acousticbrainz-cache.json):
a recording present in AB is fetched once; one confirmed absent (404 / omitted) is never re-queried; a
network hiccup is left uncached -> retried next run. Best-effort: never gates the pipeline, never moves
or deletes a file.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import typing
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger

API = "https://acousticbrainz.org/api/v1"
BATCH = 25          # AB caps recording_ids at 25 per request
TIMEOUT = 25
_UA = "gbc/0.7 (golden-beets-config)"   # default Python-urllib UA can be 403'd/throttled by the public API

# Fields that beets' mediafile does NOT know how to map to file tag frames -> stored as db-only flex
# attrs (by the in-process bulk apply, _ab_bulk.py). We inject them into the files as standard custom-tag
# frames (TXXX / Vorbis comments / MP4 freeform atoms) so Navidrome and other media servers can read them.
FLEX_ATTRS = frozenset({
    "danceable", "key_strength", "tonal",
    "mood_acoustic", "mood_aggressive", "mood_electronic", "mood_happy",
    "mood_party", "mood_relaxed", "mood_sad",
    "moods_mirex", "voice_instrumental",
})

# Mapping from AB's nested JSON to beets field names. The field NAMES are the canonical ones from beets'
# (deprecated) beetsplug/acousticbrainz.py (so the ecosystem's queries still apply), but this is a
# CURATED SUBSET -- only the musically-useful fields: moods, danceability, voice/instrumental, key. We
# deliberately DROP the noise the plugin also wrote (genre classifiers -- unreliable + owned by
# MusicBrainz/lastgenre; gender; timbre; ballroom rhythm; chord stats; average_loudness -- redundant with
# ReplayGain). A leaf "value" takes the classifier's label; an "all" sub-map takes the positive-class
# PROBABILITY (e.g. mood_happy=0.05); a (attr, idx) tuple composes one field (initial_key = key + scale).
ABSCHEME = {
    "highlevel": {
        "danceability": {"all": {"danceable": "danceable"}},
        "mood_acoustic": {"all": {"acoustic": "mood_acoustic"}},
        "mood_aggressive": {"all": {"aggressive": "mood_aggressive"}},
        "mood_electronic": {"all": {"electronic": "mood_electronic"}},
        "mood_happy": {"all": {"happy": "mood_happy"}},
        "mood_party": {"all": {"party": "mood_party"}},
        "mood_relaxed": {"all": {"relaxed": "mood_relaxed"}},
        "mood_sad": {"all": {"sad": "mood_sad"}},
        "moods_mirex": {"value": "moods_mirex"},
        "tonal_atonal": {"all": {"tonal": "tonal"}},
        "voice_instrumental": {"value": "voice_instrumental"},
    },
    "rhythm": {"bpm": "bpm"},
    "tonal": {
        "key_key": ("initial_key", 0),
        "key_scale": ("initial_key", 1),
        "key_strength": "key_strength",
    },
}


def _walk(data, scheme, out, composites):
    """Recursively pair leaf nodes of `scheme` with `data` (port of beets' _data_to_scheme_child)."""
    for k, v in scheme.items():
        if k not in data:
            continue
        if isinstance(v, dict):
            _walk(data[k], v, out, composites)
        elif isinstance(v, tuple):
            attr, idx = v
            parts = composites[attr]
            while len(parts) <= idx:
                parts.append("")
            parts[idx] = str(data[k])
        else:
            out[v] = data[k]


def _fields_for(doc: dict) -> dict:
    """Map one recording's merged low+high-level AB document to {beets_field: value}."""
    out: dict = {}
    composites: dict = defaultdict(list)
    _walk(doc, ABSCHEME, out, composites)
    for attr, parts in composites.items():
        if attr == "initial_key" and len(parts) == 2:
            # beets' MusicalKey type wants canonical "C", "Cm", "C#", "C#m" -- NOT "F# major": its parser
            # regex `[\W\s]+major` greedily eats the '#' and mangles "F# major" -> "F" (the deprecated
            # beets plugin hits this too). Emit the canonical form so the sharp + mode survive.
            root, scale = parts
            out[attr] = root + ("m" if scale.lower().startswith("min") else "")
        else:
            out[attr] = " ".join(parts).strip()
    return out


def _fetch(mbids: list[str]):
    """{mbid: merged_doc} for the mbids AB knows (others omitted). None ONLY on a TRANSIENT failure
    (timeout / network / 5xx / 429) so the caller retries next run; a 4xx (malformed/absent id) returns the
    partial result instead, so those ids get cached `None` rather than poisoning the batch on every run."""
    merged: dict = {}
    ids = ";".join(urllib.parse.quote(m, safe="") for m in mbids)   # encode each id; ';' stays the AB separator
    for level in ("low-level", "high-level"):
        req = urllib.request.Request(f"{API}/{level}?recording_ids={ids}", headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                continue                       # 4xx = a malformed/absent id in the batch -> no data this level,
            return None                        # but NOT transient: skip so absent ids cache `None`, not retry forever
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            return None                        # timeout / network / 5xx / 429 -> transient, retry next run
        for mbid, subs in data.items():
            doc = subs.get("0") if isinstance(subs, dict) else None
            if doc:
                merged.setdefault(mbid, {}).update(doc)
    return merged


def _value(field: str, value):
    """Native value for one field (bpm -> rounded int media field; the rest stay float/str as fetched).
    A non-numeric bpm (malformed AB doc) falls back to the raw value -- it must never abort the whole batch."""
    if field == "bpm":
        try:
            return round(float(value))
        except (TypeError, ValueError):
            return value
    return value


def _beets_python(beet: str) -> str:
    """Path to the BEETS venv's python (it can `import beets`; gbc's own venv cannot). Read the shebang of
    the `beet` entry point, then try a sibling python3, then fall back to a bare 'python3'."""
    try:
        path = beet if Path(beet).exists() else (shutil.which(beet) or beet)
        real = Path(path).resolve()
        shebang = real.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        if shebang.startswith("#!"):
            py = shebang[2:].strip().split()[0]
            if Path(py).is_file():
                return py
        sibling = real.parent / "python3"
        if sibling.is_file():
            return str(sibling)
    except (OSError, IndexError):
        pass
    return "python3"


def _bulk_apply(cfg: Config, modified: dict, log) -> None:
    """Apply every {mbid: fields} to the library in ONE beets process via _ab_bulk.py -- replaces one
    `beet modify` per recording (~N `beet` startups, the pass's real cost). Best-effort: a failure is
    logged, never raised (the enrichment is already cached and retried next run)."""
    payload = {m: {k: _value(k, v) for k, v in f.items()} for m, f in modified.items()}
    cfg.beetsdir.mkdir(parents=True, exist_ok=True)
    # per-run temp payload (NOT a fixed filename): two concurrent runs must not clobber each other's JSON
    fd, tmp = tempfile.mkstemp(dir=cfg.beetsdir, prefix="gbc-ab-modify-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        out = subprocess.run(
            [_beets_python(cfg.beet), str(Path(__file__).with_name("_ab_bulk.py")), str(cfg.library), tmp],
            capture_output=True, text=True)
        if out.returncode:
            log.error("acousticbrainz: bulk modify failed (rc=%d): %s", out.returncode, out.stderr.strip()[:300])
        else:
            log.info("acousticbrainz: %s item(s) written in one pass", out.stdout.strip() or "?")
    finally:
        Path(tmp).unlink(missing_ok=True)


def _write_file_tags(path: str, flex_attrs: dict, log) -> bool:
    """Inject flex attrs as custom tags into one audio file via mutagen.

    ID3  -> TXXX frames (the official ID3v2 extension mechanism for user-defined text)
    Vorbis -> key=value comments (arbitrary keys allowed by spec)
    MP4  -> ----:com.apple.itunes:<key> freeform atoms

    Best-effort: any failure is logged and swallowed (never blocks the pipeline)."""
    ext = path.rsplit(".", 1)[-1].lower()
    audio: typing.Any = None        # holds a different mutagen type per format branch (FLAC/OggOpus/ID3/MP4)
    try:
        if ext in ("flac", "ogg", "opus"):
            if ext == "flac":
                from mutagen.flac import FLAC
                audio = FLAC(path)
            elif ext == "opus":
                from mutagen.oggopus import OggOpus  # Opus != Vorbis: OggVorbis rejects an OpusHead stream
                audio = OggOpus(path)
            else:
                from mutagen.oggvorbis import OggVorbis
                audio = OggVorbis(path)
            for k, v in flex_attrs.items():
                audio[k] = str(v)
            audio.save()
        elif ext == "mp3":
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()
            for k, v in flex_attrs.items():
                desc = k
                audio.delall(f"TXXX:{desc}")
                audio.add(TXXX(encoding=3, desc=desc, text=str(v)))
            audio.save(path)
        elif ext in ("m4a", "aac", "mp4"):
            from mutagen.mp4 import MP4
            audio = MP4(path)
            for k, v in flex_attrs.items():
                audio[f"----:com.apple.itunes:{k}"] = [str(v).encode("utf-8")]
            audio.save()
        else:
            log.debug("acousticbrainz: unsupported format for tag injection: %s", path)
            return False
        return True
    except Exception as exc:
        log.warning("acousticbrainz: tag injection failed %s: %s", path, exc)
        return False


def run(cfg: Config, scope: str = "") -> int:
    """Enrich tracks added in `scope` (whole library if empty) with AcousticBrainz data. Returns the
    number of recordings enriched."""
    log = get_logger("acousticbrainz")
    sc = [scope] if scope else []
    _, text = run_beet(cfg, ["ls", "-f", "$mb_trackid", "mb_trackid::.", *sc],
                       passname="acousticbrainz", echo_lines=False)
    mbids = sorted({ln.strip() for ln in text.splitlines() if ln.strip()})
    if not mbids:
        log.info("=== acousticbrainz: no MB-matched tracks in scope ===")
        return 0

    cpath = cfg.beetsdir / "gbc-acousticbrainz-cache.json"
    try:
        cache = json.loads(cpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    todo = [m for m in mbids if m not in cache]
    pending = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        docs = _fetch(batch)
        if docs is None:                       # network hiccup -> leave uncached, retry next run
            pending += len(batch)
            continue
        for m in batch:
            doc = docs.get(m)
            cache[m] = _fields_for(doc) if doc else None   # None = confirmed absent (never re-queried)
        cfg.beetsdir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps(cache), encoding="utf-8")

    # NB: cached recordings ARE re-applied every run (not just freshly-fetched ones) -- this is intentional,
    # so a newly-added item that shares a recording id with an already-cached one still gets enriched. The
    # incremental watermark keeps `*sc` narrow on normal runs; `--all` deliberately re-applies the whole lib.
    enriched = absent = 0
    modified = {}
    for m in mbids:
        fields = cache.get(m)
        if not fields:                         # None (absent) or still-pending this run
            absent += m in cache
            continue
        modified[m] = fields
        enriched += 1
    if modified:                               # ONE beets process for the whole batch (not `beet modify` per id)
        _bulk_apply(cfg, modified, log)

    # Write flex attrs to file tags via mutagen (beets' mediafile only writes native fields).
    # Uses the official custom-tag mechanism of each format: TXXX (ID3), Vorbis comments, MP4 freeform.
    if modified and importlib.util.find_spec("mutagen") is not None:
        # Reuse the SAME scoped query (not a `mb_trackid:<id>,...` OR of every modified id -- thousands of
        # them on a full run exceed MAX_ARG_STRLEN (128 KB) for a single argv entry -> execve E2BIG), then
        # filter the rows to the recordings we just enriched.
        _, paths_text = run_beet(
            cfg, ["ls", "-f", "$mb_trackid\t$path", "mb_trackid::.", *sc],
            passname="acousticbrainz", echo_lines=False)
        tagged = 0
        for line in paths_text.splitlines():
            if "\t" not in line:
                continue
            mbid, path = line.split("\t", 1)
            if mbid not in modified:
                continue
            path = path.strip().encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
            flex = {k: v for k, v in modified[mbid].items() if k in FLEX_ATTRS}
            if flex and Path(path).is_file() and _write_file_tags(path, flex, log):
                tagged += 1
        log.info("acousticbrainz: %d file(s) tagged with flex attrs", tagged)
    elif modified and importlib.util.find_spec("mutagen") is None:
        log.warning("acousticbrainz: mutagen not installed -> flex attrs stay db-only (invisible to players)")

    log.info("=== acousticbrainz: %d recording(s) enriched, %d not in AB, %d pending (retry next run) ===",
             enriched, absent, pending)
    return enriched
