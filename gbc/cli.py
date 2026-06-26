"""gbc CLI -- one tool, several doors. beets does art/genres/replaygain/scrub natively during import."""
import argparse
import secrets

from . import admin
from . import config as configmod
from .lock import import_lock
from .logs import configure
from .passes import acousticbrainz, albumdedup, convert, import_, inbox, nova, pipeline, qa, singletons, upgrade, verify

try:                                  # TEMPORARY/detachable -- delete restore_imposters.py to remove the command
    from .passes import restore_imposters
except ImportError:                               # pragma: no cover
    restore_imposters = None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gbc", description="Beets-driven music recovery pipeline.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="full pipeline now (incremental)")
    pr.add_argument("--all", action="store_true", help="reprocess the whole library (ignore watermark)")
    pr.add_argument("--reimport", action="store_true", help="re-evaluate already-seen folders (beets -I)")
    pr.add_argument("--no-import", action="store_true",
                    help="skip the import pass -- re-run only the post-import passes on clean")
    sub.add_parser("inbox", help="cron door: import a drop if there's anything new, then the pipeline")
    pi = sub.add_parser("import", help="album-match import only (art/genres/replaygain run automatically)")
    pi.add_argument("source", nargs="?", help="source dir (default: MUSIC_SRC)")
    pi.add_argument("--reimport", action="store_true", help="re-evaluate already-seen folders (beets -I)")
    ps = sub.add_parser("singletons", help="recover LOOSE source tracks as singletons (-> _Singles/)")
    ps.add_argument("source", nargs="?", help="source dir (default: MUSIC_SRC)")
    ps.add_argument("--reimport", action="store_true", help="re-evaluate already-seen folders (beets -I)")
    ps.add_argument("--apply", action="store_true",
                    help="execute the reassembly (re-tag Nova + promote now-complete albums out of _Singles/); "
                         "without it those steps only report")
    pq = sub.add_parser("qa", help="read-only technical audit")
    pq.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    pa = sub.add_parser("anomaly", help="read-only name/anomaly scan only")
    pa.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    pv = sub.add_parser("verify", help="quarantine imposter tracks (audio != tagged recording) via AcoustID")
    pv.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    pab = sub.add_parser("acousticbrainz", help="fetch BPM/key/mood metadata from AcousticBrainz")
    pab.add_argument("query", nargs="?", default="", help="scope query (default: whole library)")
    sub.add_parser("convert", help="normalise formats (WMA->AAC, WAV/AIFF->FLAC; originals -> quarantine)")
    pad = sub.add_parser("albumdedup", help="quarantine cross-source duplicate albums (keep the best-quality copy)")
    pad.add_argument("--pretend", action="store_true", help="report only (default: move the duplicates to quarantine)")
    pnv = sub.add_parser("nova", help="[detachable] classify reconstructable Radio-Nova compils from the library")
    pnv.add_argument("--refresh-cache", action="store_true", help="re-fetch the Nova tracklists from MusicBrainz")
    pup = sub.add_parser("upgrade", help="replace a clean album with a better source copy (also runs in the pipeline)")
    pup.add_argument("source", nargs="?", help="source dir to scan (default: MUSIC_SRC)")
    pup.add_argument("--apply", action="store_true", help="execute the swap (default: report only)")
    if restore_imposters is not None:             # TEMPORARY one-off command (detachable)
        prs = sub.add_parser("restore-imposters",
                             help="[TEMPORARY] re-merge falsely-quarantined imposters back into clean")
        prs.add_argument("--apply", action="store_true", help="execute the restore (default: report only)")
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
            return pipeline.run(cfg, full=args.all, reimport=args.reimport, do_import=not args.no_import)
    if args.cmd == "inbox":
        return inbox.run(cfg)
    if args.cmd == "import":
        with import_lock(cfg, blocking=True):
            return import_.run(cfg, src=args.source, reimport=args.reimport)
    if args.cmd == "singletons":
        with import_lock(cfg, blocking=True):
            return singletons.run(cfg, src=args.source, reimport=args.reimport, apply=args.apply)
    if args.cmd == "qa":
        return qa.run(cfg, scope=args.query)
    if args.cmd == "anomaly":
        return qa.run_anomaly(cfg, scope=args.query)
    if args.cmd == "verify":
        return verify.run(cfg, scope=args.query)
    if args.cmd == "acousticbrainz":
        return acousticbrainz.run(cfg, scope=args.query)
    if args.cmd == "nova":
        return nova.run(cfg, refresh=args.refresh_cache)
    if args.cmd == "upgrade":
        with import_lock(cfg, blocking=True):
            return upgrade.run(cfg, src=args.source, apply=args.apply)
    if args.cmd == "restore-imposters":           # TEMPORARY one-off (detachable)
        with import_lock(cfg, blocking=True):
            return restore_imposters.run(cfg, apply=args.apply)
    if args.cmd == "convert":
        with import_lock(cfg, blocking=True):
            return convert.run(cfg)
    if args.cmd == "albumdedup":
        with import_lock(cfg, blocking=True):
            return albumdedup.run(cfg, do_apply=not args.pretend)
    if args.cmd == "init":
        return admin.init(cfg, cron=args.cron)
    if args.cmd == "uninstall":
        return admin.uninstall(cfg, purge=args.purge)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
