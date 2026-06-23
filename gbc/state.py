"""Watermark of the last successful pipeline run, so a run "omits what the previous run already did":
qa is scoped to items added since the watermark. First run (no watermark) -> whole
library (handles the initial bulk). `--all` ignores it. Stored in BEETSDIR/gbc-state.json.
"""
import json

from .config import Config


def _path(cfg: Config):
    return cfg.beetsdir / "gbc-state.json"


def get_watermark(cfg: Config) -> str | None:
    p = _path(cfg)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("last_run")
    except (ValueError, OSError):
        return None


def set_watermark(cfg: Config, iso_ts: str) -> None:
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")                 # write+rename: a kill mid-write can't truncate the state
    tmp.write_text(json.dumps({"last_run": iso_ts}), encoding="utf-8")
    tmp.replace(p)


def added_query(watermark: str | None) -> str:
    """beets query scoping to items added at/after the watermark; '' (whole lib) when no watermark."""
    return f"added:{watermark}.." if watermark else ""
