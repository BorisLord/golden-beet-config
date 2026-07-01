"""Pass -- network-only enrich via the AcousticBrainz read API (BPM, key, moods, danceability...).

AB is frozen but its read API still serves every recording it analysed, keyed by mb_trackid. We hit it
ourselves (not beets' deprecated `acousticbrainz` plugin). Best-effort: never gates the pipeline or moves a file.
"""
import importlib.util
import json
import re
import typing
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..util import write_json

API = "https://acousticbrainz.org/api/v1"
BATCH = 25          # AB caps recording_ids at 25 per request
TIMEOUT = 25
# AB is keyed by the MusicBrainz recording UUID. A non-UUID id (e.g. a Discogs '14266022-1') makes AB 400 the
# WHOLE batch ("not a valid UUID"), so every co-batched UUID would cache None -> drop these before batching.
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_UA = "gbc/0.8 (golden-beets-config)"   # default Python-urllib UA can be 403'd/throttled by the public API

# No mediafile tag-frame mapping -> db-only flex attrs, injected into files as custom tags (TXXX / Vorbis /
# MP4 freeform) so Navidrome reads them. bpm/initial_key are native media fields mediafile writes itself.
FLEX_ATTRS = frozenset({
    "danceable", "key_strength", "tonal",
    "mood_acoustic", "mood_aggressive", "mood_electronic", "mood_happy",
    "mood_party", "mood_relaxed", "mood_sad",
    "moods_mirex", "voice_instrumental",
})

# AB nested JSON -> beets fields. Names are beets' canonical ones (ecosystem queries still apply) but a
# CURATED SUBSET (genre/gender/timbre/rhythm/chord/average_loudness dropped -- see AGENTS.md). Leaf "value" =
# classifier label; "all" = positive-class probability; (attr, idx) tuple composes one field.
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
    """Pair leaf nodes of `scheme` with `data` (port of beets' _data_to_scheme_child)."""
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
    """One recording's merged low+high-level AB document -> {beets_field: value}."""
    out: dict = {}
    composites: dict = defaultdict(list)
    _walk(doc, ABSCHEME, out, composites)
    for attr, parts in composites.items():
        if attr == "initial_key":
            # beets' MusicalKey type wants canonical "C"/"Cm"/"C#"/"C#m", NOT "F# major" (its regex
            # `[\W\s]+major` eats the '#' -> "F"). Emit canonical form so the sharp + mode survive.
            root = parts[0] if parts else ""
            if not root:                                   # a scale alone (key_scale, no key_key) is not a key -> drop
                continue
            scale = parts[1] if len(parts) > 1 else ""
            out[attr] = root + ("m" if scale.lower().startswith("min") else "")
        else:
            out[attr] = " ".join(parts).strip()
    return out


