"""Optional pass -- normalise problematic formats in the CLEAN library (on-demand, not in run/inbox):

  WMA       -> AAC/m4a (~256k)   lossy, legacy/proprietary ASF: poor players, weak tags, scrub crash
  WAV+AIFF  -> FLAC (lossless)   uncompressed PCM: ~no tags, huge -> FLAC keeps quality, adds tags, halves size

Rule of thumb applied: lossless source -> FLAC (zero loss); lossy WMA -> AAC (one generational loss for
compat). Each original is MOVED to a quarantine subdir (keep_new) -- NEVER deleted. The NAS source is
read-only and untouched; only files that actually made it into the clean lib are converted.
"""
from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..util import backup_db, count_items

# (label, target desc, beet -f format, query, quarantine subdir). WMA is stored as "Windows Media"
# (-> format::Windows); WAV/AIFF matched by path (content-agnostic, avoids format-name surprises).
# quarantine sub = the REASON ("converted"); beets lays the originals out by album under it
# (-> quarantine/converted/<Albumartist>/<Album (Year)>/...), consistent with the other quarantine reasons.
JOBS = [
    ("WMA", "Opus (open, adaptive bitrate)", "opus", ["format::Windows"], "converted"),
    ("WAV/AIFF", "FLAC (lossless)", "flac", [r"path::(?i)\.(wav|aiff?)$"], "converted"),
]


def run(cfg: Config) -> int:
    log = get_logger("convert")
    pending = [(lbl, tgt, fmt, q, sub, n)
               for (lbl, tgt, fmt, q, sub) in JOBS
               if (n := count_items(cfg, ["ls", *q], "convert"))]
    if not pending:
        log.info("no WMA/WAV/AIFF in the library -> nothing to convert")
        return 0
    backup_db(cfg, "convert", log)
    for lbl, tgt, fmt, q, sub, n in pending:
        dest = cfg.dump / sub
        dest.mkdir(parents=True, exist_ok=True)
        log.info("converting %d %s -> %s; originals -> %s", n, lbl, tgt, dest)
        rc, _ = run_beet(cfg, ["convert", "-y", "-k", "-f", fmt, "-d", str(dest), *q],
                         overlay="convert.yaml", passname="convert")
        if rc:
            log.error("beet convert (%s) failed (rc=%d) -- originals untouched", lbl, rc)
            return rc
        left = count_items(cfg, ["ls", *q], "convert")
        log.info("done: %d %s converted, %d remain; originals quarantined in %s", n - left, lbl, left, dest)
    return 0
