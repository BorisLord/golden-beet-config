"""The pipeline: import -> upgrade -> albumdedup -> convert -> verify -> acousticbrainz -> qa -> reclaim.
`run` + `inbox` (cron) both call it; the trigger differs and the cron door skips the costly upgrade
full-source scan (`upgrade_scan=False`). Ordering is load-bearing: upgrade right after
import (it acts on copies this import dup-skipped) and before albumdedup; albumdedup needs only import metadata
so later expensive passes skip quarantined albums; convert BEFORE verify so every later pass runs identically
on the converted (WMA->Opus, WAV/AIFF->FLAC) files.
"""
from datetime import datetime

from .. import state
from ..config import Config
from ..logs import get_logger
from . import acousticbrainz, albumdedup, convert, import_, qa, reclaim, upgrade, verify


def run(cfg: Config, *, full: bool = False, src=None, reimport: bool = False, upgrade_scan: bool = True) -> int:
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

    if "import" not in done:
        rc = import_.run(cfg, src=src, reimport=reimport)
        if rc:
            # fail-fast: watermark NOT advanced so the next run retries this run's items
            log.error("pipeline ABORTED: import failed (rc=%d) -- watermark NOT advanced, will retry next run", rc)
            return rc
        done.add("import")
        _save()
    else:
        log.info("pipeline: skip import (already done this run)")

    if wm_new is None:                       # captured once just after import (items are < wm_new); reused on resume
        wm_new = datetime.now().replace(microsecond=0).isoformat()
        _save()

    # every post-import pass is best-effort: a hiccup must never break the import or block the watermark advance
    def _phase(name: str, fn) -> None:
        if name in done:
            log.info("pipeline: skip %s (already done this run)", name)
            return
        try:
            fn()
        except Exception:
            log.exception("%s pass errored (non-fatal)", name)
            return                          # NOT marked done on failure -> a resume re-runs it (file-moving passes)
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
    _phase("verify", lambda: verify.run(cfg, scope=scope))
    _phase("acousticbrainz", lambda: acousticbrainz.run(cfg, scope=scope))
    _phase("qa", lambda: qa.run(cfg, scope=scope, cull=True))
    _phase("reclaim", lambda: reclaim.run(cfg))

    state.set_watermark(cfg, wm_new)
    state.clear_progress(cfg)                # run finished cleanly -> no resume state to keep
    log.info("pipeline done; watermark -> %s", wm_new)
    return 0
