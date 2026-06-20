"""The pipeline: import -> verify -> acousticbrainz -> qa. `run` (manual) and `inbox` (cron) both call
this; only the trigger differs.

beets does the heavy lifting natively DURING `beet import` (auto: yes): match, scrub, fetchart, embedart,
lastgenre, ftintitle, replaygain. The import pass adds dedup (before) + sidecars (after); verify flags
imposter audio, acousticbrainz adds BPM/key/mood metadata, qa is a read-only audit. Fail-fast: if import
fails, the watermark is NOT advanced (next run retries). The watermark scopes the later passes to items
added since the last successful run (whole library on first run / --all).
"""
from datetime import datetime

from .. import state
from ..config import Config
from ..logs import get_logger
from . import acousticbrainz, import_, qa, verify


def run(cfg: Config, *, full: bool = False, src=None, reimport: bool = False) -> int:
    log = get_logger("pipeline")
    wm_old = None if full else state.get_watermark(cfg)
    scope = state.added_query(wm_old)        # qa scope: items added since last run ("" = whole library)
    log.info("pipeline start (%s)%s", "full" if full else "incremental", f" scope={scope}" if scope else "")

    rc = import_.run(cfg, src=src, reimport=reimport)   # match + scrub + art + genres + ftintitle + replaygain
    if rc:
        log.error("pipeline ABORTED: import failed (rc=%d) -- watermark NOT advanced, will retry next run", rc)
        return rc
    wm_new = datetime.now().replace(microsecond=0).isoformat()   # after import: this run's items are < wm_new

    try:
        verify.run(cfg, scope=scope)         # flag-only AcoustID check (imposter audio); best-effort, never gates
    except Exception:                        # a verify hiccup must never break the import pipeline
        log.exception("verify pass errored (non-fatal)")
    try:
        acousticbrainz.run(cfg, scope=scope)  # network-only acoustic metadata (BPM/key/moods); best-effort
    except Exception:                        # AB downtime must never break the import pipeline
        log.exception("acousticbrainz pass errored (non-fatal)")
    qa.run(cfg, scope=scope)                 # read-only audit (informational; never gates the watermark)
    state.set_watermark(cfg, wm_new)
    log.info("pipeline done; watermark -> %s", wm_new)
    return 0
