"""Pass -- normalise non-standard formats in the CLEAN library (pipeline + standalone `gbc convert`):
WMA -> Opus (only lossy re-encode; WMA is proprietary/broken), WAV/AIFF/ALAC -> FLAC (bit-perfect), and
oversized hi-res FLAC (>48 kHz and/or >16-bit, e.g. a 24/192 box) -> 16-bit/<=48 kHz FLAC (CD-transparent,
~6-8x smaller, fast tags). Each original is MOVED to quarantine (keep_new) -- NEVER deleted. Only files
already in the clean lib are touched.

Caveat (move-mode-safe): for a CONSUMED source the hi-res original is gone after import, so nothing re-inflates
the downsample. For a PRESERVED source the 24/192 copy survives and `upgrade` (quality.py ranks by normalised
bitrate) would treat it as "better" and re-upgrade clean back to hi-res each run -- only relevant if you switch
beets to a copy/reflink/link import.
"""
from pathlib import Path

from ..beets import run_beet
from ..config import Config
from ..logs import get_logger
from ..util import backup_db, count_items, skip_on_error

# (label, target desc, beet -f format, query, quarantine subdir). WMA stored as "Windows Media"
# (-> format::Windows); WAV/AIFF matched by path (avoids format-name surprises); ALAC by format (distinct
# from AAC, which shares .m4a). Quarantine sub "converted" = the reason; beets lays originals out by album.
JOBS = [
    ("WMA", "Opus (open, adaptive bitrate)", "opus", ["format::Windows"], "converted"),
    ("WAV/AIFF", "FLAC (lossless)", "flac", [r"path::(?i)\.(wav|aiff?)$"], "converted"),
    ("ALAC", "FLAC (lossless, universal)", "flac", ["format:ALAC"], "converted"),
    # downsample LAST: a WAV/ALAC just turned into hi-res FLAC above is caught here too. Two DISJOINT jobs
    # express ">48 kHz OR >16-bit" without a fragile comma-OR: job A = any rate >48 kHz; job B = 24-bit but
    # <=48 kHz (the `samplerate:..48000` guard keeps 24/192 out of B -- A already has it). Disjoint -> no file
    # is encoded or counted twice. Originals are GOOD masters -> own "downsampled" quarantine (never mixed with
    # the broken/junk in "converted").
    ("hi-res FLAC (>48kHz)", "16-bit/<=48kHz FLAC", "flac16", ["format:FLAC", "samplerate:48001.."], "downsampled"),
    ("24-bit FLAC", "16-bit FLAC", "flac16", ["format:FLAC", "bitdepth:17..", "samplerate:..48000"], "downsampled"),
]


def _reap_stale(cfg: Config, query: list, log) -> tuple[int, int]:
    """An item still matching the SOURCE-format query whose file VANISHED = failed encode: keep_new moved the
    original to quarantine, then the encode errored (beet convert still exits 0), leaving a row at a gone path.
    Drop those rows; original is safe in quarantine. Returns (reaped, still_present)."""
    _, text = run_beet(cfg, ["ls", "-f", "$id\t$path", *query], passname="convert", echo_lines=False)
    reaped = present = 0
    for line in text.splitlines():
        if "\t" not in line:
            continue
        with skip_on_error(log, "convert", line[:80]):
            itemid, path = line.split("\t", 1)
            path = path.strip()                    # run_beet already decoded with surrogateescape; no re-round-trip
            if path and not Path(path).exists():
                rc, _ = run_beet(cfg, ["remove", "-f", f"id:{itemid}"], passname="convert", echo_lines=False)
                if rc:
                    log.warning("convert: `beet remove` rc=%d for stale id:%s", rc, itemid)
                reaped += 1
            else:
                present += 1
    return reaped, present


def run(cfg: Config) -> int:
    log = get_logger("convert")
    pending = [(lbl, tgt, fmt, q, sub, n)
               for (lbl, tgt, fmt, q, sub) in JOBS
               if (n := count_items(cfg, ["ls", *q], "convert"))]
    if not pending:
        log.info("no WMA/WAV/AIFF/ALAC in the library -> nothing to convert")
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
        reaped, present = _reap_stale(cfg, q, log)
        log.info("done: %d %s converted, %d failed (stale row reaped, original safe in quarantine), %d still "
                 "present; originals in %s", n - reaped - present, lbl, reaped, present, dest)
    return 0
