"""Read beets' RESOLVED import op and derive how gbc adapts -- the op (move/copy/link/hardlink/reflink/delete)
is beets' call, not gbc's. `beet config` prints the merged YAML (defaults + config.yaml + overlays) = the
EFFECTIVE behaviour, never a guess. From it we derive the boolean the passes branch on:
  source_consumed -- beets removes the originals (move, or delete after copy/link) -> gbc MAY move source
                     (dedup/sidecars/prune); when preserved, the source is left untouched.
"""
import io
from dataclasses import dataclass

import yaml

from .beets import run_beet
from .config import Config
from .logs import get_logger

REQUIRED_PLUGINS = ("musicbrainz", "chroma", "scrub")  # gbc depends on these for match candidates + scrub


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "on", "1")


def _reflink_on(v) -> bool:
    # beets reflink: yes|no|auto (auto = reflink if FS supports it, else copy) -> treat auto as on
    return _as_bool(v) or str(v).strip().lower() == "auto"


@dataclass(frozen=True)
class BeetsImport:
    move: bool = False
    copy: bool = False
    link: bool = False
    hardlink: bool = False
    reflink: bool = False
    delete: bool = False

    @property
    def source_consumed(self) -> bool:
        """beets itself removes the source originals (move, or copy/link followed by delete)."""
        return self.move or self.delete

    @property
    def source_preserved(self) -> bool:
        return not self.source_consumed

    @property
    def label(self) -> str:
        for name in ("move", "delete", "reflink", "hardlink", "link", "copy"):
            if getattr(self, name):
                return name
        return "in-place"


def parse_import(config_text: str) -> BeetsImport:
    """Build BeetsImport from `beet config` YAML text (pure -> unit-testable without a beets install)."""
    try:
        data = yaml.safe_load(io.StringIO(config_text)) or {}
    except yaml.YAMLError:
        data = {}
    imp = data.get("import", {}) if isinstance(data, dict) else {}
    if not isinstance(imp, dict):
        imp = {}
    return BeetsImport(
        move=_as_bool(imp.get("move", False)),
        copy=_as_bool(imp.get("copy", False)),
        link=_as_bool(imp.get("link", False)),
        hardlink=_as_bool(imp.get("hardlink", False)),
        reflink=_reflink_on(imp.get("reflink", False)),
        delete=_as_bool(imp.get("delete", False)),
    )


def _warn_missing_plugins(config_text: str, log) -> None:
    try:
        data = yaml.safe_load(io.StringIO(config_text)) or {}
    except yaml.YAMLError:
        return
    plugins = data.get("plugins", "") if isinstance(data, dict) else ""
    if isinstance(plugins, list):
        plugins = " ".join(str(p) for p in plugins)
    enabled = str(plugins).split()
    missing = [p for p in REQUIRED_PLUGINS if p not in enabled]
    if missing:
        log.warning("beets plugins missing %s -> match/scrub silently underperform (AGENTS.md rule 7)", missing)


def read_import(cfg: Config) -> BeetsImport:
    """`beet config` (resolved) -> the effective import op; warns if a plugin gbc depends on is absent."""
    log = get_logger("beetscfg")
    # merge_stderr=False: beet's stderr warnings would corrupt the YAML we parse. check=True: a failed
    # `beet config` leaves the move-vs-copy op UNKNOWN -- abort rather than derive a wrong "preserved" and
    # skip dedup/prune while the real import still consumes the source (orphaned shells).
    _, text = run_beet(cfg, ["config"], passname="beetscfg", echo_lines=False, merge_stderr=False, check=True)
    _warn_missing_plugins(text, log)
    bi = parse_import(text)
    log.info("beets import op = %s (source %s)", bi.label,
             "preserved" if bi.source_preserved else "consumed")
    return bi
