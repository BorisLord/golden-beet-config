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
written into the file tags (a Subsonic/Navidrome player sees them); the moods/danceability classifiers
are non-standard -> stored as beets flexible attributes (db + sidecars; typed via the `types` plugin so
`mood_relaxed:0.9..` ranges work), queryable but not shown by players.

Frozen source => verdicts are cached forever per recording id (BEETSDIR/gbc-acousticbrainz-cache.json):
a recording present in AB is fetched once; one confirmed absent (404 / omitted) is never re-queried; a
network hiccup is left uncached -> retried next run. Best-effort: never gates the pipeline, never moves
or deletes a file.
"""
import json
import urllib.error
import urllib.request
from collections import defaultdict

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger

API = "https://acousticbrainz.org/api/v1"
BATCH = 25          # AB caps recording_ids at 25 per request
TIMEOUT = 25

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
    """{mbid: merged_doc} for the mbids AB knows (others omitted); None on any network/parse failure
    (-> caller leaves them uncached and retries next run)."""
    merged: dict = {}
    for level in ("low-level", "high-level"):
        url = f"{API}/{level}?recording_ids=" + ";".join(mbids)
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                data = json.load(r)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            return None
        for mbid, subs in data.items():
            doc = subs.get("0") if isinstance(subs, dict) else None
            if doc:
                merged.setdefault(mbid, {}).update(doc)
    return merged


def _assign(field: str, value) -> str:
    """`field=value` token for `beet modify` (bpm -> int media field; probabilities -> 6dp like the plugin)."""
    if field == "bpm":
        return f"bpm={round(float(value))}"
    if isinstance(value, float):
        return f"{field}={value:.6f}"
    return f"{field}={value}"


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

    enriched = absent = 0
    for m in mbids:
        fields = cache.get(m)
        if not fields:                         # None (absent) or still-pending this run
            absent += m in cache
            continue
        assigns = [_assign(k, v) for k, v in sorted(fields.items())]
        run_beet(cfg, ["modify", "-y", f"mb_trackid:{m}", *sc, *assigns],
                 passname="acousticbrainz", echo_lines=False)
        enriched += 1

    log.info("=== acousticbrainz: %d recording(s) enriched, %d not in AB, %d pending (retry next run) ===",
             enriched, absent, pending)
    return enriched
