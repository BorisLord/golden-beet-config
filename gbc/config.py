"""Config: the config.env shell vars, parsed in Python.

config.env stays shell-syntax (`VAR="${VAR:-default}"`); we source it in a subshell so its `${VAR:-default}`
and any inline env override behave exactly as in shell. No config.env -> built-in defaults.
Resolution: $GBC_CONFIG, ~/.config/gbc/config.env, <repo>/config.env (repo root for the editable install).
"""
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_VARS = ("BEET", "BEETSDIR", "MUSIC_SRC", "MUSIC_CLEAN", "MUSIC_DUMP", "LOG_DIR")
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Config:
    beet: str
    beetsdir: Path
    src: Path
    clean: Path
    dump: Path
    log_dir: Path

    @property
    def library(self) -> Path:
        return self.beetsdir / "library.db"

    def overlay(self, name: str) -> Path:
        return self.beetsdir / name


def _defaults() -> dict:
    home = Path.home()
    base = home / "Music" / "beetsPipeline"
    clean = base / "clean"
    return {
        "BEET": "beet",
        "BEETSDIR": str(home / ".config" / "beets-rebuild"),
        "MUSIC_SRC": str(base / "source"),
        "MUSIC_CLEAN": str(clean),
        "MUSIC_DUMP": str(base / "quarantine"),
        "LOG_DIR": str(clean.parent / "logs"),
    }


def config_path() -> Path | None:
    env = os.environ.get("GBC_CONFIG")
    candidates = [Path(env)] if env else []
    candidates += [Path.home() / ".config" / "gbc" / "config.env", REPO_ROOT / "config.env"]
    return next((p for p in candidates if p.is_file()), None)


def _source_env(path: Path) -> dict:
    """Source config.env in bash, read back effective values (honours ${VAR:-default} + env). RAISES on a
    sourcing failure -- a config.env typo must fail loudly, never silently fall back to the built-in defaults
    (this tool MOVES files; operating on the wrong dirs is dangerous)."""
    bash = shutil.which("bash") or shutil.which("sh")
    if not bash:
        raise RuntimeError(f"no bash/sh available to source {path}")
    # path passed as $1 (not interpolated) -> no shell injection via a weird path. `set -eu`: ANY failing
    # statement (not just the last) AND any unset-var reference inside config.env abort the source -> fail
    # loudly, never source partial/garbage values. Per var emit a "set" marker (${v+1}) THEN the value, so we
    # can tell "absent from config.env" (-> built-in default) from "present but empty" (-> hard error).
    script = ('set -eu; set -a; . "$1"; '
              + "".join(f'printf "%s\\0%s\\0" "${{{v}+1}}" "${{{v}-}}"; ' for v in _VARS))
    out = subprocess.run([bash, "-c", script, "_", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"failed to source {path} (rc={out.returncode}): {out.stderr.strip()}")
    parts = out.stdout.split("\0")
    result = {}
    for i, v in enumerate(_VARS):
        marker = parts[2 * i] if 2 * i < len(parts) else ""
        value = parts[2 * i + 1].strip() if 2 * i + 1 < len(parts) else ""
        if not marker:                 # var not set by config.env -> fall back to the built-in default
            continue
        if not value:                  # set but empty -> refuse to silently use a default (this tool moves files)
            raise RuntimeError(f"{v} is set but empty in {path} -- refusing to fall back to a default path")
        result[v] = value
    return result


# Optional API keys: config.env var -> the beets/config.yaml field whose `REPLACE_ME` `gbc init` fills.
API_KEYS = {"DISCOGS_TOKEN": "user_token", "LASTFM_KEY": "lastfm_key", "FANARTTV_KEY": "fanarttv_key"}


def read_api_keys() -> dict:
    """{beets_yaml_field: value} for the API keys that are set in config.env. Empty/absent keys are skipped --
    they're OPTIONAL (never an error, unlike the path vars). Used by `gbc init` to fill config.yaml."""
    path = config_path()
    bash = shutil.which("bash") or shutil.which("sh")
    if not path or not bash:
        return {}
    script = 'set -a; . "$1"; ' + "".join(f'printf "%s\\0" "${{{v}-}}"; ' for v in API_KEYS)
    out = subprocess.run([bash, "-c", script, "_", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    parts = out.stdout.split("\0")
    return {field: parts[i].strip()
            for i, field in enumerate(API_KEYS.values())
            if i < len(parts) and parts[i].strip()}


def load() -> Config:
    values = _defaults()
    path = config_path()
    if path:
        values.update(_source_env(path))
    return Config(
        beet=values["BEET"],
        beetsdir=Path(values["BEETSDIR"]).expanduser(),
        src=Path(values["MUSIC_SRC"]).expanduser(),
        clean=Path(values["MUSIC_CLEAN"]).expanduser(),
        dump=Path(values["MUSIC_DUMP"]).expanduser(),
        log_dir=Path(values["LOG_DIR"]).expanduser(),
    )
