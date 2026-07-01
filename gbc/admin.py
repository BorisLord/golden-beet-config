"""init / uninstall the tooling. NEVER touches your music (source / clean / quarantine).

init: config.env + dirs + deploy beets/*.yaml into BEETSDIR (filling `directory:`/`log:`), optional cron.
uninstall: remove cron entry, logs, config.env, and (with --purge) the beets config dir + catalog.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import API_KEYS, REPO_ROOT, Config, config_path, load, read_api_keys
from .logs import get_logger

CRON_MARK = "gbc inbox"
# cron does NOT expand $HOME in a PATH= line -> bake the real home dir so `gbc`/`beet` resolve.
_HOME = Path.home()
CRON_PATH = f"{_HOME}/.local/bin:{_HOME}/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"


def _entangled(a: Path, b: Path) -> bool:
    """True if a == b or either is an ancestor of the other -- rmtree(a) would then reach into b."""
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def init(cfg: Config, cron: bool = False) -> int:
    log = get_logger("init")
    example = REPO_ROOT / "config.env.example"
    if not config_path() and example.exists():
        shutil.copy2(example, REPO_ROOT / "config.env")
        cfg = load()            # re-read so dirs/YAML below use the freshly-written config, not pre-dispatch defaults
        log.info("created %s (defaults under ~/Music/beetsPipeline -- edit + re-run for other paths)",
                 REPO_ROOT / "config.env")
    elif config_path():
        log.info("using existing config.env (%s)", config_path())
    else:
        log.warning("no config.env and no template (%s) -- proceeding with built-in defaults", example)

    for d in (cfg.beetsdir, cfg.src, cfg.clean, cfg.dump, cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)

    keys = read_api_keys()                       # Discogs/last.fm/fanart.tv tokens from config.env -- read ONCE

    for y in sorted((REPO_ROOT / "beets").glob("*.yaml")):
        text = y.read_text(encoding="utf-8")
        text = text.replace("@HELPERS@", str(REPO_ROOT / "helpers"))   # convert.yaml's wma2opus wrapper path
        if y.name == "config.yaml":
            text = re.sub(r"(?m)^directory:.*$", lambda m: f"directory: {cfg.clean}", text)
            text = re.sub(r"(?m)^  log:.*$", lambda m: f"  log: {cfg.log_dir}/import-decisions.log", text)
            for field, val in keys.items():            # line-anchored: only the field assignment, not a comment
                text = re.sub(rf"(?m)^(\s*{re.escape(field)}:\s*)REPLACE_ME\s*$",
                              r"\g<1>" + val.replace("\\", r"\\"), text)
        dest = cfg.beetsdir / y.name
        if y.name == "config.yaml":                # real API keys -> 0600, never world-readable
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            dest.chmod(0o600)                      # O_CREAT mode is ignored if the file pre-existed -> enforce it
        else:
            dest.write_text(text, encoding="utf-8")
    nkeys = len(keys)
    log.info("deployed beets/*.yaml -> %s (directory + import log%s filled)",
             cfg.beetsdir, f" + {nkeys} API key(s)" if nkeys else "")
    if nkeys < len(API_KEYS):
        log.info("optional: set DISCOGS_TOKEN / LASTFM_KEY / FANARTTV_KEY in config.env for online match/art/genres")

    if cron:
        _install_cron(log)
    log.info("init done. drop album folders in %s ; then `gbc run` (or let cron do it).", cfg.src)
    return 0


def _install_cron(log) -> None:
    if not shutil.which("crontab"):
        log.info("no crontab -- add manually: */15 * * * * PATH=%s gbc inbox", CRON_PATH)
        return
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    # rc!=0 with a real error (not the benign "no crontab for user") -> DON'T overwrite a crontab we couldn't read.
    if res.returncode and res.stderr.strip() and "no crontab" not in res.stderr.lower():
        log.warning("crontab -l failed (%s) -- not scheduling (refusing to overwrite)", res.stderr.strip())
        return
    cur = res.stdout
    if CRON_MARK in cur:
        log.info("cron already scheduled")
        return
    line = f"*/15 * * * * PATH={CRON_PATH} gbc inbox >/dev/null 2>&1\n"
    w = subprocess.run(["crontab", "-"], input=cur + line, text=True)
    if w.returncode:
        log.warning("crontab write failed (rc=%d) -- not scheduled", w.returncode)
    else:
        log.info("cron scheduled (every 15 min: gbc inbox)")


def uninstall(cfg: Config, purge: bool = False) -> int:
    log = get_logger("uninstall")
    if shutil.which("crontab"):
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if res.returncode and res.stderr.strip() and "no crontab" not in res.stderr.lower():
            log.warning("crontab -l failed (%s) -- not touching crontab", res.stderr.strip())
        elif CRON_MARK in res.stdout:
            kept = "".join(ln for ln in res.stdout.splitlines(keepends=True) if CRON_MARK not in ln)
            w = subprocess.run(["crontab", "-"], input=kept, text=True)
            if w.returncode:
                log.warning("crontab write failed (rc=%d) -- entry not removed", w.returncode)
            else:
                log.info("removed cron entry")
    if purge:
        bd, home = cfg.beetsdir.resolve(), Path.home().resolve()
        music = (cfg.src, cfg.clean, cfg.dump)
        if bd == Path("/") or bd == home or bd in home.parents:   # refuse root/home/ancestor-of-home
            log.warning("refusing --purge: %s is root/home or an ancestor of home", bd)
        elif any(_entangled(bd, m) for m in music):               # refuse if it would reach into any music dir
            log.warning("refusing --purge: %s overlaps a music dir (src/clean/dump) -- music never deleted", bd)
        else:
            shutil.rmtree(bd, ignore_errors=True)
            if bd.exists():
                log.warning("purge: %s not fully removed -- some files remain", bd)
            else:
                log.info("removed beets config dir + catalog (%s)", bd)
    cenv = config_path()
    if cenv and cenv.exists():
        cenv.unlink()
        log.info("removed %s", cenv)
    if cfg.log_dir.exists():
        shutil.rmtree(cfg.log_dir, ignore_errors=True)
        if cfg.log_dir.exists():
            log.warning("logs (%s) not fully removed -- some files remain", cfg.log_dir)
        else:
            log.info("removed logs (%s)", cfg.log_dir)
    log.info("done. Your music is untouched: %s | %s | %s", cfg.src, cfg.clean, cfg.dump)
    return 0
