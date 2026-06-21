"""init / uninstall the tooling. NEVER touches your music (source / clean / quarantine).

init   : create config.env (from the example), make the dirs, deploy beets/*.yaml into BEETSDIR
         (filling `directory:` and the import `log:` -- written in Python, no fragile sed), optional cron.
uninstall: remove the cron entry, logs, config.env, and (with --purge) the beets config dir + catalog.
"""
import re
import shutil
import subprocess
from pathlib import Path

from .config import REPO_ROOT, Config, config_path
from .logs import get_logger

CRON_MARK = "gbc inbox"
# cron does NOT expand $HOME in a PATH= line -> bake the real home dir so `gbc`/`beet` resolve.
_HOME = Path.home()
CRON_PATH = f"{_HOME}/.local/bin:{_HOME}/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"


def init(cfg: Config, cron: bool = False) -> int:
    log = get_logger("init")
    example = REPO_ROOT / "config.env.example"
    if not config_path() and example.exists():
        shutil.copy2(example, REPO_ROOT / "config.env")
        log.info("created %s (defaults under ~/Music/beetsPipeline -- edit + re-run for other paths)",
                 REPO_ROOT / "config.env")
    elif config_path():
        log.info("using existing config.env (%s)", config_path())

    for d in (cfg.beetsdir, cfg.src, cfg.clean, cfg.dump, cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)

    for y in sorted((REPO_ROOT / "beets").glob("*.yaml")):
        text = y.read_text(encoding="utf-8")
        text = text.replace("@HELPERS@", str(REPO_ROOT / "helpers"))   # convert.yaml -> the wma2opus wrapper
        if y.name == "config.yaml":
            text = re.sub(r"(?m)^directory:.*$", f"directory: {cfg.clean}", text)
            text = re.sub(r"(?m)^  log:.*$", f"  log: {cfg.log_dir}/import-decisions.log", text)
        (cfg.beetsdir / y.name).write_text(text, encoding="utf-8")
    log.info("deployed beets/*.yaml -> %s (directory + import log filled)", cfg.beetsdir)
    log.info("optional: set fanarttv_key / lastfm_key in %s for online art + genres",
             cfg.beetsdir / "config.yaml")

    if cron:
        _install_cron(log)
    log.info("init done. drop album folders in %s ; then `gbc run` (or let cron do it).", cfg.src)
    return 0


def _install_cron(log) -> None:
    if not shutil.which("crontab"):
        log.info("no crontab -- add manually: */15 * * * * PATH=%s gbc inbox", CRON_PATH)
        return
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if CRON_MARK in cur:
        log.info("cron already scheduled")
        return
    line = f"*/15 * * * * PATH={CRON_PATH} gbc inbox >/dev/null 2>&1\n"
    subprocess.run(["crontab", "-"], input=cur + line, text=True)
    log.info("cron scheduled (every 15 min: gbc inbox)")


def uninstall(cfg: Config, purge: bool = False) -> int:
    log = get_logger("uninstall")
    if shutil.which("crontab"):
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if CRON_MARK in cur:
            kept = "".join(ln for ln in cur.splitlines(keepends=True) if CRON_MARK not in ln)
            subprocess.run(["crontab", "-"], input=kept, text=True)
            log.info("removed cron entry")
    if purge:
        bd, home = cfg.beetsdir.resolve(), Path.home().resolve()
        if bd == Path("/") or bd == home or bd in home.parents:   # root, home, or an ANCESTOR of home -> refuse
            log.warning("refusing --purge: %s is root/home or an ancestor of home", bd)
        else:
            shutil.rmtree(bd, ignore_errors=True)
            log.info("removed beets config dir + catalog (%s)", bd)
    cenv = config_path()
    if cenv and cenv.exists():
        cenv.unlink()
        log.info("removed %s", cenv)
    if cfg.log_dir.exists():
        shutil.rmtree(cfg.log_dir, ignore_errors=True)
        log.info("removed logs (%s)", cfg.log_dir)
    log.info("done. Your music is untouched: %s | %s | %s", cfg.src, cfg.clean, cfg.dump)
    return 0
