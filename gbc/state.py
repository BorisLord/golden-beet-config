"""Watermark of the last successful run: qa scopes to items added since. No watermark -> whole lib.
Stored in BEETSDIR/gbc-state.json. A separate gbc-run-progress.json records which passes of the CURRENT
(unfinished) pipeline run have completed, so a killed multi-hour run resumes without re-doing them (notably the
import re-walk).
"""
import json

from .config import Config
from .util import write_json


def _path(cfg: Config):
    return cfg.beetsdir / "gbc-state.json"


def _progress_path(cfg: Config):
    return cfg.beetsdir / "gbc-run-progress.json"


def get_progress(cfg: Config) -> dict:
    """{'key': run-identity, 'wm_new': iso, 'done': [pass names]} for an in-flight run, or {} if none."""
    p = _progress_path(cfg)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return d if isinstance(d, dict) else {}          # a corrupted non-dict (e.g. `[]`/`42`) -> start fresh


def set_progress(cfg: Config, data: dict) -> None:
    write_json(_progress_path(cfg), data)              # atomic tmp+replace: a kill mid-write can't truncate it


def clear_progress(cfg: Config) -> None:
    _progress_path(cfg).unlink(missing_ok=True)


def get_watermark(cfg: Config) -> str | None:
    p = _path(cfg)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return d.get("last_run") if isinstance(d, dict) else None


def set_watermark(cfg: Config, iso_ts: str) -> None:
    write_json(_path(cfg), {"last_run": iso_ts})       # atomic tmp+replace: a kill mid-write can't truncate it


def added_query(watermark: str | None) -> str:
    """beets query scoping to items added at/after the watermark; '' (whole lib) when no watermark."""
    return f"added:{watermark}.." if watermark else ""
