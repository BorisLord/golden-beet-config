"""The pipeline: import -> albumdedup -> convert -> verify -> acousticbrainz -> qa -> reclaim. `run` + `inbox`
(cron) both call this; only the trigger differs. albumdedup runs FIRST (it needs only import metadata) so the
later, expensive passes never process a duplicate album that gets quarantined anyway. convert runs BEFORE
verify so every later pass operates identically on the converted (WMA->Opus, WAV/AIFF->FLAC) files.

beets does the heavy lifting natively DURING `beet import` (auto: yes): match, scrub, fetchart, embedart,
lastgenre, ftintitle, replaygain. The import pass adds dedup (before) + sidecars (after) IN MOVE MODE; verify
flags imposter audio, acousticbrainz adds BPM/key/mood metadata, qa is a read-only audit. reclaim runs only
in PRESERVE mode (beets copy/reflink/hardlink): it moves source albums whose every track verified ok to
quarantine. Fail-fast: if import fails, the watermark is NOT advanced (next run retries). The watermark
scopes the later passes to items added since the last successful run (whole library on first run / --all).
"""
from datetime import datetime

from .. import state
from ..config import Config
from ..logs import get_logger
from . import acousticbrainz, albumdedup, convert, import_, qa, reclaim, verify


def run(cfg: Config, *, full: bool = False, src=None, reimport: bool = False) -> int:
    log = get_logger("pipeline")
    wm_old = None if full else state.get_watermark(cfg)
    scope = state.added_query(wm_old)        # qa scope: items added since last run ("" = whole library)
    log.info("pipeline start (%s)%s", "full" if full else "incremental", f" scope={scope}" if scope else "")

    rc = import_.run(cfg, src=src, reimport=reimport)   # match + scrub + art + genres + ftintitle + replaygain
    if rc:
        log.error("pipeline ABORTED: import failed (rc=%d) -- watermark NOT advanced, will retry next run", rc)
        return rc
    try:
        albumdedup.run(cfg)                  # cross-source duplicate albums (MB vs Discogs) -> quarantine the
    except Exception:                        # lesser copy. FIRST: needs only import metadata, and the later
        log.exception("album dedup pass errored (non-fatal)")   # passes then never waste work on a dropped dup
    try:
        rc_conv = convert.run(cfg)           # normalise WMA->Opus, WAV/AIFF->FLAC BEFORE verify so every later
        if rc_conv:                          # pass runs identically on the converted files; best-effort
            log.warning("convert returned rc=%d -- some originals were NOT converted (left intact in clean)", rc_conv)
    except Exception:
        log.exception("convert pass errored (non-fatal)")
    wm_new = datetime.now().replace(microsecond=0).isoformat()   # after import: this run's items are < wm_new

    try:
        verify.run(cfg, scope=scope)         # flag-only AcoustID check (imposter audio); best-effort, never gates
    except Exception:                        # a verify hiccup must never break the import pipeline
        log.exception("verify pass errored (non-fatal)")
    try:
        acousticbrainz.run(cfg, scope=scope)  # network-only acoustic metadata (BPM/key/moods); best-effort
    except Exception:                        # AB downtime must never break the import pipeline
        log.exception("acousticbrainz pass errored (non-fatal)")
    try:
        qa.run(cfg, scope=scope, cull=True)  # audit + cull corrupt files -> quarantine/corrupt (never gates)
    except Exception:                        # a qa hiccup must never block the watermark advance below
        log.exception("qa pass errored (non-fatal)")
    try:
        reclaim.run(cfg)                     # preserve-mode only: verified source albums -> quarantine
    except Exception:                        # reclaim must never break the import pipeline
        log.exception("reclaim pass errored (non-fatal)")
    state.set_watermark(cfg, wm_new)
    log.info("pipeline done; watermark -> %s", wm_new)
    return 0
