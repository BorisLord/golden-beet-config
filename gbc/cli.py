"""gbc CLI -- one tool, several doors. beets does art/genres/replaygain/scrub natively during import.

  gbc run [--all] [--reimport]  full pipeline (import -> qa); --reimport re-tries already-seen folders
  gbc inbox              cron door: import a drop if anything is new, then the pipeline
  gbc import [SOURCE] [--reimport]  album-match import only (--reimport re-tries already-seen folders)
  gbc qa [QUERY]         read-only technical audit + anomaly scan
  gbc anomaly [QUERY]    read-only name/anomaly scan only
  gbc verify [QUERY]     quarantine imposter tracks (audio != tagged recording) via AcoustID
  gbc acousticbrainz [QUERY]  fetch BPM/key/mood metadata from AcousticBrainz (network-only)
  gbc convert            normalise formats in the clean lib (WMA->AAC, WAV/AIFF->FLAC; originals->quarantine)
  gbc init [--cron]      deploy config + beets configs (+ optional cron)
  gbc uninstall [--purge] remove tooling (never your music)
"""
import argparse
import secrets

from . import admin
from . import config as configmod
from .lock import import_lock
from .logs import configure
from .passes import acousticbrainz, convert, import_, inbox, pipeline, qa, verify


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gbc", description="Beets-driven music recovery pipeline.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="full pipeline now (incremental)")
    pr.add_argument("--all", action="store_true", help="reprocess the whole library (ignore watermark)")
    pr.add_argument("--reimport", action="store_true", help="re-evaluate already-seen folders (beets -I)")
    sub.add_parser("inbox", help="cron door: import a drop if there's anything new, then the pipeline")
    pi = sub.add_parser("import", help="album-match import only (art/genres/replaygain run automatically)")
    pi.add_argument("source", nargs="?", help="source dir (default: MUSIC_SRC)")
    pi.add_argument("--reimport", action="store_true", help="re-evaluate already-seen folders (beets -I)")
    pq = sub.add_parser("qa", help="read-only technical audit")
    pq.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    pa = sub.add_parser("anomaly", help="read-only name/anomaly scan only")
    pa.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    pv = sub.add_parser("verify", help="quarantine imposter tracks (audio != tagged recording) via AcoustID")
    pv.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    pab = sub.add_parser("acousticbrainz", help="fetch BPM/key/mood metadata from AcousticBrainz")
    pab.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    sub.add_parser("convert", help="normalise formats (WMA->AAC, WAV/AIFF->FLAC; originals -> quarantine)")
    pini = sub.add_parser("init", help="deploy config + beets overlays (+ optional cron)")
    pini.add_argument("--cron", action="store_true", help="also schedule `gbc inbox` every 15 min")
    pun = sub.add_parser("uninstall", help="remove tooling (never your music)")
    pun.add_argument("--purge", action="store_true", help="also remove the beets config dir + catalog")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = configmod.load()
    configure(cfg.log_dir, secrets.token_hex(2))

    if args.cmd == "run":
        with import_lock(cfg, blocking=True):
            return pipeline.run(cfg, full=args.all, reimport=args.reimport)
    if args.cmd == "inbox":
        return inbox.run(cfg)
    if args.cmd == "import":
        with import_lock(cfg, blocking=True):
            return import_.run(cfg, src=args.source, reimport=args.reimport)
    if args.cmd == "qa":
        return qa.run(cfg, scope=args.query)
    if args.cmd == "anomaly":
        return qa.run_anomaly(cfg, scope=args.query)
    if args.cmd == "verify":
        return verify.run(cfg, scope=args.query)
    if args.cmd == "acousticbrainz":
        return acousticbrainz.run(cfg, scope=args.query)
    if args.cmd == "convert":
        with import_lock(cfg, blocking=True):
            return convert.run(cfg)
    if args.cmd == "init":
        return admin.init(cfg, cron=args.cron)
    if args.cmd == "uninstall":
        return admin.uninstall(cfg, purge=args.purge)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
