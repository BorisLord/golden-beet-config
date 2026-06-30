"""The pipeline: import -> upgrade -> albumdedup -> convert -> verify -> acousticbrainz -> qa.
`run` + `inbox` (cron) both call it; the cron door skips the costly upgrade full-source scan
(`upgrade_scan=False`). Ordering is load-bearing: upgrade right after import (it acts on copies this import
dup-skipped), albumdedup before the expensive passes (they skip quarantined albums), convert BEFORE verify so
every later pass runs identically on the converted files.
"""
import time
from datetime import datetime

from .. import state
from ..config import Config
from ..logs import get_logger
from . import acousticbrainz, albumdedup, convert, import_, qa, upgrade, verify


def run(cfg: Config, *, full: bool = False, src=None, reimport: bool = False, upgrade_scan: bool = True,
        do_import: bool = True) -> int:
    log = get_logger("pipeline")
    wm_old = None if full else state.get_watermark(cfg)
    scope = state.added_query(wm_old)        # qa scope: items added since last run ("" = whole library)

    # Resume: a run is identified by its STARTING state (full, or the watermark it began from). If the last run
    # crashed with the same identity (watermark never advanced), skip the passes it already finished -- chiefly
    # the import re-walk, which on `--reimport` over a huge source can be hours.
    run_key = "full" if full else (wm_old or "initial")
    prog = state.get_progress(cfg)
    resume = prog.get("key") == run_key
    done = set(prog.get("done", [])) if resume else set()
    wm_new = prog.get("wm_new") if resume else None
    if done:
        log.info("pipeline: resuming -- %d pass(es) already done (%s)", len(done), ", ".join(sorted(done)))

    def _save():
        state.set_progress(cfg, {"key": run_key, "wm_new": wm_new, "done": sorted(done)})

    log.info("pipeline start (%s)%s", "full" if full else "incremental", f" scope={scope}" if scope else "")

    if not do_import:
        log.info("pipeline: skip import (--no-import) -- running the post-import passes on clean only")
    elif "import" not in done:
        log.info("> pass import: start")
        t0 = time.monotonic()
        rc = import_.run(cfg, src=src, reimport=reimport)
        if rc:
            log.error("pipeline ABORTED: import failed (rc=%d) -- watermark NOT advanced, will retry next run", rc)
            return rc
        log.info("> pass import: done in %ds", int(time.monotonic() - t0))
        done.add("import")
        _save()
    else:
        log.info("pipeline: skip import (already done this run)")

    if wm_new is None:                       # captured once just after import (items are < wm_new); reused on resume
        wm_new = datetime.now().replace(microsecond=0).isoformat()
        _save()

    # post-import passes are best-effort (a hiccup never breaks the IMPORT), but a pass that ERRORS leaves its window
    # unprocessed -> HOLD the watermark so the next run re-scopes that window and re-runs only the failed pass.
    failed = False

    def _phase(name: str, fn) -> None:
        nonlocal failed
        if name in done:
            log.info("pipeline: skip %s (already done this run)", name)
            return
        log.info("> pass %s: start", name)
        t0 = time.monotonic()
        try:
            fn()
        except Exception:
            log.exception("%s pass errored (non-fatal)", name)
            failed = True                   # don't advance the watermark this run -> re-scoped + retried next run
            return
        log.info("> pass %s: done in %ds", name, int(time.monotonic() - t0))
        done.add(name)
        _save()

    def _convert() -> None:
        rc_conv = convert.run(cfg)
        if rc_conv:
            log.warning("convert returned rc=%d -- some originals were NOT converted (left intact in clean)", rc_conv)

    # upgrade walks the WHOLE source for a better copy of each clean album -- a deliberate sweep too costly for the
    # cron door (fires on every drop). `gbc run`/`--reimport`/`--all` scan; `gbc inbox` skips it (caught next sweep).
    if upgrade_scan:
        _phase("upgrade", lambda: upgrade.run(cfg, apply=True))
    else:
        log.info("pipeline: skip upgrade (cron path -- full-source scan runs on `gbc run`)")
    _phase("albumdedup", lambda: albumdedup.run(cfg))
    _phase("convert", _convert)
    _phase("verify", lambda: verify.run(cfg, scope=scope, refresh=full))
    _phase("acousticbrainz", lambda: acousticbrainz.run(cfg, scope=scope))
    _phase("qa", lambda: qa.run(cfg, scope=scope, cull=True))

    if failed:                               # keep wm_old + the progress file: the next run resumes (done passes
        log.warning("pipeline: a post-import pass errored -> watermark HELD at %s; window retried next run",
                    wm_old or "initial")     # skipped) and re-runs the failed pass on the same window
    else:
        state.set_watermark(cfg, wm_new)
        state.clear_progress(cfg)            # run finished cleanly -> no resume state to keep
        log.info("pipeline done; watermark -> %s", wm_new)
    return 0