def _fetch(mbids: list[str]):
    """{mbid: merged_doc} for the mbids AB knows (others omitted). None ONLY on a transient failure so the
    caller retries; a 4xx (malformed/absent id) returns the partial result so those ids cache `None`."""
    merged: dict = {}
    ids = ";".join(urllib.parse.quote(m, safe="") for m in mbids)   # ';' stays the AB separator
    for level in ("low-level", "high-level"):
        req = urllib.request.Request(f"{API}/{level}?recording_ids={ids}", headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                continue                       # 4xx = malformed/absent id, not transient: skip so it caches None
            return None
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            return None                        # timeout / network / 5xx / 429 -> transient, retry next run
        for mbid, subs in data.items():
            doc = subs.get("0") if isinstance(subs, dict) else None
            if doc:
                merged.setdefault(mbid, {}).update(doc)
    return merged


def _value(field: str, value):
    """bpm -> rounded int (media field); rest stay as fetched. A non-numeric bpm -> None (dropped by the
    payload builder) so a bad value never lands in beets' integer bpm field and aborts a later store()."""
    if field == "bpm":
        try:
            return round(float(value))
        except (TypeError, ValueError):
            return None
    return value


def _apply(cfg: Config, modified: dict, log) -> tuple[int, int]:
    """Apply each recording natively via `beet modify -M mb_trackid:<uuid> field=value ...` (no homemade
    try_write). One modify per recording -- a recording can sit on several albums. Returns (applied, failed)."""
    applied = failed = 0
    for mbid, fields in modified.items():
        assigns = [f"{k}={v}" for k, v in ((k, _value(k, v)) for k, v in fields.items()) if v is not None]
        if not assigns:
            continue
        rc, _ = run_beet(cfg, ["modify", "-y", "-M", f"mb_trackid:{mbid}", *assigns],
                         passname="acousticbrainz", echo_lines=False)
        if rc:
            log.warning("acousticbrainz: `beet modify` rc=%d for mb_trackid:%s", rc, mbid)
            failed += 1
        else:
            applied += 1
    return applied, failed


def _write_file_tags(path: str, flex_attrs: dict, log) -> bool:
    """Inject flex attrs as custom tags via mutagen: TXXX (ID3), Vorbis comments, MP4 freeform atoms.
    Best-effort: failure logged and swallowed (never blocks the pipeline)."""
    ext = Path(path).suffix.lstrip(".").lower()    # from the basename, not the whole path (dotted dirs)
    audio: typing.Any = None
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
    """Enrich tracks in `scope` (whole library if empty). Returns the number of recordings enriched."""
    log = get_logger("acousticbrainz")
    sc = [scope] if scope else []
    # Capture mbid->paths UP FRONT: a re-query after applying would miss files when scope filters on bpm
    # (writing bpm empties such a scope -> 0 files tagged). One row per (recording, album) -> several paths.
    _, text = run_beet(cfg, ["ls", "-f", "$mb_trackid\t$path", "mb_trackid::.", *sc],
                       passname="acousticbrainz", echo_lines=False, check=True)
    paths_by_mbid: dict = {}
    for ln in text.splitlines():
        mb, _, path = ln.partition("\t")
        mb = mb.strip()
        if mb and path and _UUID_RE.match(mb):
            paths_by_mbid.setdefault(mb, []).append(path.strip())
    mbids = sorted(paths_by_mbid)
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
            try:
                cache[m] = _fields_for(doc) if doc else None   # None = confirmed absent, never re-queried
            except Exception as e:                             # a single malformed AB doc must not abort the batch
                log.warning("acousticbrainz: parse failed for %s (%s) -> treated as absent", m, e)
                cache[m] = None
        write_json(cpath, cache)                           # atomic (tmp + replace): a crash can't corrupt the cache

    # Cached recordings are re-applied every run (not just freshly-fetched): a newly-added item sharing a
    # recording id with a cached one still gets enriched. Watermark keeps `*sc` narrow; `--all` re-applies all.
    enriched = absent = 0
    modified = {}
    for m in mbids:
        fields = cache.get(m)
        if not fields:                         # None (absent) or still-pending this run
            absent += m in cache
            continue
        modified[m] = fields
        enriched += 1
    if modified:
        applied, failed = _apply(cfg, modified, log)
        log.info("acousticbrainz: %d recording(s) applied via beet modify (%d failed)", applied, failed)

        # `beet write` reconciles bpm/initial_key to every enriched FILE (near no-op unless a modify-write
        # failed). Scope to the just-modified recordings, else a standalone/--all run rewrites the WHOLE library.
        if sc:
            wargs = ["write", *sc]
        else:
            wargs = ["write"]
            for m in modified:                 # mb_trackid is a UUID -> a bare `mb_trackid:` query can't collide
                wargs += ([","] if len(wargs) > 1 else []) + [f"mb_trackid:{m}"]
        run_beet(cfg, wargs, passname="acousticbrainz", echo_lines=False)

        # moods/flex -> file tags via mutagen: beets has no native command to write flex attrs to files.
        if importlib.util.find_spec("mutagen") is not None:
            tagged = 0
            for mbid in modified:
                flex = {k: v for k, v in modified[mbid].items() if k in FLEX_ATTRS}
                if not flex:
                    continue
                for path in paths_by_mbid.get(mbid, []):   # already surrogateescape-decoded by run_beet
                    if Path(path).is_file() and _write_file_tags(path, flex, log):
                        tagged += 1
            log.info("acousticbrainz: %d file(s) tagged with flex attrs", tagged)
        else:
            log.warning("acousticbrainz: mutagen not installed -> flex attrs stay db-only (invisible to players)")

    log.info("=== acousticbrainz: %d recording(s) enriched, %d not in AB, %d pending (retry next run) ===",
             enriched, absent, pending)
    return enriched
