"""Pass 4 -- technical QA: READ-ONLY audit -> conditional ACTIONS summary. `scope` (a beets query) narrows
to recent additions; empty = whole library."""
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from .. import anomaly
from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..sidecars import quarantine_dir, safe_move
from ..util import backup_db, prune_empty_dirs

JUNK = re.compile(r"https?://|www\.|\.(com|net|org|tk|br)|\bEAC\b|\bLame\b|\bLAMEB?\s*\d|CDex|Easy CD-DA|Tagged By"
                  r"|Encoded by|ripped by|Created with|meXPiracy|Autodesk|bandcamp|No Comment", re.I)


def _lines(cfg: Config, args):
    _, text = run_beet(cfg, args, overlay="qa.yaml", passname="qa", echo_lines=False)
    return [ln for ln in text.splitlines() if ln.strip()]


def _leadnum(s):
    m = re.match(r"\s*(\d+)", s)
    return int(m.group(1)) if m else 0


def _bucket(b):
    if b < 128:
        return "1. <128 low"
    if b < 192:
        return "2. 128-191 ok"
    if b < 256:
        return "3. 192-255 good"
    if b < 321:
        return "4. 256-320 very good"
    return "5. 320+/lossless"


def _container_mismatch(path):
    """Reason if magic bytes contradict the audio extension (RIFF/WAVE in a .mp3), else ''. Such files read in
    mediafile but carry EMPTY tags in TagLib -> break Navidrome/Jellyfin; mp3val only warns + exits 0."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return ""
    if not head:
        return "empty file"
    ext = Path(path).suffix.lower()
    ogg = head[:4] == b"OggS"
    sig = {  # ext -> True when leading bytes match the claimed extension
        ".mp3": head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0),
        ".flac": head[:4] == b"fLaC" or head[:3] == b"ID3",   # some rippers prepend an ID3v2 tag to valid FLAC
        ".ogg": ogg, ".oga": ogg, ".opus": ogg,
        ".m4a": head[4:8] == b"ftyp", ".m4b": head[4:8] == b"ftyp",
    }
    if ext in sig and not sig[ext]:
        return f"RIFF/WAVE data in a {ext}" if head[:4] == b"RIFF" else f"not a {ext.lstrip('.')} stream"
    return ""


def _cull(cfg: Config, paths, log) -> int:
    """Move corrupt clean files to quarantine/corrupt/ (never deleted) and drop the lib entry. Identity from
    the clean path (already sanitised)."""
    backup_db(cfg, "qa-cull", log)
    moved = 0
    for p in dict.fromkeys(paths):                 # dedupe, keep order
        fp = Path(p)
        if not fp.exists():
            continue
        qd = quarantine_dir(cfg.dump, "corrupt", fp.parent.parent.name, fp.parent.name, fallback=fp.parent.name)
        dest = qd / fp.name
        i = 1
        while dest.exists():
            i += 1
            dest = qd / f"{fp.stem} ({i}){fp.suffix}"
        qd.mkdir(parents=True, exist_ok=True)
        if safe_move(p, dest, log):
            rc, _ = run_beet(cfg, ["remove", "-f", f"path:{p}"], passname="qa", echo_lines=False)
            if rc:
                log.warning("qa: `beet remove` rc=%d for %s -- stale lib entry may remain", rc, p)
            moved += 1
            log.info("CULL corrupt: %s -> %s/", fp.name, qd)
    if moved:
        prune_empty_dirs(cfg.clean)                # remove album shells left empty when all tracks were culled
    log.info("  [CORRUPT] %d corrupt file(s) culled to %s/corrupt -- recoverable, never deleted", moved, cfg.dump)
    return moved


def run(cfg: Config, scope: str = "", cull: bool = False) -> int:
    log = get_logger("qa")
    sc = [scope] if scope else []

    # 1. format breakdown
    fc = Counter(f.strip() for f in _lines(cfg, ["ls", "-f", "$format", *sc]))
    log.info("=== 1. format ===")
    for fmt, n in fc.most_common():
        log.info("  %5d %s", n, fmt)

    # 2. bitrate ($bitrate is "Nkbps" -> leading number)
    buckets = Counter(_bucket(_leadnum(b)) for b in _lines(cfg, ["ls", "-f", "$bitrate", *sc]))
    log.info("=== 2. bitrate ===")
    for label in sorted(buckets):
        log.info("  %-22s %d", label, buckets[label])

    # 3. WMA stored as 'Windows Media' -> query format::Windows, NOT WMA
    wma = len(_lines(cfg, ["ls", "format::Windows", *sc]))
    log.info("=== 3. WMA: %d track(s) ===", wma)
    for x in sorted(set(_lines(cfg, ["ls", "-f", "$albumartist - $album", "format::Windows", *sc])))[:15]:
        log.info("  %s", x)

    # 4. low-quality lossy (<192k), excluding lossless
    low = [x for x in _lines(cfg, ["ls", "-f", "$bitrate $format | $artist - $title", "bitrate:..191999", *sc])
           if not re.search(r"FLAC|ALAC|WAV", x, re.I)]
    log.info("=== 4. low-quality lossy <192k: %d ===", len(low))
    for x in sorted(low, key=_leadnum)[:30]:
        log.info("  %s", x)

    # 5. duplicates (keys mb_trackid + mb_albumid, from config.yaml)
    dups = _lines(cfg, ["duplicates", "-F", "-f", "$bitrate $format | $albumartist - $album - $title", *sc])
    log.info("=== 5. duplicates: %d ===", len(dups))
    for x in dups[:40]:
        log.info("  %s", x)

    # 6. integrity: beet bad + ffmpeg decode. Match the per-FILE marker badfiles prints ("checker exited with
    #    status N" / "file does not exist") -- flac --test and mp3val emit DIFFERENT error text, so "ERROR:"
    #    alone missed flac.
    bad = _lines(cfg, ["bad", *sc])
    fail = [x for x in bad if "checker exited with status" in x or "file does not exist" in x]
    baderr = len(fail)
    log.info("=== 6. integrity (beet bad): %d bad file(s) ===", baderr)
    corrupt = []                                # clean paths to (optionally) cull -> quarantine/corrupt/
    for x in fail:
        log.info("  %s", x)
        for marker in (": checker exited with status", ": file does not exist"):
            if marker in x:
                corrupt.append(x.split(marker, 1)[0])
                break
    paths = _lines(cfg, ["ls", "-p", *sc])
    other_bad = 0
    if shutil.which("ffmpeg"):
        for p in paths:
            if Path(p).suffix.lower() in (".mp3", ".flac"):
                continue
            # Gate on the RETURN CODE, not stderr: valid-but-exotic files (ape/wv/dsf) emit harmless error-level
            # noise. -xerror aborts + exits nonzero on the first decode error (catches mid-file corruption too).
            res = subprocess.run(["ffmpeg", "-nostdin", "-xerror", "-v", "error", "-i", p, "-f", "null", "-"],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                other_bad += 1
                corrupt.append(p)
                log.info("  BAD (rc=%d): %s", res.returncode, p)
    log.info("=== 6b. other-format decode: %d bad ===", other_bad)

    # 6c. container vs extension mismatch (magic bytes) -- 6/6b miss it (see _container_mismatch)
    mism = [(p, why) for p in paths if (why := _container_mismatch(p))]
    log.info("=== 6c. container/extension mismatch: %d ===", len(mism))
    for p, why in mism:
        log.info("  MISMATCH (%s): %s", why, p)
        corrupt.append(p)
    culled = _cull(cfg, corrupt, log) if cull else 0

    # 7. junk metadata (comments + encoder noise)
    cmt = sum(1 for x in _lines(cfg, ["ls", "-f", "[$comments] | $artist - $title", "comments::.", *sc])
              if JUNK.search(x))
    enc = len(_lines(cfg, ["ls", "encoder::.", *sc]))
    log.info("=== 7. junk metadata: %d junk comment(s), %d encoder noise ===", cmt, enc)

    # 8. name/title anomalies
    fields = ("$id@@@$albumartist@@@$artist@@@$album@@@$title@@@$length@@@$bitrate"
              "@@@$singleton@@@$comp@@@$albumtype@@@$mb_trackid")
    _, tsv_text = run_beet(cfg, ["ls", "-f", fields, *sc], overlay="qa.yaml", passname="qa", echo_lines=False)
    workdir = cfg.log_dir / "anomalies"
    workdir.mkdir(parents=True, exist_ok=True)
    tsv = workdir / "all-items.tsv"
    tsv.write_text(tsv_text + "\n", encoding="utf-8")
    counts = anomaly.scan(str(tsv), str(workdir), log)
    anom = sum(counts.values())

    log.info("=== ACTIONS (only what was found; BACK UP library.db before any zero/dedup) ===")
    actions = []
    if low:
        actions.append(f"[quality] {len(low)} lossy <192k -> consider re-downloading (section 4)")
    if enc:
        actions.append(f"[tags]    {enc} encoder noise -> beet -c qa.yaml zero 'encoder::.'")
    if cmt:
        actions.append(f"[tags]    {cmt} junk comment(s) -> beet -c qa.yaml zero 'comments::.'")
    if dups:
        actions.append(f"[dups]    {len(dups)} duplicate track(s) -> review section 5, then: beet duplicates -m DUMP")
    if wma:
        actions.append(f"[format]  {wma} WMA (legacy/proprietary, breaks scrub+players) -> `gbc convert`")
    if culled:
        actions.append(f"[CORRUPT] {culled} corrupt/mismatched file(s) culled to {cfg.dump}/corrupt -- re-rip")
    else:
        if baderr + other_bad:
            actions.append(f"[CORRUPT] {baderr + other_bad} file(s) failing integrity -> move to {cfg.dump} and re-rip")
        if mism:
            actions.append(f"[FORMAT]  {len(mism)} container!=extension (RIFF in .mp3 etc.) -> remux via ffmpeg")
    if anom:
        actions.append(f"[names]   {anom} name/title anomalies -> {workdir}/*.tsv (review)")
    for a in actions:
        log.info("  %s", a)
    if not actions:
        log.info("  -> nothing flagged: tags/quality/dups/integrity/names all clean.")
    return 0


def run_anomaly(cfg: Config, scope: str = "") -> int:
    """Read-only name/anomaly scan (section 8), standalone."""
    log = get_logger("anomaly")
    sc = [scope] if scope else []
    fields = ("$id@@@$albumartist@@@$artist@@@$album@@@$title@@@$length@@@$bitrate"
              "@@@$singleton@@@$comp@@@$albumtype@@@$mb_trackid")
    _, tsv_text = run_beet(cfg, ["ls", "-f", fields, *sc], overlay="qa.yaml", passname="anomaly", echo_lines=False)
    workdir = cfg.log_dir / "anomalies"
    workdir.mkdir(parents=True, exist_ok=True)
    tsv = workdir / "all-items.tsv"
    tsv.write_text(tsv_text + "\n", encoding="utf-8")
    anomaly.scan(str(tsv), str(workdir), log)
    return 0
